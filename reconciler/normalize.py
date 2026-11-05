"""Load and normalize the two source exports into clean DataFrames.

Turns whatever spreadsheet the user uploads into a predictable frame with
canonical column names, footer rows removed, numbers coerced, and each row
tagged with the CTS accounting month of its billing week (see periods.py).
"""

from __future__ import annotations

import re
from typing import IO, Mapping

import pandas as pd

from . import config, periods


class ColumnNotFoundError(ValueError):
    """Raised when a required column cannot be located in an upload."""


def _norm_header(text: object) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _resolve_columns(
    columns, mapping: Mapping[str, list[str]]
) -> dict[str, str]:
    lookup = {_norm_header(c): c for c in columns}
    resolved: dict[str, str] = {}
    for canonical, candidates in mapping.items():
        for cand in candidates:
            actual = lookup.get(_norm_header(cand))
            if actual is not None:
                resolved[canonical] = actual
                break
    return resolved


def _clean_id(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if text == "":
        return None
    if text.upper() in {m.upper() for m in config.FOOTER_ID_MARKERS}:
        return None
    text = re.sub(r"\.0+$", "", text)
    if not re.fullmatch(r"\d+", text):
        return None
    return text


def _to_amount(value: object) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if text in ("", "-"):
        return 0.0
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1]
    try:
        num = float(text)
    except ValueError:
        return 0.0
    return -num if neg else num


def _read_any(source: "str | IO[bytes]", sheet_name: object = 0) -> pd.DataFrame:
    name = str(getattr(source, "name", source)).lower()
    if name.endswith(".csv"):
        return pd.read_csv(source, dtype=object)
    return pd.read_excel(source, sheet_name=sheet_name, dtype=object)


def _add_ats_period(out: pd.DataFrame, schedule: str) -> pd.DataFrame:
    """Tag each ATS row with its period-end anchor and accounting month."""
    out["billing_period_end"] = out["billing_period_end"].map(periods.to_ts)
    res = out["billing_period_end"].map(
        lambda d: periods.ats_anchor_period(d, schedule)
    )
    out["week_ending"] = [r[0] for r in res]      # weekly billing week (display)
    out["period_end"] = [r[1] for r in res]        # pay period it rolls up to
    out["acct_year"] = [r[2][0] for r in res]
    out["acct_month"] = [r[2][1] for r in res]
    return out


def load_ats(
    source: "str | IO[bytes]",
    sheet_name: object = 0,
    schedule: str = periods.SCHEDULE_WEEKLY,
) -> pd.DataFrame:
    """Load the Bullhorn 'ATS Hours' export (a file) into a normalized frame.

    Thin wrapper over :func:`normalize_ats_frame` that first reads the file.
    """
    return normalize_ats_frame(_read_any(source, sheet_name), schedule)


def normalize_ats_frame(
    raw: pd.DataFrame, schedule: str = periods.SCHEDULE_WEEKLY
) -> pd.DataFrame:
    """Normalize a raw ATS DataFrame (from a file OR a Snowflake query).

    ``raw`` must carry the ATS column spellings in :data:`config.ATS_COLUMNS`
    (matched case/space-insensitively). Returns columns ``candidate_id, name,
    employee_type, is_corp_corp, client, hours_paid, wages_paid,
    billing_period_end, week_ending, period_end, acct_year, acct_month``.
    Footer / blank-ID rows are dropped. ``schedule`` selects the accounting-month
    rule (weekly for CTS, biweekly for OTS).
    """
    cols = _resolve_columns(raw.columns, config.ATS_COLUMNS)
    for required in ("candidate_id", "wages_paid"):
        if required not in cols:
            raise ColumnNotFoundError(
                f"ATS export is missing a required column for '{required}'. "
                f"Expected one of {config.ATS_COLUMNS[required]}."
            )

    out = pd.DataFrame()
    out["candidate_id"] = raw[cols["candidate_id"]].map(_clean_id)
    out["name"] = (
        raw[cols["name"]].astype("string").str.strip() if "name" in cols else pd.NA
    )
    etype = (
        raw[cols["employee_type"]].astype("string").str.strip()
        if "employee_type" in cols
        else pd.Series([pd.NA] * len(raw), dtype="string")
    )
    out["employee_type"] = etype
    out["is_corp_corp"] = etype.fillna("").str.upper().eq(config.CORP_CORP_MARKER)
    out["client"] = (
        raw[cols["client"]].astype("string").str.strip() if "client" in cols else pd.NA
    )
    out["hours_paid"] = (
        raw[cols["hours_paid"]].map(_to_amount) if "hours_paid" in cols else 0.0
    )
    out["wages_paid"] = raw[cols["wages_paid"]].map(_to_amount)
    out["billing_period_end"] = (
        raw[cols["billing_period_end"]] if "billing_period_end" in cols else pd.NaT
    )

    out = out[out["candidate_id"].notna()].reset_index(drop=True)
    return _add_ats_period(out, schedule)


