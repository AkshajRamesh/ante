"""Create and verify Bronze Delta tables from the raw Parquet files in the Unity Catalog volume.

Uses the authenticated Databricks CLI profile and the SQL Statement Execution API on a SQL warehouse.
No credentials are read or stored here; the CLI handles OAuth.

usage: python scripts/load-bronze.py [--profile ante] [--warehouse-name "Serverless Starter Warehouse"]
                                     [--rebuild] [--verify-only] [--workers 4]
"""
import argparse
import csv
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pyarrow.parquet as pq

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
AUDIT = ROOT / "audit" / "bronze-load-audit.csv"
CATALOG = "workspace"
SCHEMA = "ante_bronze"
VOLUME_ROOT = "/Volumes/workspace/ante_bronze/raw_data/casino-data"
#tables that already exist and must never be recreated
PROTECTED = {"prepared_daily_view"}
EXPECTED_DATASETS = 31
METADATA_PREFERRED = ("source_file", "ingested_at")
METADATA_FALLBACK = ("_bronze_source_file", "_bronze_ingested_at")
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}
#transient conditions worth retrying; sql errors such as missing paths are not
RETRYABLE = re.compile(r"TEMPORARILY_UNAVAILABLE|RESOURCE_EXHAUSTED|429|502|503|504|timed? ?out|connection|"
                       r"WAREHOUSE.*(START|STOPP)|try again", re.IGNORECASE)
MAX_ATTEMPTS = 4
STATEMENT_TIMEOUT_S = 900


class SqlError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def find_cli():
    found = os.environ.get("DATABRICKS_CLI") or shutil.which("databricks")
    if found:
        return found
    #winget installs outside the inherited path on some shells
    pattern = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                           "Databricks.DatabricksCLI*", "databricks.exe")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    sys.exit("databricks cli not found; install it or set DATABRICKS_CLI")


