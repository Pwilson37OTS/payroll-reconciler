"""Cost Reconciler — Streamlit app.

Monthly reconciliation of Bullhorn payable charges (ATS Hours export) against
what payroll actually paid (Paycheck export). Upload both files, review the
exceptions, annotate them, and save the run as a permanent audit record.
"""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import streamlit as st

import auth
from reconciler import ColumnNotFoundError, load_ats, load_paycheck, periods, reconcile
from reconciler import config as rc
from sources import qbo_c2c, sharepoint_payroll, snowflake_bullhorn
from storage import AuditRecord, get_storage

st.set_page_config(page_title="Cost Reconciler", page_icon="🧾", layout="wide")

# Category -> background color for row highlighting in tables.
CATEGORY_COLORS = {
    rc.CAT_VARIANCE: "#fde2e1",          # red-ish: needs investigation
    rc.CAT_BILLED_NOT_PAID: "#ffe9c7",   # amber
    rc.CAT_PAID_NOT_BILLED: "#ffe9c7",   # amber
    rc.CAT_CORP_CORP: "#e3ecff",         # blue: expected exclusion
    rc.CAT_MATCHED: "#e6f4ea",           # green: clean
}

REVIEW_CATEGORIES = [
    "Unreviewed",
    "PTO",
    "Sick",
    "Holiday",
    "Expense",
    "Bonus",
    "Prior period correction",
    "Per diem",
    "Corp-Corp",
    "Missing paycheck",
    "Missing bill",
    "Error - needs correction",
    "Other",
]

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# --- helpers -----------------------------------------------------------------
def _money(x: float) -> str:
    neg = x < 0
    s = f"${abs(x):,.2f}"
    return f"(${abs(x):,.2f})" if neg else s


def _sheet_names(data: bytes, name: str) -> list[str] | None:
    if name.lower().endswith(".csv"):
        return None
    try:
        return pd.ExcelFile(io.BytesIO(data)).sheet_names
    except Exception:  # noqa: BLE001 - surfaced to the user below
        return None


def _as_source(data: bytes, name: str) -> io.BytesIO:
    b = io.BytesIO(data)
    b.name = name
    return b


def _default_sheet(sheets: list[str], *keywords: str) -> int:
    for i, s in enumerate(sheets):
        low = s.lower()
        if any(k in low for k in keywords):
            return i
    return 0


def _render_difference_detail(recon: dict) -> None:
    """Drill-down: pick a contractor with a difference and see the weekly detail
    from Bullhorn next to the payroll checks (or C2C bill lines for Corp-Corp)."""
    ats_f = recon.get("ats_frame")
    pay_f = recon.get("pay_frame")
    c2c_lines = recon.get("c2c_lines") or {}
    detail = recon["detail"]
    ym = recon.get("period_ym")
    if ats_f is None or pay_f is None:
        return

    st.divider()
    st.markdown("#### 🔍 Difference detail — trace a variance to the week")

    cand = detail[
        (detail["abs_difference"] >= 0.005)
        & detail["category"].isin(
            [rc.CAT_VARIANCE, rc.CAT_BILLED_NOT_PAID, rc.CAT_PAID_NOT_BILLED]
        )
    ].sort_values("abs_difference", ascending=False)
    if cand.empty:
        st.caption("No differences to trace — everything matched.")
        return

    def _lbl(cid: str) -> str:
        r = detail[detail["id"] == cid].iloc[0]
        nm = r["name"] if pd.notna(r["name"]) and str(r["name"]).strip() else cid
        return f"{nm} — {_money(r['difference'])} ({r['category']})"

    pick = st.selectbox("Contractor with a difference", cand["id"].tolist(),
                        format_func=_lbl, key="drilldown")
    row = detail[detail["id"] == pick].iloc[0]

    m = st.columns(4)
    m[0].metric("Bullhorn billed", _money(row["ats_amount"]))
    m[1].metric("Paid", _money(row["pay_amount"]))
    m[2].metric("Difference", _money(row["difference"]))
    m[3].metric("Per diem/exp", _money(row["pay_nonwage"]))

    left, right = st.columns(2)

    a = ats_f[ats_f["candidate_id"] == pick].copy()
    if ym is not None:
        a = a[(a["acct_year"] == ym[0]) & (a["acct_month"] == ym[1])]
    a = a.sort_values("week_ending")
    with left:
        st.markdown("**Bullhorn — weekly billing**")
        if a.empty:
            st.caption("No Bullhorn billing this month.")
        else:
            st.dataframe(
                pd.DataFrame({
                    "Week ending": a["week_ending"].dt.strftime("%Y-%m-%d"),
                    "Wages": a["wages_paid"].round(2),
                }),
                hide_index=True, use_container_width=True,
            )
            st.caption(f"Total billed: {_money(a['wages_paid'].sum())}")

    with right:
        if pick in c2c_lines:
            st.markdown("**QuickBooks C2C — bill lines**")
            recs = c2c_lines[pick]
            st.dataframe(
                pd.DataFrame([{
                    "Date": r.get("work_date"),
                    "Type": ("Per diem/exp" if r.get("kind") == "nonwage" else "Labor")
                            + (f" — {r['account']}" if r.get("account") else ""),
                    "Hours": r.get("hours"),
                    "Rate": r.get("rate"),
                    "Amount": round(r["amount"], 2),
                } for r in recs]),
                hide_index=True, use_container_width=True,
            )
            total = sum(r["amount"] for r in recs)
            nonwage = sum(r["amount"] for r in recs if r.get("kind") == "nonwage")
            cap = f"Total C2C: {_money(total)}"
            if nonwage:
                cap += f" (incl. per diem/exp {_money(nonwage)})"
            st.caption(cap)
        else:
            st.markdown("**Payroll — checks**")
            p = pay_f[pay_f["employee_number"] == pick].copy()
            if ym is not None:
                p = p[(p["acct_year"] == ym[0]) & (p["acct_month"] == ym[1])]
            p = p.sort_values("pay_date")
            if p.empty:
                st.caption("No payroll this month.")
            else:
                n1099 = p["dollars_1099"] if "dollars_1099" in p else 0.0
                st.dataframe(
                    pd.DataFrame({
                        "Pay date": p["pay_date"].dt.strftime("%Y-%m-%d"),
                        "Gross wages": p["gross_wages"].round(2),
                        "1099": (n1099.round(2) if hasattr(n1099, "round") else n1099),
                        "Total earnings": p["total_earnings"].round(2),
                        "Per diem/exp": (p["total_earnings"] - p["gross_wages"] - n1099).round(2),
                    }),
                    hide_index=True, use_container_width=True,
                )
                st.caption(
                    f"Labor (wages+1099): {_money((p['gross_wages'] + n1099).sum())} · "
                    f"Total paid: {_money(p['total_earnings'].sum())}"
                )


