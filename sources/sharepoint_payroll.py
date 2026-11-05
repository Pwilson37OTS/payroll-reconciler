"""Payroll source: pull the payroll register from a SharePoint document library.

Two backends, same output:

* **folder** (default) — read the payroll-register xlsx files from a configured
  directory. On an internal host this points at the SharePoint library synced
  locally (OneDrive) or a UNC path, so it needs no credentials. This is the
  path used today (the register export dropped into ``_shared/payroll-audit``).
* **graph** — fetch straight from SharePoint via Microsoft Graph (for a
  cloud-hosted app with an app registration). Stubbed here; wire when needed.

Two file shapes are supported:

* **Combined register** (preferred, current) — one broad ``Check Details`` export
  covering BOTH companies. Every check is routed to an entity by its person's
  Bullhorn ``Branch`` (authoritative), with the payroll **Legal Company** as a
  fallback only for people not in Bullhorn. Used for CTS *and* OTS.
* **Per-entity files** (legacy) — one file per entity, matched by the entity code
  in the filename. Kept as a fallback when no combined register is present.

Either way it returns a normalized paycheck frame (via
:func:`reconciler.normalize`), so the engine is unchanged. The engine filters to
the accounting month by pay date.
"""

from __future__ import annotations

import datetime as dt
import glob
import os

import pandas as pd

from reconciler import config, load_paycheck, normalize

# Sheet in the combined register that holds one row per check.
REGISTER_SHEET = "Check Details"


def _entity_matches(filename: str, entity: str) -> bool:
    """Match a payroll file to an entity by its code appearing in the name."""
    return entity.lower() in os.path.basename(filename).lower()


def _all_payroll_xlsx(root_dir: str) -> list[str]:
    """Every candidate payroll .xlsx under ``root_dir`` (recursive), newest first.

    Skips Excel temp lock files (``~$``) and the QBO C2C export.
    """
    hits = [
        p
        for p in glob.glob(os.path.join(root_dir, "**", "*.xlsx"), recursive=True)
        if not os.path.basename(p).startswith("~$")
        and "c2c" not in os.path.basename(p).lower()
    ]
    return sorted(hits, key=os.path.getmtime, reverse=True)


def find_payroll_files(root_dir: str, entity: str) -> list[str]:
    """Return legacy per-entity payroll .xlsx files for ``entity`` (newest first)."""
    return [p for p in _all_payroll_xlsx(root_dir) if _entity_matches(p, entity)]


def _looks_like_combined_register(path: str) -> bool:
    """True if ``path`` is the multi-company register (a ``Check Details`` sheet
    whose two-row header carries the Legal Company column)."""
    try:
        xls = pd.ExcelFile(path)
    except Exception:
        return False
    sheet = next(
        (s for s in xls.sheet_names if s.strip().lower() == REGISTER_SHEET.lower()),
        None,
    )
    if sheet is None:
        return False
    try:
        head = pd.read_excel(xls, sheet_name=sheet, header=None, nrows=3, dtype=object)
    except Exception:
        return False
    cells = {
        normalize._norm_header(v)
        for v in head.to_numpy().ravel().tolist()
        if v is not None and not (isinstance(v, float) and pd.isna(v))
    }
    return "legal company code" in cells or "legal company name" in cells


def find_combined_register(root_dir: str) -> str | None:
    """Return the newest combined multi-company register under ``root_dir``, if any."""
    for p in _all_payroll_xlsx(root_dir):
        if _looks_like_combined_register(p):
            return p
    return None


def assign_entities(frame: pd.DataFrame, entity_map: dict | None) -> pd.Series:
    """Route each payroll row to an entity: Bullhorn Branch (authoritative) first,
    then the payroll Legal Company code, then its name.

    ``entity_map`` maps candidate/employee id -> 'CTS'|'OTS' (from Bullhorn). Rows
    that resolve to neither entity get ``<NA>`` (excluded from every entity).
    """
    emap = {str(k).strip(): str(v).strip() for k, v in (entity_map or {}).items()}

    def _by_id(x: object) -> str | None:
        return emap.get(str(x).strip()) if x is not None else None

    def _by_code(x: object) -> str | None:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        return config.LEGAL_COMPANY_ENTITY.get(str(x).strip())

    def _by_name(x: object) -> str | None:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        return config.LEGAL_COMPANY_NAME_ENTITY.get(normalize._norm_header(x))

    by_id = frame["employee_number"].map(_by_id)
    by_code = frame.get("legal_company", pd.Series(index=frame.index, dtype=object)).map(_by_code)
    by_name = frame.get("legal_company_name", pd.Series(index=frame.index, dtype=object)).map(_by_name)
    routed = by_id.where(by_id.notna(), by_code)
    routed = routed.where(routed.notna(), by_name)
    return routed


def fetch_payroll(
    entity: str,
    schedule: str,
    root_dir: str,
    compare_amount: str = config.DEFAULT_COMPARE_AMOUNT,
    backend: str = "folder",
    entity_map: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Fetch and normalize the payroll side for one entity.

    Prefers the combined multi-company register (routing each check to its entity
    by Bullhorn Branch, Legal Company as fallback). Falls back to legacy
    per-entity files when no combined register is present. Returns
    ``(normalized_frame, provenance)``; the engine filters to the target month.
    """
    if backend != "folder":
        raise NotImplementedError(
            "Only the 'folder' backend is implemented. For a cloud host, add a "
            "Microsoft Graph backend (app registration with Sites.Read.All)."
        )

    register = find_combined_register(root_dir)
    if register is not None:
        raw = normalize._read_paycheck_raw(register, REGISTER_SHEET)
        frame = normalize.normalize_paycheck_frame(raw, compare_amount, schedule)
        routed = assign_entities(frame, entity_map)
        combined = frame[routed.eq(entity).fillna(False)].reset_index(drop=True)
        combined.attrs.update(frame.attrs)
        combined["_source_file"] = os.path.basename(register)
        routed_counts = routed.value_counts(dropna=False).to_dict()
        provenance = {
            "source": f"sharepoint-folder:{root_dir}",
            "entity": entity,
            "files": [os.path.basename(register)],
            "mode": "combined-register",
            "routing": "bullhorn-branch (legal-company fallback)",
            "entity_map_size": len(entity_map or {}),
            "routed_checks": {str(k): int(v) for k, v in routed_counts.items()},
            "compare_amount": compare_amount,
            "rows": int(len(combined)),
            "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        return combined, provenance

    # --- Legacy fallback: per-entity files matched by filename -----------------
    files = find_payroll_files(root_dir, entity)
    if not files:
        raise FileNotFoundError(
            f"No payroll register (or per-entity .xlsx for '{entity}') found under "
            f"{root_dir!r}."
        )

    frames = []
    for f in files:
        part = load_paycheck(f, compare_amount=compare_amount, schedule=schedule)
        part["_source_file"] = os.path.basename(f)
        frames.append(part)

    combined = pd.concat(frames, ignore_index=True)
    # A person+check could appear in two overlapping exports; keep one per
    # (employee, pay_date, source amount) to avoid double-counting.
    combined = combined.drop_duplicates(
        subset=["employee_number", "pay_date", "amount"]
    ).reset_index(drop=True)
    combined.attrs.update(frames[0].attrs)

    provenance = {
        "source": f"sharepoint-folder:{root_dir}",
        "entity": entity,
        "files": [os.path.basename(f) for f in files],
        "mode": "per-entity-files",
        "compare_amount": compare_amount,
        "rows": int(len(combined)),
        "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return combined, provenance
