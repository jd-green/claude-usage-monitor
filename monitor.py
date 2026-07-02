# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13.7"]
# ///
"""Claude Usage Monitor — a live TUI for Claude Code usage limits.

Primary data source is the passive feed statusline-dispatch writes to
~/.claude/usage-feed/ from the rate_limits JSON Claude Code pipes to every
statusline refresh — seconds-fresh, zero API calls. The rate-limited usage
API (the same endpoint Claude Code's /usage screen uses, authenticated via
the local login's OAuth token) is polled only as a supplement for scoped
limits, the limiting flag, severity, and credits. The token is never
printed, logged, or written anywhere.

Run it in a tmux pane:  uv run monitor.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-cli/2.1.198 (external, cli)"
BETA_HEADER = "oauth-2025-04-20"

# Claude Code palette
CLAUDE = "#d97757"          # brand rust/orange
BORDER = "grey42"
DIM = "grey58"
BAR_EMPTY = "grey30"

STATUSLINE_OFF = Path.home() / ".claude" / "statusline.off"
STATUSLINE_LITE = Path.home() / ".claude" / "statusline.lite"

# original pixel bar-chart mark (deliberately not the Claude Code mascot)
LOGO = [
    "      ██",
    "   ██ ██",
    "██ ██ ██",
]

KIND_LABELS = {
    "session": "Session · 5h window",
    "weekly_all": "Week · all models",
    "weekly_scoped": "Week · {scope}",
}
KIND_ORDER = {"session": 0, "weekly_all": 1, "weekly_scoped": 2}

SEVERITY_STYLES = {
    "warning": "#d9a765",
    "elevated": "#d9a765",
    "exceeded": "#e05252",
    "limited": "#e05252",
}

SPARK_CHARS = "▁▂▃▄▅▆▇█"
HISTORY_SPAN = 6 * 3600        # keep 6h of samples in memory
PACE_LOOKBACK = 45 * 60        # burn rate measured over the last 45 min
PACE_MIN_SPAN = 5 * 60         # ... but only once samples span 5+ min
NOTIFY_THRESHOLDS = (95, 80)   # notify on upward crossings, highest first
RESET_DROP = 20                # a drop this large (in points) counts as a window reset

# Passive feed: statusline-dispatch snapshots the rate_limits that Claude Code
# pipes to every statusline refresh, one file per session. While any session
# is running, the monitor reads these instead of hammering the API.
FEED_DIR = Path.home() / ".claude" / "usage-feed"
FEED_MAX_AGE = 60              # ignore session files older than this
FEED_POLL = 5                  # how often the feed thread re-reads the dir
API_RELAXED_INTERVAL = 300     # API poll cadence while the feed is fresh
FEED_KINDS = {"five_hour": "session", "seven_day": "weekly_all"}


def prettify_key(key: str) -> str:
    return key.replace("_", " ").strip().title()


# ---------------------------------------------------------------- credentials


class AuthError(Exception):
    pass


def read_access_token() -> tuple[str, str]:
    """Return (access_token, subscription_type) from the local Claude Code login."""
    raw = None
    if sys.platform == "darwin":
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            raw = proc.stdout.strip()
    if raw is None:
        cred_file = Path.home() / ".claude" / ".credentials.json"
        if cred_file.exists():
            raw = cred_file.read_text()
    if not raw:
        raise AuthError("No Claude Code login found — run `claude` and /login first")
    try:
        oauth = json.loads(raw)["claudeAiOauth"]
        return oauth["accessToken"], oauth.get("subscriptionType", "")
    except (json.JSONDecodeError, KeyError) as exc:
        raise AuthError(f"Could not parse stored credentials ({exc})") from exc


# --------------------------------------------------------------------- fetch


class RateLimited(Exception):
    def __init__(self, retry_after: int | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


def fetch_usage(token: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("Token rejected (401) — re-run /login in Claude Code")
        if exc.code == 429:
            retry_after = exc.headers.get("retry-after")
            raise RateLimited(int(retry_after) if retry_after and retry_after.isdigit() else None)
        raise
    if isinstance(body, dict) and body.get("error"):
        etype = body["error"].get("type", "")
        if etype == "rate_limit_error":
            raise RateLimited()
        if etype in ("authentication_error", "permission_error"):
            raise AuthError(body["error"].get("message", etype))
        raise RuntimeError(body["error"].get("message", etype))
    return body


# --------------------------------------------------------------------- model


@dataclass
class Limit:
    kind: str
    label: str
    percent: float | None
    resets_at: datetime | None
    severity: str = "normal"
    is_active: bool = False


@dataclass
class Credits:
    enabled: bool = False
    used: float | None = None
    limit: float | None = None
    utilization: float | None = None


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def limit_label(entry: dict) -> str:
    kind = entry.get("kind", "")
    template = KIND_LABELS.get(kind)
    if template is None:
        return prettify_key(kind or entry.get("group", "limit"))
    scope = ""
    if "{scope}" in template:
        model = (entry.get("scope") or {}).get("model") or {}
        surface = (entry.get("scope") or {}).get("surface")
        scope = model.get("display_name") or surface or "scoped"
    return template.format(scope=scope)


def parse_limits(payload: dict) -> list[Limit]:
    limits: list[Limit] = []
    for entry in payload.get("limits") or []:
        pct = entry.get("percent")
        limits.append(
            Limit(
                kind=entry.get("kind", ""),
                label=limit_label(entry),
                percent=float(pct) if pct is not None else None,
                resets_at=parse_dt(entry.get("resets_at")),
                severity=entry.get("severity") or "normal",
                is_active=bool(entry.get("is_active")),
            )
        )
    if not limits:  # older payload shape fallback
        for key, label in (("five_hour", "Session · 5h window"), ("seven_day", "Week · all models")):
            val = payload.get(key)
            if isinstance(val, dict):
                util = val.get("utilization")
                limits.append(
                    Limit(
                        kind=key,
                        label=label,
                        percent=float(util) if util is not None else None,
                        resets_at=parse_dt(val.get("resets_at")),
                    )
                )
    limits.sort(key=lambda l: KIND_ORDER.get(l.kind, 99))
    return limits


def parse_credits(payload: dict) -> Credits:
    extra = payload.get("extra_usage") or {}
    spend = payload.get("spend") or {}
    enabled = bool(extra.get("is_enabled") or spend.get("enabled"))
    used = extra.get("used_credits")
    return Credits(
        enabled=enabled,
        used=float(used) if used is not None else None,
        limit=extra.get("monthly_limit"),
        utilization=extra.get("utilization"),
    )


@dataclass
class State:
    limits: list[Limit] = field(default_factory=list)      # merged view (rendered)
    api_limits: list[Limit] = field(default_factory=list)  # last full API payload
    feed: dict[str, tuple[float, float]] | None = None     # kind -> (pct, resets_epoch)
    feed_as_of: float | None = None
    merged_prev: list[Limit] | None = None                 # for event detection
    credits: Credits = field(default_factory=Credits)
    history: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # kind -> [(epoch, pct)]
    subscription: str = ""
    fetched_at: datetime | None = None
    status: str = "starting…"
    status_style: str = DIM
    retry_at: float | None = None  # epoch of the next poll attempt while backing off
    lock: threading.Lock = field(default_factory=threading.Lock)


# ------------------------------------------------------------------ feed/merge


def read_feed() -> tuple[dict[str, tuple[float, float]], float] | None:
    """Merge fresh per-session feed files into per-window (pct, resets_epoch).

    Sessions can pipe stale numbers (they report what they last learned), so:
    take the newest resets_at as the current window instance, then the max
    percent among sessions reporting that instance — usage is monotonic
    within a window, so max = freshest.
    """
    try:
        files = list(FEED_DIR.glob("*.json"))
    except OSError:
        return None
    now_ts = time.time()
    entries: list[dict] = []
    as_of = 0.0
    for f in files:
        try:
            mtime = f.stat().st_mtime
            if now_ts - mtime > FEED_MAX_AGE:
                continue
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rl = data.get("rate_limits")
        if isinstance(rl, dict):
            entries.append(rl)
            as_of = max(as_of, mtime)
    if not entries:
        return None
    merged: dict[str, tuple[float, float]] = {}
    for feed_key, kind in FEED_KINDS.items():
        cands = []
        for rl in entries:
            w = rl.get(feed_key) or {}
            pct, rst = w.get("used_percentage"), w.get("resets_at")
            if pct is None or rst is None:
                continue
            cands.append((float(rst), float(pct)))
        if not cands:
            continue
        latest_reset = max(r for r, _ in cands)
        merged[kind] = (max(p for r, p in cands if r == latest_reset), latest_reset)
    return (merged, as_of) if merged else None


def merge_limits(api_limits: list[Limit], feed: dict[str, tuple[float, float]] | None) -> list[Limit]:
    """Overlay the passive feed onto the last API snapshot, monotonic per window."""
    limits = [replace(l) for l in api_limits]
    if not feed:
        return limits
    by_kind = {l.kind: l for l in limits}
    alias = {"session": "five_hour", "weekly_all": "seven_day"}
    for kind, (pct, reset_epoch) in feed.items():
        resets_at = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
        lim = by_kind.get(kind) or by_kind.get(alias[kind])
        if lim is None:
            limits.append(Limit(kind=kind, label=KIND_LABELS[kind], percent=pct, resets_at=resets_at))
            continue
        api_reset = lim.resets_at.timestamp() if lim.resets_at else 0.0
        if reset_epoch > api_reset + 120:      # feed sees a newer window instance
            lim.percent, lim.resets_at = pct, resets_at
        elif reset_epoch > api_reset - 120:    # same window: usage only climbs
            lim.percent = pct if lim.percent is None else max(lim.percent, pct)
        # else: feed entry is for an already-ended window; keep API data
    limits.sort(key=lambda l: KIND_ORDER.get(l.kind, 99))
    return limits


def recompute(state: State) -> list[str]:
    """Rebuild the merged view + history; return notification-worthy events."""
    now_ts = time.time()
    with state.lock:
        merged = merge_limits(state.api_limits, state.feed)
        events = detect_events(state.merged_prev, merged) if state.merged_prev is not None else []
        state.merged_prev = merged
        state.limits = merged
        for lim in merged:
            if lim.percent is None:
                continue
            hist = state.history.setdefault(lim.kind, [])
            if hist and now_ts - hist[-1][0] < 30 and hist[-1][1] == lim.percent:
                continue  # nothing new; heartbeat at most every 30s
            hist.append((now_ts, lim.percent))
            cutoff = now_ts - HISTORY_SPAN
            while hist and hist[0][0] < cutoff:
                hist.pop(0)
    return events


# ------------------------------------------------------------- notifications


def detect_events(prev: list[Limit], new: list[Limit]) -> list[str]:
    """State transitions worth interrupting someone for — never steady-state."""
    events: list[str] = []
    prev_by = {l.kind: l for l in prev}
    for lim in new:
        p = prev_by.get(lim.kind)
        if p is None or p.percent is None or lim.percent is None:
            continue
        for t in NOTIFY_THRESHOLDS:
            if p.percent < t <= lim.percent:
                events.append(f"{lim.label} at {lim.percent:.0f}%")
                break
        if lim.percent <= p.percent - RESET_DROP:
            events.append(f"{lim.label} reset — now {lim.percent:.0f}%")
    prev_active = next((l.kind for l in prev if l.is_active), None)
    new_active = next((l for l in new if l.is_active), None)
    if new_active and prev_active and new_active.kind != prev_active:
        pct = f" ({new_active.percent:.0f}%)" if new_active.percent is not None else ""
        events.append(f"Now limiting: {new_active.label}{pct}")
    return events


def send_notification(msg: str) -> None:
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(msg)} with title "Claude Usage Monitor"'
        subprocess.run(["osascript", "-e", script], capture_output=True)
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "display-message", "-d", "4000", f"⚠ {msg}"], capture_output=True)


# ------------------------------------------------------------------- polling


def poll_loop(state: State, interval: int, stop: threading.Event, notify: bool = False) -> None:
    backoff = 0
    while not stop.is_set():
        delay = interval
        try:
            token, sub = read_access_token()
            payload = fetch_usage(token)
            api_limits = parse_limits(payload)
            credits = parse_credits(payload)
            with state.lock:
                state.api_limits = api_limits
                state.credits = credits
                state.subscription = sub
                state.fetched_at = datetime.now(timezone.utc)
                state.status = "live"
                state.status_style = "green"
                state.retry_at = None
                feed_fresh = state.feed_as_of is not None and time.time() - state.feed_as_of < FEED_MAX_AGE
            for msg in recompute(state):
                if notify:
                    send_notification(msg)
            backoff = 0
            if feed_fresh:
                # the feed keeps session/weekly fresh; the API is only needed
                # for scoped limits, is_active, severity, and credits
                delay = max(interval, API_RELAXED_INTERVAL)
        except RateLimited as exc:
            with state.lock:
                have_data = bool(state.limits)
            if have_data:
                backoff = min(backoff * 2, 300) if backoff else 30
                delay = exc.retry_after or backoff
            else:
                # nothing to show yet — the limit is bursty, retry eagerly
                delay = 15
            delay += random.randint(0, 5)  # avoid syncing with statusline pollers
            with state.lock:
                cached = " · showing cached data" if have_data else ""
                state.status = f"rate limited{cached}"
                state.status_style = "yellow"
                state.retry_at = time.time() + delay
        except AuthError as exc:
            delay = max(interval, 60)
            with state.lock:
                state.status = str(exc)
                state.status_style = "red"
                state.retry_at = time.time() + delay
        except Exception as exc:  # network blips etc.
            backoff = min(backoff * 2, 300) if backoff else 15
            delay = backoff
            with state.lock:
                state.status = f"error: {exc}"
                state.status_style = "red"
                state.retry_at = time.time() + delay
        stop.wait(delay)


def feed_loop(state: State, stop: threading.Event, notify: bool = False) -> None:
    while not stop.is_set():
        result = read_feed()
        with state.lock:
            state.feed, state.feed_as_of = result if result else (None, None)
        for msg in recompute(state):
            if notify:
                send_notification(msg)
        stop.wait(FEED_POLL)


# ------------------------------------------------------------------ rendering


def fmt_delta(target: datetime | None, now: datetime) -> str:
    if target is None:
        return "—"
    secs = int((target - now).total_seconds())
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    if mins:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def fmt_clock(target: datetime | None) -> str:
    if target is None:
        return ""
    local = target.astimezone()
    now = datetime.now().astimezone()
    day = "" if local.date() == now.date() else local.strftime("%a ")
    return day + local.strftime("%-I:%M %p").lower()


def fmt_ago(then: datetime, now: datetime) -> str:
    secs = int((now - then).total_seconds())
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    return f"{secs // 60}m ago"


def pct_color(pct: float) -> str:
    if pct >= 90:
        return "#e05252"
    if pct >= 75:
        return "#d97757"
    if pct >= 50:
        return "#d9a765"
    return "#8fbf6f"


def util_color(limit: Limit) -> str:
    if limit.severity in SEVERITY_STYLES:
        return SEVERITY_STYLES[limit.severity]
    if limit.percent is None:
        return DIM
    return pct_color(limit.percent)


def compute_pace(samples: list[tuple[float, float]] | None, now_ts: float) -> float | None:
    """Percent-per-hour over the recent lookback, restarted after any reset."""
    if not samples:
        return None
    recent = [(t, p) for t, p in samples if t >= now_ts - PACE_LOOKBACK]
    start = 0
    for i in range(1, len(recent)):
        if recent[i][1] < recent[i - 1][1] - 10:  # window reset mid-lookback
            start = i
    recent = recent[start:]
    if len(recent) < 3 or recent[-1][0] - recent[0][0] < PACE_MIN_SPAN:
        return None
    (t0, p0), (t1, p1) = recent[0], recent[-1]
    return (p1 - p0) / (t1 - t0) * 3600


def fmt_span(secs: int) -> str:
    hours, rem = divmod(secs, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def render_sparkline(samples: list[tuple[float, float]], width: int, now_ts: float) -> Group | None:
    if len(samples) < 2:
        return None
    span = now_ts - samples[0][0]
    if span < 120:
        return None
    gaps = sorted(b[0] - a[0] for a, b in zip(samples, samples[1:]))
    median_gap = gaps[len(gaps) // 2]
    bucket = max(span / width, median_gap, 30.0)
    n = min(width, int(span / bucket) + 1)

    row = Text()
    for i in range(n):
        lo = now_ts - (n - i) * bucket
        hi = lo + bucket
        val = None
        for t, p in samples:
            if lo <= t < hi:
                val = p
        if val is None:
            row.append("·", style=BAR_EMPTY)
        else:
            row.append(SPARK_CHARS[min(7, int(val / 100 * 8))], style=pct_color(val))

    header = Text()
    header.append("Session history", style="bold")
    right = Text(f"last {fmt_span(int(span))}", style=DIM)
    pad = max(2, width - header.cell_len - right.cell_len)
    return Group(Text.assemble(header, " " * pad, right), row)


def usage_bar(pct: float | None, color: str, width: int) -> Text:
    bar = Text()
    if pct is None:
        bar.append("░" * width, style=BAR_EMPTY)
        return bar
    cells = max(0.0, min(100.0, pct)) / 100 * width
    full = int(cells)
    half = cells - full >= 0.5
    bar.append("█" * full, style=color)
    if full < width:
        bar.append("▌" if half else "░", style=color if half else BAR_EMPTY)
        bar.append("░" * (width - full - 1), style=BAR_EMPTY)
    return bar


def pace_line(limit: Limit, rate: float | None, now: datetime) -> Text | None:
    if rate is None or limit.percent is None:
        return None
    line = Text()
    line.append("▸ ", style=DIM)
    if abs(rate) < 0.1:
        line.append("pace steady", style=DIM)
        return line
    line.append(f"pace {rate:+.1f}%/h", style=DIM)
    if rate > 0 and limit.resets_at is not None:
        hours_left = (limit.resets_at - now).total_seconds() / 3600
        if hours_left > 0:
            projected = limit.percent + rate * hours_left
            line.append("  ·  ", style=DIM)
            if projected >= 100:
                hit = now + timedelta(hours=(100 - limit.percent) / rate)
                line.append(f"hits 100% ~{fmt_clock(hit)}, before reset", style=f"bold {pct_color(95)}")
            else:
                line.append("~", style=DIM)
                line.append(f"{projected:.0f}%", style=pct_color(projected))
                line.append(" at reset", style=DIM)
    return line


def render_limit(limit: Limit, now: datetime, width: int, rate: float | None = None) -> Group:
    color = util_color(limit)
    pct_txt = f"{limit.percent:.1f}%" if limit.percent is not None else "—"

    left = Text()
    left.append(limit.label, style="bold")
    if limit.is_active:
        left.append("  ◂ limiting", style=f"italic {CLAUDE}")

    right = Text()
    right.append(pct_txt, style=f"bold {color}")

    pad = max(2, width - left.cell_len - right.cell_len)
    line1 = Text.assemble(left, " " * pad, right)

    reset = Text()
    countdown = fmt_delta(limit.resets_at, now)
    if limit.resets_at is None:
        reset.append("⟳ ", style=DIM)
        reset.append("no reset scheduled", style=DIM)
    elif countdown == "now":
        reset.append("⟳ ", style=CLAUDE)
        reset.append("resetting now…", style=f"bold {CLAUDE}")
    else:
        reset.append("⟳ ", style=CLAUDE)
        reset.append(countdown, style="bold bright_white")
        reset.append("  until reset", style=DIM)
    clock = fmt_clock(limit.resets_at)
    if clock:
        pad2 = max(2, width - reset.cell_len - len(clock))
        reset.append(" " * pad2)
        reset.append(clock, style=DIM)

    parts = [line1, usage_bar(limit.percent, color, width), reset]
    pace = pace_line(limit, rate, now)
    if pace is not None:
        parts.append(pace)
    return Group(*parts)


def render(state: State, console: Console) -> Panel:
    now = datetime.now(timezone.utc)
    width = max(30, min(console.size.width - 10, 72))

    with state.lock:
        limits = list(state.limits)
        credits = state.credits
        history = {k: list(v) for k, v in state.history.items()}
        subscription = state.subscription
        status = state.status
        status_style = state.status_style
        retry_at = state.retry_at
        fetched = state.fetched_at
        feed_as_of = state.feed_as_of
    now_ts = now.timestamp()

    body: list = [Text()]

    logo = Text("\n".join(LOGO), style=CLAUDE)
    title = Text()
    title.append("Claude Usage Monitor", style=f"bold {CLAUDE}")
    if subscription:
        title.append(f"  ·  Claude {subscription.capitalize()}", style=DIM)
    body.append(Align.center(logo))
    body.append(Text())
    body.append(Align.center(title))
    body.append(Text())
    body.append(Text())

    if not limits:
        body.append(Align.center(Text("waiting for first response…", style=DIM)))
        body.append(Text())
    for limit in limits:
        rate = compute_pace(history.get(limit.kind), now_ts)
        body.append(render_limit(limit, now, width, rate))
        body.append(Text())

    session_hist = history.get("session") or history.get("five_hour")
    if session_hist:
        spark = render_sparkline(session_hist, width, now_ts)
        if spark is not None:
            body.append(spark)
            body.append(Text())

    if credits.enabled and credits.used is not None:
        line = Text()
        line.append("Extra usage credits  ", style="bold")
        line.append(f"${credits.used:.2f} used", style=DIM)
        if credits.limit:
            line.append(f" of ${credits.limit:.2f}/mo", style=DIM)
        body.append(line)
        body.append(Text())

    foot = Text()
    feed_fresh = feed_as_of is not None and now_ts - feed_as_of < FEED_MAX_AGE
    if feed_fresh:
        # session/weekly numbers stream in passively; the API channel is auxiliary
        foot.append("● live", style="green")
        foot.append(f"  ·  feed {fmt_ago(datetime.fromtimestamp(feed_as_of, tz=timezone.utc), now)}", style=DIM)
        if status_style == "green":
            if fetched:
                foot.append(f"  ·  api {fmt_ago(fetched, now)}", style=DIM)
        else:
            foot.append(f"  ·  api: {status}", style=DIM)
            if retry_at is not None:
                if retry_at > now_ts:
                    countdown = fmt_delta(datetime.fromtimestamp(retry_at, tz=timezone.utc), now)
                    foot.append(", retry in ", style=DIM)
                    foot.append(countdown, style=DIM)
                else:
                    foot.append(", retrying…", style=DIM)
    else:
        dot = {"green": "●", "yellow": "◐", "red": "✗"}.get(status_style, "●")
        foot.append(f"{dot} {status}", style=status_style)
        if retry_at is not None and status_style != "green":
            if retry_at > now_ts:
                countdown = fmt_delta(datetime.fromtimestamp(retry_at, tz=timezone.utc), now)
                foot.append("  ·  retrying in ", style=DIM)
                foot.append(countdown, style="bold")
            else:
                foot.append("  ·  retrying…", style=DIM)
        if fetched:
            foot.append(f"  ·  updated {fmt_ago(fetched, now)}", style=DIM)
    body.append(Align.center(foot))

    return Panel(
        Group(*body),
        box=box.ROUNDED,
        border_style=BORDER,
        padding=(0, 3),
        title=Text(" ✳ usage ", style=CLAUDE),
        title_align="left",
        width=width + 8,
    )


# ---------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="Live monitor for Claude Code usage limits")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("CLAUDE_MONITOR_INTERVAL", "60")),
        help="poll interval in seconds (default 60, min 30)",
    )
    parser.add_argument("--once", action="store_true", help="fetch once, print raw JSON, and exit")
    parser.add_argument(
        "--mute-statusline",
        action="store_true",
        help="hide the Claude Code statusline in all sessions while the monitor runs (restored on exit)",
    )
    parser.add_argument(
        "--lite-statusline",
        action="store_true",
        help="switch the statusline to lite (context info only, no usage polling) while the monitor runs (restored on exit)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="send a notification (macOS + tmux message) on threshold crossings, window resets, and limiting changes",
    )
    args = parser.parse_args()

    console = Console()

    if args.once:
        token, _ = read_access_token()
        for attempt in range(8):
            try:
                console.print_json(json.dumps(fetch_usage(token)))
                return
            except RateLimited as exc:
                wait = (exc.retry_after or 15) + random.randint(0, 5)
                console.print(f"[yellow]rate limited, retrying in {wait}s ({attempt + 1}/8)…[/yellow]")
                time.sleep(wait)
        console.print("[red]still rate limited, giving up[/red]")
        sys.exit(1)

    flag = STATUSLINE_OFF if args.mute_statusline else STATUSLINE_LITE if args.lite_statusline else None
    created_flag = None
    if flag is not None and flag.parent.is_dir() and not flag.exists():
        flag.touch()
        created_flag = flag

    state = State()
    stop = threading.Event()
    thread = threading.Thread(
        target=poll_loop, args=(state, max(args.interval, 30), stop, args.notify), daemon=True
    )
    thread.start()
    threading.Thread(target=feed_loop, args=(state, stop, args.notify), daemon=True).start()

    def bye(*_):
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)  # tmux pane closed

    try:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while not stop.is_set():
                live.update(Align.center(render(state, console), vertical="middle"))
                stop.wait(0.5)
    finally:
        if created_flag is not None:
            created_flag.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