# --- Paycheck ----------------------------------------------------------------
def _seek0(source: "str | IO[bytes]") -> None:
    """Rewind a file-like source so it can be read more than once."""
    seek = getattr(source, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except (OSError, ValueError):
            pass


def _detect_header_row(source: "str | IO[bytes]", sheet_name: object) -> int:
    """Find the header row: the first top row with an ID label + an amount label.

    Handles single-row headers (row 0) and the register's two-row header (row 1,
    where row 0 holds group labels). CSVs always use row 0.
    """
    name = str(getattr(source, "name", source)).lower()
    if name.endswith(".csv"):
        return 0
    _seek0(source)
    probe = pd.read_excel(source, sheet_name=sheet_name, header=None, nrows=12, dtype=object)
    id_markers = {_norm_header(m) for m in config.PAYCHECK_HEADER_ID_MARKERS}
    amt_markers = {_norm_header(m) for m in config.PAYCHECK_HEADER_AMOUNT_MARKERS}
    for i in range(len(probe)):
        vals = {_norm_header(v) for v in probe.iloc[i].tolist() if v is not None}
        if (vals & id_markers) and (vals & amt_markers):
            return i
    return 0


def _dedupe_headers(names: list[str]) -> list[str]:
    """Make column names unique (pandas needs it; first occurrence keeps its
    clean name so the resolver still finds the fields it wants)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}.{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


def _read_paycheck_raw(
    source: "str | IO[bytes]", sheet_name: object = 0
) -> pd.DataFrame:
    """Read a payroll export into a raw DataFrame with resolvable column names.

    * CSV / single-row-header export → read as-is.
    * Register two-row header → combine the group-label row with the field row
      (``field`` unless it is a generic sub-header, then the group label above),
      matching the register's real column names (e.g. ``Regular Dollars``,
      ``1099 Dollars``, ``Legal Company Code``).
    """
    name = str(getattr(source, "name", source)).lower()
    if name.endswith(".csv"):
        _seek0(source)
        return pd.read_csv(source, dtype=object)

    header_row = _detect_header_row(source, sheet_name)
    if header_row <= 0:
        _seek0(source)
        return pd.read_excel(source, sheet_name=sheet_name, header=0, dtype=object)

    # Two-row header: build a combined header from the group row + field row.
    _seek0(source)
    full = pd.read_excel(source, sheet_name=sheet_name, header=None, dtype=object)
    group = full.iloc[header_row - 1].tolist()
    field = full.iloc[header_row].tolist()
    generic = {_norm_header(g) for g in config.PAYCHECK_GENERIC_SUBHEADERS}
    def _txt(v: object) -> str:
        return "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v).strip()

    combined: list[str] = []
    for i in range(len(field)):
        f = _txt(field[i])
        g = _txt(group[i])
        chosen = f if (f and _norm_header(f) not in generic) else (g or f)
        combined.append(chosen or f"col_{i}")
    data = full.iloc[header_row + 1:].reset_index(drop=True)
    data.columns = _dedupe_headers(combined)
    return data


def load_paycheck(
    source: "str | IO[bytes]",
    sheet_name: object = 0,
    compare_amount: str = config.DEFAULT_COMPARE_AMOUNT,
    schedule: str = periods.SCHEDULE_WEEKLY,
) -> pd.DataFrame:
    """Load any supported payroll export into a normalized per-row frame.

    Thin wrapper over :func:`normalize_paycheck_frame` that first reads the file
    (handling CSV, single-row headers, and the register's two-row header).
    """
    raw = _read_paycheck_raw(source, sheet_name)
    return normalize_paycheck_frame(raw, compare_amount, schedule)


def normalize_paycheck_frame(
    raw: pd.DataFrame,
    compare_amount: str = config.DEFAULT_COMPARE_AMOUNT,
    schedule: str = periods.SCHEDULE_WEEKLY,
) -> pd.DataFrame:
    """Normalize a raw payroll DataFrame into a clean per-row frame.

    Columns: ``employee_number, name, pay_type, check_type, pay_date,
    legal_company, week_ending, acct_year, acct_month, total_earnings,
    gross_wages, wages, amount`` (+ ``pt_*`` explainer columns). ``amount`` is
    the field selected by ``compare_amount`` ("Total Earnings" or "Gross Wages"),
    falling back to whichever amount the file has. When the export has no
    "Total Earnings" column (the register), it is reconstructed from Gross Pay +
    1099 + per diem / expense / cell-phone. ``df.attrs['has_dates']`` indicates
    whether it can be week-filtered.
    """
    cols = _resolve_columns(raw.columns, config.PAYCHECK_FIELD_CANDIDATES)
    if "employee_number" not in cols:
        raise ColumnNotFoundError(
            "Payroll export is missing an employee-number column "
            f"(expected one of {config.PAYCHECK_FIELD_CANDIDATES['employee_number']})."
        )
    if not ({"total_earnings", "gross_wages"} & set(cols)):
        raise ColumnNotFoundError(
            "Payroll export has no comparable amount column "
            "(expected 'Total Earnings', 'Gross Wages', or 'Gross Pay')."
        )

    out = pd.DataFrame()
    out["employee_number"] = raw[cols["employee_number"]].map(_clean_id)
    out["name"] = (
        raw[cols["name"]].astype("string").str.strip() if "name" in cols else pd.NA
    )
    out["pay_type"] = (
        raw[cols["pay_type"]].astype("string").str.strip() if "pay_type" in cols else pd.NA
    )
    out["check_type"] = (
        raw[cols["check_type"]].astype("string").str.strip()
        if "check_type" in cols
        else pd.NA
    )
    te_present = "total_earnings" in cols
    gw_present = "gross_wages" in cols
    n1099_present = "dollars_1099" in cols
    out["gross_wages"] = raw[cols["gross_wages"]].map(_to_amount) if gw_present else 0.0
    out["dollars_1099"] = (
        raw[cols["dollars_1099"]].map(_to_amount) if n1099_present else 0.0
    )
    # Labor wages = W2 gross + 1099 (both are work pay Bullhorn bills). Per diem /
    # expense is everything beyond that. The split needs Total + Gross Wages.
    out["wages"] = out["gross_wages"] + out["dollars_1099"]

    # Total Earnings: use the column if present; otherwise (the register has none)
    # rebuild it as wages + non-wage earnings (per diem / expense / cell phone).
    # The register's taxed per diem is already inside Gross Pay, so it is excluded.
    if te_present:
        out["total_earnings"] = raw[cols["total_earnings"]].map(_to_amount)
    elif gw_present:
        extra = out["wages"].copy()
        for fld in config.PAYCHECK_TOTAL_EXTRA_FIELDS:
            if fld in cols:
                extra = extra + raw[cols[fld]].map(_to_amount).values
        out["total_earnings"] = extra
        te_present = True  # a reconstructed Total Earnings is a usable basis
    else:
        out["total_earnings"] = 0.0
    has_wage_split = te_present and gw_present

    # Legal payroll company (routing fallback for people not in Bullhorn).
    out["legal_company"] = (
        raw[cols["legal_company"]].astype("string").str.strip()
        if "legal_company" in cols
        else pd.NA
    )
    out["legal_company_name"] = (
        raw[cols["legal_company_name"]].astype("string").str.strip()
        if "legal_company_name" in cols
        else pd.NA
    )

    # Payroll-only pay types (PTO, sick, holiday, …) kept per check so the app can
    # auto-explain variances that equal their sum. Assign here — before the
    # blank-ID filter/reset below — so they align to the same rows as the amounts.
    explainer_cols = {}
    for label, cands in config.EXPLAINER_PAY_TYPES.items():
        found = _resolve_columns(raw.columns, {label: cands}).get(label)
        colname = "pt_" + re.sub(r"\W+", "_", label)
        out[colname] = raw[found].map(_to_amount) if found else 0.0
        explainer_cols[label] = colname

    # Choose which amount to compare, honoring the requested mode with fallback.
    preference = config.COMPARE_AMOUNT_OPTIONS.get(
        compare_amount, config.COMPARE_AMOUNT_OPTIONS[config.DEFAULT_COMPARE_AMOUNT]
    )
    avail = {"wages": gw_present or n1099_present, "total_earnings": te_present}
    amount_source = next((f for f in preference if avail.get(f)), preference[0])
    out["amount"] = out[amount_source]

    if "pay_date" in cols:
        out["pay_date"] = raw[cols["pay_date"]].map(periods.to_ts)
    else:
        out["pay_date"] = pd.NaT

    out = out[out["employee_number"].notna()].reset_index(drop=True)

    res = out["pay_date"].map(lambda d: periods.pay_anchor_period(d, schedule))
    out["week_ending"] = [r[0] for r in res]
    out["period_end"] = [r[1] for r in res]
    out["acct_year"] = [r[2][0] for r in res]
    out["acct_month"] = [r[2][1] for r in res]

    out.attrs["has_dates"] = bool(out["pay_date"].notna().any())
    out.attrs["has_wage_split"] = has_wage_split
    out.attrs["compare_amount"] = compare_amount
    out.attrs["amount_source"] = amount_source
    out.attrs["explainer_cols"] = explainer_cols
    return out
