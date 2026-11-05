"""CTS accounting-month logic.

A CTS billing week is anchored to its **Sunday**: the week's accounting month is
the month that Sunday falls in. Different clients close their week on Fri, Sat,
or Sun, and payroll pays each week on the *following Friday*, so several dates
can describe the same week. Everything is normalized to that week's Sunday:

* **ATS side** — the billing-period-end date (Fri/Sat/Sun) snaps *forward* to the
  Sunday that ends its week.
* **Payroll side** — the pay date (the following Friday) snaps *back* 5 days to
  the same Sunday.

Both then map to the accounting month = that Sunday's month.
"""

from __future__ import annotations

import datetime as _dt

import pandas as pd

# --- CTS (weekly) ---
# Pay date (the Friday after the week) minus this many days = the week's Sunday.
PAY_DATE_TO_SUNDAY_OFFSET_DAYS = 5

# --- OTS (bi-weekly, two-week delay) ---
# A pay period is two weeks (Mon-Sun) ending on a Sunday; the accounting month is
# the month of the period's LAST FRIDAY (the Friday two days before that Sunday).
# Pay date is the Friday two weeks after the period ends, so pay_date - 14 lands
# exactly on that last Friday, and pay_date - 12 on the period-end Sunday.
BIWEEKLY_PAY_TO_LAST_FRIDAY_DAYS = 14
BIWEEKLY_PAY_TO_PERIOD_END_DAYS = 12
# Any known OTS period-end Sunday fixes the bi-weekly phase (every 14 days).
DEFAULT_BIWEEKLY_ANCHOR = pd.Timestamp("2026-08-02")

# Schedule identifiers used across the app.
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_BIWEEKLY = "biweekly"


def to_ts(value: object) -> pd.Timestamp:
    """Parse a cell into a normalized (midnight) Timestamp, or NaT."""
    if value is None:
        return pd.NaT
    # Guard NaT/NaN first — NaT is a subclass of datetime, so the isinstance
    # checks below would otherwise try to .normalize() it and blow up.
    try:
        if pd.isna(value):
            return pd.NaT
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    if isinstance(value, (_dt.datetime, _dt.date)):
        return pd.Timestamp(value).normalize()
    s = str(value).strip()
    if not s:
        return pd.NaT
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return pd.Timestamp(_dt.datetime.strptime(s, fmt)).normalize()
        except ValueError:
            continue
    t = pd.to_datetime(s, errors="coerce")
    return pd.NaT if pd.isna(t) else pd.Timestamp(t).normalize()


def week_ending_sunday(value: object) -> pd.Timestamp:
    """ATS billing-period-end date -> the Sunday that ends that week.

    Snaps forward: Fri (+2), Sat (+1), Sun (+0). weekday(): Mon=0 … Sun=6.
    """
    t = to_ts(value)
    if pd.isna(t):
        return pd.NaT
    return (t + pd.Timedelta(days=(6 - t.weekday()))).normalize()


def paydate_to_sunday(
    value: object, offset_days: int = PAY_DATE_TO_SUNDAY_OFFSET_DAYS
) -> pd.Timestamp:
    """Payroll pay date (the following Friday) -> that week's Sunday.

    Snaps back ``offset_days`` (5) days. For the handful of off-cycle Adjustment
    checks that aren't paid on a Friday, this still lands in the correct
    accounting month, which is all the month filter needs.
    """
    t = to_ts(value)
    if pd.isna(t):
        return pd.NaT
    return (t - pd.Timedelta(days=offset_days)).normalize()


def period_of(sunday: pd.Timestamp) -> tuple[int, int] | tuple[object, object]:
    """(year, month) of a date, or (NA, NA) if unknown."""
    if pd.isna(sunday):
        return (pd.NA, pd.NA)
    return (int(sunday.year), int(sunday.month))


def biweekly_period_end(
    sunday: pd.Timestamp, anchor: pd.Timestamp = DEFAULT_BIWEEKLY_ANCHOR
) -> pd.Timestamp:
    """Given any week's Sunday, return the OTS pay-period-end Sunday it belongs to.

    Periods are 14 days. A week-ending Sunday is either itself a period end (even
    number of weeks from the anchor) or one week inside a period that ends the
    following Sunday (odd).
    """
    if pd.isna(sunday):
        return pd.NaT
    weeks = round((sunday - anchor).days / 7)
    if weeks % 2 != 0:
        return (sunday + pd.Timedelta(days=7)).normalize()
    return sunday.normalize()


def ats_anchor_period(
    billing_end: object,
    schedule: str = SCHEDULE_WEEKLY,
    anchor: pd.Timestamp = DEFAULT_BIWEEKLY_ANCHOR,
) -> tuple[pd.Timestamp, pd.Timestamp, tuple]:
    """Map an ATS billing-period-end date to (display week, period-end, (yr, mo)).

    * display week — the actual WEEKLY billing week's Sunday (what the audit shows,
      so every billing week is visible even when payroll is bi-weekly).
    * period-end — the pay period this week rolls up to (used to match against
      payroll runs).
    * (year, month) — the accounting month.

    Weekly (CTS): display = period-end = the week's Sunday; month = that month.
    Bi-weekly (OTS): display = the weekly Sunday; period-end = the bi-weekly
    period-end Sunday; month = that period's LAST FRIDAY's month.
    """
    sunday = week_ending_sunday(billing_end)
    if pd.isna(sunday):
        return (pd.NaT, pd.NaT, (pd.NA, pd.NA))
    if schedule == SCHEDULE_BIWEEKLY:
        pend = biweekly_period_end(sunday, anchor)
        last_fri = pend - pd.Timedelta(days=2)
        return (sunday, pend, (int(last_fri.year), int(last_fri.month)))
    return (sunday, sunday, (int(sunday.year), int(sunday.month)))


def pay_anchor_period(
    pay_date: object,
    schedule: str = SCHEDULE_WEEKLY,
    anchor: pd.Timestamp = DEFAULT_BIWEEKLY_ANCHOR,
) -> tuple[pd.Timestamp, pd.Timestamp, tuple]:
    """Map a payroll pay date to (display anchor, period-end, (year, month)).

    Payroll pays once per period, so display anchor == period-end.
    Weekly (CTS): pay Friday - 5 = the week's Sunday; month = that Sunday's month.
    Bi-weekly (OTS): pay Friday - 14 = the period's last Friday (month source);
    period-end Sunday = pay Friday - 12.
    """
    t = to_ts(pay_date)
    if pd.isna(t):
        return (pd.NaT, pd.NaT, (pd.NA, pd.NA))
    if schedule == SCHEDULE_BIWEEKLY:
        last_fri = (t - pd.Timedelta(days=BIWEEKLY_PAY_TO_LAST_FRIDAY_DAYS)).normalize()
        pend = (t - pd.Timedelta(days=BIWEEKLY_PAY_TO_PERIOD_END_DAYS)).normalize()
        return (pend, pend, (int(last_fri.year), int(last_fri.month)))
    sunday = (t - pd.Timedelta(days=PAY_DATE_TO_SUNDAY_OFFSET_DAYS)).normalize()
    return (sunday, sunday, (int(sunday.year), int(sunday.month)))