def _render_weeks_panel(summary: dict, period_label: str) -> None:
    """Show which billing weeks were counted vs. excluded for the month."""
    ats_weeks = summary.get("ats_weeks") or []
    pay_weeks = summary.get("pay_weeks") or []
    if not ats_weeks and not pay_weeks:
        return

    inc = [w for w in ats_weeks if w["included"]]
    exc = [w for w in ats_weeks if not w["included"]]
    label_inc = ", ".join(w["week_ending"] for w in inc) or "none"
    with st.expander(
        f"📅 Weeks counted for {period_label} — "
        f"{len(inc)} included, {len(exc) + len([w for w in pay_weeks if not w['included']])} excluded",
        expanded=bool(exc) or bool([w for w in pay_weeks if not w["included"]]),
    ):
        st.caption("A week belongs to the month that contains its **Sunday**.")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Bullhorn (ATS) — billing weeks**")
            if ats_weeks:
                st.dataframe(
                    pd.DataFrame(ats_weeks).rename(
                        columns={"week_ending": "Week ending (Sun)", "rows": "Rows",
                                 "included": "Counted"}),
                    use_container_width=True, hide_index=True,
                )
                if summary.get("n_ats_rows_excluded"):
                    st.caption(
                        f"Excluded {summary['n_ats_rows_excluded']} ATS rows "
                        f"({_money(summary.get('ats_excluded_amount', 0))}) outside the month."
                    )
            else:
                st.caption("No billing-period dates found on the ATS side.")
        with c2:
            st.markdown("**Payroll — pay weeks**")
            if pay_weeks:
                st.dataframe(
                    pd.DataFrame(pay_weeks).rename(
                        columns={"week_ending": "Week ending (Sun)", "rows": "Checks",
                                 "included": "Counted"}),
                    use_container_width=True, hide_index=True,
                )
                if summary.get("n_pay_rows_excluded"):
                    st.caption(
                        f"Excluded {summary['n_pay_rows_excluded']} checks "
                        f"({_money(summary.get('pay_excluded_amount', 0))}) outside the month."
                    )
                if summary.get("n_adjustment_checks"):
                    st.caption(
                        f"ℹ️ {summary['n_adjustment_checks']} off-cycle "
                        "adjustment/additional check(s) included this month."
                    )
            else:
                st.caption("Payroll export had no pay dates — not week-filtered.")


def _style_detail(df: pd.DataFrame):
    def _row_color(row):
        color = CATEGORY_COLORS.get(row["category"], "")
        return [f"background-color: {color}"] * len(row)

    fmt = {c: "{:,.2f}" for c in ("ats_amount", "pay_amount", "difference", "pay_nonwage") if c in df}
    return df.style.apply(_row_color, axis=1).format(fmt)


