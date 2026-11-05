"""Unit tests for the reconciler.

The synthetic fixtures below mirror the shape and the known-good outcome of the
real February 2026 CTS cost audit: the same four material variances, corp-corp
handling, a stripped 'REPORT TOTAL' footer row, and the unmatched buckets.
"""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pytest

from reconciler import config, load_ats, load_paycheck, reconcile


# --- Fixtures ----------------------------------------------------------------

def _ats_rows() -> pd.DataFrame:
    """Weekly ATS rows. Amounts sum per candidate to the audited totals."""
    rows = [
        # (type, id, name, client, hours, wages)
        # Noel: billed 14,620 across two weeks
        ("W2 Hourly", 169132, "Noel Ranido", "Bechtel", 40, 7310),
        ("W2 Hourly", 169132, "Noel Ranido", "Bechtel", 40, 7310),
        # Ali: billed 15,660
        ("W2 Hourly", 181049, "Ali Esmaili", "Worley", 40, 7830),
        ("W2 Hourly", 181049, "Ali Esmaili", "Worley", 40, 7830),
        # Joseph: billed 18,975
        ("W2 Hourly", 189318, "Joseph Aucoin", "Bechtel", 40, 9487.5),
        ("W2 Hourly", 189318, "Joseph Aucoin", "Bechtel", 40, 9487.5),
        # Lee: billed 12,235.09
        ("W2 Hourly", 181702, "Lee Luetge", "Worley", 40, 6117.545),
        ("W2 Hourly", 181702, "Lee Luetge", "Worley", 40, 6117.545),
        # Matched-within-tolerance person (2 cent rounding)
        ("W2 Hourly", 182423, "Phillip Chapman", "Bechtel", 40, 12023.07),
        # Corp-Corp worker: billed but excluded from dollar comparison
        ("Corp-Corp", 999001, "Corp Worker", "Worley", 40, 8000),
        # Billed but NOT paid via payroll (no paycheck row)
        ("W2 Hourly", 555000, "Unpaid Person", "Bechtel", 40, 5000),
    ]
    df = pd.DataFrame(
        rows,
        columns=[
            "Employee Type",
            "Candidate ID",
            "Candidate Name",
            "Client Company Name",
            "Total Hours Paid",
            "Total Wages Paid",
        ],
    )
    return df


def _paycheck_rows() -> pd.DataFrame:
    """Payroll rows keyed by Employee Number (== Candidate ID)."""
    rows = [
        # (emp_no, name, pay_type, total_earnings)
        ("169132", "Ranido, Noel", "Hourly", 14492.5),   # diff -127.5
        ("181049", "Esmaili, Ali", "Hourly", 15555.0),   # diff -105.0
        ("189318", "Aucoin, Joseph", "Hourly", 19097.3), # diff +122.3
        ("181702", "Luetge, Lee", "Hourly", 12637.86),   # diff +402.77
        ("182423", "Chapman, Phillip", "Hourly", 12023.05),  # diff -0.02 -> matched
        ("777000", "Ghost, Paid Only", "Hourly", 3000.0),    # paid, not billed
        ("REPORT TOTAL", "", "", 999999.99),                  # footer -> dropped
    ]
    df = pd.DataFrame(
        rows,
        columns=[
            "Employee Number",
            "Employee Name",
            "Pay Type",
            "Total Earnings",
        ],
    )
    return df


@pytest.fixture()
def result():
    ats = load_ats(_to_xlsx(_ats_rows()))
    pay = load_paycheck(_to_xlsx(_paycheck_rows()))
    return reconcile(ats, pay, threshold=config.DEFAULT_THRESHOLD)


