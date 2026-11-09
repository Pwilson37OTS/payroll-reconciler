"""Corp-to-Corp (C2C) cost source for Corp-Corp contractors.

Corp-Corp contractors aren't paid through payroll — their cost is billed by the
vendor LLC. This reads a C2C export from the payroll folder and returns per-row
records the app maps to a Bullhorn candidate, to use as the "paid" side for
Corp-Corp people in the reconciliation.

Three export shapes are supported (auto-detected):

* **QuickBooks Transaction Detail** (e.g. ``CTS - C2C.xlsx``) — one row per bill
  line; the Bullhorn Placement ID and name are embedded in the Description
  (``"<placementId>, <Name>, <date>, <hrs>, <rate>"``). No candidate id, so the
  app resolves placement -> candidate via Bullhorn.
* **QuickBooks AP "C2C Audit Report"** (current ``C2C - OTS.xlsx``) — a broad
  vendor-bill export with a title block then ``Date, Due date, Bill number,
  Vendor, Line description, Amount, Paid status, ..., Item class``. The placement
  id, name and work date live in ``Line description`` in the same
  ``"<placementId>, <Name>, <date>, <hrs>, <rate>"`` format. Only rows carrying a
  placement id are kept (labor + expense, all-in); name-only expense lines and
  overhead/benefits rows (no placement id) are skipped. Paid and Unpaid both
  count. Month attribution uses the Line-description work date.
* **Bullhorn C2C export** (legacy ``OTS - C2C.xlsx``) — columns include
  ``Candidate ID``, ``Placement ID``, ``Name``, ``Period End Date`` and
  ``Payroll Amount`` directly (no parsing / mapping needed).
"""

from __future__ import annotations

import datetime as dt
import glob
import os
import re

import openpyxl
import pandas as pd

from reconciler import periods

_PERIOD_RE = re.compile(r"^[A-Za-z]+ \d{4}$")            # "August 2026"
_QBO_COST_TYPES = {"bill"}                               # exclude Bill Payments
# A C2C bill line is labor ("wage") when booked to the contractor-fees account;
# anything else (e.g. "Employee Advance" for per diem / expense) is non-wage and
# is reconciled like payroll per diem: shown separately, not a wage variance.
_C2C_WAGE_ACCOUNT_MARKER = "contractor fee"


def _c2c_kind(account: object) -> str:
    """Classify a C2C bill line by its QuickBooks distribution account.

    Returns "wage" for contractor-fee (labor) lines — and when the account is
    unknown/blank, so labor is never accidentally dropped — otherwise "nonwage"
    (per diem / expense / advance paid to the C2C vendor).
    """
    if account is None:
        return "wage"
    a = str(account).strip().lower()
    if not a:
        return "wage"
    return "wage" if _C2C_WAGE_ACCOUNT_MARKER in a else "nonwage"


def find_c2c_file(root_dir: str, entity: str) -> str | None:
    """Newest .xlsx under root_dir whose name has the entity code and 'c2c'."""
    hits = [
        p for p in glob.glob(os.path.join(root_dir, "**", "*.xlsx"), recursive=True)
        if not os.path.basename(p).startswith("~$")
        and entity.lower() in os.path.basename(p).lower()
        and "c2c" in os.path.basename(p).lower()
    ]
    return max(hits, key=os.path.getmtime) if hits else None


def _num(text: object) -> str | None:
    if text is None:
        return None
    s = str(text).strip()
    s = re.sub(r"\.0+$", "", s)
    return s if s.isdigit() else None


def _amount(v: object) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_desc(desc: object) -> dict:
    """Pull work date / hours / rate from a C2C description string.

    Format: ``"<placementId>, <Name>, <date>, <hours>, <rate>"``. Returns the
    fields it can find (placement_id, name, work_date, hours, rate); missing
    pieces are None.
    """
    out = {"placement_id": None, "name": None, "work_date": None,
           "hours": None, "rate": None}
    if not isinstance(desc, str):
        return out
    parts = [p.strip() for p in desc.split(",")]
    if parts and parts[0].isdigit():
        out["placement_id"] = parts[0]
    if len(parts) > 1:
        out["name"] = parts[1]
    if len(parts) > 2:
        out["work_date"] = parts[2]
    if len(parts) > 3:
        out["hours"] = _amount(parts[3])
    if len(parts) > 4:
        out["rate"] = _amount(parts[4])
    return out