# =============================================================================
# New reconciliation
# =============================================================================
def view_new(storage, user_email: str) -> None:
    st.header("New reconciliation")

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1.2])
        today = dt.date.today()
        with c1:
            entity = st.selectbox(
                "Business unit", list(rc.ENTITIES.keys()),
                format_func=lambda e: rc.ENTITIES[e]["label"],
                help="Sets the payroll cadence: CTS is weekly (month = week's "
                     "Sunday); OTS is bi-weekly on a 2-week delay (month = the pay "
                     "period's last Friday).",
            )
        with c2:
            month = st.selectbox(
                "Month", MONTHS,
                index=(today.month - 2) % 12,  # default to the prior month
            )
        with c3:
            year = st.number_input(
                "Year", min_value=2020, max_value=2100, value=today.year, step=1
            )
        with c4:
            threshold = st.number_input(
                "Variance threshold ($)",
                min_value=0.01, max_value=1000.0,
                value=rc.DEFAULT_THRESHOLD, step=0.25,
                help="Differences smaller than this are treated as rounding matches.",
            )
        _compare_opts = list(rc.COMPARE_AMOUNT_OPTIONS.keys())
        compare_amount = st.radio(
            "Payroll amount to compare against Bullhorn *Total Wages Paid*",
            options=_compare_opts,
            index=_compare_opts.index(rc.default_compare_for(entity)),
            horizontal=True,
            help="Gross Wages is hours-based wages only — a tighter match to Bullhorn "
                 "(default for OTS, which pays heavy per diem/expense). Total Earnings "
                 "also includes per diem / expense / 1099 (default for CTS). Per diem / "
                 "expense is shown separately either way.",
        )

    schedule = rc.schedule_for(entity)
    period = f"{month} {int(year)}"
    audit_id = f"{entity}-{int(year)}-{MONTHS.index(month) + 1:02d}"

    if storage.exists(audit_id):
        st.warning(
            f"An audit for **{period} ({entity})** already exists. "
            "Running and saving again will overwrite it."
        )

    period_ym = (int(year), MONTHS.index(month) + 1)
    ctx = dict(audit_id=audit_id, period=period, entity=entity, schedule=schedule,
               compare_amount=compare_amount, threshold=float(threshold),
               period_ym=period_ym, storage=storage)

    source_mode = st.radio(
        "Data source",
        ["Pull automatically", "Upload files"],
        horizontal=True,
        help="Pull queries Bullhorn from Snowflake and reads the payroll register "
             "from the configured SharePoint folder. Upload is the manual fallback.",
    )
    if source_mode == "Pull automatically":
        _run_pull(ctx)
    else:
        _run_upload(ctx)

    recon = st.session_state.get("recon")
    if recon and recon.get("audit_id") == audit_id:
        _render_results(storage, recon, user_email)


