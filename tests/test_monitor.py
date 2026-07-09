"""Tests for monitor.py — pure logic, merge rules, rendering semantics.

No network, no Keychain, no real ~/.claude: filesystem-touching pieces are
pointed at tmp_path via monkeypatch.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

import monitor as m

NOW = 1_800_000_000.0  # fixed epoch for deterministic tests


def dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def make_console() -> Console:
    return Console(record=True, width=90, force_terminal=True)


API_PAYLOAD = {
    "five_hour": {"utilization": 15.0, "resets_at": "2026-07-02T18:10:00+00:00"},
    "seven_day": {"utilization": 14.0, "resets_at": "2026-07-04T01:00:00+00:00"},
    "extra_usage": {"is_enabled": False, "used_credits": 0.0, "monthly_limit": None, "utilization": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 15, "severity": "normal",
         "resets_at": "2026-07-02T18:10:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 14, "severity": "normal",
         "resets_at": "2026-07-04T01:00:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 27, "severity": "normal",
         "resets_at": "2026-07-04T01:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": True},
    ],
    "spend": {"enabled": False},
}


# ------------------------------------------------------------------ parsing


def test_parse_limits_from_limits_array():
    limits = m.parse_limits(API_PAYLOAD)
    assert [l.kind for l in limits] == ["session", "weekly_all", "weekly_scoped"]
    assert limits[0].label == "Session · 5h window"
    assert limits[2].label == "Week · Fable"
    assert limits[2].is_active and not limits[0].is_active
    assert limits[0].percent == 15.0
    assert limits[0].resets_at == dt(1783015800)  # 2026-07-02T18:10:00Z


def test_parse_limits_fallback_shape():
    payload = {"five_hour": {"utilization": 12.0, "resets_at": "2026-07-02T18:10:00+00:00"},
               "seven_day": {"utilization": 13.0, "resets_at": None}}
    limits = m.parse_limits(payload)
    assert [l.kind for l in limits] == ["five_hour", "seven_day"]
    assert limits[0].percent == 12.0
    assert limits[1].resets_at is None


def test_parse_limits_unknown_kind_last():
    payload = {"limits": [{"kind": "mystery_new", "percent": 5, "resets_at": None},
                          {"kind": "session", "percent": 1, "resets_at": None}]}
    limits = m.parse_limits(payload)
    assert limits[0].kind == "session"
    assert limits[-1].kind == "mystery_new"
    assert limits[-1].label == "Mystery New"


def test_parse_limits_tolerates_garbage_entries_and_scope():
    payload = {"limits": [
        "not-a-dict",
        {"kind": "weekly_scoped", "percent": 5, "scope": "bad-shape"},
        {"kind": "weekly_scoped", "percent": 6, "scope": {"model": "bad-model", "surface": "api"}},
    ]}
    limits = m.parse_limits(payload)
    assert [l.label for l in limits] == ["Week · scoped", "Week · api"]


def test_parse_dt_variants():
    assert m.parse_dt("2026-07-02T18:10:00Z") == dt(1783015800)
    assert m.parse_dt(1783015800) == dt(1783015800)
    assert m.parse_dt(None) is None
    assert m.parse_dt("") is None
    assert m.parse_dt("not-a-date") is None


def test_parse_credits_disabled_and_enabled():
    assert m.parse_credits(API_PAYLOAD).enabled is False
    # canonical shape: spend.used is a money object in minor units (14179 = $141.79)
    payload = {"spend": {"enabled": True,
                         "used": {"amount_minor": 14179, "currency": "USD", "exponent": 2}}}
    credits = m.parse_credits(payload)
    assert credits.enabled and credits.used == 141.79 and credits.currency == "USD"


def test_parse_credits_extra_usage_minor_units():
    # extra_usage.used_credits/monthly_limit are minor units scaled by decimal_places
    payload = {"extra_usage": {"is_enabled": True, "used_credits": 1250.0,
                               "monthly_limit": 5000, "decimal_places": 2}}
    credits = m.parse_credits(payload)
    assert credits.enabled and credits.used == 12.5 and credits.limit == 50.0
    assert credits.available == 37.5  # 50 cap - 12.5 used


def test_parse_credits_spend_beats_extra_usage():
    # both blocks present: spend (canonical) wins
    payload = {"spend": {"enabled": True,
                         "used": {"amount_minor": 999, "exponent": 2, "currency": "USD"}},
               "extra_usage": {"is_enabled": True, "used_credits": 4200.0, "decimal_places": 2}}
    assert m.parse_credits(payload).used == 9.99


def test_parse_credits_available_prefers_balance():
    payload = {"spend": {"enabled": True,
                         "used": {"amount_minor": 1000, "exponent": 2},
                         "balance": {"amount_minor": 2500, "exponent": 2}}}
    credits = m.parse_credits(payload)
    assert credits.balance == 25.0 and credits.available == 25.0  # prepaid balance, not cap-used


def test_parse_credits_uncapped_has_no_available():
    # the real Empractica payload: enabled, used known, every cap/balance null
    payload = {"extra_usage": {"is_enabled": True, "used_credits": 14179.0, "monthly_limit": None,
                               "decimal_places": 2, "currency": "USD"},
               "spend": {"enabled": True, "used": {"amount_minor": 14179, "exponent": 2, "currency": "USD"},
                         "limit": None, "cap": None, "balance": None}}
    credits = m.parse_credits(payload)
    assert credits.used == 141.79 and credits.available is None


# ------------------------------------------------------------------ formatting


def test_fmt_delta():
    now = dt(NOW)
    assert m.fmt_delta(None, now) == "—"
    assert m.fmt_delta(dt(NOW - 5), now) == "now"
    assert m.fmt_delta(dt(NOW + 45), now) == "45s"
    assert m.fmt_delta(dt(NOW + 125), now) == "2m 05s"
    assert m.fmt_delta(dt(NOW + 2 * 3600 + 300), now) == "2h 05m"
    assert m.fmt_delta(dt(NOW + 26 * 3600), now) == "1d 2h"


def test_fmt_ago():
    now = dt(NOW)
    assert m.fmt_ago(dt(NOW - 2), now) == "just now"
    assert m.fmt_ago(dt(NOW - 30), now) == "30s ago"
    assert m.fmt_ago(dt(NOW - 200), now) == "3m ago"


def test_colors():
    assert m.pct_color(10) == "#8fbf6f"
    assert m.pct_color(60) == "#d9a765"
    assert m.pct_color(80) == "#d97757"
    assert m.pct_color(95) == "#e05252"
    warn = m.Limit("session", "s", 10.0, None, severity="warning")
    assert m.util_color(warn) == m.SEVERITY_STYLES["warning"]  # severity beats percent
    assert m.util_color(m.Limit("session", "s", None, None)) == m.DIM


def test_usage_bar():
    assert m.usage_bar(100.0, "red", 20).plain == "█" * 20
    assert m.usage_bar(150.0, "red", 20).plain == "█" * 20  # clamped
    assert m.usage_bar(None, "red", 20).plain == "░" * 20
    half = m.usage_bar(50.0, "red", 20).plain
    assert half.startswith("█" * 10) and len(half) == 20


# ------------------------------------------------------------------ pace


def test_compute_pace_steady_climb():
    samples = [(NOW - 1800 + i * 60, 20 + i / 3) for i in range(31)]  # +10% in 30m
    rate = m.compute_pace(samples, NOW)
    assert rate is not None and abs(rate - 20) < 0.5


def test_compute_pace_restarts_after_reset():
    samples = [(NOW - 2400 + i * 60, 85 + i) for i in range(7)]
    samples += [(NOW - 900 + i * 60, 2 + i * 0.4) for i in range(16)]  # reset, +24%/h
    rate = m.compute_pace(samples, NOW)
    assert rate is not None and 20 < rate < 28


def test_compute_pace_insufficient_data():
    assert m.compute_pace(None, NOW) is None
    assert m.compute_pace([], NOW) is None
    assert m.compute_pace([(NOW - 60, 10), (NOW, 11)], NOW) is None       # too few
    assert m.compute_pace([(NOW - 100, 10), (NOW - 50, 11), (NOW, 12)], NOW) is None  # span < 5m


def test_pace_line_projection():
    lim = m.Limit("session", "Session · 5h window", 47.0, dt(NOW) + timedelta(hours=1))
    now = dt(NOW)
    assert "67% at reset" in m.pace_line(lim, 20.0, now).plain
    hits = m.pace_line(lim, 60.0, now).plain
    assert "hits 100%" in hits and "before reset" in hits
    assert "steady" in m.pace_line(lim, 0.05, now).plain
    assert m.pace_line(lim, None, now) is None
    past = m.Limit("session", "s", 47.0, dt(NOW) - timedelta(minutes=1))
    assert "at reset" not in m.pace_line(past, 20.0, now).plain  # no projection past reset


# ------------------------------------------------------------------ events


def test_detect_events_threshold_single_fire_highest():
    prev = [m.Limit("session", "Session · 5h window", 78.0, None)]
    new = [m.Limit("session", "Session · 5h window", 96.0, None)]
    events = m.detect_events(prev, new)
    assert events == ["Session · 5h window at 96%"]  # one event, not one per threshold


def test_detect_events_reset_and_limiting_move():
    prev = [m.Limit("session", "Session · 5h window", 47.0, None, is_active=False),
            m.Limit("weekly_scoped", "Week · Fable", 38.0, None, is_active=True)]
    new = [m.Limit("session", "Session · 5h window", 2.0, None, is_active=True),
           m.Limit("weekly_scoped", "Week · Fable", 38.0, None, is_active=False)]
    events = m.detect_events(prev, new)
    assert "Session · 5h window reset — now 2%" in events
    assert any(e.startswith("Now limiting: Session") for e in events)


def test_detect_events_limiting_move_between_scoped_models():
    prev = [m.Limit("weekly_scoped", "Week · Fable", 98.0, None, is_active=True),
            m.Limit("weekly_scoped", "Week · Opus", 40.0, None, is_active=False)]
    new = [m.Limit("weekly_scoped", "Week · Fable", 98.0, None, is_active=False),
           m.Limit("weekly_scoped", "Week · Opus", 100.0, None, is_active=True)]
    assert "Now limiting: Week · Opus (100%)" in m.detect_events(prev, new)


def test_detect_events_steady_state_silent():
    prev = [m.Limit("session", "s", 47.0, None, is_active=True)]
    new = [m.Limit("session", "s", 48.0, None, is_active=True)]
    assert m.detect_events(prev, new) == []


def test_detect_events_ignores_none_percent():
    prev = [m.Limit("session", "s", None, None)]
    new = [m.Limit("session", "s", 90.0, None)]
    assert m.detect_events(prev, new) == []


# ------------------------------------------------------------------ feed


SOON = time.time() + 3600        # a live 5h window
LATER = time.time() + 3 * 86400  # a live weekly window
DEAD = time.time() - 600         # a window that has already reset


def write_feed_file(feed_dir, name, rate_limits, age=0.0, captured_at=None, account_uuid=None):
    path = feed_dir / f"{name}.json"
    body = {"captured_at": captured_at or (time.time() - age),
            "session_id": name, "rate_limits": rate_limits}
    if account_uuid is not None:
        body["account_uuid"] = account_uuid
    path.write_text(json.dumps(body))
    if age:
        mtime = time.time() - age
        os.utime(path, (mtime, mtime))
    return path


def test_read_feed_staleness_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    # two fresh sessions on the current window, one fresh-but-stale-data, one old file
    write_feed_file(tmp_path, "a", {"five_hour": {"used_percentage": 23, "resets_at": SOON},
                                    "seven_day": {"used_percentage": 24, "resets_at": LATER}})
    write_feed_file(tmp_path, "b", {"five_hour": {"used_percentage": 22, "resets_at": SOON},
                                    "seven_day": {"used_percentage": 11, "resets_at": LATER}})
    write_feed_file(tmp_path, "c", {"five_hour": {"used_percentage": 93, "resets_at": SOON - 1800}})  # old window
    write_feed_file(tmp_path, "d", {"five_hour": {"used_percentage": 99, "resets_at": SOON}},
                    age=m.FEED_MAX_AGE + 30)  # too old a file: ignored entirely
    result = m.read_feed()
    assert result is not None
    feed, as_of = result
    assert feed["session"] == (23.0, SOON)      # newest window, max pct; 93 and 99 ignored
    assert feed["weekly_all"] == (24.0, LATER)  # stale 11% loses to 24%
    assert as_of > 0


def test_read_feed_drops_windows_that_already_reset(tmp_path, monkeypatch):
    """A session idle for days re-renders its statusline with the numbers it last
    saw, stamped captured_at=now. The window they describe is long gone."""
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    write_feed_file(tmp_path, "idle", {"five_hour": {"used_percentage": 100, "resets_at": DEAD},
                                       "seven_day": {"used_percentage": 44, "resets_at": DEAD}})
    assert m.read_feed() is None
    # ...but a dead 5h window doesn't discredit a still-live weekly one
    write_feed_file(tmp_path, "idle", {"five_hour": {"used_percentage": 100, "resets_at": DEAD},
                                       "seven_day": {"used_percentage": 44, "resets_at": LATER}})
    feed, _ = m.read_feed()
    assert "session" not in feed and feed["weekly_all"] == (44.0, LATER)


def test_read_feed_scopes_to_the_live_login(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    write_feed_file(tmp_path, "mine", {"five_hour": {"used_percentage": 30, "resets_at": SOON}},
                    account_uuid="uuid-a")
    write_feed_file(tmp_path, "theirs", {"five_hour": {"used_percentage": 99, "resets_at": SOON + 7200}},
                    account_uuid="uuid-b")
    feed, _ = m.read_feed("uuid-a")
    assert feed["session"] == (30.0, SOON)          # uuid-b's later window never considered
    # an unstamped file is untrusted once we know which login is live
    write_feed_file(tmp_path, "legacy", {"five_hour": {"used_percentage": 88, "resets_at": SOON + 7200}})
    feed, _ = m.read_feed("uuid-a")
    assert feed["session"] == (30.0, SOON)
    # ...but with no live login known, everything counts (startup, before the first poll)
    feed, _ = m.read_feed()
    assert feed["session"][0] == 99.0


def test_read_feed_drops_entries_from_another_weekly_anchor(tmp_path, monkeypatch):
    """The regression: a session predating a /login still reports the OLD account's
    windows, and the dispatcher stamps it with the NEW account's uuid. The weekly
    reset is a per-account anchor, so it gives the entry away."""
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    write_feed_file(tmp_path, "stale-login",
                    {"five_hour": {"used_percentage": 96, "resets_at": SOON},
                     "seven_day": {"used_percentage": 12, "resets_at": LATER + 5 * 86400}},
                    account_uuid="uuid-a")
    write_feed_file(tmp_path, "current",
                    {"five_hour": {"used_percentage": 41, "resets_at": SOON},
                     "seven_day": {"used_percentage": 100, "resets_at": LATER}},
                    account_uuid="uuid-a")
    feed, _ = m.read_feed("uuid-a", weekly_reset=LATER)
    assert feed["weekly_all"] == (100.0, LATER)  # not the other account's 12%
    assert feed["session"] == (41.0, SOON)       # and its 5h reading is discarded wholesale


def test_read_feed_garbage_tolerant(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "null.json").write_text(json.dumps({"session_id": "x", "rate_limits": None}))
    (tmp_path / "list.json").write_text(json.dumps([1, 2, 3]))
    assert m.read_feed() is None
    monkeypatch.setattr(m, "FEED_DIR", tmp_path / "does-not-exist")
    assert m.read_feed() is None


def test_read_feed_uses_captured_at_for_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    write_feed_file(tmp_path, "old-capture",
                    {"five_hour": {"used_percentage": 99, "resets_at": SOON}},
                    captured_at=time.time() - m.FEED_MAX_AGE - 30)
    write_feed_file(tmp_path, "fresh-capture",
                    {"five_hour": {"used_percentage": 21, "resets_at": SOON}})
    result = m.read_feed()
    assert result is not None
    feed, _ = result
    assert feed["session"] == (21.0, SOON)


def test_merge_limits_rules():
    api = [m.Limit("session", "Session · 5h window", 45.0, dt(1_000_000), is_active=True),
           m.Limit("weekly_scoped", "Week · Fable", 38.0, dt(2_000_000))]
    # same window: max wins, flags preserved
    out = m.merge_limits(api, {"session": (47.0, 1_000_000.0)})
    ses = next(l for l in out if l.kind == "session")
    assert ses.percent == 47.0 and ses.is_active
    # same window, feed lower: API kept (monotonic)
    out = m.merge_limits(api, {"session": (44.0, 1_000_000.0)})
    assert next(l for l in out if l.kind == "session").percent == 45.0
    # newer window: feed replaces
    out = m.merge_limits(api, {"session": (2.0, 1_020_000.0)})
    ses = next(l for l in out if l.kind == "session")
    assert ses.percent == 2.0 and ses.resets_at == dt(1_020_000)
    # stale window: ignored
    out = m.merge_limits(api, {"session": (93.0, 980_000.0)})
    assert next(l for l in out if l.kind == "session").percent == 45.0
    # scoped limit untouched; no API -> synthesized
    assert next(l for l in out if l.kind == "weekly_scoped").percent == 38.0
    out = m.merge_limits([], {"session": (23.0, 1_000_000.0)})
    assert out[0].kind == "session" and out[0].label == "Session · 5h window"
    # merge must not mutate the API snapshot
    assert api[0].percent == 45.0


def test_merge_limits_never_adopts_a_foreign_window():
    """The 100%-week-reads-12% regression: the API says the weekly window is
    maxed and still live; a feed entry claims a window resetting five days
    later. A later reset is not evidence of a newer window — only of another
    login. The API's live window wins."""
    now = 1_000_000.0
    api = [m.Limit("weekly_all", "Week · all models", 100.0, dt(now + 86_400), is_active=True)]
    out = m.merge_limits(api, {"weekly_all": (12.0, now + 6 * 86_400)}, now_ts=now)
    wk = next(l for l in out if l.kind == "weekly_all")
    assert wk.percent == 100.0 and wk.resets_at == dt(now + 86_400)
    # once the API's own window has expired, the successor is real: adopt it
    api = [m.Limit("weekly_all", "Week · all models", 100.0, dt(now - 60), is_active=True)]
    out = m.merge_limits(api, {"weekly_all": (12.0, now + 6 * 86_400)}, now_ts=now)
    wk = next(l for l in out if l.kind == "weekly_all")
    assert wk.percent == 12.0 and wk.resets_at == dt(now + 6 * 86_400)


