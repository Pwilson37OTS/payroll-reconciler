"""Bullhorn (payable) source: pull the ATS side straight from Snowflake.

Replaces the manual "ATS Hours" spreadsheet upload. Runs one lean query per
entity per month against ``BULLHORN_MIRROR.RAW."PayableCharge"`` (an XS,
auto-suspending warehouse — a few seconds of compute), shapes the rows into the
same layout the file export had, and reuses :func:`reconciler.normalize_ats_frame`
so the accounting-month logic is byte-for-byte identical to the upload path.

Mirror gotchas baked in (see the BULLHORN_MIRROR notes):
  * amount = SUM of the LEDGER ``PayMasterTransaction."Amount"`` (joined through
    ``PayMaster`` to the wage charge, EntryTypeid = 1), NOT the charge header's
    ``"Total"`` and NOT ``PayableChargeSummaryTransaction``. Two reasons:
      - the header ``"Total"`` goes stale (a charge first synced at 0 before
        hours were approved, then updated in Bullhorn without its
        ``DateLastModified`` bumping, so the incremental sync never re-pulled it);
      - ``PayableChargeSummaryTransaction``/``PayTransaction`` retain SUPERSEDED
        transactions when a timesheet is revised (both the original and the
        corrected lines stay), so summing them double-counts adjusted weeks.
    ``PayMasterTransaction`` is the adjustment-aware ledger; sum its
    ``"NetAmount"`` (not ``"Amount"``) over non-deleted rows. ``NetAmount`` is
    the field that stays correct through every adjustment shape seen:
      - stale header (cand 208082 charge 25414: header 0 → $880);
      - revised timesheet leaving superseded lines (cand 206970 charge 25336,
        6h→5h: header $814.34, raw Amount $1607.25, NetAmount $792.91);
      - orphaned reversals whose originals are missing from the mirror (cand
        181049 charge 25126: raw Amount $1400, NetAmount $5250);
      - a fully voided charge (cand 174092 charge 25500: raw Amount $3960,
        NetAmount $0 — reversed to zero).
    Summing raw ``"Amount"`` gets every one of those wrong. Confirmed with Phil
    2026-09-17/18.
  * billing week = "PeriodEndDate" (the master txn's PayPeriodEndDate is NULL)
  * entity routing by "Branch" (the authoritative CTS/OTS field — PayCycle is
    unreliable: some OTS people are labeled Weekly/CTS-Weekly)
  * the weekly sync means data can be up to ~7 days stale — surfaced via
    :func:`mirror_freshness`, which the app can gate on.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Sequence

import pandas as pd

from reconciler import normalize_ats_frame, periods

# The Bullhorn "Branch" value that defines each entity (authoritative). The
# accounting-month *schedule* (weekly vs bi-weekly) still comes from the entity
# config, not from the per-charge PayCycle field.
ENTITY_BRANCH = {
    "CTS": "CTS",
    "OTS": "OTS",
}

# Exception to Branch: some CTS contractors carry Branch = OTS in Bullhorn but
# are billed/paid as CTS and marked with this PayCycle. It overrides Branch and
# routes them to CTS (confirmed with Phil 2026-09-14). Correcting their Branch in
# Bullhorn is the lasting fix; until then this rule keeps them reconciling in CTS.
CTS_WEEKLY_PAYCYCLE = "CTS-Weekly"

# Columns pulled, aliased to the ATS file's header spellings so the existing
# normalizer resolves them unchanged. The amount is summed from the LEDGER
# PayMasterTransaction."NetAmount" (joined through PayMaster to the wage charge),
# over non-deleted rows. NetAmount — not the charge header "Total", not the
# SummaryTransaction ledger, not the raw transaction Amount — is the only field
# that stays correct through stale headers, revised timesheets, orphaned
# reversals, and voided charges (see the module docstring). COALESCE falls back
# to Amount only if NetAmount is ever NULL (none are in practice). The charge
# header still supplies the entity / period / type context for filter + grouping.
_SQL = """
SELECT
    pc."EmployeeType"   AS "Employee Type",
    pc."Candidateid"    AS "Candidate ID",
    ''                  AS "Candidate Name",
    ''                  AS "Client Company Name",
    0                   AS "Total Hours Paid",
    SUM(COALESCE(pmt."NetAmount", pmt."Amount")) AS "Gross Wages Amount",
    pc."PeriodEndDate"  AS "Billing Period End Date"