def _auto_note_explainable(detail, pay_frame, threshold) -> None:
    """For variances whose difference is fully accounted for by payroll-only pay
    types (PTO, sick, holiday, bonus…), pre-fill the Note with the breakdown so
    reviewers don't have to hunt. Leaves the row a Variance (still shown).

    Reads the per-candidate pay-type sums the engine attached to ``detail`` (the
    ``pt_*`` columns), so they line up exactly with each row's difference.
    """
    explainer_cols = (pay_frame.attrs.get("explainer_cols") if pay_frame is not None else None) or {}
    explainer_cols = {lbl: col for lbl, col in explainer_cols.items() if col in detail.columns}
    if not explainer_cols:
        return

    for idx, r in detail[detail["category"] == rc.CAT_VARIANCE].iterrows():
        comps = {
            lbl: float(r[col])
            for lbl, col in explainer_cols.items()
            if abs(float(r[col])) >= 0.005
        }
        total = sum(comps.values())
        # The difference (payroll − Bullhorn) should be positive and equal the
        # sum of these payroll-only amounts.
        if comps and r["difference"] > 0 and abs(r["difference"] - total) < threshold:
            detail.at[idx, "note"] = (
                "; ".join(f"{lbl} {_money(a)}" for lbl, a in comps.items())
                + " (payroll-only pay, not billed)"
            )
            if len(comps) == 1:
                cat = {"PTO Hourly": "PTO", "PTO Payout": "PTO"}.get(
                    next(iter(comps)), next(iter(comps)))
                if cat in REVIEW_CATEGORIES:
                    detail.at[idx, "review_category"] = cat


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Return the (year, month) ``delta`` months away from (year, month)."""
    m = (month - 1) + delta
    return year + m // 12, (m % 12) + 1


def _auto_note_prior_period(detail, ctx) -> None:
    """Flag a variance that is offset by an equal-and-opposite variance for the
    same contractor in an **adjacent saved month** — i.e. a fix that straddles two
    months (an error booked one month and corrected the next, so both months read
    as variances). Pre-fills the Note and sets the 'Prior period correction'
    category so reviewers don't chase a discrepancy that nets to zero across the
    boundary. Requires the adjacent month to already be saved in history.
    """
    storage = ctx.get("storage")
    if storage is None:
        return
    entity = ctx["entity"]
    year, month = ctx["period_ym"]
    threshold = float(ctx["threshold"])
    tol = max(1.0, threshold)

    # Pull per-contractor differences from the prior and next saved months.
    adjacents = []  # (direction, period_label, {id: difference})
    for delta, direction in ((-1, "prior"), (1, "next")):
        y, m = _shift_month(year, month, delta)
        aid = f"{entity}-{y}-{m:02d}"
        try:
            if not storage.exists(aid):
                continue
            rec = storage.get_audit(aid)
        except Exception:
            continue
        if rec is None or rec.detail is None or "difference" not in rec.detail.columns:
            continue
        diffs = {}
        for _, rr in rec.detail.iterrows():
            try:
                diffs[str(rr["id"])] = float(rr["difference"])
            except (TypeError, ValueError):
                continue
        adjacents.append((direction, rec.period, diffs))
    if not adjacents:
        return

    for idx, r in detail[detail["category"] == rc.CAT_VARIANCE].iterrows():
        if str(detail.at[idx, "note"]):          # keep any PTO/sick auto-note
            continue
        cur = float(r["difference"])
        if abs(cur) < threshold:
            continue
        rid = str(r["id"])
        for direction, period_label, diffs in adjacents:   # prior checked first
            adj = diffs.get(rid)
            if adj is None or abs(adj) < threshold:
                continue
            # Opposite sign and nets to ~zero across the month boundary.
            if cur * adj < 0 and abs(cur + adj) <= tol:
                if direction == "prior":
                    detail.at[idx, "note"] = (
                        f"Offsets {period_label} variance of {_money(adj)} — "
                        "correcting prior-period discrepancy (nets to ~$0)."
                    )
                else:
                    detail.at[idx, "note"] = (
                        f"Offset by {period_label} variance of {_money(adj)} — "
                        "discrepancy straddles into next period (nets to ~$0)."
                    )
                detail.at[idx, "review_category"] = "Prior period correction"
                break


def _finalize_recon(result, ctx: dict, source_meta: dict) -> None:
    """Stash a reconciliation result in session_state for review/save."""
    detail = result.detail.copy()
    detail["review_category"] = "Unreviewed"
    detail["note"] = ""
    detail.loc[detail["category"] == rc.CAT_CORP_CORP, "review_category"] = "Corp-Corp"
    _auto_note_explainable(detail, source_meta.get("pay_frame"), ctx["threshold"])
    _auto_note_prior_period(detail, ctx)
    recon = {
        "audit_id": ctx["audit_id"], "period": ctx["period"], "entity": ctx["entity"],
        "threshold": ctx["threshold"], "summary": result.summary,
        "warnings": list(result.warnings), "detail": detail,
        "period_ym": ctx["period_ym"], "schedule": ctx["schedule"],
    }
    recon.update(source_meta)
    st.session_state["recon"] = recon


def _run_upload(ctx: dict) -> None:
    up1, up2 = st.columns(2)
    with up1:
        ats_file = st.file_uploader(
            "Bullhorn export — ATS Hours", type=["xlsx", "xlsm", "csv"], key="ats_up"
        )
    with up2:
        pay_file = st.file_uploader(
            "Payroll export — Paycheck", type=["xlsx", "xlsm", "csv"], key="pay_up"
        )

    ats_sheet = pay_sheet = 0
    ats_bytes = pay_bytes = None
    if ats_file is not None:
        ats_bytes = ats_file.getvalue()
        sheets = _sheet_names(ats_bytes, ats_file.name)
        if sheets and len(sheets) > 1:
            with up1:
                ats_sheet = st.selectbox(
                    "ATS sheet", sheets,
                    index=_default_sheet(sheets, "ats", "hours"), key="ats_sheet",
                )
    if pay_file is not None:
        pay_bytes = pay_file.getvalue()
        sheets = _sheet_names(pay_bytes, pay_file.name)
        if sheets and len(sheets) > 1:
            with up2:
                pay_sheet = st.selectbox(
                    "Paycheck sheet", sheets,
                    index=_default_sheet(sheets, "check details", "paycheck", "check", "pay"),
                    key="pay_sheet",
                )

    if st.button("▶ Run reconciliation", type="primary",
                 disabled=(ats_bytes is None or pay_bytes is None)):
        try:
            ats = load_ats(_as_source(ats_bytes, ats_file.name),
                           sheet_name=ats_sheet, schedule=ctx["schedule"])
            pay = load_paycheck(_as_source(pay_bytes, pay_file.name),
                                sheet_name=pay_sheet,
                                compare_amount=ctx["compare_amount"],
                                schedule=ctx["schedule"])
            result = reconcile(ats, pay, threshold=ctx["threshold"], period=ctx["period_ym"])
        except ColumnNotFoundError as e:
            st.error(f"Could not read a required column: {e}")
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"Reconciliation failed: {e}")
            return
        _finalize_recon(result, ctx, {
            "raw_files": {"ats": ats_bytes, "payroll": pay_bytes},
            "provenance": {"source": "manual upload",
                           "files": {"ats": ats_file.name, "payroll": pay_file.name}},
            "ats_frame": ats, "pay_frame": pay, "c2c_lines": {},
        })


@st.cache_resource(show_spinner=False)
def _private_key_der(pem: str) -> bytes:
    """Parse a PEM private key once per process into the DER bytes the Snowflake
    connector wants. On Community Cloud there is no filesystem for a key file, so
    the key is pasted into Secrets as ``private_key_pem`` instead (same pattern as
    the Commission Console / invoice-reconciler apps)."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _snowflake_conn_params() -> dict | None:
    """Resolve plain snowflake-connector kwargs from secrets (for code paths that
    use the raw driver, e.g. the Snowflake storage backend). Supports both a key
    file on disk (local) and an inline PEM (``private_key_pem``, for Cloud).
    Returns None if no connection is configured."""
    cfg = dict((st.secrets.get("connections", {}) or {}).get("snowflake", {}))
    if not cfg:
        cfg = dict(st.secrets.get("snowflake", {}))
    if not cfg:
        return None
    pem = cfg.pop("private_key_pem", None)
    if pem:
        cfg["private_key"] = _private_key_der(pem)
    # Streamlit-only keys the connector would reject.
    cfg.pop("client_session_keep_alive", None)
    return cfg


