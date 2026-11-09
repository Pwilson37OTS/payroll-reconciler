"""Tests for the automated data-source connectors (no live services needed)."""

from __future__ import annotations

import os

import pandas as pd

import openpyxl

from reconciler import periods, reconcile
from sources import qbo_c2c
from sources import sharepoint_payroll as sp
from sources import snowflake_bullhorn as sf


def test_qbo_c2c_parser(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["CTS - Complete Technical Services"])
    ws.append(["Transaction Report"])
    ws.append(["August 2026"])
    ws.append([])
    ws.append([None, "Transaction date", "Transaction type", "Num", "Name",
               "Description", "Account Name", "Item split account", "Amount", "Balance"])
    ws.append(["Contractor Fees"])
    ws.append([None, "08/02/2026", "Bill", "25175", "SIS LLC",
               "2684, Somlith Siharath, 08/02/2026, 50.0, 70.0", "Contractor Fees",
               "A/P", 3500.0, 3500.0])
    ws.append([None, "08/09/2026", "Bill", "25605", "SIS LLC",
               "2684, Somlith Siharath, 08/09/2026, 50.0, 70.0", "Contractor Fees",
               "A/P", 3500.0, 7000.0])
    ws.append([None, "08/16/2026", "Bill Payment", "P1", "SIS LLC",
               "2684, Somlith Siharath, 08/16/2026, 50.0, 70.0", "A/P",
               "", 3500.0, 0.0])   # payment, not a Bill -> excluded
    f = tmp_path / "CTS - C2C.xlsx"
    wb.save(f)

    records, period = qbo_c2c.parse_c2c(str(f))
    assert len(records) == 2                 # two bills; payment excluded
    assert all(r["placement_id"] == "2684" for r in records)
    assert all(r["candidate_id"] is None for r in records)   # QBO has no cand id
    assert sum(r["amount"] for r in records) == 7000.0
    assert records[0]["name"] == "Somlith Siharath"
    assert period == "August 2026"
    assert qbo_c2c.find_c2c_file(str(tmp_path), "CTS") == str(f)
    assert qbo_c2c.find_c2c_file(str(tmp_path), "OTS") is None


def test_qbo_c2c_classifies_perdiem_by_account(tmp_path):
    # The updated CTS export adds non-"Contractor Fees" bill lines (per diem /
    # expense) via the Distribution account column. All lines still count as paid
    # (Bullhorn bills these contractors all-in), but each is tagged wage/nonwage.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["CTS - Complete Technical Services"])
    ws.append(["Purchases by Vendor Detail"])
    ws.append(["September 2026"])
    ws.append([])
    ws.append([None, "Transaction date", "Transaction type", "Num", "Description",
               "Quantity", "Rate", "Amount", "Balance", "Product/Service",
               "Distribution account"])
    ws.append(["3rd Coast Consulting, LLC"])
    ws.append([None, "09/06/2026", "Bill", 100, "2716, Russ R, 09/04/2026, 60.0, 75.0",
               None, None, 4500.0, 4500.0, None, "Contractor Fees-Staffing"])
    ws.append([None, "09/20/2026", "Bill", 101, "2699, Sri D, 09/20/2026, 1.0, 304.5",
               None, None, 304.5, 304.5, None, "Employee Advance"])
    f = tmp_path / "C2C - CTS.xlsx"
    wb.save(f)

    records, _ = qbo_c2c.parse_c2c(str(f))
    by_plc = {r["placement_id"]: r for r in records}
    assert by_plc["2716"]["kind"] == "wage"
    assert by_plc["2699"]["kind"] == "nonwage"           # Employee Advance = per diem/exp
    assert by_plc["2699"]["account"] == "Employee Advance"

    _, prov = qbo_c2c.fetch_c2c("CTS", str(tmp_path))
    assert prov["wage_total"] == 4500.0
    assert prov["nonwage_total"] == 304.5
    assert prov["total"] == 4804.5                        # all lines count as paid


