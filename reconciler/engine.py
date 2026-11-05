"""The reconciliation engine.

Optionally filter both sides to a CTS accounting month (Sunday-anchored weeks),
aggregate each side by person, join on the shared ID (Bullhorn Candidate ID ==
payroll Employee Number), and classify every person into a category. Pure and
deterministic so it can be unit-tested against known-good historical results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import config

# Opt into pandas' future no-silent-downcasting behavior so filling NaNs in the
# boolean merge-indicator columns doesn't emit a FutureWarning (we cast to bool
# explicitly right after).
pd.set_option("future.no_silent_downcasting", True)

DETAIL_COLUMNS = [
    "id",
    "name",
    "ats_amount",
    "pay_amount",
    "difference",
    "abs_difference",
    "pay_nonwage",   # payroll per diem / expense (Total Earnings - Gross Wages)
    "category",
    "employee_type",
    "client",
]


@dataclass
class ReconResult:
    detail: pd.DataFrame
    summary: dict[str, Any] = field(default_factory=dict)
    threshold: float = config.DEFAULT_THRESHOLD
    warnings: list[str] = field(default_factory=list)

    @property
    def exceptions(self) -> pd.DataFrame:
        return self.detail[self.detail["category"] != config.CAT_MATCHED].copy()


def _first_valid(series: pd.Series) -> Any:
    for v in series:
        if pd.notna(v) and str(v).strip() != "":
            return v
    return pd.NA


def _aggregate_ats(ats: pd.DataFrame) -> pd.DataFrame:
    if ats.empty:
        return pd.DataFrame(
            columns=["id", "name", "ats_amount", "is_corp_corp", "employee_type", "client"]
        )
    agg = ats.groupby("candidate_id", sort=False).agg(
        name=("name", _first_valid),
        ats_amount=("wages_paid", "sum"),
        is_corp_corp=("is_corp_corp", "any"),
        employee_type=("employee_type", _first_valid),
        client=("client", _first_valid),
    ).reset_index()
    return agg.rename(columns={"candidate_id": "id"})


def _aggregate_paycheck(pay: pd.DataFrame) -> pd.DataFrame:
    if pay.empty:
        return pd.DataFrame(
            columns=["id", "pay_name", "pay_amount", "pay_te", "pay_wages"]
        )
    spec = {
        "pay_name": ("name", _first_valid),
        "pay_amount": ("amount", "sum"),
        "pay_te": ("total_earnings", "sum"),
        "pay_wages": ("wages", "sum"),   # labor = gross wages + 1099
    }
    # Sum each payroll-only pay type (PTO, sick, …) with the same filtering as the
    # wages, so a variance can be matched against them consistently.
    for c in [c for c in pay.columns if c.startswith("pt_")]:
        spec[c] = (c, "sum")
    agg = pay.groupby("employee_number", sort=False).agg(**spec).reset_index()
    return agg.rename(columns={"employee_number": "id"})


def _classify(row: pd.Series, threshold: float) -> str:
    if bool(row.get("is_corp_corp")):
        # Corp-Corp is normally excluded — but if we have their QuickBooks C2C
        # cost, reconcile them like anyone else (Bullhorn payable vs QBO paid).
        if not bool(row.get("_has_c2c")):
            return config.CAT_CORP_CORP
    in_ats = bool(row.get("_in_ats"))
    in_pay = bool(row.get("_in_pay"))
    if in_ats and not in_pay:
        return config.CAT_BILLED_NOT_PAID
    if in_pay and not in_ats:
        return config.CAT_PAID_NOT_BILLED
    if abs(row.get("difference", 0.0)) >= threshold:
        return config.CAT_VARIANCE
    return config.CAT_MATCHED


def _filter_period(df: pd.DataFrame, period: tuple[int, int]) -> pd.DataFrame:
    y, m = period
    mask = (df["acct_year"] == y) & (df["acct_month"] == m)
    return df[mask.fillna(False)]


def _weeks_breakdown(df: pd.DataFrame, period: tuple[int, int] | None) -> list[dict]:
    """Per-period (anchor Sunday) row counts, marked included/excluded.

    'Included' is decided by each row's *accounting* month (acct_year/acct_month),
    not the anchor date's own month — these differ for OTS, where the period
    ending e.g. Aug 2 belongs to July (its last Friday is Jul 31).
    """
    if "week_ending" not in df.columns or df["week_ending"].isna().all():
        return []
    grp = df.dropna(subset=["week_ending"]).groupby("week_ending", sort=True)
    rows = []
    for sunday, g in grp:
        ay = g["acct_year"].iloc[0]
        am = g["acct_month"].iloc[0]
        if period is None:
            included = True
        elif pd.isna(ay) or pd.isna(am):
            included = False
        else:
            included = (int(ay), int(am)) == period
        rows.append({
            "week_ending": sunday.strftime("%Y-%m-%d"),
            "rows": int(len(g)),
            "included": included,
        })
    return rows


def _period_end_sets(
    df: pd.DataFrame, period: tuple[int, int], min_rows: int
) -> tuple[set, set]:
    """Return (all, material) pay-period-end anchors of ``df`` within ``period``.

    ``material`` keeps only anchors with at least ``min_rows`` rows, so tiny
    off-cycle adjustment checks don't drive the missing-run warning.
    """
    if "period_end" not in df.columns or df["period_end"].isna().all():
        return set(), set()
    sub = df.dropna(subset=["period_end"])
    all_set: set[str] = set()
    material: set[str] = set()
    for pend, g in sub.groupby("period_end"):
        ay = g["acct_year"].iloc[0]
        am = g["acct_month"].iloc[0]
        if pd.isna(ay) or pd.isna(am) or (int(ay), int(am)) != period:
            continue
        key = pend.strftime("%Y-%m-%d")
        all_set.add(key)
        if len(g) >= min_rows:
            material.add(key)
    return all_set, material


def reconcile(
    ats: pd.DataFrame,
    paycheck: pd.DataFrame,
    threshold: float = config.DEFAULT_THRESHOLD,
    period: tuple[int, int] | None = None,
    c2c_paid: dict[str, float] | None = None,
    c2c_names: dict[str, str] | None = None,
) -> ReconResult:
    """Reconcile normalized ATS and Paycheck frames.

    If ``period`` (year, month) is given, both sides are filtered to weeks whose
    Sunday falls in that month before aggregating. If the payroll export has no
    dates it cannot be filtered; the full export is used and a warning is added.

    ``c2c_paid`` maps candidate id -> QuickBooks Corp-Corp cost for the period.
    When provided, those Corp-Corp workers are reconciled (Bullhorn payable vs
    QBO paid) instead of excluded.
    """
    warnings: list[str] = []
    c2c_paid = {str(k): float(v) for k, v in (c2c_paid or {}).items()}

    ats_weeks = _weeks_breakdown(ats, period)
    pay_weeks = _weeks_breakdown(paycheck, period)

    ats_used = ats
    pay_used = paycheck
    pay_has_dates = bool(paycheck.attrs.get("has_dates", False))
    n_ats_excluded = n_pay_excluded = 0
    ats_excluded_amount = pay_excluded_amount = 0.0

    if period is not None:
        ats_used = _filter_period(ats, period)
        n_ats_excluded = len(ats) - len(ats_used)
        ats_excluded_amount = float(ats["wages_paid"].sum() - ats_used["wages_paid"].sum())
        if n_ats_excluded and ats["week_ending"].isna().any():
            warnings.append(
                "Some ATS rows have no billing-period-end date and were excluded "
                "from the accounting month."
            )
        if pay_has_dates:
            pay_used = _filter_period(paycheck, period)
            n_pay_excluded = len(paycheck) - len(pay_used)
            pay_excluded_amount = float(
                paycheck["amount"].sum() - pay_used["amount"].sum()
            )
        else:
            warnings.append(
                "Payroll export has no pay dates, so it could NOT be filtered to "
                "the accounting month. Comparing the entire payroll export as-is - "
                "upload the register 'Check Details' sheet to enable week filtering."
            )

    # Warn when a period is present on one side but not the other — the usual
    # cause is an audit run before every pay run exists (e.g. the last period's
    # payroll hasn't happened yet), which otherwise looks like mass underpayment.
    if period is not None and pay_has_dates:
        # Compare the two sides on their PAY-PERIOD-END anchors (bi-weekly billing
        # weeks roll up to the period payroll actually pays). A "material" period
        # has more than a couple of rows, so 1-2 row off-cycle adjustment checks
        # don't raise spurious mismatch warnings.
        MIN_MATERIAL_ROWS = 5
        ats_all, ats_material = _period_end_sets(ats, period, MIN_MATERIAL_ROWS)
        pay_all, pay_material = _period_end_sets(paycheck, period, MIN_MATERIAL_ROWS)
        bh_only = sorted(ats_material - pay_all)
        pay_only = sorted(pay_material - ats_all)
        if bh_only:
            warnings.append(
                "Bullhorn has hours for period(s) ending "
                f"{', '.join(bh_only)} with no matching payroll run in this file "
                "(likely not paid yet). Those people will look under-paid until "
                "that payroll is added."
            )
        if pay_only:
            warnings.append(
                "Payroll has run(s) for period(s) ending "
                f"{', '.join(pay_only)} with no matching Bullhorn hours in this file."
            )

    # Surface off-cycle adjustment checks (register only) for transparency.
    n_adjustments = 0
    if "check_type" in pay_used.columns:
        adj = {t.lower() for t in config.PAYCHECK_ADJUSTMENT_TYPES}
        n_adjustments = int(
            pay_used["check_type"].astype("string").str.strip().str.lower().isin(adj).sum()
        )

    a = _aggregate_ats(ats_used).assign(_in_ats=True)
    p = _aggregate_paycheck(pay_used).assign(_in_pay=True)

    merged = pd.merge(a, p, on="id", how="outer")
    merged["_in_ats"] = merged["_in_ats"].fillna(False).astype(bool)
    merged["_in_pay"] = merged["_in_pay"].fillna(False).astype(bool)
    merged["ats_amount"] = merged["ats_amount"].fillna(0.0)
    merged["pay_amount"] = merged["pay_amount"].fillna(0.0)
    merged["is_corp_corp"] = merged["is_corp_corp"].fillna(False).astype(bool)

    merged["name"] = merged["name"].where(
        merged["name"].notna() & (merged["name"].astype("string").str.strip() != ""),
        merged["pay_name"],
    )

    # Corp-Corp cost from QuickBooks (C2C): add it to the paid side and add any
    # QBO-only people (paid via QBO, not billed in Bullhorn) as Corp-Corp rows.
    if c2c_paid:
        present = set(merged["id"])
        extra_ids = [i for i in c2c_paid if i not in present]
        if extra_ids:
            extra = pd.DataFrame({
                "id": extra_ids,
                "ats_amount": 0.0, "pay_amount": 0.0, "is_corp_corp": True,
                "_in_ats": False, "_in_pay": True,
            })
            merged = pd.concat([merged, extra], ignore_index=True)
        merged["c2c_amount"] = merged["id"].map(lambda i: c2c_paid.get(i, 0.0))
    else:
        merged["c2c_amount"] = 0.0
    merged["is_corp_corp"] = merged["is_corp_corp"].fillna(False).astype(bool)
    merged["_in_ats"] = merged["_in_ats"].fillna(False).astype(bool)
    merged["_in_pay"] = merged["_in_pay"].fillna(False).astype(bool)
    merged["ats_amount"] = merged["ats_amount"].fillna(0.0)
    merged["pay_amount"] = merged["pay_amount"].fillna(0.0) + merged["c2c_amount"]
    merged["_has_c2c"] = merged["c2c_amount"] > 0
    # C2C cost counts as "paid" so these aren't miscategorized as billed-not-paid.
    merged["_in_pay"] = merged["_in_pay"] | merged["_has_c2c"]

    # Fill Corp-Corp names from the C2C file (they aren't in payroll to borrow one).
    if c2c_names:
        blank = merged["name"].isna() | (merged["name"].astype("string").str.strip() == "")
        merged.loc[blank, "name"] = merged.loc[blank, "id"].map(
            lambda i: c2c_names.get(str(i))
        )

    has_split = bool(paycheck.attrs.get("has_wage_split"))
    amount_source = paycheck.attrs.get("amount_source")
    for c in ("pay_te", "pay_wages"):
        merged[c] = merged[c].fillna(0.0) if c in merged.columns else 0.0
    # Per diem / expense = everything paid beyond labor (wages + 1099).
    merged["pay_nonwage"] = (merged["pay_te"] - merged["pay_wages"]) if has_split else 0.0

    # Bullhorn's payable sometimes bundles per diem and sometimes doesn't, so a
    # payroll person reconciles if their Bullhorn amount equals EITHER labor wages
    # (gross + 1099) OR total earnings. The reported "difference" is the smaller
    # (unexplained) residual, so a gap that is purely per diem is not a variance.
    diff_primary = merged["pay_amount"] - merged["ats_amount"]
    merged["difference"] = diff_primary
    if has_split:
        alt = merged["pay_te"] if amount_source == "wages" else merged["pay_wages"]
        diff_alt = alt - merged["ats_amount"]
        has_payroll = (merged["pay_wages"].abs() + merged["pay_te"].abs()) > 0
        eligible = has_payroll & (~merged["is_corp_corp"]) & (~merged["_has_c2c"])
        use_alt = eligible & (diff_alt.abs() < diff_primary.abs())
        merged.loc[use_alt, "difference"] = diff_alt[use_alt]
    merged["abs_difference"] = merged["difference"].abs()

    # A broad multi-company register can carry $0 checks and inactive people; a
    # person with no billing AND no pay of any kind (wages, total, per diem) has
    # nothing to reconcile, so drop them rather than surface an empty exception.
    no_activity = (
        (merged["ats_amount"].abs() < 0.005)
        & (merged["pay_amount"].abs() < 0.005)
        & (merged["pay_te"].abs() < 0.005)
        & (merged["pay_wages"].abs() < 0.005)
    )
    merged = merged[~no_activity].reset_index(drop=True)

    merged["category"] = merged.apply(lambda r: _classify(r, threshold), axis=1)

    for col in ("employee_type", "client"):
        if col not in merged.columns:
            merged[col] = pd.NA

    pt_cols = [c for c in merged.columns if c.startswith("pt_")]
    for c in pt_cols:
        merged[c] = merged[c].fillna(0.0)
    detail = merged[DETAIL_COLUMNS + pt_cols].copy()
    detail = detail.sort_values(
        by=["category", "abs_difference"],
        key=lambda s: s.map(_category_rank) if s.name == "category" else s,
        ascending=[True, False],
    ).reset_index(drop=True)

    # Guardrail: a material set of people billed in Bullhorn with no payroll
    # record usually means the payroll export is missing a pay group / batch
    # (not that staff went unpaid). Surface it loudly.
    bnp = detail[
        (detail["category"] == config.CAT_BILLED_NOT_PAID) & (detail["ats_amount"] >= 1.0)
    ]
    total_billed = float(detail["ats_amount"].sum())
    if len(bnp) >= 5 or bnp["ats_amount"].sum() > 0.02 * max(total_billed, 1.0):
        warnings.append(
            f"{len(bnp)} people (${bnp['ats_amount'].sum():,.0f}) are billed in "
            "Bullhorn with no payroll record. The payroll export may be missing a "
            "pay group / batch - or these are genuine non-payments to investigate."
        )

    summary = _summarize(detail)
    summary.update({
        "period": {"year": period[0], "month": period[1]} if period else None,
        "ats_weeks": ats_weeks,
        "pay_weeks": pay_weeks,
        "paycheck_filtered": bool(period is not None and pay_has_dates),
        "n_ats_rows_excluded": int(n_ats_excluded),
        "n_pay_rows_excluded": int(n_pay_excluded),
        "ats_excluded_amount": ats_excluded_amount,
        "pay_excluded_amount": pay_excluded_amount,
        "n_adjustment_checks": n_adjustments,
        "compare_amount": paycheck.attrs.get("compare_amount"),
        "amount_source": paycheck.attrs.get("amount_source"),
    })

    return ReconResult(detail=detail, summary=summary, threshold=threshold, warnings=warnings)


def _category_rank(category: str) -> int:
    try:
        return config.CATEGORY_ORDER.index(category)
    except ValueError:
        return len(config.CATEGORY_ORDER)


def _summarize(detail: pd.DataFrame) -> dict[str, Any]:
    counts = detail["category"].value_counts().to_dict()
    variances = detail[detail["category"] == config.CAT_VARIANCE]
    comparable = detail[
        detail["category"].isin([config.CAT_MATCHED, config.CAT_VARIANCE])
    ]
    return {
        "n_people": int(len(detail)),
        "counts": {cat: int(counts.get(cat, 0)) for cat in config.CATEGORY_ORDER},
        "total_billed": float(detail["ats_amount"].sum()),
        "total_paid": float(detail["pay_amount"].sum()),
        "comparable_billed": float(comparable["ats_amount"].sum()),
        "comparable_paid": float(comparable["pay_amount"].sum()),
        # Net of the *unexplained* residuals (per-diem-only gaps net to ~0).
        "net_variance": float(comparable["difference"].sum()),
        "n_exceptions": int((detail["category"] != config.CAT_MATCHED).sum()),
        "variance_abs_total": float(variances["abs_difference"].sum()),
        "largest_variance": float(variances["abs_difference"].max())
        if not variances.empty
        else 0.0,
        "pay_nonwage_total": float(detail["pay_nonwage"].sum())
        if "pay_nonwage" in detail
        else 0.0,
    }