def parse_c2c(path: str) -> tuple[list[dict], str | None]:
    """Return (records, period_label).

    Each record: ``{candidate_id, placement_id, name, amount, period_end}``
    (candidate_id/placement_id are strings or None; period_end is a Timestamp or
    None). ``period_label`` is the report's month label if the file carries one.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))

    def header_index(*markers):
        want = {m.lower() for m in markers}
        for i, r in enumerate(rows[:12]):
            vals = {str(c).strip().lower() for c in r if c is not None}
            if want.issubset(vals):
                return i
        return None

    # --- Bullhorn C2C export (explicit columns) ---
    bh = header_index("candidate id", "payroll amount")
    if bh is not None:
        header = [str(c).strip() if c is not None else "" for c in rows[bh]]
        col = {h.lower(): j for j, h in enumerate(header)}
        ci, pi = col.get("candidate id"), col.get("placement id")
        ni, ai = col.get("name"), col.get("payroll amount")
        pei, dci = col.get("period end date"), col.get("c2c description")
        records = []
        for r in rows[bh + 1:]:
            amt = _amount(r[ai]) if ai is not None and ai < len(r) else None
            cid = _num(r[ci]) if ci is not None and ci < len(r) else None
            if amt is None or cid is None:
                continue
            d = _parse_desc(r[dci] if dci is not None and dci < len(r) else None)
            records.append({
                "candidate_id": cid,
                "placement_id": _num(r[pi]) if pi is not None and pi < len(r) else None,
                "name": str(r[ni]).strip() if ni is not None and r[ni] is not None else None,
                "amount": amt,
                "period_end": periods.to_ts(r[pei]) if pei is not None and pei < len(r) else None,
                "work_date": d["work_date"], "hours": d["hours"], "rate": d["rate"],
                # Bullhorn export has no account breakdown -> treat as labor.
                "account": None, "kind": "wage",
            })
        return records, None

    # --- QuickBooks Transaction Detail (parse Description) ---
    qbo = header_index("transaction date", "amount")
    if qbo is not None:
        header = [str(c).strip() if c is not None else "" for c in rows[qbo]]
        col = {h: j for j, h in enumerate(header)}
        ti, di, ai = col.get("Transaction type"), col.get("Description"), col.get("Amount")
        tdi = col.get("Transaction date")
        # The expense account column differs by QBO report: "Purchases by Vendor
        # Detail" uses "Distribution account"; "Transaction Report" uses
        # "Account Name". (Never "Item split account" — that's the A/P offset.)
        dai = col.get("Distribution account")
        if dai is None:
            dai = col.get("Account Name")
        period = None
        for r in rows[:qbo]:
            for c in r:
                if isinstance(c, str) and _PERIOD_RE.match(c.strip()):
                    period = c.strip()
        records = []
        for r in rows[qbo + 1:]:
            desc = r[di] if di is not None and di < len(r) else None
            d = _parse_desc(desc)
            if d["placement_id"] is None:
                continue
            ttype = str(r[ti]).strip().lower() if ti is not None and r[ti] is not None else ""
            if ttype not in _QBO_COST_TYPES:
                continue
            amt = _amount(r[ai])
            if amt is None:
                continue
            account = r[dai] if dai is not None and dai < len(r) else None
            # The Description's work date is the billing week (best for month
            # attribution), but it can be malformed (e.g. "12/28//2025"). Fall
            # back to the bill's Transaction date so one typo can't drop/mis-date
            # a line — and never leaves an unusable period_end that crashes the
            # month filter.
            period_end = periods.to_ts(d["work_date"])
            if period_end is None or pd.isna(period_end):
                period_end = periods.to_ts(r[tdi] if tdi is not None and tdi < len(r) else None)
            records.append({
                "candidate_id": None,
                "placement_id": d["placement_id"],
                "name": d["name"],
                "amount": amt,
                "period_end": period_end,
                "work_date": d["work_date"], "hours": d["hours"], "rate": d["rate"],
                "account": str(account).strip() if account is not None else None,
                "kind": _c2c_kind(account),
            })
        return records, period

    # --- QuickBooks AP "C2C Audit Report" (vendor bill lines) ---
    # A broad A/P export with a title block, then columns: Date, Due date, Bill
    # number, Vendor, Line description, Amount, Paid status, ..., Item class.
    # Contractor identity + work date are embedded in "Line description" exactly
    # like the Transaction-Detail "Description": "<placementId>, <Name>, <date>,
    # <hours>, <rate>". Per Phil (2026-11): include ONLY rows that carry a
    # placement id (the staffing cost — labor + expense, all-in); skip name-only
    # expense lines and overhead/benefits (Delta Dental, Blue Cross, background
    # checks) which have no placement id. Month attribution uses the Line
    # description's work date. Paid AND Unpaid bills both count.
    ap = header_index("line description", "amount")
    if ap is not None:
        header = [str(c).strip() if c is not None else "" for c in rows[ap]]
        col = {h.lower(): j for j, h in enumerate(header)}
        di, ai = col.get("line description"), col.get("amount")
        bdi = col.get("date")  # bill date — fallback only if the work date is bad
        period = None
        for r in rows[:ap]:
            for c in r:
                if isinstance(c, str) and _PERIOD_RE.match(c.strip()):
                    period = c.strip()
        records = []
        for r in rows[ap + 1:]:
            raw = r[di] if di is not None and di < len(r) else None
            # A leading apostrophe (Excel text-forcing) sometimes rides along.
            desc = str(raw).lstrip("'").strip() if raw is not None else None
            d = _parse_desc(desc)
            if d["placement_id"] is None:
                continue  # only placement-tagged contractor bill lines
            amt = _amount(r[ai]) if ai is not None and ai < len(r) else None
            if amt is None:
                continue
            period_end = periods.to_ts(d["work_date"])
            if period_end is None or pd.isna(period_end):
                period_end = periods.to_ts(r[bdi] if bdi is not None and bdi < len(r) else None)
            records.append({
                "candidate_id": None,
                "placement_id": d["placement_id"],
                "name": d["name"],
                "amount": amt,
                "period_end": period_end,
                "work_date": d["work_date"], "hours": d["hours"], "rate": d["rate"],
                # All-in C2C cost (labor + expense); this export has no per-account
                # split, so everything tagged to the placement is the staffing cost.
                "account": None, "kind": "wage",
            })
        return records, period

    raise ValueError(
        f"{os.path.basename(path)} isn't a recognized C2C export (needs "
        "'Candidate ID'+'Payroll Amount', 'Transaction date'+'Amount', or "
        "'Line description'+'Amount' columns)."
    )


def fetch_c2c(entity: str, root_dir: str) -> tuple[list[dict], dict]:
    """Find + parse the entity's C2C file. Returns (records, provenance).

    Returns ([], prov with found=False) when no C2C file is present — the caller
    then leaves Corp-Corp excluded, as before.
    """
    path = find_c2c_file(root_dir, entity)
    if not path:
        return [], {"source": "c2c", "found": False, "entity": entity}
    records, period = parse_c2c(path)
    prov = {
        "source": "c2c",
        "found": True,
        "file": os.path.basename(path),
        "period_label": period,
        "rows": len(records),
        "total": round(sum(r["amount"] for r in records), 2),
        "wage_total": round(sum(r["amount"] for r in records if r.get("kind") != "nonwage"), 2),
        "nonwage_total": round(sum(r["amount"] for r in records if r.get("kind") == "nonwage"), 2),
        "has_candidate_ids": all(r["candidate_id"] for r in records) if records else False,
        "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return records, prov