def test_qbo_c2c_malformed_workdate_falls_back_to_transaction_date(tmp_path):
    # A malformed Description work date ("12/28//2025") must not leave an unusable
    # NaT period_end (which crashes the month filter) — fall back to the bill's
    # Transaction date so the line is still dated.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["CTS - Complete Technical Services"])
    ws.append(["Purchases by Vendor Detail"])
    ws.append(["September 2026"])
    ws.append([])
    ws.append([None, "Transaction date", "Transaction type", "Num", "Description",
               "Quantity", "Rate", "Amount", "Balance", "Product/Service",
               "Distribution account"])
    ws.append(["Vendor"])
    ws.append([None, "01/04/2026", "Bill", 1,
               "2684, Somlith Siharath, 12/28//2025, -1, -84.00",
               None, None, -84.0, -84.0, None, "Employee Advance"])
    f = tmp_path / "C2C - CTS.xlsx"
    wb.save(f)

    records, _ = qbo_c2c.parse_c2c(str(f))
    assert len(records) == 1
    pe = records[0]["period_end"]
    assert pe is not None and not pd.isna(pe)          # not an unusable NaT
    assert pe.year == 2026 and pe.month == 1           # fell back to Transaction date
    assert records[0]["kind"] == "nonwage"             # Employee Advance = per diem/exp


def test_bullhorn_c2c_parser(tmp_path):
    # The OTS-style export: explicit Candidate ID / Placement ID / Payroll Amount.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Placement Source", "Payable Charge ID", "Company Name",
               "Corp-Corp Company", "Name", "Candidate ID", "Placement ID",
               "C2C Description", "Period End Date", "Status", "Payroll Amount"])
    ws.append(["Gimmie", 26726, "ConocoPhillips", "Above Below", "David Moeller",
               136202, 2289, "2289, David Moeller, ...", "2026-08-30", "Exported", 6750.0])
    ws.append(["Gimmie", 26367, "ConocoPhillips", "Above Below", "David Moeller",
               136202, 2289, "2289, David Moeller, ...", "2026-08-23", "Exported", 5400.0])
    f = tmp_path / "OTS - C2C.xlsx"
    wb.save(f)

    records, period = qbo_c2c.parse_c2c(str(f))
    assert len(records) == 2
    assert all(r["candidate_id"] == "136202" for r in records)   # direct id
    assert records[0]["name"] == "David Moeller"
    assert sum(r["amount"] for r in records) == 12150.0
    assert records[0]["period_end"] is not None


def test_c2c_audit_report_parser(tmp_path):
    # The current OTS shape: a QuickBooks AP "C2C Audit Report" with a title block,
    # then bill-line columns. Placement id + work date live in "Line description".
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["OakTree Software, Inc."])
    ws.append(["C2C Audit Report"])
    ws.append(["April-October, 2026"])
    ws.append([])
    ws.append(["Date", "Due date", "Bill number", "Vendor", "Line description",
               "Amount", "Paid status", "Vendor terms", "Line Order", "Item class"])
    # Labor line — bill Date (10/20) deliberately differs from the work date in the
    # description (10/04) to prove month attribution uses the description's date.
    ws.append(["10/20/2026", "10/31/2026", "OT-0022", "Aladdin Services, LLC",
               "3055, Wesley Qualls, 10/04/2026, 45.000000, 330.000000",
               14850, "Paid", "", 2, "PR-Miscellaneous"])
    # Expense line, Unpaid — must still be counted.
    ws.append(["10/20/2026", "10/31/2026", "OT-0022", "American Express",
               "3055, Wesley Qualls, 10/11/2026, 1.000000, 25.430000",
               25.43, "Unpaid", "", 3, "Qualls, Wesley"])
    # Name-only expense (no placement id) — excluded.
    ws.append(["10/11/2026", "10/31/2026", "AX", "American Express",
               "AX - BECKHAM JORDAN PREPYMT", 500, "Paid", "", 2, "Jordan, Beckham"])
    # Overhead / benefits (no placement id) — excluded.
    ws.append(["10/01/2026", "10/31/2026", "INV1", "Delta Dental Plan of Oklahoma",
               "INV #103901", 1000, "Paid", "", 2, "PR-IT"])
    f = tmp_path / "C2C - OTS.xlsx"
    wb.save(f)

    records, _ = qbo_c2c.parse_c2c(str(f))
    # Only the two placement-tagged rows survive (name-only + overhead dropped).
    assert len(records) == 2
    assert all(r["placement_id"] == "3055" for r in records)
    assert all(r["candidate_id"] is None for r in records)  # resolved via placement map
    # Paid + Unpaid both counted.
    assert sum(r["amount"] for r in records) == 14875.43
    # Month attribution uses the Line-description work date, not the bill Date.
    labor = next(r for r in records if r["amount"] == 14850)
    assert str(labor["period_end"])[:10] == "2026-10-04"


