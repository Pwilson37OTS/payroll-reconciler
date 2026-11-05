"""Pluggable storage for saved reconciliations."""

from .base import AuditRecord, StorageBackend
from .local import LocalFileStorage


def get_storage(kind: str = "local", **kwargs) -> StorageBackend:
    """Factory: return a storage backend by name.

    Add ``"snowflake"`` / ``"postgres"`` cases here when a durable backend is
    built; the rest of the app depends only on the :class:`StorageBackend`
    interface, so nothing else needs to change.
    """
    kind = (kind or "local").lower()
    if kind == "local":
        return LocalFileStorage(**kwargs)
    if kind == "snowflake":
        from .snowflake_store import SnowflakeStorage
        return SnowflakeStorage(**kwargs)
    raise ValueError(f"Unknown storage backend: {kind!r}")


__all__ = ["AuditRecord", "StorageBackend", "LocalFileStorage", "get_storage"]