def test_window_is_current():
    now = 1_000_000.0
    assert m.window_is_current(now + 100, None, now)          # no API view yet
    assert m.window_is_current(now + 100, 0.0, now)           # ...nor a usable one
    assert m.window_is_current(now + 3600, now + 3660, now)   # same instance, within tolerance
    assert m.window_is_current(now + 3600, now - 10, now)     # successor of an expired window
    assert not m.window_is_current(now + 3600, now + 600, now)   # API window still live
    assert not m.window_is_current(now - 3600, now + 600, now)   # an older window entirely
    # a window about to reset is still live: no slack that would let a foreign
    # (further-future) window take it over in its last seconds
    assert not m.window_is_current(now + 5 * 86400, now + 30, now)


def test_weekly_reset_of():
    assert m.weekly_reset_of([]) is None
    assert m.weekly_reset_of([m.Limit("weekly_all", "w", 5.0, None)]) is None
    assert m.weekly_reset_of([m.Limit("session", "s", 5.0, dt(1_000_000)),
                              m.Limit("seven_day", "w", 5.0, dt(2_000_000))]) == 2_000_000.0


def test_recompute_events_and_history():
    state = m.State()
    state.api_limits = [m.Limit("session", "Session · 5h window", 78.0, dt(NOW))]
    assert m.recompute(state) == []  # first pass: baseline, no events
    state.feed = {"session": (81.0, NOW)}
    assert m.recompute(state) == ["Session · 5h window at 81%"]
    assert m.recompute(state) == []  # same data: no duplicate event
    assert [p for _, p in state.history["session"]] == [78.0, 81.0]  # dedupe of repeats
    # a change within 30s still appends
    state.feed = {"session": (82.0, NOW)}
    m.recompute(state)
    assert state.history["session"][-1][1] == 82.0