def test_build_query_routes_entity_and_window():
    sql = sf.build_query("OTS", 2026, 8, periods.SCHEDULE_BIWEEKLY)
    assert "\"Branch\" = 'OTS'" in sql
    # Include both Timesheet (1) and Expense Report (2) charges.
    assert "EntryTypeid" in sql and "IN (1, 2)" in sql
    # Bi-weekly uses a 16-day buffer around the month.
    assert "'2026-07-16'" in sql and "'2026-09-16'" in sql

    cts = sf.build_query("CTS", 2026, 8, periods.SCHEDULE_WEEKLY)
    assert "\"Branch\" = 'CTS'" in cts
    # PayCycle 'CTS-Weekly' routes to CTS even when Branch = OTS.
    assert "'CTS-Weekly'" in cts and "OR" in cts
    assert "<> 'CTS-Weekly'" in sql   # OTS excludes the CTS-Weekly marker


def test_entity_overrides_reroute_candidate():
    # 201524 is mislabeled Weekly in Bullhorn but belongs to OTS.
    ov = {"201524": "OTS"}
    cts = sf.build_query("CTS", 2026, 8, periods.SCHEDULE_WEEKLY, overrides=ov)
    ots = sf.build_query("OTS", 2026, 8, periods.SCHEDULE_BIWEEKLY, overrides=ov)
    # Excluded from CTS, force-included into OTS (on top of the Branch clause).
    assert 'NOT IN (201524)' in cts
    assert 'OR "Candidateid" IN (201524)' in ots
    # No override -> no candidate-id force clauses (the base clause's own OR/AND
    # for the CTS-Weekly rule is fine).
    plain = sf.build_query("CTS", 2026, 8, periods.SCHEDULE_WEEKLY)
    assert "NOT IN" not in plain and '"Candidateid" IN' not in plain


def test_fetch_ats_transforms_rows_via_injected_runner():
    cols = ["Employee Type", "Candidate ID", "Candidate Name",
            "Client Company Name", "Total Hours Paid", "Gross Wages Amount",
            "Billing Period End Date"]
    rows = [
        ["W2 Hourly", 111, "", "", 0, 4000, "2026-08-09"],   # Aug (bi-weekly)
        ["Corp-Corp", 222, "", "", 0, 9999, "2026-08-16"],
    ]
    frame, prov = sf.fetch_ats(
        "OTS", 2026, 8, periods.SCHEDULE_BIWEEKLY, conn_params={},
        runner=lambda sql, params: (rows, cols),
    )
    assert prov["rows_returned"] == 2
    assert set(frame["candidate_id"]) == {"111", "222"}
    assert bool(frame[frame["candidate_id"] == "222"]["is_corp_corp"].iloc[0]) is True
    assert "query" in prov and "PayableCharge" in prov["query"]


def _write_payroll(path):
    df = pd.DataFrame(
        [("111", "Worker A", "8/28/2026", 4000.0, 4500.0)],
        columns=["Employee Number", "Employee Name", "Pay Date", "Gross Wages", "Total Earnings"],
    )
    df.to_excel(path, index=False)