def _snowflake_runner():
    """Return a (runner, label) using the configured Snowflake connection.

    Prefers Streamlit's native ``[connections.snowflake]`` (key-pair auth via
    ``private_key_file`` on disk, or an inline ``private_key_pem`` for Cloud);
    falls back to a plain ``[snowflake]`` block passed straight to
    snowflake-connector-python. Returns (None, None) if neither is configured.
    """
    has_conn = "connections" in st.secrets and "snowflake" in st.secrets["connections"]
    has_flat = "snowflake" in st.secrets
    if has_conn:
        pem = (st.secrets["connections"]["snowflake"] or {}).get("private_key_pem")

        if pem:
            # Cloud: go straight to snowflake-connector-python — the same raw
            # connector the storage backend uses successfully here — instead of
            # st.connection(), whose result path (Snowpark/Arrow) ignores our
            # JSON request. JSON result format keeps pyarrow out of the decode
            # ("Converting strings to null is not implemented" on constant cols).
            params = _snowflake_conn_params() or {}
            params["session_parameters"] = {
                **(params.get("session_parameters") or {}),
                "PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "JSON",
            }

            def runner(sql, _params):
                import snowflake.connector  # lazy
                conn = snowflake.connector.connect(**params)
                try:
                    cur = conn.cursor()
                    cur.execute(sql)
                    cols = [c[0] for c in cur.description]
                    data = cur.fetchall()
                    return data, cols
                finally:
                    conn.close()
            return runner, "connections.snowflake(raw/json)"

        # Local: st.connection with the on-disk private_key_file works as-is.
        def runner(sql, _params):
            conn = st.connection("snowflake")
            df = conn.query(sql, ttl=0)
            return list(df.itertuples(index=False, name=None)), list(df.columns)
        return runner, "connections.snowflake"
    if has_flat:
        params = dict(st.secrets["snowflake"])
        return (lambda sql, _p: snowflake_bullhorn._default_runner(sql, params)), "snowflake"
    return None, None