def test_recompute_scoped_history_uses_label_identity(monkeypatch):
    monkeypatch.setattr(m.time, "time", lambda: NOW)
    state = m.State()
    state.api_limits = [
        m.Limit("weekly_scoped", "Week · Fable", 10.0, dt(NOW + 86400)),
        m.Limit("weekly_scoped", "Week · Opus", 70.0, dt(NOW + 86400)),
    ]
    m.recompute(state)
    assert state.history["weekly_scoped:Week · Fable"] == [(NOW, 10.0)]
    assert state.history["weekly_scoped:Week · Opus"] == [(NOW, 70.0)]


# ------------------------------------------------------------------ sparkline


def test_sparkline_window_anchoring():
    end = NOW + 3600
    start = end - m.WINDOW_LEN
    samples = [(start - 600 + i * 60, 95.0) for i in range(10)]           # previous window
    samples += [(start + 300 + i * 60, 3 + i * 0.2) for i in range(220)]  # this one: 3 -> 47%
    g = m.render_sparkline(samples, 60, NOW, end)
    assert g is not None
    top, mid, bot = (r.plain for r in g.renderables[1:])
    assert "█" not in top  # 95% samples from the old window excluded
    future = int(60 * (end - NOW) / m.WINDOW_LEN) - 1
    for r in (top, mid, bot):
        assert r[-future:].strip() == ""  # unelapsed window is blank


def test_sparkline_gap_dots_and_header():
    end = NOW + 3600
    start = end - m.WINDOW_LEN
    samples = [(start + i * 60, 10.0) for i in range(60)]
    samples += [(start + 10800 + i * 60, 30.0) for i in range(60)]  # 2h gap
    g = m.render_sparkline(samples, 60, NOW, end)
    assert "·" in g.renderables[3].plain            # gap dots in bottom row
    head = g.renderables[0].plain
    assert "→" in head and "0–100%" in head


def test_sparkline_hidden_cases():
    assert m.render_sparkline([(NOW, 5.0), (NOW - 60, 4.0)], 60, NOW, None) is None
    assert m.render_sparkline([(NOW, 5.0)], 60, NOW, NOW + 3600) is None
    # fresh window with two quick samples shows immediately
    g = m.render_sparkline([(NOW - 20, 2.0), (NOW - 10, 2.5)], 60, NOW, NOW + m.WINDOW_LEN - 30)
    assert g is not None


# ------------------------------------------------------------------ persistence


def test_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "HISTORY_FILE", tmp_path / "hist.json")
    state = m.State()
    old = time.time() - m.HISTORY_SPAN - 100
    state.history = {"session": [(old, 5.0), (time.time(), 30.0)]}
    m.save_history(state)
    loaded = m.State()
    m.load_history(loaded)
    assert [p for _, p in loaded.history["session"]] == [30.0]  # stale sample dropped


def test_load_history_garbage(tmp_path, monkeypatch):
    path = tmp_path / "hist.json"
    monkeypatch.setattr(m, "HISTORY_FILE", path)
    state = m.State()
    m.load_history(state)  # missing file: no-op
    path.write_text("{broken")
    m.load_history(state)
    path.write_text(json.dumps({"session": "not-a-list"}))
    m.load_history(state)
    assert state.history == {}


# ------------------------------------------------------------------ loops


def test_poll_loop_success_path(monkeypatch):
    calls = {"n": 0}
    notes = []

    def fake_fetch(token):
        calls["n"] += 1
        payload = json.loads(json.dumps(API_PAYLOAD))
        payload["limits"][0]["percent"] = [78, 96, 3][min(calls["n"] - 1, 2)]
        return payload

    monkeypatch.setattr(m, "fetch_usage", fake_fetch)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok", "max"))
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-live")
    monkeypatch.setattr(m, "send_notification", notes.append)

    state = m.State()
    stop = threading.Event()

    def wait(t=None):  # poll_loop sleeps on poll_wake so a switch can cut it short
        if calls["n"] >= 3:
            stop.set()
            return True
        return False

    state.poll_wake.wait = wait
    m.poll_loop(state, 30, stop, notify=True)

    assert state.status == "live" and state.retry_at is None
    assert state.subscription == "max"
    assert state.credits_owner == "uuid-live"
    assert state.live_uuid == "uuid-live"
    assert [p for _, p in state.history["session"]] == [78.0, 96.0, 3.0]
    assert any("at 96%" in n for n in notes)
    assert any("reset — now 3%" in n for n in notes)


