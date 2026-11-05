"""Local-filesystem storage backend (development / single-machine use).

Layout under ``root``::

    audit_store/
      index.json                 # list of record metadata, for fast listing
      2026-02/
        meta.json                # AuditRecord minus detail
        detail.csv               # per-person rows incl. annotations
        raw/
          ats.xlsm
          paycheck.xlsm

NOTE: on Streamlit Community Cloud the filesystem is ephemeral, so this backend
is for local development only. Swap in a Snowflake/Postgres backend for a
durable production history (see storage/base.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .base import AuditRecord, StorageBackend


class LocalFileStorage(StorageBackend):
    def __init__(self, root: str | Path = "audit_store") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths ---------------------------------------------------------------
    def _dir(self, audit_id: str) -> Path:
        return self.root / audit_id

    def _index_path(self) -> Path:
        return self.root / "index.json"

    # --- index helpers -------------------------------------------------------
    def _read_index(self) -> list[dict[str, Any]]:
        p = self._index_path()
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        entries.sort(key=lambda e: e.get("run_at", ""), reverse=True)
        self._index_path().write_text(
            json.dumps(entries, indent=2, default=str), encoding="utf-8"
        )

    # --- API -----------------------------------------------------------------
    def save_audit(
        self, record: AuditRecord, raw_files: dict[str, bytes] | None = None
    ) -> None:
        d = self._dir(record.audit_id)
        (d / "raw").mkdir(parents=True, exist_ok=True)

        # Persist raw snapshots (uploaded files or pulled data) and remember
        # their filenames. Extension is inferred: xlsx files start with the ZIP
        # magic "PK\x03\x04"; pulled data is saved as CSV.
        source_files = dict(record.source_files)
        for label, data in (raw_files or {}).items():
            ext = ".xlsx" if data[:4] == b"PK\x03\x04" else ".csv"
            fname = f"{label}{ext}"
            (d / "raw" / fname).write_bytes(data)
            source_files[label] = fname
        record.source_files = source_files

        record.detail.to_csv(d / "detail.csv", index=False)
        (d / "meta.json").write_text(
            json.dumps(record.meta(), indent=2, default=str), encoding="utf-8"
        )

        entries = [e for e in self._read_index() if e.get("audit_id") != record.audit_id]
        entries.append(record.meta())
        self._write_index(entries)

    def list_audits(self) -> list[dict[str, Any]]:
        return self._read_index()

    def get_audit(self, audit_id: str) -> AuditRecord | None:
        d = self._dir(audit_id)
        meta_p = d / "meta.json"
        detail_p = d / "detail.csv"
        if not meta_p.exists() or not detail_p.exists():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        detail = pd.read_csv(detail_p, dtype={"id": str})
        return AuditRecord(detail=detail, **meta)

    def exists(self, audit_id: str) -> bool:
        return (self._dir(audit_id) / "meta.json").exists()

    def get_raw_file(self, audit_id: str, label: str) -> bytes | None:
        meta_p = self._dir(audit_id) / "meta.json"
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        fname = meta.get("source_files", {}).get(label)
        if not fname:
            return None
        path = self._dir(audit_id) / "raw" / fname
        return path.read_bytes() if path.exists() else None