def _run_pull(ctx: dict) -> None:
    entity, schedule = ctx["entity"], ctx["schedule"]
    year, month = ctx["period_ym"]

    sf_runner, sf_label = _snowflake_runner()
    sf_configured = sf_runner is not None
    pay_root = (st.secrets.get("payroll", {}) or {}).get("root_dir")

    c1, c2 = st.columns(2)
    branch_note = snowflake_bullhorn.ENTITY_BRANCH.get(entity, entity)
    if entity == "CTS":
        branch_note += " (+ CTS-Weekly pay cycle)"
    c1.markdown(
        f"**Bullhorn → Snowflake** &nbsp; {'✅ configured' if sf_configured else '⚠️ not configured'}\n\n"
        f"`BULLHORN_MIRROR.PayableCharge` · Branch = {branch_note}"
    )
    if pay_root:
        c2.markdown("**Payroll → SharePoint** &nbsp; ✅")
        c2.code(pay_root, language=None)   # code = literal path, no markdown escaping
    else:
        c2.markdown("**Payroll → SharePoint** &nbsp; ⚠️ no folder set")
    if not sf_configured or not pay_root:
        missing = []
        if not sf_configured:
            missing.append("`[connections.snowflake]` connection")
        if not pay_root:
            missing.append("`[payroll] root_dir`")
        st.info(
            f"Add {' and '.join(missing)} to the app's secrets to enable "
            "automatic pull. Until then, use **Upload files**."
        )
        return

    if st.button("⏬ Pull & reconcile", type="primary"):
        try:
            overrides = dict(st.secrets.get("entity_overrides", {}) or {})
            with st.spinner("Querying Snowflake…"):
                ats, sf_prov = snowflake_bullhorn.fetch_ats(
                    entity, year, month, schedule, conn_params={}, runner=sf_runner,
                    overrides=overrides)
                fresh = snowflake_bullhorn.mirror_freshness(conn_params={}, runner=sf_runner)
                # Authoritative routing for the combined multi-company register:
                # each person's entity comes from their Bullhorn Branch.
                entity_map = snowflake_bullhorn.fetch_candidate_entity_map(
                    runner=sf_runner, overrides=overrides)
            with st.spinner("Reading payroll from SharePoint folder…"):
                pay, pay_prov = sharepoint_payroll.fetch_payroll(
                    entity, schedule, pay_root, compare_amount=ctx["compare_amount"],
                    entity_map=entity_map)
            # Corp-Corp cost from a C2C export (optional file in the folder).
            c2c_records, c2c_prov = qbo_c2c.fetch_c2c(entity, pay_root)
            c2c_paid, c2c_names, c2c_lines, unmapped = {}, {}, {}, []
            if c2c_records:
                need_map = any(r["candidate_id"] is None for r in c2c_records)
                pmap = (snowflake_bullhorn.fetch_placement_map(
                    entity, runner=sf_runner, overrides=overrides) if need_map else {})
                for r in c2c_records:
                    # If a row carries its own period, keep only the target month.
                    # Guard against an unusable date (a malformed QBO work date
                    # leaves NaT, whose month is <NA> and would crash the compare).
                    pe = r["period_end"]
                    if pe is not None and not pd.isna(pe):
                        if periods.ats_anchor_period(pe, schedule)[2] != ctx["period_ym"]:
                            continue
                    elif r["period_end"] is not None:
                        # Had a date but it couldn't be parsed -> can't place it in
                        # a month; skip rather than count it in every month.
                        unmapped.append(r.get("placement_id"))
                        continue
                    cid = r["candidate_id"] or pmap.get(r["placement_id"])
                    if cid:
                        # All C2C bill lines count as paid — Bullhorn bills these
                        # contractors all-in (labor + per diem / expense), so the
                        # full vendor cost is what reconciles against the charge.
                        c2c_paid[cid] = c2c_paid.get(cid, 0.0) + r["amount"]
                        c2c_names[cid] = r["name"]
                        c2c_lines.setdefault(cid, []).append(r)
                    else:
                        unmapped.append(r["placement_id"])
                c2c_prov["mapped_candidates"] = len(c2c_paid)
                c2c_prov["unmapped_placements"] = unmapped
            result = reconcile(ats, pay, threshold=ctx["threshold"],
                               period=ctx["period_ym"], c2c_paid=c2c_paid,
                               c2c_names=c2c_names)
        except ColumnNotFoundError as e:
            st.error(f"Could not read a required column: {e}")
            return
        except FileNotFoundError as e:
            st.error(str(e))
            return
        except ModuleNotFoundError:
            st.error("`snowflake-connector-python` isn't installed on this host. "
                     "Add it to requirements and redeploy.")
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"Pull failed: {e}")
            return

        # Freshness gate: warn if the mirror hasn't synced past the month.
        if fresh is not None:
            result.warnings.insert(0, f"Bullhorn mirror last synced {fresh:%Y-%m-%d %H:%M} UTC.")
            month_end = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
            if fresh.date() <= month_end:
                result.warnings.insert(1,
                    f"Mirror sync ({fresh:%Y-%m-%d}) is on/before month-end "
                    f"({month_end:%Y-%m-%d}) — data may be incomplete for {ctx['period']}.")

        # Corp-Corp / QuickBooks C2C checks.
        if c2c_prov.get("found"):
            plabel = c2c_prov.get("period_label")
            if plabel and plabel.lower() != ctx["period"].lower():
                result.warnings.append(
                    f"C2C file covers **{plabel}** but you're reconciling "
                    f"**{ctx['period']}** — check you dropped the right month's C2C export.")
            if unmapped:
                result.warnings.append(
                    f"{len(unmapped)} C2C bill placement(s) didn't map to a Bullhorn "
                    f"candidate for {entity}: {', '.join(unmapped)}. Their cost is not "
                    "in this reconciliation.")
        elif entity == "CTS":
            result.warnings.append(
                "No C2C file found — Corp-Corp contractors are excluded from the "
                "dollar comparison. Drop a 'CTS - C2C' QuickBooks export in the "
                "folder to include their cost.")

        raw_files = {
            "ats_pulled": ats.to_csv(index=False).encode(),
            "payroll_pulled": pay.to_csv(index=False).encode(),
        }
        _finalize_recon(result, ctx, {
            "raw_files": raw_files,
            "provenance": {"bullhorn": sf_prov, "payroll": pay_prov, "c2c": c2c_prov,
                           "mirror_synced": fresh.isoformat() if fresh else None},
            "ats_frame": ats, "pay_frame": pay, "c2c_lines": c2c_lines,
        })


