"""Configuration for the Cost Reconciler.

Centralizes the column mappings and constants that describe how the two source
exports are read. Keeping these here means that if Bullhorn or the payroll
provider renames or reorders a column, there is exactly one place to update.
"""

from __future__ import annotations

# --- Category labels ---------------------------------------------------------
# Every person in a reconciliation lands in exactly one of these buckets.
CAT_MATCHED = "Matched"
CAT_VARIANCE = "Variance"
CAT_CORP_CORP = "Corp-Corp"
CAT_BILLED_NOT_PAID = "Billed, not paid"
CAT_PAID_NOT_BILLED = "Paid, not billed"

# Order used when displaying / sorting categories (most attention-worthy first).
CATEGORY_ORDER = [
    CAT_VARIANCE,
    CAT_BILLED_NOT_PAID,
    CAT_PAID_NOT_BILLED,
    CAT_CORP_CORP,
    CAT_MATCHED,
]

# Default dollar threshold: a person whose |Paycheck - Bullhorn| is below this
# is treated as a rounding match rather than a variance to investigate.
DEFAULT_THRESHOLD = 1.00

# --- Business units (entities) ----------------------------------------------
# Each entity has its own payroll cadence, which drives how billing weeks / pay
# periods are assigned to an accounting month (see reconciler/periods.py):
#
#   * CTS  — WEEKLY. Month = the month of the billing week's Sunday.
#   * OTS  — BI-WEEKLY on a two-week delay. Month = the month of the pay period's
#            LAST FRIDAY (period-end Sunday minus 2 days). July is a rare
#            3-period month; most months have 2.
ENTITIES = {
    "CTS": {
        "schedule": "weekly",
        "label": "CTS (weekly)",
        "default_compare": "Total Earnings",
    },
    "OTS": {
        # OTS pays heavy per diem / expense that Bullhorn doesn't carry as
        # payable wages, so compare wage-to-wage and show the rest separately.
        "schedule": "biweekly",
        "label": "OTS (bi-weekly, 2-week delay)",
        "default_compare": "Gross Wages",
    },
}
DEFAULT_ENTITY = "CTS"


def schedule_for(entity: str) -> str:
    """Return the payroll schedule for an entity (defaults to weekly)."""
    return ENTITIES.get(entity, ENTITIES[DEFAULT_ENTITY])["schedule"]


def default_compare_for(entity: str) -> str:
    """Return the default payroll compare basis for an entity."""
    return ENTITIES.get(entity, ENTITIES[DEFAULT_ENTITY]).get(
        "default_compare", DEFAULT_COMPARE_AMOUNT
    )

# Value in the ATS "Employee Type" column that marks a corp-to-corp worker.
# These are excluded from the dollar comparison (they are not paid via payroll).
CORP_CORP_MARKER = "CORP-CORP"

# --- Source column mappings --------------------------------------------------
# Keys are the canonical internal names; values are the list of header spellings
# to look for in the uploaded file (matched case-insensitively, whitespace
# trimmed). The first spelling that is found wins.

ATS_COLUMNS = {
    "employee_type": ["Employee Type"],
    "candidate_id": ["Candidate ID"],
    "name": ["Candidate Name"],
    "client": ["Client Company Name"],
    "hours_paid": ["Total Hours Paid"],
    # Gross wages billed to the worker. CTS exports call this "Total Wages Paid";
    # OTS exports call it "Gross Wages Amount". NOT the *Burdened* amount, which
    # adds employer tax/benefit burden and is not what we compare.
    "wages_paid": ["Total Wages Paid", "Gross Wages Amount"],
    "billing_period_end": ["Billing Period End Date"],
}