def test_folder_payroll_fetch_and_entity_match(tmp_path):
    _write_payroll(tmp_path / "NEW_OTS_Cost_Audit_Report.xlsx")
    _write_payroll(tmp_path / "NEW_CTS_Cost_Audit_Report.xlsx")

    files = sp.find_payroll_files(str(tmp_path), "OTS")
    assert len(files) == 1 and "OTS" in os.path.basename(files[0])

    pay, prov = sp.fetch_payroll("OTS", periods.SCHEDULE_BIWEEKLY, str(tmp_path),
                                 compare_amount="Gross Wages")
    assert pay.attrs["has_dates"] is True
    assert prov["files"] == ["NEW_OTS_Cost_Audit_Report.xlsx"]
    assert pay["amount"].iloc[0] == 4000.0     # Gross Wages basis


def _write_register(path, people):
    """Write a register-style workbook: a 'Check Details' sheet with the two-row
    header (group labels above, field names below) the real export uses.

    ``people`` is a list of dicts with keys legal_code, legal_name, number, name,
    pay_date, gross, d1099, per_diem, expense, cell, pto.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Check Details"
    # Row 0 = group labels (blank for identity cols, name for earning cols).
    ws.append([None, None, "Employee", None, None, None,
               "1099 Dollars ", "Per Diem Dollars ", "Expense Reimbur Dollars ",
               "Cell Phone Dollars ", "PTO Dollars "])
    # Row 1 = field labels (name for identity cols, blank for earning cols).
    ws.append(["Legal Company Code", "Legal Company Name", "Number", "Name",
               "Pay Date", "Gross Pay", None, None, None, None, None])
    for p in people:
        ws.append([p["legal_code"], p["legal_name"], p["number"], p["name"],
                   p["pay_date"], p["gross"], p.get("d1099", 0), p.get("per_diem", 0),
                   p.get("expense", 0), p.get("cell", 0), p.get("pto", 0)])
    wb.save(path)


def test_register_combined_header_and_total_earnings(tmp_path):
    from reconciler import load_paycheck

    f = tmp_path / "Payroll_Register_Export_by_Date_Range_1.xlsx"
    _write_register(f, [
        {"legal_code": "2340", "legal_name": "Oaktree Software Inc", "number": "111",
         "name": "Worker A", "pay_date": "8/28/2026", "gross": 4000.0, "d1099": 500.0,
         "per_diem": 100.0, "expense": 25.0, "cell": 10.0, "pto": 0.0},
    ])
    df = load_paycheck(str(f), sheet_name="Check Details",
                       compare_amount="Gross Wages", schedule=periods.SCHEDULE_BIWEEKLY)
    r = df.iloc[0]
    assert r["gross_wages"] == 4000.0
    assert r["dollars_1099"] == 500.0
    assert r["wages"] == 4500.0                       # gross + 1099
    # Total Earnings has no column: rebuilt as wages + per diem + expense + cell.
    assert r["total_earnings"] == 4635.0
    assert r["legal_company"] == "2340"
    assert df.attrs["has_wage_split"] is True
    assert r["amount"] == 4500.0                       # Gross Wages basis -> wages


def test_register_explainer_column_from_register_spelling(tmp_path):
    from reconciler import load_paycheck

    f = tmp_path / "Payroll_Register_Export_by_Date_Range_2.xlsx"
    _write_register(f, [
        {"legal_code": "2341", "legal_name": "Complete Technical Services",
         "number": "222", "name": "Worker B", "pay_date": "8/28/2026",
         "gross": 1000.0, "pto": 320.0},
    ])
    df = load_paycheck(str(f), sheet_name="Check Details",
                       compare_amount="Total Earnings", schedule=periods.SCHEDULE_WEEKLY)
    # The register spells it "PTO Dollars"; it must land in the pt_PTO explainer.
    assert df.iloc[0]["pt_PTO"] == 320.0


def test_assign_entities_branch_wins_then_legal_fallback():
    frame = pd.DataFrame({
        "employee_number": ["111", "222", "333", "444"],
        "legal_company": ["2340", "2341", "2340", None],
        "legal_company_name": [None, None, None, "Complete Technical Services"],
    })
    # 111 is in Bullhorn as CTS though its Legal Company (2340) is OTS -> Branch wins.
    emap = {"111": "CTS"}
    routed = sp.assign_entities(frame, emap)
    assert list(routed) == ["CTS", "CTS", "OTS", "CTS"]
    # 222 not in map -> legal code 2341 = CTS; 333 not in map -> 2340 = OTS;
    # 444 has no code -> falls back to the legal company *name*.


def test_fetch_candidate_entity_map_applies_rules():
    rows = [
        (111, "CTS", "CTS-Weekly"),   # CTS by branch and pay cycle
        (222, "OTS", "OTS-BiWeekly"), # OTS by branch
        (333, "OTS", "CTS-Weekly"),   # CTS-Weekly overrides Branch=OTS -> CTS
        (444, "OTS", None),           # OTS
        (555, "HQ", None),            # unknown branch -> omitted
    ]
    emap = sf.fetch_candidate_entity_map(
        runner=lambda sql, p: (rows, ["Candidateid", "Branch", "PayCycle"]),
        overrides={"444": "CTS"},     # manual pin wins last
    )
    assert emap["111"] == "CTS"
    assert emap["222"] == "OTS"
    assert emap["333"] == "CTS"
    assert emap["444"] == "CTS"       # override applied
    assert "555" not in emap


def test_fetch_payroll_combined_register_routes_by_entity(tmp_path):
    f = tmp_path / "Payroll_Register_Export_by_Date_Range_3.xlsx"
    _write_register(f, [
        {"legal_code": "2340", "legal_name": "Oaktree Software Inc", "number": "111",
         "name": "OTS Worker", "pay_date": "8/28/2026", "gross": 4000.0},
        {"legal_code": "2341", "legal_name": "Complete Technical Services",
         "number": "222", "name": "CTS Worker", "pay_date": "8/28/2026", "gross": 2000.0},
    ])
    assert sp.find_combined_register(str(tmp_path)) == str(f)
    # Route by Bullhorn: 111 -> OTS, 222 -> CTS (agreeing with Legal Company here).
    emap = {"111": "OTS", "222": "CTS"}
    ots, prov = sp.fetch_payroll("OTS", periods.SCHEDULE_BIWEEKLY, str(tmp_path),
                                 compare_amount="Gross Wages", entity_map=emap)
    assert set(ots["employee_number"]) == {"111"}
    assert prov["mode"] == "combined-register"
    assert prov["routed_checks"].get("OTS") == 1 and prov["routed_checks"].get("CTS") == 1
    cts, _ = sp.fetch_payroll("CTS", periods.SCHEDULE_WEEKLY, str(tmp_path),
                              compare_amount="Total Earnings", entity_map=emap)
    assert set(cts["employee_number"]) == {"222"}


def test_connectors_feed_engine_end_to_end(tmp_path):
    # ATS via injected runner + payroll via folder -> one reconcile.
    cols = ["Employee Type", "Candidate ID", "Candidate Name",
            "Client Company Name", "Total Hours Paid", "Gross Wages Amount",
            "Billing Period End Date"]
    rows = [["W2 Hourly", 111, "", "", 0, 8000, "2026-08-16"]]  # -> Aug period
    ats, _ = sf.fetch_ats("OTS", 2026, 8, periods.SCHEDULE_BIWEEKLY, {},
                          runner=lambda s, p: (rows, cols))
    _write_payroll(tmp_path / "NEW_OTS_x.xlsx")  # pays 8/28 -> Aug, gross 4000
    pay, _ = sp.fetch_payroll("OTS", periods.SCHEDULE_BIWEEKLY, str(tmp_path),
                              compare_amount="Gross Wages")
    res = reconcile(ats, pay, period=(2026, 8))
    row = res.detail[res.detail["id"] == "111"].iloc[0]
    assert row["ats_amount"] == 8000.0 and row["pay_amount"] == 4000.0
