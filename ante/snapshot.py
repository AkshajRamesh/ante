#verified gold snapshot loader. live databricks reads plug in behind open_gold later.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

FORBIDDEN_COLUMNS = frozenset({
    "player_id",
    "observed_visit_id",
    "session_segment_id",
    "loyalty_event_id",
    "transaction_id",
    "comp_id",
    "redeemed_pos_id",
    "source_segment_id",
    "assignment_id",
    "incident_id",
})


class GoldError(RuntimeError):
    #raised instead of showing a made-up number
    pass


def _family(manifest_type: str) -> str:
    name = manifest_type.strip().upper()
    if name.startswith("DECIMAL"):
        return "decimal"
    if name == "DOUBLE":
        return "float"
    if name in ("INT", "BIGINT"):
        return "integer"
    if name == "DATE":
        return "date"
    if name == "TIMESTAMP_NTZ":
        return "timestamp"
    if name == "STRING":
        return "string"
    if name == "BOOLEAN":
        return "boolean"
    raise GoldError(f"Manifest type {manifest_type!r} is not one this app understands.")


def _arrow_family(arrow_type: pa.DataType) -> str:
    if pa.types.is_decimal(arrow_type):
        return "decimal"
    if pa.types.is_floating(arrow_type):
        return "float"
    if pa.types.is_integer(arrow_type):
        return "integer"
    if pa.types.is_date(arrow_type):
        return "date"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "string"
    if pa.types.is_boolean(arrow_type):
        return "boolean"
    return "other"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce(frame: pd.DataFrame, columns: list[dict]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        name = column["name"]
        family = _family(column["type"])
        if family in ("decimal", "float"):
            out[name] = pd.to_numeric(out[name], errors="coerce")
        elif family == "integer":
            out[name] = pd.to_numeric(out[name], errors="coerce").astype("Int64")
        elif family in ("date", "timestamp"):
            out[name] = pd.to_datetime(out[name])
    return out


class SnapshotGold:
    #local parquet verified against snapshot/manifest.json

    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = snapshot_dir
        self.manifest = self._read_manifest()
        self._entries = {entry["table"]: entry for entry in self.manifest["tables"]}
        self._frames: dict[str, pd.DataFrame] = {}
        for name in self._entries:
            self._verify(name)

    def _read_manifest(self) -> dict:
        path = self.snapshot_dir / "manifest.json"
        if not path.is_file():
            raise GoldError(
                "snapshot/manifest.json is missing. The dashboard will not estimate or "
                "substitute numbers. Rebuild Gold with scripts/build-gold.py."
            )
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GoldError(f"snapshot/manifest.json is not valid JSON ({exc}).") from exc
        for key in ("build_id", "built_at", "source", "tables"):
            if key not in manifest:
                raise GoldError(f"snapshot/manifest.json has no {key}.")
        if not isinstance(manifest["tables"], list) or not manifest["tables"]:
            raise GoldError("snapshot/manifest.json lists no tables.")
        return manifest

    def _verify(self, name: str) -> None:
        entry = self._entries[name]
        filename = entry.get("file", "")
        if not filename or Path(filename).name != filename:
            raise GoldError(f"Manifest file name for {name} is not a plain filename.")
        path = self.snapshot_dir / filename
        if not path.is_file():
            raise GoldError(
                f"snapshot/{filename} is missing. The dashboard will not fill this table with estimates."
            )
        actual_hash = _sha256(path)
        if actual_hash != entry.get("sha256"):
            raise GoldError(
                f"snapshot/{filename} sha256 is {actual_hash}, but the manifest expects "
                f"{entry.get('sha256')}. The file is corrupt or from a different build."
            )
        try:
            parquet = pq.ParquetFile(path)
        except Exception as exc:
            raise GoldError(f"snapshot/{filename} could not be read as Parquet ({exc}).") from exc
        rows = parquet.metadata.num_rows
        if rows != entry.get("rows"):
            raise GoldError(
                f"snapshot/{filename} has {rows} rows; the manifest expects {entry.get('rows')}."
            )
        expected_cols = [column["name"] for column in entry["columns"]]
        actual_cols = [field.name for field in parquet.schema_arrow]
        if actual_cols != expected_cols:
            missing = [col for col in expected_cols if col not in actual_cols]
            extra = [col for col in actual_cols if col not in expected_cols]
            raise GoldError(
                f"snapshot/{filename} columns do not match the manifest. "
                f"Missing: {missing or 'none'}. Extra: {extra or 'none'}."
            )
        forbidden = FORBIDDEN_COLUMNS.intersection(actual_cols)
        if forbidden:
            raise GoldError(
                f"snapshot/{filename} contains identifiers that must not be published: "
                + ", ".join(sorted(forbidden))
            )
        for column, field in zip(entry["columns"], parquet.schema_arrow):
            expected = _family(column["type"])
            actual = _arrow_family(field.type)
            if actual != expected:
                raise GoldError(
                    f"snapshot/{filename} column {column['name']} is {field.type}, "
                    f"but the manifest says {column['type']}."
                )

    def frame(self, name: str) -> pd.DataFrame:
        if name not in self._entries:
            known = ", ".join(sorted(self._entries))
            raise GoldError(f"No Gold table named {name}. The snapshot has: {known}.")
        if name not in self._frames:
            entry = self._entries[name]
            path = self.snapshot_dir / entry["file"]
            try:
                loaded = pd.read_parquet(path)
            except Exception as exc:
                raise GoldError(f"snapshot/{entry['file']} could not be loaded ({exc}).") from exc
            self._frames[name] = _coerce(loaded, entry["columns"])
        return self._frames[name]

    def provenance(self) -> dict:
        return {
            "build_id": self.manifest["build_id"],
            "built_at": self.manifest["built_at"],
            "source": self.manifest["source"],
            "note": self.manifest.get("note", ""),
            "tables": len(self._entries),
            "mode": "snapshot",
        }


def open_gold(project_root: Path) -> SnapshotGold:
    #ANTE_GOLD_SOURCE=databricks is reserved for a later read-only sql connection.
    mode = os.environ.get("ANTE_GOLD_SOURCE", "snapshot").strip().lower()
    if mode in ("", "snapshot"):
        return SnapshotGold(project_root / "snapshot")
    if mode == "databricks":
        raise GoldError(
            "Live Databricks reads are not connected in this build. "
            "Unset ANTE_GOLD_SOURCE or set it to snapshot. No credentials were read."
        )
    raise GoldError(f"ANTE_GOLD_SOURCE={mode!r} is not a known source. Use snapshot.")
