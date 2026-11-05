"""Storage adapter contract.

The app persists each saved reconciliation through a ``StorageBackend``. Today
the only implementation is :class:`storage.local.LocalFileStorage`; a Snowflake
or Postgres backend can be dropped in later by implementing this same interface,
with no changes to the app or engine.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd


@dataclass
class AuditRecord:
    """One saved reconciliation, as handed to / returned from a backend."""

    audit_id: str                       # unique key, e.g. "2026-02"
    period: str                         # human label, e.g. "February 2026"
    entity: str                         # business unit, e.g. "CTS"
    run_by: str                         # user email / name
    run_at: str                         # ISO-8601 timestamp (UTC)
    threshold: float                    # dollar tolerance used
    summary: dict[str, Any]             # headline numbers (engine summary)
    detail: pd.DataFrame                # per-person rows incl. annotations
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    # ^ maps a label ("ats", "paycheck") -> stored filename of the raw upload.

    def meta(self) -> dict[str, Any]:
        """Everything except the (large) detail frame, for index listings."""
        d = asdict(self)
        d.pop("detail", None)
        return d


class StorageBackend(abc.ABC):
    """Persist and retrieve :class:`AuditRecord` objects."""

    @abc.abstractmethod
    def save_audit(
        self, record: AuditRecord, raw_files: dict[str, bytes] | None = None
    ) -> None:
        """Persist a record. ``raw_files`` maps a label -> original file bytes.

        Implementations should overwrite an existing record with the same
        ``audit_id`` so re-running a month replaces it rather than duplicating.
        """

    @abc.abstractmethod
    def list_audits(self) -> list[dict[str, Any]]:
        """Return metadata for every saved audit, newest ``run_at`` first."""

    @abc.abstractmethod
    def get_audit(self, audit_id: str) -> AuditRecord | None:
        """Return a full record (with detail), or ``None`` if not found."""

    @abc.abstractmethod
    def exists(self, audit_id: str) -> bool:
        """True if an audit with this id is already saved."""

    @abc.abstractmethod
    def get_raw_file(self, audit_id: str, label: str) -> bytes | None:
        """Return the stored bytes of an original upload, or ``None``."""
