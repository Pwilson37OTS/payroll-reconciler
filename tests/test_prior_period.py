"""Tests for the cross-month 'prior period correction' auto-flag in app.py."""

from __future__ import annotations

import pandas as pd

import app
from reconciler import config as rc
from storage.base import AuditRecord


class _FakeStorage:
    def __init__(self, records):
        self._r = records                      # {audit_id: AuditRecord}

    def exists(self, aid):
        return aid in self._r

    def get_audit(self, aid):
        return self._r.get(aid)


def _record(audit_id, period, rows):
    """rows: list of (id, difference, category)."""
    detail = pd.DataFrame(
        [{"id": i, "difference": d, "category": c} for i, d, c in rows]
    )
    return AuditRecord(
        audit_id=audit_id, period=period, entity="CTS", run_by="t", run_at="t",
        threshold=1.0, summary={}, detail=detail,
    )


def _current(rows):
    """rows: list of (id, difference, category). Returns a detail frame with the
    annotation columns the finalizer adds."""
    df = pd.DataFrame(
        [{"id": i, "difference": d, "category": c} for i, d, c in rows]
    )
    df["note"] = ""
    df["review_category"] = "Unreviewed"
    return df


def test_flags_offsetting_prior_month_variance():
    prior = _record("CTS-2026-07", "July 2026", [
        ("181049", -532.50, rc.CAT_VARIANCE),      # Ali: over-billed in July
        ("999", 40.0, rc.CAT_VARIANCE),            # unrelated
    ])
    ctx = {"entity": "CTS", "period_ym": (2026, 8), "threshold": 1.0,
           "storage": _FakeStorage({"CTS-2026-07": prior})}
    detail = _current([
        ("181049", 532.50, rc.CAT_VARIANCE),       # Ali: corrected in August
        ("777", 300.0, rc.CAT_VARIANCE),           # genuine, no offset
        ("555", 0.0, rc.CAT_MATCHED),
    ])

    app._auto_note_prior_period(detail, ctx)

    ali = detail[detail["id"] == "181049"].iloc[0]
    assert ali["review_category"] == "Prior period correction"
    assert "July 2026" in ali["note"] and "prior-period" in ali["note"].lower()
    # A variance with no offsetting match is left for review.
    assert detail[detail["id"] == "777"].iloc[0]["review_category"] == "Unreviewed"


def test_flags_offset_in_next_month():
    nxt = _record("CTS-2026-08", "August 2026", [("181049", 532.50, rc.CAT_VARIANCE)])
    ctx = {"entity": "CTS", "period_ym": (2026, 7), "threshold": 1.0,
           "storage": _FakeStorage({"CTS-2026-08": nxt})}
    detail = _current([("181049", -532.50, rc.CAT_VARIANCE)])

    app._auto_note_prior_period(detail, ctx)
    row = detail.iloc[0]
    assert row["review_category"] == "Prior period correction"
    assert "August 2026" in row["note"] and "next period" in row["note"].lower()


def test_same_sign_or_mismatch_not_flagged():
    prior = _record("CTS-2026-07", "July 2026", [
        ("A", 532.50, rc.CAT_VARIANCE),            # same sign -> not an offset
        ("B", -100.0, rc.CAT_VARIANCE),            # opposite but wrong magnitude
    ])
    ctx = {"entity": "CTS", "period_ym": (2026, 8), "threshold": 1.0,
           "storage": _FakeStorage({"CTS-2026-07": prior})}
    detail = _current([
        ("A", 532.50, rc.CAT_VARIANCE),
        ("B", 532.50, rc.CAT_VARIANCE),
    ])
    app._auto_note_prior_period(detail, ctx)
    assert (detail["review_category"] == "Unreviewed").all()


def test_does_not_overwrite_existing_note():
    prior = _record("CTS-2026-07", "July 2026", [("X", -532.50, rc.CAT_VARIANCE)])
    ctx = {"entity": "CTS", "period_ym": (2026, 8), "threshold": 1.0,
           "storage": _FakeStorage({"CTS-2026-07": prior})}
    detail = _current([("X", 532.50, rc.CAT_VARIANCE)])
    detail.loc[0, "note"] = "Sick $532.50 (payroll-only pay, not billed)"
    detail.loc[0, "review_category"] = "Sick"
    app._auto_note_prior_period(detail, ctx)
    assert detail.iloc[0]["review_category"] == "Sick"       # PTO/sick note preserved


def test_no_adjacent_saved_is_noop():
    ctx = {"entity": "CTS", "period_ym": (2026, 8), "threshold": 1.0,
           "storage": _FakeStorage({})}
    detail = _current([("X", 532.50, rc.CAT_VARIANCE)])
    app._auto_note_prior_period(detail, ctx)
    assert detail.iloc[0]["review_category"] == "Unreviewed"