def test_poll_loop_rate_limited_sets_retry_deadline(monkeypatch):
    def fake_fetch(token):
        raise m.RateLimited(120)

    monkeypatch.setattr(m, "fetch_usage", fake_fetch)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok", "max"))
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-live")
    state = m.State()
    stop = threading.Event()
    snapshots = []

    def wait(t=None):
        snapshots.append((state.status, state.retry_at))
        stop.set()
        return True

    state.poll_wake.wait = wait
    m.poll_loop(state, 30, stop, notify=False)
    status, retry_at = snapshots[0]
    assert status.startswith("rate limited")
    assert retry_at is not None and retry_at > time.time()


def test_poll_loop_discards_result_when_login_switches_mid_flight(monkeypatch):
    state = m.State()

    def fake_fetch(token):
        state.login_epoch += 1  # an auto-rotate lands while this request is in flight
        return json.loads(json.dumps(API_PAYLOAD))

    monkeypatch.setattr(m, "fetch_usage", fake_fetch)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok", "max"))
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-live")
    stop = threading.Event()
    waits = []

    def wait(t=None):
        waits.append(t)
        stop.set()
        return True

    state.poll_wake.wait = wait
    m.poll_loop(state, 30, stop, notify=False)
    # the response described the OLD login: nothing written, prompt re-poll
    assert state.api_limits == [] and state.status == "starting…"
    assert waits[0] == 2


def test_poll_loop_discards_result_when_owner_moves_mid_flight(monkeypatch):
    # an external claude-account switch lands between the token read and the
    # response: the epoch is unchanged, but the live uuid moved — the payload
    # belongs to the old login and must not be written under the new one
    uuids = iter(["uuid-a"])
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: next(uuids, "uuid-b"))
    monkeypatch.setattr(m, "fetch_usage", lambda tok: json.loads(json.dumps(API_PAYLOAD)))
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok", "max"))
    state = m.State()
    stop = threading.Event()
    waits = []

    def wait(t=None):
        waits.append(t)
        stop.set()
        return True

    state.poll_wake.wait = wait
    m.poll_loop(state, 30, stop, notify=False)
    assert state.api_limits == [] and state.credits_owner == ""
    assert waits[0] == 2


# ------------------------------------------------------------------ rendering


def full_state() -> m.State:
    state = m.State()
    state.api_limits = m.parse_limits(API_PAYLOAD)
    state.limits = m.merge_limits(state.api_limits, None)
    state.credits = m.parse_credits(API_PAYLOAD)
    state.subscription = "max"
    state.fetched_at = datetime.now(timezone.utc)
    state.status = "live"
    state.status_style = "green"
    return state


def test_render_footer_feed_fresh_api_backoff():
    state = full_state()
    state.feed_as_of = time.time()
    state.status = "rate limited · showing cached data"
    state.status_style = "yellow"
    state.retry_at = time.time() + 301
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "● live" in text and "feed just now" in text
    assert "api: rate limited" in text and "retry in 5m" in text


def test_render_footer_no_feed_backoff_countdown():
    state = full_state()
    state.status = "rate limited · showing cached data"
    state.status_style = "yellow"
    state.retry_at = time.time() + 301
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "◐ rate limited" in text and "retrying in 5m" in text
    assert "● live" not in text


def test_render_shows_limits_and_limiting_tag():
    state = full_state()
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "Session · 5h window" in text and "Week · Fable" in text
    assert "◂ limiting" in text
    assert "15.0%" in text and "27.0%" in text


# ------------------------------------------------------------------ accounts


def account_rows() -> list[m.AccountRow]:
    spare_limits = [
        m.Limit("session", "Session · 5h window", 12.0, dt(NOW + 3600)),
        m.Limit("weekly_all", "Week · all models", 8.0, dt(NOW + 86400)),
        m.Limit("weekly_scoped", "Week · Fable", 41.0, dt(NOW + 86400)),
    ]
    return [
        m.AccountRow("james", "james@jdgreen.io", "uuid-a", is_live=True),
        m.AccountRow("spare", "spare@x.io", "uuid-b", limits=spare_limits, fetched_at=NOW - 120),
    ]


def test_render_accounts_rows_live_and_polled():
    rows = account_rows()
    live = [m.Limit("session", "Session · 5h window", 47.0, dt(NOW + 3600))]
    g = m.render_accounts(rows, live, 72, dt(NOW))
    lines = [r.plain for r in g.renderables]
    assert "Accounts" in lines[0]
    assert "● james" in lines[1] and "5h 47%" in lines[1] and lines[1].rstrip().endswith("live")
    assert "○ spare" in lines[2] and "5h 12%" in lines[2] and "wk 8%" in lines[2]
    assert "fable 41%" in lines[2]  # scoped label shortened
    assert lines[2].rstrip().endswith("2m ago")


def test_render_accounts_aligns_limit_columns():
    rows = [
        m.AccountRow("james", "j@x.io", "uuid-a", is_live=True),
        m.AccountRow("empractica-jamie", "e@x.io", "uuid-b",
                     limits=[
                         m.Limit("session", "Session · 5h window", 100.0, dt(NOW + 3 * 3600 + 57 * 60)),
                         m.Limit("weekly_all", "Week · all models", 29.0, dt(NOW + 86400)),
                         m.Limit("weekly_scoped", "Week · Fable", 49.0, dt(NOW + 86400)),
                     ],
                     fetched_at=NOW),
        m.AccountRow("mosaic", "m@x.io", "uuid-c",
                     limits=[
                         m.Limit("session", "Session · 5h window", 100.0, dt(NOW + 47 * 60 + 7)),
                         m.Limit("weekly_all", "Week · all models", 21.0, dt(NOW + 86400)),
                         m.Limit("weekly_scoped", "Week · Fable", 38.0, dt(NOW + 86400)),
                     ],
                     fetched_at=NOW),
    ]
    live = [
        m.Limit("session", "Session · 5h window", 101.0, dt(NOW + 3 * 3600 + 57 * 60)),
        m.Limit("weekly_all", "Week · all models", 6.0, dt(NOW + 86400)),
    ]
    g = m.render_accounts(rows, live, 72, dt(NOW))
    lines = [r.plain for r in g.renderables[1:]]
    assert len({line.index("5h") for line in lines}) == 1
    assert len({line.index("wk") for line in lines}) == 1
    assert lines[1].index("fable") == lines[2].index("fable")
    assert "empractica-jamie5h" not in lines[1]
    assert lines[0].rstrip().endswith("live")
    assert lines[1].rstrip().endswith("just now")


def test_render_accounts_hot_session_shows_reset_countdown():
    rows = account_rows()
    rows[1].limits[0] = m.Limit("session", "Session · 5h window", 94.0, dt(NOW + 45 * 60))
    g = m.render_accounts(rows, [], 72, dt(NOW))
    assert "⟳45m" in g.renderables[2].plain


def test_render_accounts_hidden_without_second_account():
    assert m.render_accounts([], [], 72, dt(NOW)) is None
    only_live = [m.AccountRow("james", "j@x.io", "uuid-a", is_live=True)]
    assert m.render_accounts(only_live, [], 72, dt(NOW)) is None


def test_render_accounts_no_data_shows_dashes_not_other_accounts_numbers():
    # right after a login switch the live row has no numbers of its own yet —
    # placeholders, never blanks or a stale copy of another login's data
    rows = [
        m.AccountRow("mosaic", "m@x.io", "uuid-a", is_live=True),
        m.AccountRow("spare", "s@x.io", "uuid-b",
                     limits=[m.Limit("session", "Session · 5h window", 12.0, dt(NOW + 3600)),
                             m.Limit("weekly_scoped", "Week · Fable", 41.0, dt(NOW + 86400))],
                     fetched_at=NOW - 120),
    ]
    g = m.render_accounts(rows, [], 72, dt(NOW))  # live limits cleared by the switch
    live_line = g.renderables[1].plain
    assert "—" in live_line and "%" not in live_line
    assert "waiting…" not in live_line
    assert live_line.rstrip().endswith("live")
    assert "12%" in g.renderables[2].plain  # the spare row's own poll still shows