def _render_results(storage, recon: dict, user_email: str) -> None:
    s = recon["summary"]
    detail: pd.DataFrame = recon["detail"]

    st.divider()
    st.subheader(f"Results — {recon['period']} ({recon['entity']})")

    if s.get("compare_amount"):
        st.caption(
            f"Comparing Bullhorn **Total Wages Paid** vs payroll **{s['compare_amount']}**."
        )
    for w in recon.get("warnings", []):
        st.warning(w)

    _render_weeks_panel(s, recon["period"])

    m = st.columns(6)
    m[0].metric("People", s["n_people"])
    m[1].metric("Clean matches", s["counts"][rc.CAT_MATCHED])
    m[2].metric("Exceptions", s["n_exceptions"])
    m[3].metric("Net variance", _money(s["net_variance"]),
                help="Payroll − Bullhorn wages across comparable people "
                     "(excludes Corp-Corp and unmatched).")
    m[4].metric("Largest variance", _money(s["largest_variance"]))
    m[5].metric("Per diem/exp (payroll)", _money(s.get("pay_nonwage_total", 0.0)),
                help="Payroll per diem + expense (Total Earnings − Gross Wages). "
                     "Not a wage variance — shown for visibility.")

    chips = "  ".join(
        f"**{cat}:** {s['counts'][cat]}" for cat in rc.CATEGORY_ORDER if s["counts"][cat]
    )
    st.caption(chips)

    # --- exceptions with annotation editor ----------------------------------
    exceptions = detail[detail["category"] != rc.CAT_MATCHED].copy()
    st.markdown("#### Exceptions to review")
    if exceptions.empty:
        st.success("No exceptions — every comparable person matched within tolerance.")
        edited = exceptions
    else:
        st.caption(
            "Edit **Review** and **Note** to explain each item "
            "(mirrors the old spreadsheet's comment column). Everything else is read-only."
        )
        edited = st.data_editor(
            exceptions,
            key="exc_editor",
            use_container_width=True,
            hide_index=True,
            column_order=[
                "id", "name", "category", "ats_amount", "pay_amount",
                "difference", "pay_nonwage", "review_category", "note", "client",
            ],
            disabled=[
                "id", "name", "category", "ats_amount", "pay_amount",
                "difference", "abs_difference", "pay_nonwage", "employee_type", "client",
            ],
            column_config={
                "id": "ID",
                "name": "Name",
                "category": "Category",
                "ats_amount": st.column_config.NumberColumn("Bullhorn wages $", format="%.2f"),
                "pay_amount": st.column_config.NumberColumn("Payroll wages $", format="%.2f"),
                "difference": st.column_config.NumberColumn("Diff (Pay−BH)", format="%.2f"),
                "pay_nonwage": st.column_config.NumberColumn("Per diem/exp $", format="%.2f"),
                "review_category": st.column_config.SelectboxColumn(
                    "Review", options=REVIEW_CATEGORIES, required=True, width="medium",
                ),
                "note": st.column_config.TextColumn("Note", width="large"),
                "client": "Client",
            },
        )

    with st.expander(f"Full detail — all {len(detail)} people"):
        st.dataframe(
            _style_detail(detail[[
                "id", "name", "category", "ats_amount", "pay_amount", "difference",
                "pay_nonwage",
            ]]),
            use_container_width=True, hide_index=True,
        )

    _render_difference_detail(recon)

    # --- downloads -----------------------------------------------------------
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button(
        "⬇ Exceptions (CSV)",
        (edited if not exceptions.empty else exceptions).to_csv(index=False),
        file_name=f"{recon['audit_id']}_exceptions.csv", mime="text/csv",
    )
    d2.download_button(
        "⬇ Full detail (CSV)",
        detail.to_csv(index=False),
        file_name=f"{recon['audit_id']}_detail.csv", mime="text/csv",
    )

    # --- save ----------------------------------------------------------------
    st.divider()
    unreviewed = 0
    if not exceptions.empty:
        unreviewed = int((edited["review_category"] == "Unreviewed").sum())
    if unreviewed:
        st.info(f"{unreviewed} exception(s) still marked *Unreviewed*. "
                "You can save now and finish annotating later, or review them first.")

    if st.button("💾 Save this reconciliation", type="primary"):
        # Merge the edited annotations back into the full detail frame.
        merged = detail.set_index("id")
        if not exceptions.empty:
            ann = edited.set_index("id")[["review_category", "note"]]
            merged.loc[ann.index, ["review_category", "note"]] = ann
        merged = merged.reset_index()

        record = AuditRecord(
            audit_id=recon["audit_id"],
            period=recon["period"],
            entity=recon["entity"],
            run_by=user_email,
            run_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            threshold=recon["threshold"],
            summary=recon["summary"],
            warnings=recon.get("warnings", []),
            provenance=recon.get("provenance", {}),
            detail=merged,
        )
        storage.save_audit(record, raw_files=recon.get("raw_files", {}))
        st.session_state.pop("recon", None)
        st.success(f"Saved **{record.period} ({record.entity})** to history.")
        st.balloons()


