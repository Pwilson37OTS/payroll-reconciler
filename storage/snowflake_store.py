"""Snowflake storage backend — durable audit history for the cloud deploy.

Community Cloud's filesystem is wiped on every redeploy (and ``refresh_data.ps1``
redeploys on each data push), so the local-file store would lose saved audits.
This backend persists each saved reconciliation to two tables in Snowflake so the
history survives restarts and is shared across everyone who opens the app.

Tables (created on first use), in the configured schema
(``BULLHORN_MIRROR.ANALYTICS`` by default):

* ``COST_RECON_AUDIT`` — one row per saved audit (metadata + the detail frame as
  CSV text + the summary/warnings/provenance/source_files as JSON text).
* ``COST_RECON_AUDIT_FILE`` — one row per stored raw file (base64 text), so the
  original uploads / pulled snapshots are downloadable from history.

It implements the same :class:`storage.base.StorageBackend` interface as the
local backend, so nothing else in the app changes. This module is
Streamlit-free: it takes plain connector kwargs (``conn_params``) and talks to
``snowflake.connector`` directly.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
from typing import Any

import pandas as pd

from .base import AuditRecord, StorageBackend


class SnowflakeStorage(StorageBackend):
    def __init__(
        self,
        conn_params: dict,
        schema: str = "BULLHORN_MIRROR.ANALYTICS",
    ) -> None:
        if not conn_params:
            raise ValueError("SnowflakeStorage needs Snowflake connection params.")
        self._params = dict(conn_params)
        self._schema = schema.rstrip(".")
        self._audit = f"{self._schema}.COST_RECON_AUDIT"
        self._files = f"{self._schema}.COST_RECON_AUDIT_FILE"
        self._ready = False

    # --- connection ----------------------------------------------------------
    def _connect(self):
        import snowflake.connector  # lazy so the app imports without the driver

        return snowflake.connector.connect(**self._params)

    def _ensure_tables(self, cur) -> None:
        if self._ready:
            return
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._audit} (
                AUDIT_ID STRING, PERIOD STRING, ENTITY STRING, RUN_BY STRING,
                RUN_AT STRING, THRESHOLD FLOAT, SUMMARY STRING, DETAIL_CSV STRING,
                WARNINGS STRING, PROVENANCE STRING, SOURCE_FILES STRING,
                UPDATED_AT TIMESTAMP_NTZ
            )"""
        )
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._files} (
                AUDIT_ID STRING, LABEL STRING, CONTENT_B64 STRING
            )"""
        )
        self._ready = True

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _loads(text: object, default):
        if text is None:
            return default
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default

    def _meta_from_row(self, cols: list[str], row: tuple) -> dict[str, Any]:
        r = dict(zip(cols, row))
        return {
            "audit_id": r.get("AUDIT_ID"),
            "period": r.get("PERIOD"),
            "entity": r.get("ENTITY"),
            "run_by": r.get("RUN_BY"),
            "run_at": r.get("RUN_AT"),
            "threshold": float(r.get("THRESHOLD") or 0.0),
            "summary": self._loads(r.get("SUMMARY"), {}),
            "warnings": self._loads(r.get("WARNINGS"), []),
            "provenance": self._loads(r.get("PROVENANCE"), {}),
            "source_files": self._loads(r.get("SOURCE_FILES"), {}),
        }

    # --- API -----------------------------------------------------------------
    def save_audit(
        self, record: AuditRecord, raw_files: dict[str, bytes] | None = None
    ) -> None:
        # Record raw files as base64; name them like the local backend does so a
        # download gets the right extension (xlsx starts with the ZIP magic).
        source_files = dict(record.source_files)
        file_rows: list[tuple] = []
        for label, data in (raw_files or {}).items():
            ext = ".xlsx" if data[:4] == b"PK\x03\x04" else ".csv"
            source_files[label] = f"{label}{ext}"
            file_rows.append((record.audit_id, label, base64.b64encode(data).decode("ascii")))
        record.source_files = source_files

        detail_csv = record.detail.to_csv(index=False)
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_tables(cur)
            # Overwrite any existing audit with this id (re-running a month).
            cur.execute(f"DELETE FROM {self._audit} WHERE AUDIT_ID = %s", (record.audit_id,))
            cur.execute(f"DELETE FROM {self._files} WHERE AUDIT_ID = %s", (record.audit_id,))
            cur.execute(
                f"""INSERT INTO {self._audit}
                    (AUDIT_ID, PERIOD, ENTITY, RUN_BY, RUN_AT, THRESHOLD, SUMMARY,
                     DETAIL_CSV, WARNINGS, PROVENANCE, SOURCE_FILES, UPDATED_AT)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    record.audit_id, record.period, record.entity, record.run_by,
                    record.run_at, float(record.threshold),
                    json.dumps(record.summary, default=str),
                    detail_csv,
                    json.dumps(list(record.warnings), default=str),
                    json.dumps(record.provenance, default=str),
                    json.dumps(record.source_files, default=str),
                    dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                ),
            )
            for fr in file_rows:
                cur.execute(
                    f"INSERT INTO {self._files} (AUDIT_ID, LABEL, CONTENT_B64) "
                    f"VALUES (%s,%s,%s)",
                    fr,
                )

    def list_audits(self) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_tables(cur)
            cur.execute(
                f"""SELECT AUDIT_ID, PERIOD, ENTITY, RUN_BY, RUN_AT, THRESHOLD,
                           SUMMARY, WARNINGS, PROVENANCE, SOURCE_FILES
                    FROM {self._audit} ORDER BY RUN_AT DESC"""
            )
            cols = [c[0] for c in cur.description]
            return [self._meta_from_row(cols, row) for row in cur.fetchall()]

    def get_audit(self, audit_id: str) -> AuditRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_tables(cur)
            cur.execute(
                f"""SELECT AUDIT_ID, PERIOD, ENTITY, RUN_BY, RUN_AT, THRESHOLD,
                           SUMMARY, WARNINGS, PROVENANCE, SOURCE_FILES, DETAIL_CSV
                    FROM {self._audit} WHERE AUDIT_ID = %s""",
                (audit_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            r = dict(zip(cols, row))
            meta = self._meta_from_row(cols, row)
            detail = pd.read_csv(io.StringIO(r.get("DETAIL_CSV") or ""), dtype={"id": str})
            return AuditRecord(detail=detail, **meta)

    def exists(self, audit_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_tables(cur)
            cur.execute(f"SELECT COUNT(*) FROM {self._audit} WHERE AUDIT_ID = %s", (audit_id,))
            return int(cur.fetchone()[0]) > 0

    def get_raw_file(self, audit_id: str, label: str) -> bytes | None:
        with self._connect() as conn, conn.cursor() as cur:
            self._ensure_tables(cur)
            cur.execute(
                f"SELECT CONTENT_B64 FROM {self._files} WHERE AUDIT_ID = %s AND LABEL = %s",
                (audit_id, label),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            try:
                return base64.b64decode(row[0])
            except (ValueError, TypeError):
                return None