def test_render_accounts_note_and_waiting_states():
    rows = [
        m.AccountRow("james", "j@x.io", "uuid-a", is_live=True),
        m.AccountRow("spare", "s@x.io", "uuid-b", note="needs /login"),
        m.AccountRow("third", "t@x.io", "uuid-c"),
    ]
    g = m.render_accounts(rows, [], 72, dt(NOW))
    assert "needs /login" in g.renderables[2].plain
    assert "waiting…" in g.renderables[3].plain


def test_render_includes_accounts_section():
    state = full_state()
    state.accounts = account_rows()
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "Accounts" in text and "spare" in text


# ------------------------------------------------------------- auto-rotate


def _limits(**kinds) -> list:
    when = {"session": NOW + 3600, "weekly_all": NOW + 86400, "weekly_scoped": NOW + 86400}
    return [m.Limit(k, k, p, dt(when.get(k, NOW + 3600))) for k, p in kinds.items()]


def _live_state(live_max: float, fetched: float | None = NOW) -> m.State:
    s = m.State()
    s.limits = _limits(session=live_max)
    s.fetched_at = dt(fetched) if fetched else None
    return s


def test_max_pct():
    assert m._max_pct(_limits(session=10.0, weekly_all=55.0)) == 55.0
    assert m._max_pct([m.Limit("s", "s", None, None)]) is None
    assert m._max_pct([]) is None


def test_render_accounts_autorotate_indicator():
    rows = account_rows()
    on = m.render_accounts(rows, [], 72, dt(NOW), autorotate=True)
    off = m.render_accounts(rows, [], 72, dt(NOW), autorotate=False)
    assert "auto-rotate" in on.renderables[0].plain
    assert "auto-rotate" not in off.renderables[0].plain
    narrow = m.render_accounts(rows, [], 40, dt(NOW), autorotate=True)
    assert "auto-rotate" not in narrow.renderables[0].plain  # dropped, not overflowed


def test_render_accounts_narrow_width_never_overflows():
    rows = [
        m.AccountRow("empractica-jamie", "e@x.io", "uuid-b", is_live=True),
        m.AccountRow("mosaic", "m@x.io", "uuid-c",
                     limits=[m.Limit("session", "Session · 5h window", 100.0, dt(NOW + 3 * 3600)),
                             m.Limit("weekly_all", "Week · all models", 21.0, dt(NOW + 86400))],
                     credits=m.Credits(enabled=True, used=5.0, limit=20.0),
                     fetched_at=NOW - 60),
    ]
    live = [m.Limit("session", "Session · 5h window", 100.0, dt(NOW + 3 * 3600 + 57 * 60))]
    for width in (30, 34, 40, 46, 52):
        g = m.render_accounts(rows, live, width, dt(NOW), autorotate=True)
        for line in g.renderables:
            assert line.cell_len <= width, (width, repr(line.plain))


def _autorotate_env(monkeypatch, live_pct=100.0, marker=None):
    """Stub the file/keychain touchpoints an autorotate pass crosses."""
    monkeypatch.setattr(m.accounts, "read_marker", lambda: marker)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok-live", "max"))
    monkeypatch.setattr(m.accounts, "ensure_fresh_slot",
                        lambda name: {"claudeAiOauth": {"accessToken": f"tok-{name}"}})
    monkeypatch.setattr(m, "fetch_usage",
                        lambda tok: {"limits": [{"kind": "session",
                                                 "percent": live_pct if tok == "tok-live" else 20.0}]})


def test_autorotate_skips_when_live_has_headroom(monkeypatch):
    _autorotate_env(monkeypatch)
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("live is fine"))
    state = _live_state(60.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=10.0), fetched_at=NOW)]
    assert m.autorotate_once(state, rows, {}, 0.0) == 0.0


def test_autorotate_switches_to_login_with_headroom(monkeypatch):
    _autorotate_env(monkeypatch)
    switched = {}
    monkeypatch.setattr(m.accounts, "cmd_switch",
                        lambda name, quiet=False: switched.update(name=name, quiet=quiet))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=20.0), fetched_at=NOW)]
    state.api_limits = _limits(session=100.0)  # old login's numbers, must be dropped
    state.credits = m.Credits(enabled=True, used=141.79)
    next_poll = {"spare": 999.0, "other": 555.0}
    ts = m.autorotate_once(state, rows, next_poll, 0.0)
    assert switched == {"name": "spare", "quiet": True}
    assert ts > 0.0
    assert next_poll == {"other": 555.0}  # new live leaves the rotation; backoffs survive
    assert "auto-rotated to spare" in state.autorotate_note
    assert state.api_limits == [] and state.fetched_at is None  # stale live data cleared
    assert not state.credits.enabled       # the old login's credit spend cleared too
    assert state.login_epoch == 1          # in-flight polls of the old login get dropped
    assert [r.is_live for r in rows] == [False, True]  # panel flips without waiting a tick


def test_autorotate_live_confirm_vetoes_stale_100(monkeypatch):
    # state says 100% but the confirming poll shows the window just reset —
    # no switch, and the fresh truth replaces the stale panel data
    _autorotate_env(monkeypatch, live_pct=20.0)
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("live just reset"))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=5.0), fetched_at=NOW)]
    assert m.autorotate_once(state, rows, {}, 0.0) == 0.0
    assert [l.percent for l in state.api_limits] == [20.0]


def test_autorotate_rate_limited_confirm_falls_back_to_state(monkeypatch):
    # a 429 on the confirm poll doesn't dispute the recency-guarded 100% — rotate anyway
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok-live", "max"))
    monkeypatch.setattr(m.accounts, "ensure_fresh_slot",
                        lambda name: {"claudeAiOauth": {"accessToken": f"tok-{name}"}})

    def fetch(tok):
        if tok == "tok-live":
            raise m.RateLimited(60)
        return {"limits": [{"kind": "session", "percent": 20.0}]}

    monkeypatch.setattr(m, "fetch_usage", fetch)
    switched = {}
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda name, quiet=False: switched.update(name=name))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=20.0), fetched_at=NOW)]
    assert m.autorotate_once(state, rows, {}, 0.0) > 0.0
    assert switched["name"] == "spare"


def test_autorotate_waits_while_switch_marker_pending(monkeypatch):
    _autorotate_env(monkeypatch, marker={"target": "spare"})
    monkeypatch.setattr(m, "fetch_usage", lambda tok: pytest.fail("must not poll mid-heal"))
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("half-applied switch pending"))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=5.0), fetched_at=NOW)]
    assert m.autorotate_once(state, rows, {}, 0.0) == 0.0


def test_autorotate_respects_cooldown(monkeypatch):
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("still cooling down"))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=5.0), fetched_at=NOW)]
    recent = time.time()
    assert m.autorotate_once(state, rows, {}, recent) == recent


def test_autorotate_ignores_stale_live_data(monkeypatch):
    # cooldown has passed, but the live numbers were fetched BEFORE the last
    # switch — a leftover 100% must not trigger an immediate re-switch
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("stale 100% must not switch"))
    last = time.time() - (m.AUTOROTATE_COOLDOWN + 5)
    state = _live_state(100.0, fetched=last - 10)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=5.0), fetched_at=NOW)]
    assert m.autorotate_once(state, rows, {}, last) == last


def test_autorotate_all_logins_maxed_sets_note_and_paces(monkeypatch):
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok-live", "max"))
    monkeypatch.setattr(m.accounts, "ensure_fresh_slot",
                        lambda name: {"claudeAiOauth": {"accessToken": f"tok-{name}"}})
    monkeypatch.setattr(m, "fetch_usage",
                        lambda tok: {"limits": [{"kind": "session", "percent": 100.0}]})
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda *a, **k: pytest.fail("no login has room"))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("spare", "s", "b", limits=_limits(session=100.0), fetched_at=NOW)]
    ts = m.autorotate_once(state, rows, {}, 0.0)
    assert ts > 0.0  # a failed full pass advances the clock: no 20s confirm-poll storm
    assert "all logins at their limit" in state.autorotate_note
    # ...and the very next tick is inside the cooldown, so nothing is re-polled
    monkeypatch.setattr(m, "fetch_usage", lambda tok: pytest.fail("pass must be paced"))
    assert m.autorotate_once(state, rows, {}, ts) == ts