# Payroll arrives in a few shapes, all handled by one loader that auto-detects
# the header row and maps whatever columns are present:
#
#   * Detailed weekly export (preferred) — one row per check, single header row,
#     with Pay Date, Gross Wages, and Total Earnings.
#   * Register "Check Details" — one row per check, TWO-row header (group labels
#     above field names), Pay Date, Gross Pay.
#   * Old pre-summarized sheet — one row per employee, NO date (cannot be
#     week-filtered; the whole export is compared as-is, with a warning).
#
# Field spellings are matched case/whitespace-insensitively; first match wins.
PAYCHECK_FIELD_CANDIDATES = {
    "employee_number": ["Employee Number", "Number"],
    "name": ["Employee Name", "Name"],
    "pay_type": ["Pay Type"],
    "check_type": ["Check Type Description"],
    "pay_date": ["Pay Date"],
    "total_earnings": ["Total Earnings"],
    "gross_wages": ["Gross Wages", "Gross Pay"],
    # 1099 = contractor labor, counts as wages. "Dollars 1099" (detailed export)
    # and "1099 Dollars" (register) are the same figure.
    "dollars_1099": ["Dollars 1099", "1099 Dollars"],
    "paid_gross": ["Paid Gross"],
    # Non-wage earnings that make up the gap between Gross Pay and Total Earnings.
    # The register has no "Total Earnings" column, so it is reconstructed as
    # Gross Pay + 1099 + these (see PAYCHECK_TOTAL_EXTRA_FIELDS). The register's
    # "Taxed PerDiem Dollars" is already inside Gross Pay, so it is NOT added.
    "per_diem": ["Per Diem Dollars", "Dollars Per Diem"],
    "expense_reimbur": ["Expense Reimbur Dollars", "Expense Reimbursement Dollars",
                        "Dollars Expense Reimbursement"],
    "cell_phone": ["Cell Phone Dollars", "Dollars Cell Phone"],
    # Legal payroll company — used only to route people who aren't in Bullhorn to
    # an entity (Bullhorn Branch is authoritative for everyone who is).
    "legal_company": ["Legal Company Code"],
    "legal_company_name": ["Legal Company Name"],
}
# The header row is the first row (scanning the top of the sheet) that contains
# an ID label plus at least one amount label.
PAYCHECK_HEADER_ID_MARKERS = ["Employee Number", "Number"]
PAYCHECK_HEADER_AMOUNT_MARKERS = ["Total Earnings", "Gross Wages", "Gross Pay"]

# On the register's two-row header, these second-row cells are generic group
# labels (the real column name lives in the row above), so the combined header
# falls back to the row-above label for any column whose second-row cell is one
# of these. Matched case/space-insensitively.
PAYCHECK_GENERIC_SUBHEADERS = ["Memo Dollars", "Amount", "Tax Amount", "ER Tax Amount"]

# When a payroll export has no "Total Earnings" column (the register), rebuild it
# as wages (Gross Pay + 1099) plus these non-wage earning fields.
PAYCHECK_TOTAL_EXTRA_FIELDS = ["per_diem", "expense_reimbur", "cell_phone"]

# Fallback entity routing for people NOT found in Bullhorn (Branch wins for
# everyone who is). Maps the payroll Legal Company Code -> entity.
LEGAL_COMPANY_ENTITY = {
    "2341": "CTS",   # Complete Technical Services
    "2340": "OTS",   # Oaktree Software Inc
}
# Same, matched on the Legal Company *name* when the code is absent/unrecognized.
LEGAL_COMPANY_NAME_ENTITY = {
    "complete technical services": "CTS",
    "oaktree software inc": "OTS",
    "oaktree software, inc": "OTS",
}

# Which normalized payroll field each compare-mode uses, in preference order
# (falls back to the next if the first isn't in the file).
# "Gross Wages" here means labor wages = Gross Wages + 1099 (1099 is contractor
# labor that Bullhorn bills). "Total Earnings" adds per diem / expense / etc.
COMPARE_AMOUNT_OPTIONS = {
    "Total Earnings": ["total_earnings", "wages"],
    "Gross Wages": ["wages", "total_earnings"],
}
DEFAULT_COMPARE_AMOUNT = "Total Earnings"

# The single amount field compared on each side (post-normalization names).
ATS_AMOUNT_FIELD = "wages_paid"
PAYCHECK_AMOUNT_FIELD = "amount"

# Payroll check types that are off-cycle corrections; surfaced for transparency.
PAYCHECK_ADJUSTMENT_TYPES = ["Adjustment", "Additional Check"]

# Payroll pay types that are paid but NOT worked, so Bullhorn typically doesn't
# bill them — a variance equal to their sum is explainable (auto-noted, not a
# mystery to hunt down). Label -> header spellings (case/space-insensitive).
# Each label lists both the detailed-export spelling (" Dollars X") and the
# register spelling ("X Dollars"); first match in the file wins.
EXPLAINER_PAY_TYPES = {
    "Holiday": ["Dollars Holiday", "Holiday Dollars"],
    "PTO": ["Dollars PTO", "PTO Dollars"],
    "PTO Hourly": ["Dollars PTO Hourly", "PTO Hourly Dollars"],
    "PTO Payout": ["Dollars PTO Payout", "PTO Payout Dollars"],
    "Sick": ["Dollars Sick", "Sick Dollars"],
    "Bonus": ["Dollars Bonus", "Bonus Dollars"],
}

# Text found in footer / subtotal rows that must be dropped before reconciling.
# Any row whose ID matches one of these (case-insensitive) is discarded.
FOOTER_ID_MARKERS = ["REPORT TOTAL", "TOTAL", "GRAND TOTAL", "SUBTOTAL"]
