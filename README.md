# Cost Reconciler

A Streamlit app that reconciles **what Bullhorn shows as a payable charge**
against **what payroll actually paid**, every month, and keeps a permanent
history of each audit.

It replaces the old `Cost Audit.xlsm` macro workflow.

## What it does

1. Sign in (shared app password + optional email allowlist).
2. Pick the period (month / year / business unit) and a variance threshold.
3. Upload the two exports:
   - **Bullhorn — ATS Hours** (payable side)
   - **Payroll — Paycheck** (paid side)
4. The engine matches people and buckets everyone into:
   | Bucket | Meaning |
   |---|---|
   | **Matched** | On both sides, difference under the threshold (rounding) |
   | **Variance** | On both sides, difference at/above the threshold — investigate |
   | **Corp-Corp** | Excluded from the dollar comparison (not paid via payroll) |
   | **Billed, not paid** | In Bullhorn, no paycheck |
   | **Paid, not billed** | Paid, but not billed in Bullhorn |
5. Annotate each exception (a *Review* category + free-text *Note*, mirroring the
   old spreadsheet's comment column). Variances whose difference is fully accounted
   for by **payroll-only pay** (PTO, sick, holiday, bonus) are **auto-noted** with
   the breakdown so no one has to hunt them down (still shown as differences).
   A **Difference detail** section lets you pick
   any contractor with a difference and see Bullhorn's weekly billing next to the
   payroll checks (or the QuickBooks C2C bill lines for Corp-Corp), to trace which
   week is causing the variance.
6. **Save** — the run (summary, annotated detail, and the original uploads) is
   stored so you can reopen any past month read-only.

## The reconciliation logic (reverse-engineered from the macro)

- **Match key:** Bullhorn `Candidate ID` = payroll `Employee Number`.
- **Bullhorn amount:** sum of `Total Wages Paid` across a person's weekly rows.
- **Payroll amount:** selectable per run — **`Total Earnings`** (default; includes
  per diem / expense reimbursements / 1099, matching the old spreadsheet) or
  **`Gross Wages`** (hours-based wages only, a tighter match to Bullhorn). The
  app maps `Gross Pay` to `Gross Wages` on the register export.
- **Difference:** `Payroll − Bullhorn`.
- **Corp-Corp** (`Employee Type = CORP-CORP`) is flagged, not dollar-compared.
- **Footer/subtotal rows** (e.g. `REPORT TOTAL`) are stripped automatically —
  the old macro counted these as a fake employee.
- **Net variance** on the dashboard covers only *comparable* people (matched +
  variance), so Corp-Corp workers don't distort the headline number.
- **Labor vs per diem:** "wages" = **Gross Wages + 1099** (1099 is contractor
  labor that Bullhorn bills — some workers are paid part W2 / part 1099 for the
  same work). *Per diem/exp* is everything beyond that.
- **Per diem handling:** Bullhorn's payable sometimes bundles per diem and
  sometimes doesn't, so a payroll person reconciles if their Bullhorn amount
  matches payroll **wages (gross + 1099) *or* Total Earnings**. The reported
  difference is the unexplained residual, so a gap that is purely per diem doesn't
  read as a variance (it's shown in the separate *Per diem/exp* column instead).

### Accounting month (per business unit)

Each business unit has its own payroll cadence, chosen with the **Business unit**
selector. Uploads routinely include periods outside the month; those are excluded
automatically, and the results screen's **"Weeks counted"** panel shows exactly
what was included vs. excluded.

**CTS — weekly.** A billing week belongs to the month containing its **Sunday**.
- **Bullhorn (ATS):** each `Billing Period End Date` (Fri/Sat/Sun) snaps *forward*
  to that week's Sunday.
- **Payroll:** each `Pay Date` (the following Friday) snaps *back* 5 days to the
  same Sunday.

**OTS — bi-weekly, two-week delay.** A pay period is two weeks (Mon–Sun); the
month is the one containing the period's **last Friday** (the Friday two days
before the period-end Sunday). So the period ending Sun Aug 2 counts as *July*
(last Friday Jul 31). July is a rare **3-period** month; most months have 2.
- **Bullhorn (ATS):** each week's Sunday is grouped into its bi-weekly period-end
  Sunday, then anchored to that period's last Friday.
- **Payroll:** `Pay Date − 14 days` = the period's last Friday (its month).

Either way, payroll filtering needs a **per-check, dated** export (detailed
weekly report or register `Check Details`). Holiday-shifted paydays (e.g.
Juneteenth moving a Friday check to Thursday) still resolve to the correct month.
A pre-summarized sheet with no dates **cannot** be week-filtered — the app warns
and compares the whole export as-is.

The payroll loader auto-detects the header row, so it reads the detailed weekly
export (single header row), the register (two-row header), and the old
summarized sheet without configuration.

The results screen shows a **"Weeks counted"** panel listing every week and
whether it was included or excluded, plus the dollar amount excluded — so the
month boundary is always auditable. Off-cycle Adjustment/Additional checks that
fall in the month are counted and flagged in that panel.

> **Payroll upload:** use the register workbook and pick the **`Check Details`**
> sheet (the app auto-selects it). That's the only payroll sheet that can be
> filtered to the accounting month.

Column names are resolved case/space-insensitively and are configurable in
[`reconciler/config.py`](reconciler/config.py).

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit the password
streamlit run app.py
```

## Run the tests

```bash
pip install pytest
pytest -q
```

The suite in [`tests/test_engine.py`](tests/test_engine.py) reproduces the
known-good **February 2026 CTS** result (the four flagged variances, Corp-Corp
handling, footer stripping, and the unmatched buckets).

## Deploying to Streamlit Community Cloud

1. Push this repo to a **private** GitHub repo.
2. Create the app on [share.streamlit.io](https://share.streamlit.io) pointing at
   `app.py`.
3. In the app's **Secrets**, paste the contents of your `secrets.toml`
   (`app_password`, `allowed_emails`).

> **Storage caveat.** Community Cloud's filesystem is *ephemeral* — the default
> `LocalFileStorage` backend is wiped on restart/redeploy, so it is fine for
> local use and demos but **not** for a durable production history. When you're
> ready, implement a `StorageBackend` (Snowflake or Postgres) in
> [`storage/`](storage/) and switch the factory in `get_storage()`. Nothing in
> the app or engine changes — they depend only on the interface.

## Automatic data pull (no more manual exports)

The **New reconciliation** screen has a **Data source** toggle:

- **Pull automatically** — pulls Bullhorn from Snowflake and reads the payroll
  register from a configured SharePoint folder, then reconciles. Requires the
  `[snowflake]` and `[payroll]` secrets below; the screen shows a live
  configured/not-configured status and a mirror-freshness warning.
- **Upload files** — the manual fallback (unchanged).

Both paths run the *same* engine and save the same audit record; automatic pulls
also store a CSV snapshot of the pulled data plus a **provenance** block (the SQL
run, row counts, files read, mirror sync time) for reproducibility.

Connectors live in [`sources/`](sources/):
- `snowflake_bullhorn.py` — one lean query joining `PayableCharge` (EntryType 1
  wages) through `PayMaster` to the adjustment-aware ledger
  `PayMasterTransaction`, summing **`NetAmount`** (over non-deleted rows) rather
  than the charge header's `Total`. `NetAmount` is the only field that stays
  correct through all four mirror quirks: a **stale header** (`Total` synced at 0
  before hours were approved and never re-pulled), a **revised timesheet** (old
  lines left behind, which double-count if summed raw), **orphaned reversals**
  (a reversal whose original is missing from the mirror), and a **voided charge**
  (adjusted back to 0). Runs on an XS auto-suspending warehouse. Entity routing
  is by **`Branch`**
  (authoritative), with one exception: `PayCycle = 'CTS-Weekly'` always routes to
  CTS (a group of CTS contractors mislabeled `Branch = OTS`). Reconciliation runs
  in Python, so **no Snowflake compute** beyond the pull.
- `sharepoint_payroll.py` — reads the payroll register `.xlsx` from a folder (the
  SharePoint library synced locally, or a UNC path). It prefers **one combined
  register covering both companies** (the broad `Check Details` export): every
  check is routed to an entity by that person's **Bullhorn Branch** (the same
  authoritative signal the ATS query uses), with the payroll **Legal Company**
  (`2341` = CTS, `2340` = OTS) as a fallback only for people not in Bullhorn. The
  register has no `Total Earnings` column, so it is reconstructed as `Gross Pay +
  1099 + per diem + expense + cell phone`. Legacy per-entity files (matched by
  entity code in the name) are still read when no combined register is present. A
  Microsoft Graph backend is stubbed for a cloud-hosted app.
- `qbo_c2c.py` — Corp-Corp contractors are paid via QuickBooks, not payroll.
  Drop a QBO "Purchases by Vendor Detail" export named like `C2C - CTS.xlsx` in
  the same folder; **every** vendor **Bill** line is summed per Bullhorn Placement
  ID (the leading number in each bill's Description) and used as the **paid side
  for Corp-Corp** people, so they reconcile instead of being excluded. This
  includes non-labor lines (per diem / expense, booked to accounts like
  *Employee Advance*) because Bullhorn bills these contractors **all-in** — their
  payable charge already bundles the per diem, so the full vendor cost is what
  reconciles. Each line is tagged labor vs per diem/expense (by its Distribution
  account) for the drill-down view. Corp-Corp with no C2C match stays excluded.

**Entity overrides** (`[entity_overrides]` secret): map a candidate id to the
entity they really belong to, to fix Bullhorn `PayCycle` mislabels (e.g. someone
billed Weekly but paid in OTS). The lasting fix is correcting PayCycle in
Bullhorn; remove the override after the mirror syncs.

Secrets (`.streamlit/secrets.toml`):

```toml
# Streamlit-native connection; key-pair auth (works headless — SSO/MFA don't).
[connections.snowflake]
account = "…"; user = "…"; private_key_file = "…/rsa.p8"
role = "…"; warehouse = "…"; database = "BULLHORN_MIRROR"; schema = "ANALYTICS"

[payroll]
root_dir = "…/OakTree Operations/_shared/payroll-audit"
```

## Project layout

```
app.py                 Streamlit UI (auth, pull/upload, review, history)
auth.py                Shared-password + email allowlist gate
reconciler/
  config.py            Column maps, entities, categories, thresholds
  periods.py           CTS weekly / OTS bi-weekly accounting-month logic
  normalize.py         Read + clean exports; normalize_ats_frame for connectors
  engine.py            Aggregate, match, classify, summarize, warnings
sources/
  snowflake_bullhorn.py  Bullhorn payable charges from Snowflake
  sharepoint_payroll.py  Payroll register from a SharePoint folder
  qbo_c2c.py             Corp-Corp contractor cost from a QuickBooks export
storage/
  base.py              StorageBackend interface + AuditRecord
  local.py             Local-filesystem backend (dev/internal host)
tests/
  test_engine.py       Engine + accounting-month regression tests
  test_sources.py      Connector tests (no live services needed)
```