FROM BULLHORN_MIRROR.RAW."PayableCharge" pc
JOIN BULLHORN_MIRROR.RAW."PayMaster" pm
    ON pm."ChargeID" = pc."ID"
JOIN BULLHORN_MIRROR.RAW."PayMasterTransaction" pmt
    ON pmt."PayMasterID" = pm."ID"
WHERE {entity_clause}
  AND pc."EntryTypeid" = 1
  AND pc."PeriodEndDate" BETWEEN '{start}' AND '{end}'
  AND COALESCE(pmt."IsDeleted", FALSE) = FALSE
GROUP BY pc."EmployeeType", pc."Candidateid", pc."PeriodEndDate"
"""


def _entity_clause(entity: str, overrides: dict | None) -> str:
    """WHERE fragment selecting an entity's candidates by Bullhorn ``Branch``,
    with optional per-candidate overrides (rarely needed now that Branch is the
    authoritative signal — kept as a safety valve).

    ``overrides`` maps a candidate id -> the entity they really belong to. A
    candidate pinned to another entity is force-excluded; one pinned to this
    entity is force-included regardless of Branch.
    """
    branch = ENTITY_BRANCH.get(entity, entity)
    ov = {str(k).strip(): str(v).strip() for k, v in (overrides or {}).items()}
    force_in = [c for c, e in ov.items() if e == entity and c.isdigit()]
    force_out = [c for c, e in ov.items() if e != entity and c.isdigit()]

    # Branch is authoritative, except PayCycle 'CTS-Weekly' always means CTS.
    if branch == "CTS":
        clause = f"(\"Branch\" = 'CTS' OR \"PayCycle\" = '{CTS_WEEKLY_PAYCYCLE}')"
    elif branch == "OTS":
        clause = f"(\"Branch\" = 'OTS' AND \"PayCycle\" <> '{CTS_WEEKLY_PAYCYCLE}')"
    else:
        clause = f"\"Branch\" = '{branch}'"
    if force_out:
        clause = f'({clause} AND "Candidateid" NOT IN ({", ".join(force_out)}))'
    if force_in:
        clause = f'({clause} OR "Candidateid" IN ({", ".join(force_in)}))'
    return clause


def _month_window(year: int, month: int, schedule: str) -> tuple[str, str]:
    """A generous PeriodEndDate range that safely covers the month's billing
    weeks; the engine then assigns/filters precisely. Bi-weekly needs a wider
    buffer because a period's weeks can sit two weeks either side of the month.
    """
    first = dt.date(year, month, 1)
    last = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    buf = 16 if schedule == periods.SCHEDULE_BIWEEKLY else 10
    return (
        (first - dt.timedelta(days=buf)).isoformat(),
        (last + dt.timedelta(days=buf)).isoformat(),
    )


def build_query(
    entity: str, year: int, month: int, schedule: str, overrides: dict | None = None
) -> str:
    """Return the exact SQL that will be run (also used for the audit provenance)."""
    start, end = _month_window(year, month, schedule)
    clause = _entity_clause(entity, overrides)
    return _SQL.format(entity_clause=clause, start=start, end=end).strip()


def rows_to_ats_frame(rows: Sequence[Sequence], columns: Sequence[str]) -> pd.DataFrame:
    """Turn raw query rows into the ATS-layout DataFrame the normalizer expects."""
    return pd.DataFrame(list(rows), columns=list(columns))


def _default_runner(sql: str, conn_params: dict) -> tuple[list, list]:
    """Execute ``sql`` via snowflake-connector-python. Returns (rows, columns)."""
    import snowflake.connector  # imported lazily so the app loads without it

    conn = snowflake.connector.connect(**conn_params)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        data = cur.fetchall()
        return data, cols
    finally:
        conn.close()


def fetch_ats(
    entity: str,
    year: int,
    month: int,
    schedule: str,
    conn_params: dict,
    runner: Callable[[str, dict], tuple[list, list]] = _default_runner,
    overrides: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Fetch and normalize the Bullhorn ATS side for one entity-month.

    Returns ``(normalized_frame, provenance)``. ``runner`` is injectable so the
    transform can be unit-tested without a live Snowflake connection.
    ``overrides`` maps candidate id -> the entity they really belong to (for
    Bullhorn PayCycle mislabels).
    """
    sql = build_query(entity, year, month, schedule, overrides)
    rows, cols = runner(sql, conn_params)
    raw = rows_to_ats_frame(rows, cols)
    frame = normalize_ats_frame(raw, schedule)
    applied = {c: e for c, e in (overrides or {}).items()}
    provenance = {
        "source": "snowflake:BULLHORN_MIRROR.PayableCharge+PayMasterTransaction(ledger)",
        "entity": entity,
        "branch": ENTITY_BRANCH.get(entity, entity),
        "entity_overrides": applied or None,
        "query": sql,
        "rows_returned": int(len(raw)),
        "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return frame, provenance


def fetch_placement_map(
    entity: str,
    runner: Callable[[str, dict], tuple[list, list]] = _default_runner,
    overrides: dict | None = None,
) -> dict[str, str]:
    """Return {placement_id: candidate_id} for an entity (for QBO C2C joining)."""
    clause = _entity_clause(entity, overrides)
    sql = (
        'SELECT DISTINCT "Placementid", "Candidateid" '
        f'FROM BULLHORN_MIRROR.RAW."PayableCharge" WHERE {clause}'
    )
    rows, _ = runner(sql, {})
    return {
        str(int(p)): str(int(c))
        for p, c in rows
        if p is not None and c is not None
    }


def fetch_candidate_entity_map(
    runner: Callable[[str, dict], tuple[list, list]] = _default_runner,
    overrides: dict | None = None,
    lookback_days: int = 180,
) -> dict[str, str]:
    """Return ``{candidate_id: 'CTS'|'OTS'}`` from Bullhorn — the authoritative
    routing for every person who appears in the payable data.

    A person's entity is their Bullhorn ``Branch``, except ``PayCycle =
    'CTS-Weekly'`` always wins for CTS (the same rule the per-entity query uses).
    ``overrides`` (candidate id -> entity) is applied last as a manual pin.
    Scoped to charges in the last ``lookback_days`` so it reflects current
    branches and stays cheap. Candidates whose Branch is neither CTS nor OTS are
    omitted (the payroll loader then falls back to Legal Company for them).
    """
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    sql = (
        'SELECT "Candidateid", "Branch", "PayCycle" '
        'FROM BULLHORN_MIRROR.RAW."PayableCharge" '
        f'WHERE "PeriodEndDate" >= \'{since}\' '
        'GROUP BY "Candidateid", "Branch", "PayCycle"'
    )
    rows, _ = runner(sql, {})
    out: dict[str, str] = {}
    for cid, branch, paycycle in rows:
        if cid is None:
            continue
        key = str(int(cid)) if isinstance(cid, float) else str(cid).strip()
        b = (str(branch).strip() if branch is not None else "")
        pc = (str(paycycle).strip() if paycycle is not None else "")
        if pc == CTS_WEEKLY_PAYCYCLE or b == "CTS":
            entity = "CTS"
        elif b == "OTS":
            entity = "OTS"
        else:
            continue
        # CTS wins on any conflict across a candidate's rows (the CTS-Weekly rule).
        if out.get(key) != "CTS":
            out[key] = entity
    for k, v in (overrides or {}).items():
        kk = str(k).strip()
        vv = str(v).strip()
        if vv in ENTITY_BRANCH:
            out[kk] = vv
    return out


def mirror_freshness(
    conn_params: dict,
    runner: Callable[[str, dict], tuple[list, list]] = _default_runner,
) -> dt.datetime | None:
    """Return the latest DateLastModified in the charge ledger (mirror sync
    recency). Uses the transaction ledger we actually read for amounts."""
    sql = (
        'SELECT MAX("DateLastModified") '
        'FROM BULLHORN_MIRROR.RAW."PayMasterTransaction"'
    )
    rows, _ = runner(sql, conn_params)
    if rows and rows[0] and rows[0][0] is not None:
        return pd.Timestamp(rows[0][0]).to_pydatetime()
    return None