class Warehouse:
    def __init__(self, cli, profile, warehouse_id):
        self.cli, self.profile, self.warehouse_id = cli, profile, warehouse_id

    def _cli(self, args, body=None):
        payload_path = None
        try:
            if body is not None:
                handle, payload_path = tempfile.mkstemp(suffix=".json")
                with os.fdopen(handle, "w", encoding="utf-8") as f:
                    json.dump(body, f)
                args = args + ["--json", f"@{payload_path}"]
            done = subprocess.run([self.cli, *args, "-p", self.profile], capture_output=True, text=True,
                                  encoding="utf-8", timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise SqlError(f"cli call timed out: {' '.join(args[:3])}", retryable=True) from exc
        finally:
            if payload_path:
                os.remove(payload_path)
        if done.returncode != 0:
            message = (done.stderr or done.stdout).strip()
            raise SqlError(f"cli error: {message}", retryable=bool(RETRYABLE.search(message)))
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise SqlError(f"unparseable cli output: {done.stdout[:200]}", retryable=True) from exc

    def _execute_once(self, statement):
        response = self._cli(["api", "post", "/api/2.0/sql/statements"],
                             {"statement": statement, "warehouse_id": self.warehouse_id, "wait_timeout": "30s",
                              "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"})
        statement_id = response.get("statement_id")
        started = time.monotonic()
        while response.get("status", {}).get("state") not in TERMINAL_STATES:
            if time.monotonic() - started > STATEMENT_TIMEOUT_S:
                self._cli(["api", "post", f"/api/2.0/sql/statements/{statement_id}/cancel"], {})
                raise SqlError(f"statement {statement_id} exceeded {STATEMENT_TIMEOUT_S}s and was canceled")
            time.sleep(2)
            response = self._cli(["api", "get", f"/api/2.0/sql/statements/{statement_id}"])
        status = response["status"]
        if status["state"] != "SUCCEEDED":
            error = status.get("error", {})
            message = f"{status['state']} {error.get('error_code', '')}: {error.get('message', '')}".strip()
            raise SqlError(message, retryable=bool(RETRYABLE.search(message)))
        if response.get("manifest", {}).get("truncated"):
            raise SqlError("result truncated; query must return a small result")
        columns = [c["name"] for c in response.get("manifest", {}).get("schema", {}).get("columns", [])]
        return [dict(zip(columns, row)) for row in response.get("result", {}).get("data_array", []) or []]

    def execute(self, statement):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._execute_once(statement)
            except SqlError as exc:
                if not exc.retryable or attempt == MAX_ATTEMPTS:
                    raise
                wait = 5 * 2 ** (attempt - 1)
                print(f"  retry {attempt}/{MAX_ATTEMPTS - 1} in {wait}s: {exc}", flush=True)
                time.sleep(wait)


def resolve_warehouse(cli, profile, name, warehouse_id):
    if warehouse_id:
        return warehouse_id
    done = subprocess.run([cli, "warehouses", "list", "-p", profile, "-o", "json"], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    if done.returncode != 0:
        sys.exit(f"could not list warehouses: {done.stderr.strip()}")
    matches = [w for w in json.loads(done.stdout) if w["name"] == name]
    if len(matches) != 1:
        sys.exit(f"expected one warehouse named {name!r}, found {len(matches)}")
    return matches[0]["id"]


def discover():
    datasets = []
    for path in sorted(DATA.rglob("*.parquet")):
        relative = path.relative_to(DATA).as_posix()
        #real public sources get a prefix so they never read as synthetic casino tables
        table = ("real_" if relative.startswith("4_context/real_sources/") else "") + path.stem
        if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
            sys.exit(f"invalid table name {table!r} for {relative}")
        metadata = pq.ParquetFile(path).metadata
        columns = pq.read_schema(path).names
        lower = {c.lower() for c in columns}
        metadata_columns = METADATA_PREFERRED if not lower & set(METADATA_PREFERRED) else METADATA_FALLBACK
        if lower & set(metadata_columns):
            sys.exit(f"{relative}: source columns collide with both metadata name sets")
        datasets.append({"dataset": path.stem, "local_path": f"data/{relative}", "volume_path": f"{VOLUME_ROOT}/{relative}",
                         "table": table, "local_rows": metadata.num_rows, "local_columns": columns,
                         "metadata_columns": metadata_columns})
    names = [d["table"] for d in datasets]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        sys.exit(f"duplicate table names: {duplicates}")
    if len(datasets) != EXPECTED_DATASETS:
        sys.exit(f"expected {EXPECTED_DATASETS} parquet datasets, found {len(datasets)}")
    return datasets


def quote(identifier):
    return "`" + identifier.replace("`", "``") + "`"


def create_table(warehouse, dataset, existing, rebuild):
    table = dataset["table"]
    if table in PROTECTED:
        return "protected (not recreated)"
    if table in existing and not rebuild:
        return "existing (not recreated)"
    source_column, ingested_column = dataset["metadata_columns"]
    verb = "CREATE OR REPLACE TABLE" if rebuild else "CREATE TABLE IF NOT EXISTS"
    warehouse.execute(
        f"{verb} {CATALOG}.{SCHEMA}.{quote(table)} AS "
        f"SELECT *, _metadata.file_path AS {quote(source_column)}, current_timestamp() AS {quote(ingested_column)} "
        f"FROM parquet.{quote(dataset['volume_path'])}")
    return "rebuilt" if table in existing else "created"


def verify_table(warehouse, dataset, table_columns):
    table = dataset["table"]
    remote_columns = table_columns.get(table)
    if remote_columns is None:
        raise SqlError("table not found after load")
    #prepared_daily_view was created earlier with source_file/ingested_at
    source_column, ingested_column = METADATA_PREFERRED if table in PROTECTED else dataset["metadata_columns"]
    rows = warehouse.execute(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT {quote(source_column)}) AS files, MIN({quote(source_column)}) AS source, "
        f"SUM(CASE WHEN {quote(ingested_column)} IS NULL THEN 1 ELSE 0 END) AS missing_ingested "
        f"FROM {CATALOG}.{SCHEMA}.{quote(table)}")[0]
    remote_lower = {c.lower(): c for c in remote_columns}
    local_lower = {c.lower() for c in dataset["local_columns"]}
    missing = [c for c in dataset["local_columns"] if c.lower() not in remote_lower]
    extra = [c for c in remote_columns if c.lower() not in local_lower | {source_column, ingested_column}]
    remote_rows = int(rows["n"])
    problems = []
    if remote_rows != dataset["local_rows"]:
        problems.append(f"row count {remote_rows:,} vs local {dataset['local_rows']:,}")
    if missing:
        problems.append(f"missing source columns {missing}")
    if source_column not in remote_lower or ingested_column not in remote_lower:
        problems.append("metadata columns missing")
    if int(rows["files"]) != 1 or not str(rows["source"]).endswith(dataset["volume_path"]):
        problems.append(f"unexpected source_file {rows['source']!r}")
    if int(rows["missing_ingested"] or 0):
        problems.append("null ingested_at values")
    return remote_rows, len(remote_columns), missing, extra, problems


def process(warehouse, dataset, existing, rebuild, verify_only):
    record = {"dataset": dataset["dataset"], "source_path": dataset["local_path"], "volume_path": dataset["volume_path"],
              "table_name": f"{CATALOG}.{SCHEMA}.{dataset['table']}", "action": "", "local_row_count": dataset["local_rows"],
              "databricks_row_count": "", "local_column_count": len(dataset["local_columns"]),
              "databricks_column_count": "", "metadata_columns": "", "extra_columns": "", "verification_status": "",
              "detail": ""}
    try:
        record["action"] = "verify only" if verify_only else create_table(warehouse, dataset, existing, rebuild)
    except SqlError as exc:
        record.update(action="create failed", verification_status="FAILED", detail=str(exc)[:500])
        return record
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="ante")
    parser.add_argument("--warehouse-name", default="Serverless Starter Warehouse")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--rebuild", action="store_true", help="replace existing tables (never protected ones)")
    parser.add_argument("--verify-only", action="store_true", help="skip creation, only verify")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    cli = find_cli()
    warehouse = Warehouse(cli, args.profile, resolve_warehouse(cli, args.profile, args.warehouse_name, args.warehouse_id))
    datasets = discover()
    print(f"{len(datasets)} parquet datasets found under data/; warehouse {warehouse.warehouse_id}; profile {args.profile}")

    existing = {r["tableName"] for r in warehouse.execute(f"SHOW TABLES IN {CATALOG}.{SCHEMA}")}
    print(f"{len(existing)} tables already in {CATALOG}.{SCHEMA}")
    missing_protected = PROTECTED - existing
    if missing_protected:
        print(f"WARNING: protected tables not found and will not be created: {sorted(missing_protected)}")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        records = list(pool.map(lambda d: process(warehouse, d, existing, args.rebuild, args.verify_only), datasets))
    for record in records:
        print(f"  {record['table_name']}: {record['action']}", flush=True)

    columns = warehouse.execute(
        f"SELECT table_name, column_name FROM {CATALOG}.information_schema.columns "
        f"WHERE table_schema = '{SCHEMA}' ORDER BY table_name, ordinal_position")
    table_columns = {}
    for row in columns:
        table_columns.setdefault(row["table_name"], []).append(row["column_name"])

    for dataset, record in zip(datasets, records):
        if record["verification_status"] == "FAILED":
            continue
        try:
            remote_rows, remote_columns, missing, extra, problems = verify_table(warehouse, dataset, table_columns)
        except SqlError as exc:
            record.update(verification_status="FAILED", detail=f"verification error: {str(exc)[:500]}")
            continue
        metadata = METADATA_PREFERRED if dataset["table"] in PROTECTED else dataset["metadata_columns"]
        record.update(databricks_row_count=remote_rows, databricks_column_count=remote_columns,
                      metadata_columns="; ".join(metadata), extra_columns="; ".join(extra),
                      verification_status="MISMATCH" if problems else "VERIFIED", detail="; ".join(problems))

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    AUDIT.parent.mkdir(exist_ok=True)
    with open(AUDIT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[*records[0].keys(), "checked_at"])
        writer.writeheader()
        for record in records:
            writer.writerow({**record, "checked_at": checked_at})

    counts = {}
    for record in records:
        counts[record["verification_status"]] = counts.get(record["verification_status"], 0) + 1
    created = sum(r["action"] in ("created", "rebuilt") for r in records)
    print(f"\ncreated or rebuilt: {created}; status counts: {counts}")
    for record in records:
        if record["verification_status"] != "VERIFIED":
            print(f"  {record['verification_status']}: {record['table_name']}: {record['detail']}")
        elif record["extra_columns"]:
            print(f"  note: {record['table_name']} has extra columns: {record['extra_columns']}")
    print(f"audit written to {AUDIT.relative_to(ROOT).as_posix()}")
    return 0 if counts.get("VERIFIED", 0) == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
