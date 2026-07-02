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


def test_parse_dt_variants():
    assert m.parse_dt("2026-07-02T18:10:00Z") == dt(1783015800)
    assert m.parse_dt(1783015800) == dt(1783015800)
    assert m.parse_dt(None) is None
    assert m.parse_dt("") is None
    assert m.parse_dt("not-a-date") is None


def test_parse_credits_disabled_and_enabled():
    assert m.parse_credits(API_PAYLOAD).enabled is False
    payload = {"extra_usage": {"is_enabled": True, "used_credits": 12.5, "monthly_limit": 50}}
    credits = m.parse_credits(payload)
    assert credits.enabled and credits.used == 12.5 and credits.limit == 50


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


def test_detect_events_steady_state_silent():
    prev = [m.Limit("session", "s", 47.0, None, is_active=True)]
    new = [m.Limit("session", "s", 48.0, None, is_active=True)]
    assert m.detect_events(prev, new) == []


def test_detect_events_ignores_none_percent():
    prev = [m.Limit("session", "s", None, None)]
    new = [m.Limit("session", "s", 90.0, None)]
    assert m.detect_events(prev, new) == []


# ------------------------------------------------------------------ feed


def write_feed_file(feed_dir, name, rate_limits, age=0.0, captured_at=None):
    path = feed_dir / f"{name}.json"
    path.write_text(json.dumps({"captured_at": captured_at or (time.time() - age),
                                "session_id": name, "rate_limits": rate_limits}))
    if age:
        mtime = time.time() - age
        os.utime(path, (mtime, mtime))
    return path


def test_read_feed_staleness_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    # two fresh sessions on the current window, one fresh-but-stale-data, one old file
    write_feed_file(tmp_path, "a", {"five_hour": {"used_percentage": 23, "resets_at": 2_000_000},
                                    "seven_day": {"used_percentage": 24, "resets_at": 3_000_000}})
    write_feed_file(tmp_path, "b", {"five_hour": {"used_percentage": 22, "resets_at": 2_000_000},
                                    "seven_day": {"used_percentage": 11, "resets_at": 3_000_000}})
    write_feed_file(tmp_path, "c", {"five_hour": {"used_percentage": 93, "resets_at": 1_900_000}})  # old window
    write_feed_file(tmp_path, "d", {"five_hour": {"used_percentage": 99, "resets_at": 2_000_000}},
                    age=m.FEED_MAX_AGE + 30)  # too old a file: ignored entirely
    result = m.read_feed()
    assert result is not None
    feed, as_of = result
    assert feed["session"] == (23.0, 2_000_000.0)      # newest window, max pct; 93 and 99 ignored
    assert feed["weekly_all"] == (24.0, 3_000_000.0)   # stale 11% loses to 24%
    assert as_of > 0


def test_read_feed_garbage_tolerant(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "FEED_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "null.json").write_text(json.dumps({"session_id": "x", "rate_limits": None}))
    assert m.read_feed() is None
    monkeypatch.setattr(m, "FEED_DIR", tmp_path / "does-not-exist")
    assert m.read_feed() is None


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
    monkeypatch.setattr(m, "send_notification", notes.append)

    state = m.State()
    stop = threading.Event()
    orig_wait = stop.wait

    def wait(t=None):
        if calls["n"] >= 3:
            stop.set()
            return True
        return orig_wait(0)

    stop.wait = wait
    m.poll_loop(state, 30, stop, notify=True)

    assert state.status == "live" and state.retry_at is None
    assert state.subscription == "max"
    assert [p for _, p in state.history["session"]] == [78.0, 96.0, 3.0]
    assert any("at 96%" in n for n in notes)
    assert any("reset — now 3%" in n for n in notes)


def test_poll_loop_rate_limited_sets_retry_deadline(monkeypatch):
    def fake_fetch(token):
        raise m.RateLimited(120)

    monkeypatch.setattr(m, "fetch_usage", fake_fetch)
    monkeypatch.setattr(m, "read_access_token", lambda: ("tok", "max"))
    state = m.State()
    stop = threading.Event()
    snapshots = []
    orig_wait = stop.wait

    def wait(t=None):
        snapshots.append((state.status, state.retry_at))
        stop.set()
        return True

    stop.wait = wait
    m.poll_loop(state, 30, stop, notify=False)
    status, retry_at = snapshots[0]
    assert status.startswith("rate limited")
    assert retry_at is not None and retry_at > time.time()


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


def test_render_accounts_hot_session_shows_reset_countdown():
    rows = account_rows()
    rows[1].limits[0] = m.Limit("session", "Session · 5h window", 94.0, dt(NOW + 45 * 60))
    g = m.render_accounts(rows, [], 72, dt(NOW))
    assert "⟳45m" in g.renderables[2].plain


def test_render_accounts_hidden_without_second_account():
    assert m.render_accounts([], [], 72, dt(NOW)) is None
    only_live = [m.AccountRow("james", "j@x.io", "uuid-a", is_live=True)]
    assert m.render_accounts(only_live, [], 72, dt(NOW)) is None


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
    payload = {"extra_usage": {"is_enabled": True, "used_credits": "12.5", "monthly_limit": "fifty"}}
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
    write_feed_file(tmp_path, "bad1", {"five_hour": {"used_percentage": "bogus", "resets_at": 2_000_000}})
    write_feed_file(tmp_path, "bad2", {"five_hour": ["not", "a", "dict"]})
    write_feed_file(tmp_path, "bad3", {"five_hour": {"used_percentage": float("nan"), "resets_at": 2_000_000}})
    write_feed_file(tmp_path, "good", {"five_hour": {"used_percentage": 21, "resets_at": 2_000_000}})
    result = m.read_feed()
    assert result is not None
    feed, _ = result
    assert feed["session"] == (21.0, 2_000_000.0)  # bad leaves skipped, good survives


def test_merge_limits_nonfinite_reset_epoch_skipped():
    out = m.merge_limits([], {"session": (50.0, float("inf"))})
    assert out == []  # unrepresentable timestamp ignored, no OverflowError


def test_feed_loop_survives_read_feed_exception(monkeypatch):
    def boom():
        raise RuntimeError("corrupt feed")

    monkeypatch.setattr(m, "read_feed", boom)
    state = m.State()
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