# =============================================================================
# History
# =============================================================================
def view_history(storage) -> None:
    st.header("Audit history")
    audits = storage.list_audits()
    if not audits:
        st.info("No saved reconciliations yet. Run one from **New reconciliation**.")
        return

    table = pd.DataFrame([
        {
            "Period": a["period"],
            "Unit": a["entity"],
            "Exceptions": a["summary"].get("n_exceptions"),
            "Net variance": _money(a["summary"].get("net_variance", 0.0)),
            "Threshold": _money(a.get("threshold", 0.0)),
            "Run by": a["run_by"],
            "Run at (UTC)": a["run_at"],
            "audit_id": a["audit_id"],
        }
        for a in audits
    ])
    st.dataframe(
        table.drop(columns=["audit_id"]), use_container_width=True, hide_index=True
    )

    ids = table["audit_id"].tolist()
    labels = [f"{r['Period']} ({r['Unit']})" for _, r in table.iterrows()]
    choice = st.selectbox(
        "Open a saved reconciliation",
        options=list(range(len(ids))),
        format_func=lambda i: labels[i],
    )
    rec = storage.get_audit(ids[choice])
    if rec is None:
        st.error("Could not load that audit.")
        return
    # Backfill columns added after older audits were saved.
    for _col, _default in (("pay_nonwage", 0.0), ("review_category", ""), ("note", "")):
        if _col not in rec.detail.columns:
            rec.detail[_col] = _default

    st.divider()
    st.subheader(f"{rec.period} ({rec.entity}) — read-only")
    s = rec.summary
    m = st.columns(6)
    m[0].metric("People", s["n_people"])
    m[1].metric("Clean matches", s["counts"][rc.CAT_MATCHED])
    m[2].metric("Exceptions", s["n_exceptions"])
    m[3].metric("Net variance", _money(s["net_variance"]))
    m[4].metric("Largest variance", _money(s["largest_variance"]))
    m[5].metric("Per diem/exp (payroll)", _money(s.get("pay_nonwage_total", 0.0)))
    basis = s.get("compare_amount")
    st.caption(
        f"Run by **{rec.run_by}** at {rec.run_at} · threshold {_money(rec.threshold)}"
        + (f" · compared vs payroll **{basis}**" if basis else "")
    )

    for w in rec.warnings:
        st.warning(w)
    _render_weeks_panel(rec.summary, rec.period)
    if rec.provenance:
        with st.expander("Provenance (how this audit was sourced)"):
            st.json(rec.provenance)

    exc = rec.detail[rec.detail["category"] != rc.CAT_MATCHED]
    st.markdown("#### Exceptions & annotations")
    st.dataframe(
        exc[[
            "id", "name", "category", "ats_amount", "pay_amount", "difference",
            "pay_nonwage", "review_category", "note",
        ]],
        use_container_width=True, hide_index=True,
    )
    with st.expander(f"Full detail — all {len(rec.detail)} people"):
        st.dataframe(
            _style_detail(rec.detail[[
                "id", "name", "category", "ats_amount", "pay_amount", "difference",
                "pay_nonwage",
            ]]),
            use_container_width=True, hide_index=True,
        )

    st.download_button(
        "⬇ Download this audit (CSV)",
        rec.detail.to_csv(index=False),
        file_name=f"{rec.audit_id}_audit.csv", mime="text/csv",
    )
    for label, fname in (rec.source_files or {}).items():
        data = storage.get_raw_file(rec.audit_id, label)
        if data:
            st.download_button(
                f"⬇ Source: {label}", data,
                file_name=f"{rec.audit_id}_{fname}",
                key=f"raw_{label}",
            )


# =============================================================================
# Main
# =============================================================================
def _get_storage():
    """Pick the storage backend from secrets.

    ``[storage] backend = "snowflake"`` keeps audit history in Snowflake — the
    durable choice for Community Cloud, whose disk is wiped on every redeploy.
    Defaults to the local-file store for on-machine use. Falls back to local with
    a warning if Snowflake storage is requested but not connectable.
    """
    kind = (st.secrets.get("storage", {}) or {}).get("backend", "local").lower()
    if kind == "snowflake":
        params = _snowflake_conn_params()
        schema = (st.secrets.get("storage", {}) or {}).get(
            "schema", "BULLHORN_MIRROR.ANALYTICS"
        )
        if params:
            return get_storage("snowflake", conn_params=params, schema=schema)
        st.warning(
            "Storage backend is set to **snowflake** but no Snowflake connection "
            "is configured — saved audits will NOT persist. Falling back to local.",
            icon="⚠️",
        )
    return get_storage("local")


def main() -> None:
    user_email = auth.require_login()
    auth.logout_button()
    storage = _get_storage()

    st.sidebar.title("🧾 Cost Reconciler")
    page = st.sidebar.radio("Go to", ["New reconciliation", "Audit history"])
    st.sidebar.divider()
    st.sidebar.caption(
        "Bullhorn Candidate ID is matched to payroll Employee Number. "
        "**CTS** is weekly (accounting month = the week's **Sunday**); **OTS** is "
        "bi-weekly on a 2-week delay (month = the pay period's **last Friday**). "
        "Per diem / expense is reported separately from wage variances."
    )

    if page == "New reconciliation":
        view_new(storage, user_email)
    else:
        view_history(storage)


if __name__ == "__main__":
    main()