def test_autorotate_picks_most_headroom(monkeypatch):
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok-live", "max"))
    monkeypatch.setattr(m.accounts, "ensure_fresh_slot",
                        lambda name: {"claudeAiOauth": {"accessToken": f"tok-{name}"}})
    polls = {"tok-live": {"limits": [{"kind": "session", "percent": 100.0}]},
             "tok-b": {"limits": [{"kind": "session", "percent": 80.0}]},
             "tok-c": {"limits": [{"kind": "session", "percent": 30.0}]}}
    monkeypatch.setattr(m, "fetch_usage", lambda tok: polls[tok])
    switched = {}
    monkeypatch.setattr(m.accounts, "cmd_switch", lambda name, quiet=False: switched.update(name=name))
    state = _live_state(100.0)
    rows = [m.AccountRow("live", "l", "a", is_live=True),
            m.AccountRow("b", "b", "b", limits=_limits(session=79.0), fetched_at=NOW),
            m.AccountRow("c", "c", "c", limits=_limits(session=29.0), fetched_at=NOW)]
    m.autorotate_once(state, rows, {}, 0.0)
    assert switched["name"] == "c"  # lowest known usage tried first, confirmed with headroom


def test_accounts_loop_polls_only_non_live(monkeypatch):
    monkeypatch.setattr(m.accounts, "load_index", lambda: [
        m.accounts.Account("james", "j@x.io", "uuid-a"),
        m.accounts.Account("spare", "s@x.io", "uuid-b"),
    ])
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-a")
    fetched = []

    def fake_ensure(name):
        return {"claudeAiOauth": {"accessToken": f"tok-{name}"}}

    def fake_fetch(token):
        fetched.append(token)
        return API_PAYLOAD

    monkeypatch.setattr(m.accounts, "ensure_fresh_slot", fake_ensure)
    monkeypatch.setattr(m, "fetch_usage", fake_fetch)

    state = m.State()
    stop = threading.Event()
    ticks = {"n": 0}
    orig_wait = stop.wait

    def wait(t=None):
        ticks["n"] += 1
        if ticks["n"] >= 3:  # a few ticks so the staggered first poll comes due
            stop.set()
            return True
        return orig_wait(0)

    stop.wait = wait
    monkeypatch.setattr(m, "ACCOUNTS_TICK", 0)
    monkeypatch.setattr(m, "ACCOUNTS_STAGGER", 0)
    m.accounts_loop(state, stop)

    assert fetched == ["tok-spare"]  # live account never polled
    rows = {r.name: r for r in state.accounts}
    assert rows["james"].is_live and not rows["spare"].is_live
    assert rows["spare"].limits and rows["spare"].fetched_at is not None


def test_accounts_loop_marks_auth_failures(monkeypatch):
    monkeypatch.setattr(m.accounts, "load_index",
                        lambda: [m.accounts.Account("spare", "s@x.io", "uuid-b")])
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-a")

    def dead(name):
        raise m.accounts.AccountError("refresh token rejected")

    monkeypatch.setattr(m.accounts, "ensure_fresh_slot", dead)
    state = m.State()
    stop = threading.Event()
    orig_wait = stop.wait

    def wait(t=None):
        if state.accounts and (state.accounts[0].note or stop.is_set()):
            stop.set()
            return True
        return orig_wait(0)

    stop.wait = wait
    monkeypatch.setattr(m, "ACCOUNTS_TICK", 0)
    m.accounts_loop(state, stop)
    assert state.accounts[0].note == "needs /login"


def _one_tick(stop: threading.Event):
    def wait(t=None):
        stop.set()
        return True
    return wait


def test_accounts_loop_external_switch_clears_stale_panel(monkeypatch):
    # `claude-account switch` run outside the monitor: every panel number still
    # describes the OLD login and must be dropped, not shown under the new
    # account's name (the "mosaic at fable 100% when really 38%" bug)
    monkeypatch.setattr(m.accounts, "load_index", lambda: [])
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-b")
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    state = m.State()
    state.live_uuid = "uuid-a"
    state.api_limits = _limits(session=100.0, weekly_scoped=100.0)
    state.limits = list(state.api_limits)
    state.merged_prev = list(state.api_limits)
    state.feed = {"session": (100.0, NOW + 3600)}
    state.feed_as_of = NOW
    state.credits = m.Credits(enabled=True, used=141.79)
    state.credit_today = 10.83
    state.subscription = "max"
    state.fetched_at = dt(NOW)
    stop = threading.Event()
    stop.wait = _one_tick(stop)
    m.accounts_loop(state, stop)
    assert state.live_uuid == "uuid-b"
    assert state.login_epoch == 1                # in-flight polls of the old login drop
    assert state.api_limits == [] and state.limits == []
    assert state.feed is None and state.feed_as_of is None
    assert not state.credits.enabled and state.credit_today is None
    assert state.subscription == "" and state.fetched_at is None
    assert state.poll_wake.is_set()              # immediate re-poll requested
    assert "login switched" in state.status


def test_accounts_loop_first_sighting_sets_uuid_without_clearing(monkeypatch):
    # startup (or an unreadable ~/.claude.json recovering): learning the live
    # uuid for the first time is not a switch — keep the data already fetched
    monkeypatch.setattr(m.accounts, "load_index", lambda: [])
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-a")
    monkeypatch.setattr(m.accounts, "read_marker", lambda: None)
    state = m.State()
    state.api_limits = _limits(session=50.0)
    stop = threading.Event()
    stop.wait = _one_tick(stop)
    m.accounts_loop(state, stop)
    assert state.live_uuid == "uuid-a"
    assert state.login_epoch == 0 and state.api_limits
    assert not state.poll_wake.is_set()


# --------------------------------------------------- review-fix regressions


def test_parse_dt_naive_string_assumed_utc():
    parsed = m.parse_dt("2026-07-02T18:10:00")  # no offset: must not stay naive
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed == dt(1783015800)
    # aware math must not raise (this crashed render() before the fix)
    m.fmt_delta(parsed, datetime.now(timezone.utc))


def test_parse_dt_rejects_nonfinite_and_garbage_epoch():
    assert m.parse_dt(float("nan")) is None
    assert m.parse_dt(float("inf")) is None
    assert m.parse_dt(0) == dt(0)  # epoch 0 is a legitimate timestamp, not "missing"


def test_parse_credits_garbage_types_do_not_crash_render():
    # "1250" parses (minor units -> $12.50); "fifty" is unparseable -> no limit
    payload = {"extra_usage": {"is_enabled": True, "used_credits": "1250", "monthly_limit": "fifty",
                               "decimal_places": 2}}
    credits = m.parse_credits(payload)
    assert credits.used == 12.5 and credits.limit is None
    state = full_state()
    state.credits = credits
    c = make_console()
    c.print(m.render(state, c))  # used shown, bogus limit skipped, no crash
    assert "Extra usage credits" in c.export_text()


def test_parse_limits_garbage_percent():
    payload = {"limits": [{"kind": "session", "percent": "bogus", "resets_at": None}]}
    assert m.parse_limits(payload)[0].percent is None