def _to_xlsx(df: pd.DataFrame) -> io.BytesIO:
    """Round-trip a frame through xlsx bytes so we exercise the real readers."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    buf.name = "fixture.xlsx"
    return buf


# --- Tests -------------------------------------------------------------------

def test_footer_row_is_stripped():
    pay = load_paycheck(_to_xlsx(_paycheck_rows()))
    assert "REPORT TOTAL" not in set(pay["employee_number"])
    assert (pay["employee_number"].str.fullmatch(r"\d+")).all()


def test_known_variances(result):
    var = result.detail[result.detail["category"] == config.CAT_VARIANCE]
    got = dict(zip(var["id"], var["difference"].round(2)))
    assert got == {
        "169132": -127.5,
        "181049": -105.0,
        "189318": 122.3,
        "181702": 402.77,
    }


def test_corp_corp_excluded_from_dollar_comparison(result):
    row = result.detail[result.detail["id"] == "999001"].iloc[0]
    assert row["category"] == config.CAT_CORP_CORP


def test_billed_not_paid_bucket(result):
    row = result.detail[result.detail["id"] == "555000"].iloc[0]
    assert row["category"] == config.CAT_BILLED_NOT_PAID
    assert row["pay_amount"] == 0.0


def test_paid_not_billed_bucket(result):
    row = result.detail[result.detail["id"] == "777000"].iloc[0]
    assert row["category"] == config.CAT_PAID_NOT_BILLED
    assert row["ats_amount"] == 0.0


def test_penny_difference_is_matched(result):
    row = result.detail[result.detail["id"] == "182423"].iloc[0]
    assert row["category"] == config.CAT_MATCHED


def test_summary_counts(result):
    s = result.summary
    assert s["counts"][config.CAT_VARIANCE] == 4
    assert s["counts"][config.CAT_CORP_CORP] == 1
    assert s["counts"][config.CAT_BILLED_NOT_PAID] == 1
    assert s["counts"][config.CAT_PAID_NOT_BILLED] == 1
    assert s["n_exceptions"] == 7  # 4 variance + 1 corp + 1 billed + 1 paid


def test_threshold_is_adjustable():
    ats = load_ats(_to_xlsx(_ats_rows()))
    pay = load_paycheck(_to_xlsx(_paycheck_rows()))
    # Raise threshold above the two smallest variances (105, 127.5) -> only 2 remain.
    res = reconcile(ats, pay, threshold=200.0)
    assert res.summary["counts"][config.CAT_VARIANCE] == 1  # only Lee (402.77)


def test_ats_accepts_ots_gross_wages_amount_column():
    """OTS Bullhorn exports name the wage column 'Gross Wages Amount'."""
    from reconciler import load_ats

    df = pd.DataFrame(
        [("W2 Hourly", 321, "OTS Person", "Client", 40, 1500)],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Gross Wages Amount"],
    )
    ats = load_ats(_to_xlsx(df))
    assert ats.iloc[0]["wages_paid"] == 1500.0


def test_explainer_columns_align_after_blank_id_filter():
    """Payroll-only pay types must stay on the right row after blank-ID rows are
    dropped (regression: they were sourced from the pre-filter frame and shifted)."""
    from reconciler import load_paycheck

    df = pd.DataFrame(
        [
            ("", "blank", "8/28/2026", 0.0, 0.0, 0.0),          # blank id -> dropped
            ("111", "Real", "8/28/2026", 4000.0, 4500.0, 500.0),  # PTO 500
        ],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages",
                 "Total Earnings", "Dollars PTO"])
    p = load_paycheck(_to_xlsx(df))
    row = p[p["employee_number"] == "111"].iloc[0]
    assert row["pt_PTO"] == 500.0
    # And the engine carries the per-candidate pay-type sum into detail.
    from reconciler import load_ats, reconcile
    ats = load_ats(_to_xlsx(pd.DataFrame(
        [("W2 Hourly", 111, "Real", "C", 40, 4000, dt.datetime(2026, 8, 23))],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Total Wages Paid",
                 "Billing Period End Date"])))
    d = reconcile(ats, p, period=(2026, 8)).detail
    assert d[d["id"] == "111"].iloc[0]["pt_PTO"] == 500.0


def test_accounting_negative_parsing():
    from reconciler.normalize import _to_amount

    assert _to_amount("(1,234.50)") == -1234.5
    assert _to_amount("$2,000") == 2000.0
    assert _to_amount("") == 0.0
    assert _to_amount(None) == 0.0


# --- accounting-month (Sunday-anchor) logic ---------------------------------

def test_sunday_anchor_rules():
    import datetime as dt

    from reconciler import periods as P

    # ATS billing-period-end snaps FORWARD to the week's Sunday.
    assert P.week_ending_sunday(dt.date(2026, 1, 30)) == pd.Timestamp("2026-02-01")  # Fri
    assert P.week_ending_sunday(dt.date(2026, 1, 31)) == pd.Timestamp("2026-02-01")  # Sat
    assert P.week_ending_sunday(dt.date(2026, 2, 1)) == pd.Timestamp("2026-02-01")   # Sun
    # Pay date (following Friday) snaps BACK 5 days to the same Sunday.
    assert P.paydate_to_sunday("7/10/2026") == pd.Timestamp("2026-07-05")
    assert P.paydate_to_sunday("8/7/2026") == pd.Timestamp("2026-08-02")
    # NaT-safe.
    assert pd.isna(P.week_ending_sunday(None))
    assert pd.isna(P.paydate_to_sunday(pd.NaT))


def _ats_with_dates() -> pd.DataFrame:
    rows = [
        # July week (Sun 7/5) -> counts for July
        ("W2 Hourly", 111, "In July", "C", 40, 4000, dt.datetime(2026, 7, 3)),   # Fri
        ("W2 Hourly", 111, "In July", "C", 40, 4000, dt.datetime(2026, 7, 5)),   # Sun
        # August week (Sun 8/2) -> excluded from July
        ("W2 Hourly", 111, "In July", "C", 40, 9999, dt.datetime(2026, 8, 1)),   # Sat->8/2
        # June week (Sun 6/28) -> excluded from July
        ("W2 Hourly", 222, "June Only", "C", 40, 5000, dt.datetime(2026, 6, 28)),
    ]
    return pd.DataFrame(rows, columns=[
        "Employee Type", "Candidate ID", "Candidate Name", "Client Company Name",
        "Total Hours Paid", "Total Wages Paid", "Billing Period End Date",
    ])


def test_period_filtering_excludes_other_months():
    from reconciler import load_ats, reconcile

    ats = load_ats(_to_xlsx(_ats_with_dates()))
    # Payroll: register-style not needed here; reuse summary loader via a frame
    # with an Employee Number so it has no dates (worst case: unfiltered).
    pay = load_paycheck(_to_xlsx(pd.DataFrame(
        [("111", "In July", "Hourly", 8000.0)],
        columns=["Employee Number", "Employee Name", "Pay Type", "Total Earnings"],
    )))
    res = reconcile(ats, pay, period=(2026, 7))
    # Only the two July rows for id 111 count -> 8000 billed; id 222 (June) dropped.
    row = res.detail[res.detail["id"] == "111"].iloc[0]
    assert row["ats_amount"] == 8000.0           # 4000 + 4000, NOT the 9999 Aug row
    assert "222" not in set(res.detail["id"])     # June-only person excluded
    assert res.summary["n_ats_rows_excluded"] == 2
    # Payroll had no dates -> warning present, not filtered.
    assert any("no pay dates" in w for w in res.warnings)
    assert res.summary["paycheck_filtered"] is False


def test_per_diem_shown_separately_and_ots_default_basis():
    from reconciler import config, load_ats, load_paycheck, reconcile

    ats = load_ats(_to_xlsx(_ats_with_dates()))  # id 111 -> July wages 8000
    # Payroll: gross wages 8000 (matches), plus 500 per diem in Total Earnings.
    pay = pd.DataFrame(
        [
            ("111", "In July", "7/10/2026", 4000.0, 4000.0),
            ("111", "In July", "7/17/2026", 4000.0, 4500.0),  # +500 per diem
        ],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"],
    )
    # Gross Wages basis: wages match (diff 0), per diem surfaced separately.
    p = load_paycheck(_to_xlsx(pay), compare_amount="Gross Wages")
    assert p.attrs["has_wage_split"] is True
    res = reconcile(ats, p, period=(2026, 7))
    row = res.detail[res.detail["id"] == "111"].iloc[0]
    assert row["difference"] == 0.0            # wage-to-wage matches
    assert row["pay_nonwage"] == 500.0          # per diem shown, not a variance
    assert row["category"] == config.CAT_MATCHED
    assert res.summary["pay_nonwage_total"] == 500.0

    # OTS defaults to Gross Wages; CTS to Total Earnings.
    assert config.default_compare_for("OTS") == "Gross Wages"
    assert config.default_compare_for("CTS") == "Total Earnings"


def _paycheck_dated() -> pd.DataFrame:
    """New detailed payroll format: one row per check, dated, TE and GW differ."""
    return pd.DataFrame(
        [
            # id, name, pay date, gross wages, total earnings (TE = GW + per diem)
            ("111", "In July", "7/10/2026", 4000.0, 4000.0),   # Sun 7/5 -> July
            ("111", "In July", "7/17/2026", 4000.0, 4500.0),   # Sun 7/12 -> July, +500 perdiem
            ("111", "In July", "8/7/2026", 9999.0, 9999.0),    # Sun 8/2 -> Aug (excluded)
        ],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"],
    )


def test_dated_payroll_is_week_filtered_and_compare_amount_switches():
    from reconciler import load_ats, load_paycheck, reconcile

    ats = load_ats(_to_xlsx(_ats_with_dates()))

    # Total Earnings mode: July = 4000 + 4500 = 8500 (Aug row excluded).
    pay_te = load_paycheck(_to_xlsx(_paycheck_dated()), compare_amount="Total Earnings")
    assert pay_te.attrs["has_dates"] is True
    assert pay_te.attrs["amount_source"] == "total_earnings"
    res_te = reconcile(ats, pay_te, period=(2026, 7))
    assert res_te.summary["paycheck_filtered"] is True
    assert res_te.detail[res_te.detail["id"] == "111"].iloc[0]["pay_amount"] == 8500.0

    # Gross Wages mode: July = 4000 + 4000 = 8000 (per diem excluded).
    # amount_source is "wages" (= gross wages + 1099; no 1099 here, so == gross).
    pay_gw = load_paycheck(_to_xlsx(_paycheck_dated()), compare_amount="Gross Wages")
    assert pay_gw.attrs["amount_source"] == "wages"
    res_gw = reconcile(ats, pay_gw, period=(2026, 7))
    assert res_gw.detail[res_gw.detail["id"] == "111"].iloc[0]["pay_amount"] == 8000.0


# --- OTS bi-weekly (two-week delay, last-Friday anchor) ---------------------

def test_perdiem_bundled_in_bullhorn_reconciles():
    """If Bullhorn's payable bundles per diem, matching payroll's Total Earnings
    (not just Gross Wages) should reconcile — the gap is per diem, not a variance."""
    from reconciler import config, load_ats, load_paycheck, reconcile

    # Bullhorn bills 12,404 = wages 10,864 + per diem 1,540, as one figure.
    ats = load_ats(_to_xlsx(pd.DataFrame(
        [("W2 Hourly", 144241, "Guerrero", "C", 40, 12404, dt.datetime(2026, 8, 16))],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Total Wages Paid",
                 "Billing Period End Date"])))
    pay = load_paycheck(_to_xlsx(pd.DataFrame(
        [("144241", "Guerrero", "8/28/2026", 10864.0, 12404.0)],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"])),
        compare_amount="Gross Wages")
    row = reconcile(ats, pay, period=(2026, 8)).detail
    row = row[row["id"] == "144241"].iloc[0]
    assert row["pay_nonwage"] == 1540.0
    assert abs(row["difference"]) < 0.01          # reconciled via Total Earnings
    assert row["category"] == config.CAT_MATCHED

    # A gap NOT explained by per diem is still a variance.
    pay2 = load_paycheck(_to_xlsx(pd.DataFrame(
        [("144241", "Guerrero", "8/28/2026", 9000.0, 9500.0)],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"])),
        compare_amount="Gross Wages")
    r2 = reconcile(ats, pay2, period=(2026, 8)).detail
    r2 = r2[r2["id"] == "144241"].iloc[0]
    assert r2["category"] == config.CAT_VARIANCE   # 12404 vs 9000/9500: neither matches


def test_1099_counts_as_wages():
    """A worker paid partly as 1099 (contractor labor) reconciles when Bullhorn's
    payable equals Gross Wages + 1099 — the 1099 is labor, not per diem."""
    from reconciler import config, load_ats, load_paycheck, reconcile

    # Bullhorn bills 21,840 = OT wages 9,360 + 1099 12,480; per diem 5,775 extra.
    ats = load_ats(_to_xlsx(pd.DataFrame(
        [("W2 Hourly", 185245, "Landry", "C", 40, 21840, dt.datetime(2026, 8, 16))],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Total Wages Paid",
                 "Billing Period End Date"])))
    pay = load_paycheck(_to_xlsx(pd.DataFrame(
        [("185245", "Landry", "8/28/2026", 9360.0, 12480.0, 27615.0)],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages",
                 "Dollars 1099", "Total Earnings"])),
        compare_amount="Gross Wages")
    row = reconcile(ats, pay, period=(2026, 8)).detail
    row = row[row["id"] == "185245"].iloc[0]
    assert row["pay_amount"] == 21840.0            # wages = gross + 1099
    assert abs(row["difference"]) < 0.01           # reconciles
    assert row["category"] == config.CAT_MATCHED
    assert row["pay_nonwage"] == 5775.0            # per diem only (1099 excluded)


def test_corp_corp_reconciled_via_c2c():
    from reconciler import config, load_ats, load_paycheck, reconcile

    ats_df = pd.DataFrame(
        [
            ("Corp-Corp", 999, "Corp Guy", "Client", 40, 3500, dt.datetime(2026, 7, 5)),
            ("Corp-Corp", 999, "Corp Guy", "Client", 40, 3500, dt.datetime(2026, 7, 12)),
        ],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Total Wages Paid",
                 "Billing Period End Date"],
    )
    ats = load_ats(_to_xlsx(ats_df))  # weekly; billed 7000 in July
    pay = load_paycheck(_to_xlsx(pd.DataFrame(
        [("111", "W2 Person", "7/10/2026", 1000.0, 1000.0)],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"],
    )))

    # No C2C -> Corp-Corp excluded (as before).
    r0 = reconcile(ats, pay, period=(2026, 7))
    assert r0.detail[r0.detail["id"] == "999"].iloc[0]["category"] == config.CAT_CORP_CORP

    # C2C matches the billed amount -> reconciled as Matched.
    r1 = reconcile(ats, pay, period=(2026, 7), c2c_paid={"999": 7000.0})
    row = r1.detail[r1.detail["id"] == "999"].iloc[0]
    assert row["pay_amount"] == 7000.0 and row["category"] == config.CAT_MATCHED

    # C2C differs -> Variance (Bullhorn vs QBO).
    r2 = reconcile(ats, pay, period=(2026, 7), c2c_paid={"999": 7500.0})
    assert r2.detail[r2.detail["id"] == "999"].iloc[0]["category"] == config.CAT_VARIANCE


def test_ots_pay_date_maps_by_last_friday():
    from reconciler import periods as P

    b = P.SCHEDULE_BIWEEKLY
    # Phil's example: pay date 8/14 pays the period ending Sun 8/2 -> July.
    assert P.pay_anchor_period("8/14/2026", b)[2] == (2026, 7)
    # July's three periods paid 7/17, 7/31, 8/14 -> all July.
    assert P.pay_anchor_period("7/17/2026", b)[2] == (2026, 7)
    assert P.pay_anchor_period("7/31/2026", b)[2] == (2026, 7)
    # August's two periods paid 8/28, 9/11 -> August.
    assert P.pay_anchor_period("8/28/2026", b)[2] == (2026, 8)
    assert P.pay_anchor_period("9/11/2026", b)[2] == (2026, 8)


def test_ots_ats_biweekly_week_grouping():
    from reconciler import periods as P

    b = P.SCHEDULE_BIWEEKLY
    # July = 3 periods = weeks ending Jun 28, Jul 5, Jul 12, Jul 19, Jul 26, Aug 2.
    july_week_ends = ["2026-06-28", "2026-07-05", "2026-07-12",
                      "2026-07-19", "2026-07-26", "2026-08-02"]
    for d in july_week_ends:
        assert P.ats_anchor_period(pd.Timestamp(d), b)[2] == (2026, 7), d
    # The period ending Sun 8/2 is July (last Friday 7/31); Aug 9 week is August.
    assert P.ats_anchor_period(pd.Timestamp("2026-08-09"), b)[2] == (2026, 8)
    # Week ending Jun 21 (period Jun 8-21, last Fri Jun 19) is June.
    assert P.ats_anchor_period(pd.Timestamp("2026-06-21"), b)[2] == (2026, 6)
    # Display week stays the actual weekly Sunday; period-end rolls up bi-weekly.
    disp, pend, _ = P.ats_anchor_period(pd.Timestamp("2026-06-28"), b)
    assert disp == pd.Timestamp("2026-06-28")     # weekly billing week is visible
    assert pend == pd.Timestamp("2026-07-05")     # rolls up to the 7/5 period


def test_ots_full_reconcile_filters_to_month():
    from reconciler import load_ats, load_paycheck, periods, reconcile

    # One worker billed across the whole July period span + one August week.
    ats = pd.DataFrame(
        [
            ("W2 Hourly", 500, "OTS Worker", "Client", 40, 2000, dt.datetime(2026, 7, 26)),
            ("W2 Hourly", 500, "OTS Worker", "Client", 40, 2000, dt.datetime(2026, 8, 2)),
            ("W2 Hourly", 500, "OTS Worker", "Client", 40, 9999, dt.datetime(2026, 8, 9)),  # Aug
        ],
        columns=["Employee Type", "Candidate ID", "Candidate Name",
                 "Client Company Name", "Total Hours Paid", "Total Wages Paid",
                 "Billing Period End Date"],
    )
    # Paid for the period ending 8/2 on 8/14 (July), plus an August check on 8/28.
    pay = pd.DataFrame(
        [
            ("500", "OTS Worker", "8/14/2026", 4000.0, 4000.0),   # -> July
            ("500", "OTS Worker", "8/28/2026", 9999.0, 9999.0),   # -> August (excluded)
        ],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"],
    )
    a = load_ats(_to_xlsx(ats), schedule=periods.SCHEDULE_BIWEEKLY)
    p = load_paycheck(_to_xlsx(pay), schedule=periods.SCHEDULE_BIWEEKLY)
    res = reconcile(a, p, period=(2026, 7))
    row = res.detail[res.detail["id"] == "500"].iloc[0]
    assert row["ats_amount"] == 4000.0    # the two July weeks (2000+2000); Aug 9 dropped
    assert row["pay_amount"] == 4000.0    # the 8/14 check; 8/28 dropped
    assert row["category"] == config.CAT_MATCHED
    assert res.summary["paycheck_filtered"] is True