def test_read_feed_wrong_typed_leaves(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    write_feed_file(tmp_path, "bad1", {"five_hour": {"used_percentage": "bogus", "resets_at": SOON}})
    write_feed_file(tmp_path, "bad2", {"five_hour": ["not", "a", "dict"]})
    write_feed_file(tmp_path, "bad3", {"five_hour": {"used_percentage": float("nan"), "resets_at": SOON}})
    write_feed_file(tmp_path, "good", {"five_hour": {"used_percentage": 21, "resets_at": SOON}})
    result = m.read_feed()
    assert result is not None
    feed, _ = result
    assert feed["session"] == (21.0, SOON)  # bad leaves skipped, good survives


def test_merge_limits_nonfinite_reset_epoch_skipped():
    out = m.merge_limits([], {"session": (50.0, float("inf"))})
    assert out == []  # unrepresentable timestamp ignored, no OverflowError


def test_feed_loop_ignores_the_feed_without_an_api_anchor(monkeypatch):
    """Right after a login switch api_limits is empty, and every open session is
    still writing the OLD account's windows under the NEW account's uuid. With no
    anchor to check them against, the feed must not reach the panel at all."""
    seen = {"called": False}

    def spy(live_uuid="", weekly_reset=None):
        seen["called"] = True
        return ({"weekly_all": (12.0, time.time() + 6 * 86400)}, time.time())

    def run_one_iteration(state):
        stop = threading.Event()
        stop.wait = lambda t=None: stop.set() or True  # stop after the first body
        m.feed_loop(state, stop)

    monkeypatch.setattr(m, "read_feed", spy)
    monkeypatch.setattr(m.accounts, "active_account_uuid", lambda: "uuid-a")
    state = m.State()

    run_one_iteration(state)
    assert not seen["called"] and state.feed is None and state.limits == []

    # once a poll of the live login lands, the feed is anchored and flows again
    state.api_limits = [m.Limit("weekly_all", "Week · all models", 9.0, dt(time.time() + 6 * 86400))]
    run_one_iteration(state)
    assert seen["called"] and state.feed is not None
    assert next(l for l in state.limits if l.kind == "weekly_all").percent == 12.0


def test_feed_loop_survives_read_feed_exception(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("corrupt feed")

    monkeypatch.setattr(m, "read_feed", boom)
    state = m.State()
    state.api_limits = [m.Limit("session", "Session · 5h window", 5.0, dt(2_000_000))]  # anchored: read_feed runs
    stop = threading.Event()
    calls = {"n": 0}
    orig_wait = stop.wait

    def wait(t=None):
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
            return True
        return orig_wait(0)

    stop.wait = wait
    m.feed_loop(state, stop)  # must complete 3 iterations without raising
    assert calls["n"] >= 3


def test_recompute_clock_backwards_keeps_history_ordered(monkeypatch):
    state = m.State()
    state.history["session"] = [(NOW + 1000, 40.0)]  # clock has since jumped back
    state.api_limits = [m.Limit("session", "Session · 5h window", 45.0, None)]
    monkeypatch.setattr(m.time, "time", lambda: NOW)
    m.recompute(state)
    times = [t for t, _ in state.history["session"]]
    assert times == sorted(times) and len(times) == 1  # no out-of-order append


def test_load_history_nonfinite_filtered(tmp_path, monkeypatch):
    path = tmp_path / "hist.json"
    monkeypatch.setattr(m, "HISTORY_FILE", path)
    path.write_text('{"session": [[NaN, 10.0], [%f, 20.0]]}' % time.time())
    state = m.State()
    m.load_history(state)
    assert [p for _, p in state.history.get("session", [])] == [20.0]


def test_env_interval_garbage(monkeypatch):
    monkeypatch.setenv("CLAUDE_MONITOR_INTERVAL", "sixty")
    assert m.env_interval() == 60
    monkeypatch.setenv("CLAUDE_MONITOR_INTERVAL", "90")
    assert m.env_interval() == 90


def test_fetch_usage_error_dispatch(monkeypatch):
    import email.message
    import io
    import urllib.error

    def http_error(code, headers=None):
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://x", code, "err", hdrs, io.BytesIO(b""))

    def raise_401(req, timeout):
        raise http_error(401)

    monkeypatch.setattr(m.urllib.request, "urlopen", raise_401)
    with pytest.raises(m.AuthError):
        m.fetch_usage("tok")

    def raise_429(req, timeout):
        raise http_error(429, {"retry-after": "30"})

    monkeypatch.setattr(m.urllib.request, "urlopen", raise_429)
    with pytest.raises(m.RateLimited) as exc_info:
        m.fetch_usage("tok")
    assert exc_info.value.retry_after == 30

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def body_error(req, timeout):
        return FakeResp(json.dumps({"error": {"type": "rate_limit_error"}}).encode())

    monkeypatch.setattr(m.urllib.request, "urlopen", body_error)
    with pytest.raises(m.RateLimited):
        m.fetch_usage("tok")

    def auth_body(req, timeout):
        return FakeResp(json.dumps({"error": {"type": "authentication_error", "message": "no"}}).encode())

    monkeypatch.setattr(m.urllib.request, "urlopen", auth_body)
    with pytest.raises(m.AuthError):
        m.fetch_usage("tok")


# ------------------------------------------------------------------ credits

# The real /api/oauth/usage payload the user is on: credits enabled and funded,
# all plan windows maxed, org meters the cap centrally so every $ cap is null.
CREDITS_ACTIVE_PAYLOAD = {
    "extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": 14179.0,
                    "utilization": None, "currency": "USD", "decimal_places": 2},
    "spend": {"enabled": True, "used": {"amount_minor": 14179, "currency": "USD", "exponent": 2},
              "limit": None, "cap": None, "balance": None, "percent": 0},
    "limits": [
        {"kind": "session", "percent": 100.0, "resets_at": "2026-07-03T21:00:00+00:00", "is_active": True},
        {"kind": "weekly_all", "percent": 100.0, "resets_at": "2026-07-08T00:00:00+00:00", "is_active": False},
        {"kind": "weekly_scoped", "percent": 100.0, "resets_at": "2026-07-08T00:00:00+00:00",
         "scope": {"model": {"display_name": "Opus"}}, "is_active": False},
    ],
}


def test_money_to_major_shapes():
    assert m.money_to_major({"amount_minor": 14179, "exponent": 2}) == 141.79
    assert m.money_to_major({"amount_minor": 500, "exponent": 3}) == 0.5
    assert m.money_to_major(1250, exponent_default=2) == 12.5          # bare minor-unit number
    assert m.money_to_major(None) is None
    assert m.money_to_major({"amount_minor": None}) is None
    assert m.money_to_major("nope") is None
    assert m.money_to_major({"amount_minor": 100, "exponent": "bad"}) == 1.0  # bad exponent -> default 2


def test_fmt_money():
    assert m.fmt_money(None) == "—"
    assert m.fmt_money(141.79) == "$141.79"
    assert m.fmt_money(1234.5, "USD") == "$1,234.50"
    assert m.fmt_money(9.99, "EUR") == "€9.99"
    assert m.fmt_money(5.0, "CHF") == "5.00 CHF"  # unknown symbol -> code suffix


def test_credits_available_property():
    assert m.Credits(enabled=True, used=10.0).available is None            # uncapped
    assert m.Credits(enabled=True, used=10.0, limit=50.0).available == 40.0
    assert m.Credits(enabled=True, used=60.0, limit=50.0).available == 0.0  # never negative
    assert m.Credits(enabled=True, used=10.0, balance=25.0).available == 25.0
    assert m.Credits(enabled=True, used=10.0, balance=-3.0).available == 0.0  # overdrawn, not "-$3 left"
    # balance wins over a cap when both are present
    assert m.Credits(enabled=True, used=10.0, limit=50.0, balance=5.0).available == 5.0


def test_credits_active_detection():
    credits = m.parse_credits(CREDITS_ACTIVE_PAYLOAD)
    limits = m.parse_limits(CREDITS_ACTIVE_PAYLOAD)
    assert m.credits_active(limits, credits) is True                       # enabled + is_active at 100%
    assert m.credits_active([], credits) is False                          # no limit data yet
    assert m.credits_active(limits, m.Credits(enabled=False)) is False     # credits off
    # is_active window below the threshold -> not spilling yet
    calm = [m.Limit("session", "s", 40.0, dt(NOW + 3600), is_active=True)]
    assert m.credits_active(calm, credits) is False
    # no is_active flag anywhere -> require session and weekly evidence, all maxed
    session_only = [m.Limit("session", "s", 100.0, dt(NOW + 3600), is_active=False)]
    assert m.credits_active(session_only, credits) is False
    partial = [m.Limit("session", "s", 100.0, dt(NOW + 3600), is_active=False),
               m.Limit("weekly_all", "w", 40.0, dt(NOW + 86400), is_active=False)]
    assert m.credits_active(partial, credits) is False
    maxed = [m.Limit("session", "s", 100.0, dt(NOW + 3600), is_active=False),
             m.Limit("weekly_all", "w", 100.0, dt(NOW + 86400), is_active=False)]
    assert m.credits_active(maxed, credits) is True


def test_credit_binding_limit_prefers_future_reset():
    past = m.Limit("session", "s", 100.0, dt(NOW - 600), is_active=False)
    future = m.Limit("weekly_all", "w", 100.0, dt(NOW + 86400), is_active=False)
    binding, api_bound = m.credit_binding_limit([past, future], dt(NOW))
    assert binding is future and api_bound is False  # a live deadline beats a stale "now"
    binding, _ = m.credit_binding_limit([past], dt(NOW))
    assert binding is past  # nothing in the future -> still better than no countdown


def test_credits_active_requires_used_known():
    # enabled but `used` never parsed (partial payload) -> can't be "drawing"
    limits = m.parse_limits(CREDITS_ACTIVE_PAYLOAD)
    assert m.credits_active(limits, m.Credits(enabled=True, used=None)) is False


def test_track_credit_today_baseline_and_delta():
    state = m.State()
    state.credits = m.Credits(enabled=True, used=129.32)
    m._track_credit_today(state, NOW)
    assert state.credit_today == 0.0                    # first sample sets the baseline
    state.credits = m.Credits(enabled=True, used=141.79)
    m._track_credit_today(state, NOW + 600)
    assert round(state.credit_today, 2) == 12.47        # delta from baseline


def test_track_credit_today_day_rollover_rebaselines():
    state = m.State()
    state.credits = m.Credits(enabled=True, used=100.0)
    m._track_credit_today(state, NOW)
    state.credits = m.Credits(enabled=True, used=150.0)
    m._track_credit_today(state, NOW + 2 * 86400)       # two days later
    assert state.credit_today == 0.0                    # new day -> baseline resets to 150


def test_track_credit_today_counter_reset_clamps():
    state = m.State()
    state.credits = m.Credits(enabled=True, used=50.0)
    m._track_credit_today(state, NOW)
    state.credits = m.Credits(enabled=True, used=3.0)   # billing period reset mid-day
    m._track_credit_today(state, NOW + 600)
    assert state.credit_today == 0.0                    # rebaselines, never negative


def test_track_credit_today_ignores_disabled():
    state = m.State()
    state.credits = m.Credits(enabled=False, used=None)
    m._track_credit_today(state, NOW)
    assert state.credit_today is None and state.credit_baseline is None


def test_track_credit_today_login_change_rebaselines():
    state = m.State()
    state.credits_owner = "acct-a"
    state.credits = m.Credits(enabled=True, used=50.0)
    m._track_credit_today(state, NOW)
    state.credits = m.Credits(enabled=True, used=62.0)
    m._track_credit_today(state, NOW + 300)
    assert round(state.credit_today, 2) == 12.0
    # switch to a login whose cumulative counter is HIGHER — the jump must not
    # count as today's spend (used is per-account, deltas across logins are noise)
    state.credits_owner = "acct-b"
    state.credits = m.Credits(enabled=True, used=141.79)
    m._track_credit_today(state, NOW + 600)
    assert state.credit_today == 0.0 and state.credit_account == "acct-b"


def test_credit_baseline_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CREDITS_FILE", tmp_path / "credits.json")
    state = m.State()
    state.credit_day = datetime.now().strftime("%Y-%m-%d")
    state.credit_baseline = 129.32
    state.credit_account = "acct-a"
    m.save_credit_baseline(state)
    loaded = m.State()
    m.load_credit_baseline(loaded)
    assert loaded.credit_day == state.credit_day and loaded.credit_baseline == 129.32
    assert loaded.credit_account == "acct-a"  # restart under another login discards it


def test_credit_baseline_stale_day_rejected(tmp_path, monkeypatch):
    path = tmp_path / "credits.json"
    monkeypatch.setattr(m, "CREDITS_FILE", path)
    path.write_text(json.dumps({"day": "2000-01-01", "baseline": 42.0}))  # yesteryear
    loaded = m.State()
    m.load_credit_baseline(loaded)
    assert loaded.credit_baseline is None  # a stale baseline would make "today" span days


def test_credit_baseline_nothing_to_save(tmp_path, monkeypatch):
    path = tmp_path / "credits.json"
    monkeypatch.setattr(m, "CREDITS_FILE", path)
    m.save_credit_baseline(m.State())  # no baseline observed yet
    assert not path.exists()


def _credits_state() -> m.State:
    state = m.State()
    payload = json.loads(json.dumps(CREDITS_ACTIVE_PAYLOAD))
    payload["limits"][0]["resets_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    state.api_limits = m.parse_limits(payload)
    state.limits = m.merge_limits(state.api_limits, None)
    state.credits = m.parse_credits(payload)
    state.credit_day = datetime.now().strftime("%Y-%m-%d")
    state.credit_baseline = 129.32
    m._track_credit_today(state, time.time())
    state.subscription = "max"
    state.status = "live"
    state.status_style = "green"
    state.fetched_at = datetime.now(timezone.utc)
    return state


def test_render_big_dollar_replaces_bars():
    state = _credits_state()
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "Drawing on usage credits" in text
    assert "$12.47" in text and "spent today" in text
    assert "$141.79 used this month" in text
    assert "until reset" not in text          # the three per-limit bars are gone
    assert "resets in" in text                # ...but the reset countdown stays


def test_render_credit_spend_without_active_uses_neutral_reset_label():
    state = _credits_state()
    for lim in state.limits:
        lim.is_active = False
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "Next plan reset in" in text
    assert "Session · 5h window resets in" not in text


def test_render_big_dollar_zero_when_no_spend_tracked_yet():
    state = _credits_state()
    state.credit_today = None                 # no baseline established yet
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "$0.00" in text and "spent today" in text


def test_render_small_credits_line_fixed_dollars():
    # plan has headroom -> normal bars + the small credits line, in real dollars
    state = m.State()
    lims = m.parse_limits({"limits": [
        {"kind": "session", "percent": 30.0, "resets_at": "2026-07-03T21:00:00+00:00", "is_active": True}]})
    state.limits = lims
    state.api_limits = lims
    state.credits = m.parse_credits(CREDITS_ACTIVE_PAYLOAD)  # used $141.79
    state.subscription = "max"
    state.status = "live"
    state.status_style = "green"
    state.fetched_at = datetime.now(timezone.utc)
    c = make_console()
    c.print(m.render(state, c))
    text = c.export_text()
    assert "Extra usage credits" in text
    assert "$141.79 used" in text
    assert "$14179" not in text               # regression guard: the old 100x bug


def test_render_accounts_credit_cell_on_non_live():
    live = [m.Limit("session", "Session · 5h window", 100.0, dt(NOW + 3600))]
    capped = m.Credits(enabled=True, used=5.0, limit=20.0, currency="USD")
    uncapped = m.Credits(enabled=True, used=141.79, currency="USD")
    rows = [
        m.AccountRow("james", "j@x.io", "uuid-a", is_live=True, credits=uncapped),
        m.AccountRow("work", "w@x.io", "uuid-b",
                     limits=[m.Limit("session", "Session · 5h window", 42.0, dt(NOW + 3600))],
                     credits=capped, fetched_at=NOW - 60),
        m.AccountRow("solo", "s@x.io", "uuid-c",
                     limits=[m.Limit("session", "Session · 5h window", 10.0, dt(NOW + 3600))],
                     credits=uncapped, fetched_at=NOW - 60),
    ]
    g = m.render_accounts(rows, live, 72, dt(NOW))
    live_line = g.renderables[1].plain
    work_line = g.renderables[2].plain
    solo_line = g.renderables[3].plain
    assert "left" not in live_line and "used" not in live_line   # live row shows no credit cell
    assert "$15.00 left" in work_line                            # cap 20 - used 5
    assert "$141.79 used" in solo_line                           # uncapped -> falls back to used


def test_render_accounts_credit_cell_only_for_enabled():
    live = [m.Limit("session", "s", 50.0, dt(NOW + 3600))]
    rows = [
        m.AccountRow("james", "j@x.io", "uuid-a", is_live=True),
        m.AccountRow("work", "w@x.io", "uuid-b",
                     limits=[m.Limit("session", "s", 42.0, dt(NOW + 3600))],
                     credits=m.Credits(enabled=False), fetched_at=NOW - 60),
    ]
    g = m.render_accounts(rows, live, 72, dt(NOW))
    assert "used" not in g.renderables[2].plain and "left" not in g.renderables[2].plain
