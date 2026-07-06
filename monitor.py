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
import math
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

import accounts

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
FEED_KIND_ALIASES = {"session": "five_hour", "weekly_all": "seven_day"}
PLAN_LIMIT_KINDS = {"session", "five_hour", "weekly_all", "seven_day", "weekly_scoped"}

# History persists across restarts so the sparkline keeps the day's shape
HISTORY_FILE = Path.home() / ".claude" / "usage-monitor-history.json"
HISTORY_SAVE_EVERY = 60        # seconds between background saves

# Today's usage-credit spend is tracked as a delta from the day's first sample;
# the baseline persists so a monitor restarted mid-day keeps counting from midnight
CREDITS_FILE = Path.home() / ".claude" / "usage-monitor-credits.json"
PLAN_EXHAUSTED_AT = 99.5       # a plan window at/above this % is "maxed" — usage now spills to credits

# Saved accounts (claude-account): poll each non-live account's usage on its
# own token — separate rate-limit budgets, so this never crowds the main poll
ACCOUNTS_POLL = 300            # per-account usage cadence
ACCOUNTS_TICK = 20             # how often the accounts thread wakes up
ACCOUNTS_STAGGER = 10          # spread the first polls out at startup
ACCOUNT_LABELS = {"session": "5h", "five_hour": "5h", "weekly_all": "wk", "seven_day": "wk"}

# Auto-rotate (opt-in, --autorotate): when the live login hits its limit, switch
# to a saved login that still has headroom so running sessions resume on their
# next retry. Only acts with >= 2 saved accounts.
AUTOROTATE_AT = 100.0          # switch once the live limiting window reaches this %
AUTOROTATE_COOLDOWN = 120      # min seconds between auto-switches (anti-flap)


def prettify_key(key: str) -> str:
    return key.replace("_", " ").strip().title()


def to_float(value) -> float | None:
    """Finite float or None — API/feed payloads are untrusted input."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def limit_key(limit: "Limit") -> str:
    """Stable identity for history/events; scoped limits share a kind."""
    if limit.kind in ("session", "five_hour"):
        return "session"
    if limit.kind in ("weekly_all", "seven_day"):
        return "weekly_all"
    return f"{limit.kind}:{limit.label}"


# ---------------------------------------------------------------- credentials


class AuthError(Exception):
    pass


def read_access_token() -> tuple[str, str]:
    """Return (access_token, subscription_type) from the local Claude Code login."""
    raw = None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=20,  # a Keychain GUI prompt must not wedge the poll thread
            )
        except subprocess.TimeoutExpired:
            raise AuthError("keychain read timed out — is the Keychain locked?")
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
    """Usage-credit spend for one login. All money is in major units (e.g. dollars)."""
    enabled: bool = False
    used: float | None = None        # spent to date this period
    limit: float | None = None       # spend cap, if the account/plan sets one
    balance: float | None = None     # prepaid balance remaining, if any
    utilization: float | None = None
    currency: str = "USD"

    @property
    def available(self) -> float | None:
        """Spend headroom in money, or None when the account is uncapped.

        A prepaid balance is the remaining amount directly; a spend cap gives
        headroom as cap minus used. Uncapped pay-as-you-go accounts (and orgs
        that meter the cap centrally, so the per-member payload reports null)
        expose neither — callers fall back to showing `used`.
        """
        if self.balance is not None:
            return max(0.0, self.balance)   # an overdrawn balance is 0 left, not negative
        if self.limit is not None and self.used is not None:
            return max(0.0, self.limit - self.used)
        return None


def parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                return None
            return datetime.fromtimestamp(value, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo is None:  # naive timestamps would crash aware-datetime math later
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def limit_label(entry: dict) -> str:
    kind = entry.get("kind", "")
    template = KIND_LABELS.get(kind)
    if template is None:
        return prettify_key(kind or entry.get("group", "limit"))
    scope = ""
    if "{scope}" in template:
        scope_obj = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        model = scope_obj.get("model")
        model = model if isinstance(model, dict) else {}
        surface = scope_obj.get("surface")
        scope = model.get("display_name") or surface or "scoped"
    return template.format(scope=scope)


def parse_limits(payload: dict) -> list[Limit]:
    limits: list[Limit] = []
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        limits.append(
            Limit(
                kind=entry.get("kind", ""),
                label=limit_label(entry),
                percent=to_float(entry.get("percent")),
                resets_at=parse_dt(entry.get("resets_at")),
                severity=entry.get("severity") or "normal",
                is_active=bool(entry.get("is_active")),
            )
        )
    if not limits:  # older payload shape fallback
        for key, label in (("five_hour", "Session · 5h window"), ("seven_day", "Week · all models")):
            val = payload.get(key)
            if isinstance(val, dict):
                limits.append(
                    Limit(
                        kind=key,
                        label=label,
                        percent=to_float(val.get("utilization")),
                        resets_at=parse_dt(val.get("resets_at")),
                    )
                )
    limits.sort(key=lambda l: KIND_ORDER.get(l.kind, 99))
    return limits


def _clamp_exp(value, default: int = 2) -> int:
    """A sane money exponent (decimal places): a small non-negative int."""
    try:
        exp = int(value)
    except (TypeError, ValueError):
        return default
    return exp if 0 <= exp <= 12 else default


def money_to_major(block, *, exponent_default: int = 2) -> float | None:
    """Convert an API money value to major units (e.g. dollars).

    The usage API's canonical shape is {"amount_minor", "currency", "exponent"}
    (spend.used): amount_minor / 10**exponent. Older fields carry a bare
    minor-unit number instead (extra_usage.used_credits, scaled by
    decimal_places) — pass those as the number with exponent_default set.
    Returns None for anything unparseable; the payload is untrusted input.
    """
    if isinstance(block, dict):
        minor, exp = to_float(block.get("amount_minor")), _clamp_exp(block.get("exponent"), exponent_default)
    else:
        minor, exp = to_float(block), exponent_default
    if minor is None:
        return None
    return minor / (10 ** exp)


def parse_credits(payload: dict) -> Credits:
    """Usage-credit spend, normalised to major units.

    `spend` is the canonical, self-describing source (money objects with an
    explicit exponent); `extra_usage` is an older, flatter shape whose
    *_credits/limit values are minor units scaled by decimal_places. Prefer
    spend, fall back to extra_usage field by field so partial payloads still
    populate. NB: extra_usage.used_credits is minor units — e.g. 14179 with
    decimal_places 2 is $141.79, not $14179.
    """
    extra = payload.get("extra_usage") or {}
    spend = payload.get("spend") or {}
    dp = _clamp_exp(extra.get("decimal_places"))

    used = money_to_major(spend.get("used"))
    if used is None and extra.get("used_credits") is not None:
        used = money_to_major(extra.get("used_credits"), exponent_default=dp)

    # Cap: spend.cap/spend.limit (money objects) then extra_usage.monthly_limit
    # (minor units, shape inferred — every login we can see is uncapped).
    limit = money_to_major(spend.get("cap"))
    if limit is None:
        limit = money_to_major(spend.get("limit"))
    if limit is None and extra.get("monthly_limit") is not None:
        limit = money_to_major(extra.get("monthly_limit"), exponent_default=dp)

    balance = money_to_major(spend.get("balance"))

    used_block = spend.get("used") if isinstance(spend.get("used"), dict) else {}
    currency = used_block.get("currency") or extra.get("currency") or "USD"

    return Credits(
        enabled=bool(spend.get("enabled") or extra.get("is_enabled")),
        used=used,
        limit=limit,
        balance=balance,
        utilization=to_float(extra.get("utilization")),
        currency=str(currency) or "USD",
    )


@dataclass
class AccountRow:
    name: str
    email: str
    account_uuid: str
    is_live: bool = False
    limits: list[Limit] = field(default_factory=list)
    credits: Credits = field(default_factory=Credits)
    fetched_at: float | None = None
    note: str = ""


@dataclass
class State:
    limits: list[Limit] = field(default_factory=list)      # merged view (rendered)
    api_limits: list[Limit] = field(default_factory=list)  # last full API payload
    feed: dict[str, tuple[float, float]] | None = None     # kind -> (pct, resets_epoch)
    feed_as_of: float | None = None
    merged_prev: list[Limit] | None = None                 # for event detection
    credits: Credits = field(default_factory=Credits)
    credit_today: float | None = None      # $ spent on credits today (computed from `used`)
    credit_day: str = ""                   # local YYYY-MM-DD the baseline below belongs to
    credit_baseline: float | None = None   # `used` at the first sample seen that day
    credit_account: str = ""               # login uuid the baseline was measured on
    credits_owner: str = ""                # login uuid the current `credits` came from
    login_epoch: int = 0                   # bumped on any login switch; stamps poll provenance
    live_uuid: str = ""                    # login the panel's api_limits/credits describe
    history: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # kind -> [(epoch, pct)]
    accounts: list[AccountRow] = field(default_factory=list)
    accounts_warning: str = ""
    autorotate: bool = False       # --autorotate: auto-switch on limit (shown in the panel)
    autorotate_note: str = ""      # last auto-rotate action, for the panel
    subscription: str = ""
    fetched_at: datetime | None = None
    status: str = "starting…"
    status_style: str = DIM
    retry_at: float | None = None  # epoch of the next poll attempt while backing off
    lock: threading.Lock = field(default_factory=threading.Lock)
    # set to cut the poll thread's sleep short (login switch → re-poll now)
    poll_wake: threading.Event = field(default_factory=threading.Event)


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
        captured_at = to_float(data.get("captured_at"))
        observed_at = mtime
        if captured_at is not None:
            if captured_at > now_ts + 5 or now_ts - captured_at > FEED_MAX_AGE:
                continue
            observed_at = captured_at
        rl = data.get("rate_limits")
        if isinstance(rl, dict):
            entries.append(rl)
            as_of = max(as_of, observed_at)
    if not entries:
        return None
    merged: dict[str, tuple[float, float]] = {}
    for feed_key, kind in FEED_KINDS.items():
        cands = []
        for rl in entries:
            w = rl.get(feed_key)
            if not isinstance(w, dict):
                continue
            pct = to_float(w.get("used_percentage"))
            rst = to_float(w.get("resets_at"))
            if pct is None or rst is None:
                continue
            cands.append((rst, pct))
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
    for kind, (pct, reset_epoch) in feed.items():
        try:
            resets_at = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        alias = FEED_KIND_ALIASES.get(kind)
        lim = next((l for l in limits if l.kind == kind), None)
        if lim is None and alias is not None:
            lim = next((l for l in limits if l.kind == alias), None)
        if lim is None:
            limits.append(Limit(kind=kind, label=KIND_LABELS.get(kind, prettify_key(kind)),
                                percent=pct, resets_at=resets_at))
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
            hist = state.history.setdefault(limit_key(lim), [])
            if hist and now_ts <= hist[-1][0]:
                continue  # wall clock jumped backwards; keep history ordered
            if hist and now_ts - hist[-1][0] < 30 and hist[-1][1] == lim.percent:
                continue  # nothing new; heartbeat at most every 30s
            hist.append((now_ts, lim.percent))
            cutoff = now_ts - HISTORY_SPAN
            while hist and hist[0][0] < cutoff:
                hist.pop(0)
        _track_credit_today(state, now_ts)
    return events


def _track_credit_today(state: State, now_ts: float) -> None:
    """Maintain today's credit spend from the cumulative `used` counter.

    Baseline = `used` at the first sample on/after local midnight; today's
    spend = current used - baseline. A day rollover rebaselines; a mid-day drop
    (a new billing period resetting the counter) rebaselines too, so the figure
    never runs negative. A login change (claude-account switch / auto-rotate)
    also rebaselines: `used` is cumulative per account, so a delta across two
    different logins would be meaningless. Must be called with state.lock held.
    """
    c = state.credits
    if not (c.enabled and c.used is not None):
        return
    today = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")  # local calendar day
    if (state.credit_day != today or state.credit_baseline is None
            or c.used < state.credit_baseline or state.credits_owner != state.credit_account):
        state.credit_day = today
        state.credit_baseline = c.used
        state.credit_account = state.credits_owner
    state.credit_today = max(0.0, c.used - state.credit_baseline)


# ------------------------------------------------------------- notifications


def detect_events(prev: list[Limit], new: list[Limit]) -> list[str]:
    """State transitions worth interrupting someone for — never steady-state."""
    events: list[str] = []
    prev_by = {limit_key(l): l for l in prev}
    for lim in new:
        p = prev_by.get(limit_key(lim))
        if p is None or p.percent is None or lim.percent is None:
            continue
        for t in NOTIFY_THRESHOLDS:
            if p.percent < t <= lim.percent:
                events.append(f"{lim.label} at {lim.percent:.0f}%")
                break
        if lim.percent <= p.percent - RESET_DROP:
            events.append(f"{lim.label} reset — now {lim.percent:.0f}%")
    prev_active = next((limit_key(l) for l in prev if l.is_active), None)
    new_active = next((l for l in new if l.is_active), None)
    if new_active and prev_active and limit_key(new_active) != prev_active:
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
            with state.lock:
                epoch = state.login_epoch
            # whose counters these are — read BEFORE the token, so a switch
            # landing between the two reads shows up as an owner change below
            owner = accounts.active_account_uuid()
            token, sub = read_access_token()
            payload = fetch_usage(token)
            api_limits = parse_limits(payload)
            credits = parse_credits(payload)
            feed_fresh = False
            owner_now = accounts.active_account_uuid()
            with state.lock:
                # A login switch that landed while this request was in flight
                # (auto-rotate bumps the epoch; a claude-account switch or
                # manual /login moves the uuid) means the response describes
                # the OLD login — writing it back would show one account's
                # numbers under another's name
                switched_mid_poll = state.login_epoch != epoch or owner_now != owner
                if not switched_mid_poll:
                    state.api_limits = api_limits
                    state.credits = credits
                    state.credits_owner = owner
                    if owner:
                        state.live_uuid = owner
                    state.subscription = sub
                    state.fetched_at = datetime.now(timezone.utc)
                    state.status = "live"
                    state.status_style = "green"
                    state.retry_at = None
                    feed_fresh = state.feed_as_of is not None and time.time() - state.feed_as_of < FEED_MAX_AGE
            if switched_mid_poll:
                delay = 2  # drop the result and re-poll under the new login
            else:
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
        if state.poll_wake.wait(delay):
            state.poll_wake.clear()  # kicked: a login switch wants an immediate re-poll


def feed_loop(state: State, stop: threading.Event, notify: bool = False) -> None:
    last_save = time.time()
    while not stop.is_set():
        try:
            result = read_feed()
            with state.lock:
                state.feed, state.feed_as_of = result if result else (None, None)
            for msg in recompute(state):
                if notify:
                    send_notification(msg)
            if time.time() - last_save >= HISTORY_SAVE_EVERY:
                save_history(state)
                save_credit_baseline(state)
                last_save = time.time()
        except Exception:
            pass  # a malformed feed file must never kill this thread
        stop.wait(FEED_POLL)


def _max_pct(limits: list[Limit]) -> float | None:
    """Fullness of a login: the highest window percent, or None if unknown."""
    ps = [l.percent for l in limits if l.percent is not None]
    return max(ps) if ps else None


def autorotate_once(state: State, rows: list[AccountRow], next_poll: dict[str, float],
                    last_action_ts: float) -> float:
    """When the live login is maxed, switch to a saved login that still has
    headroom so blocked sessions resume on their next retry. Returns the
    timestamp of the last rotation *action* — a switch, or a full pass that
    found every login maxed (advancing it then paces the confirm polls to one
    pass per cooldown instead of one per tick). Best-effort — never raises.
    """
    now = time.time()
    if now - last_action_ts < AUTOROTATE_COOLDOWN:
        return last_action_ts  # just acted; let the live numbers settle
    if accounts.read_marker() is not None:
        return last_action_ts  # half-applied switch pending heal — don't stack another
    with state.lock:
        live_limits = list(state.limits)
        live_fetched = state.fetched_at.timestamp() if state.fetched_at else 0.0
        feed_as_of = state.feed_as_of or 0.0
    live_max = _max_pct(live_limits)
    if live_max is None or live_max < AUTOROTATE_AT:
        return last_action_ts
    # Only act on live numbers gathered AFTER the last action — otherwise a
    # stale 100% from the previous login would trigger an instant re-switch. On
    # switch we drop that login's api_limits so the merged view falls back to
    # the freshly-cleared feed; whichever source is newer must post-date it.
    if max(live_fetched, feed_as_of) < last_action_ts:
        return last_action_ts
    # Ground truth before acting: the state's 100% can be a pre-reset snapshot,
    # or feed data from a still-running session that hasn't retried onto the
    # current login yet. One authoritative poll of the live login decides; a
    # 429 doesn't dispute the data, so only a *successful* poll can veto.
    try:
        fresh_live = parse_limits(fetch_usage(read_access_token()[0]))
    except RateLimited:
        fresh_live = None              # endpoint busy — trust the recency-guarded view
    except Exception:
        return last_action_ts          # can't reach the API — retry next tick
    if fresh_live is not None:
        fresh_max = _max_pct(fresh_live)
        if fresh_max is None or fresh_max < AUTOROTATE_AT:
            with state.lock:           # not actually maxed (e.g. just reset) — correct the panel
                state.api_limits = fresh_live
                state.fetched_at = datetime.now(timezone.utc)
            recompute(state)
            return last_action_ts
    # Try other logins best-known-headroom first; confirm capacity with a fresh
    # poll at the moment of need (cheap — this only runs when actually blocked).
    for cand in sorted((r for r in rows if not r.is_live),
                       key=lambda r: _max_pct(r.limits) if _max_pct(r.limits) is not None else 1e9):
        try:
            slot = accounts.ensure_fresh_slot(cand.name)
            fresh = parse_limits(fetch_usage(slot["claudeAiOauth"]["accessToken"]))
        except Exception:
            continue
        with state.lock:  # rows are shared with the render thread
            cand.limits = fresh
            cand.fetched_at = time.time()
        if (_max_pct(fresh) or 0.0) >= AUTOROTATE_AT:
            continue  # this login is maxed too
        try:
            accounts.cmd_switch(cand.name, quiet=True)
        except accounts.AccountError:
            continue
        # the new live login leaves the per-account rotation; everyone else
        # keeps their schedule (including rate-limit/error backoffs)
        next_poll.pop(cand.name, None)
        with state.lock:
            for r in rows:
                r.is_live = r.name == cand.name
            state.login_epoch += 1     # in-flight polls of the old login must be dropped
            state.live_uuid = cand.account_uuid  # accounts_loop must not re-clear this switch
            state.api_limits = []      # the old login's API numbers no longer apply
            state.credits = Credits()  # ...nor its credit spend, until the next poll
            state.fetched_at = None    # force a fresh poll under the new login
            state.merged_prev = None   # don't emit cross-login "reset" events
            state.autorotate_note = f"⟳ {time.strftime('%H:%M')} auto-rotated to {cand.name} — previous login hit 100%"
        state.poll_wake.set()  # fill the panel from the new login now, not next cycle
        return time.time()
    with state.lock:
        state.autorotate_note = f"⚠ {time.strftime('%H:%M')} all logins at their limit — waiting for a reset"
    return time.time()  # a full (failed) pass counts as the action, pacing the next one


def accounts_loop(state: State, stop: threading.Event, autorotate: bool = False) -> None:
    """Track saved accounts (claude-account) and poll the non-live ones.

    The live account's numbers come from the main panel's data; every other
    slot gets its own relaxed usage poll, refreshing (and persisting) its
    token via the accounts store when stale. With autorotate on, a maxed live
    login is swapped for one that still has headroom.
    """
    next_poll: dict[str, float] = {}
    last_action_ts = 0.0
    while not stop.is_set():
        try:  # a bad index file or store hiccup must never kill this thread
            index = accounts.load_index()
            live_uuid = accounts.active_account_uuid()
            switched = False
            with state.lock:
                if live_uuid and state.live_uuid and live_uuid != state.live_uuid:
                    # The login changed outside this process (claude-account
                    # switch, or a manual /login). Every number on the panel
                    # still describes the OLD login — drop it all and show
                    # placeholders until a poll of the new login lands, rather
                    # than another account's data under this login's name.
                    switched = True
                    state.login_epoch += 1        # drop in-flight polls of the old login
                    state.api_limits = []
                    state.credits = Credits()
                    state.credit_today = None
                    state.subscription = ""
                    state.fetched_at = None
                    state.merged_prev = None      # no cross-login "reset" notifications
                    state.feed = None             # feed snapshots described the old login
                    state.feed_as_of = None
                    state.status = "login switched — refreshing…"
                    state.status_style = DIM
                    state.retry_at = None
                if live_uuid:
                    state.live_uuid = live_uuid
                prev = {r.name: r for r in state.accounts}
            if switched:
                recompute(state)                  # rebuild the merged view (now empty)
                state.poll_wake.set()             # fetch the new login's numbers now
            rows: list[AccountRow] = []
            for i, acct in enumerate(index):
                old = prev.get(acct.name)
                row = replace(old) if old else AccountRow(acct.name, acct.email, acct.account_uuid)
                row.email, row.account_uuid = acct.email, acct.account_uuid
                row.is_live = bool(live_uuid) and acct.account_uuid == live_uuid
                rows.append(row)
                if row.is_live:
                    row.note = ""
                    continue
                now_ts = time.time()
                due = next_poll.setdefault(acct.name, now_ts + i * ACCOUNTS_STAGGER)
                if now_ts < due:
                    continue
                try:
                    slot = accounts.ensure_fresh_slot(acct.name)
                    payload = fetch_usage(slot["claudeAiOauth"]["accessToken"])
                    row.limits = parse_limits(payload)
                    row.credits = parse_credits(payload)
                    row.fetched_at = time.time()
                    row.note = ""
                    next_poll[acct.name] = time.time() + ACCOUNTS_POLL + random.randint(0, 30)
                except RateLimited as exc:
                    row.note = "rate limited"
                    next_poll[acct.name] = time.time() + (exc.retry_after or 120)
                except (AuthError, accounts.AccountError):
                    row.note = "needs /login"
                    next_poll[acct.name] = time.time() + 600
                except Exception:
                    row.note = "error"
                    next_poll[acct.name] = time.time() + 300
            warning = (
                "interrupted account switch — run any `claude-account` command to heal"
                if accounts.read_marker() is not None
                else ""
            )
            with state.lock:
                state.accounts = rows
                state.accounts_warning = warning
            if autorotate and len(rows) >= 2:
                last_action_ts = autorotate_once(state, rows, next_poll, last_action_ts)
        except Exception:
            pass
        stop.wait(ACCOUNTS_TICK)


# ---------------------------------------------------------------- persistence


def save_history(state: State) -> None:
    try:
        with state.lock:
            data = {k: [[round(t, 1), p] for t, p in v] for k, v in state.history.items()}
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(HISTORY_FILE)
    except OSError:
        pass


def load_history(state: State) -> None:
    try:
        data = json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return
    cutoff = time.time() - HISTORY_SPAN
    if not isinstance(data, dict):
        return
    with state.lock:
        for kind, samples in data.items():
            try:
                pairs = [(to_float(t), to_float(p)) for t, p in samples]
            except (TypeError, ValueError):
                continue
            clean = [(t, p) for t, p in pairs if t is not None and p is not None and t >= cutoff]
            if clean:
                state.history[kind] = clean


def save_credit_baseline(state: State) -> None:
    try:
        with state.lock:
            day, base, acct = state.credit_day, state.credit_baseline, state.credit_account
        if not day or base is None:
            return  # nothing observed yet
        tmp = CREDITS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day, "baseline": base, "account": acct}))
        tmp.replace(CREDITS_FILE)
    except OSError:
        pass


def load_credit_baseline(state: State) -> None:
    """Restore today's spend baseline so a restart keeps counting from midnight."""
    try:
        data = json.loads(CREDITS_FILE.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    day = str(data.get("day", ""))
    base = to_float(data.get("baseline"))
    # Only honour a baseline from *today* — a stale one would make "today" span days
    if not day or base is None or day != datetime.now().strftime("%Y-%m-%d"):
        return
    with state.lock:
        state.credit_day = day
        state.credit_baseline = base
        # If the first poll runs under a different login, the owner mismatch
        # in _track_credit_today discards this baseline rather than mixing accounts
        state.credit_account = str(data.get("account", ""))


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


SPARK_ROWS = 3           # chart height; each row covers a third of 0-100%
WINDOW_LEN = 5 * 3600    # the chart's x-axis spans one full 5h window


def render_sparkline(
    samples: list[tuple[float, float]], width: int, now_ts: float, window_end: float | None
) -> Group | None:
    """Chart of the CURRENT 5h window, reset boundary to reset boundary.

    Fills left to right as the window progresses; dots mark elapsed time with
    no data (monitor wasn't running), blank space is the window's remainder.
    """
    if window_end is None or len(samples) < 2:
        return None
    window_start = window_end - WINDOW_LEN
    bucket = WINDOW_LEN / width

    filled: dict[int, float] = {}
    in_window = 0
    for t, p in samples:  # sorted by time; last sample in a bucket wins
        if t < window_start or t > now_ts:
            continue
        in_window += 1
        filled[min(width - 1, int((t - window_start) / bucket))] = p
    if in_window < 2:
        return None

    elapsed_buckets = min(width, int((now_ts - window_start) / bucket) + 1)
    band = 100.0 / SPARK_ROWS
    rows = []
    for level in range(SPARK_ROWS - 1, -1, -1):  # top band first
        lo_bound = level * band
        row = Text()
        for i in range(width):
            val = filled.get(i)
            if i >= elapsed_buckets:
                row.append(" ")  # window hasn't gotten here yet
            elif val is None:
                row.append("·" if level == 0 else " ", style=BAR_EMPTY)
            elif val >= lo_bound + band:
                row.append("█", style=pct_color(val))
            elif val > lo_bound or level == 0:
                frac = max(0.0, (val - lo_bound) / band)
                row.append(SPARK_CHARS[max(0, min(7, int(frac * 8)))], style=pct_color(val))
            else:
                row.append(" ", style=BAR_EMPTY)
        rows.append(row)

    header = Text()
    header.append("Session history", style="bold")
    header.append("  0–100%", style=DIM)
    start_clock = fmt_clock(datetime.fromtimestamp(window_start, tz=timezone.utc))
    end_clock = fmt_clock(datetime.fromtimestamp(window_end, tz=timezone.utc))
    right = Text(f"{start_clock} → {end_clock}", style=DIM)
    pad = max(2, width - header.cell_len - right.cell_len)
    return Group(Text.assemble(header, " " * pad, right), *rows)


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


CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "$", "AUD": "$"}


def fmt_money(amount: float | None, currency: str = "USD") -> str:
    if amount is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {currency}"


def credits_active(limits: list[Limit], credits: Credits) -> bool:
    """True when new usage is spilling onto usage credits: credits are enabled
    and the binding plan window is maxed out. Prefer the API's is_active flag;
    without it, require all known session/weekly plan windows to be maxed.
    """
    if not (credits.enabled and credits.used is not None):
        return False
    known = [l for l in limits if l.kind in PLAN_LIMIT_KINDS and l.percent is not None]
    active = [l for l in known if l.is_active]
    if active:
        return any(l.percent >= PLAN_EXHAUSTED_AT for l in active)

    # Without the API's binding flag, only enter credits mode when the known
    # session and weekly plan windows are all exhausted. A single fresh 5h feed
    # sample at 100% is not enough evidence to hide the plan bars.
    has_session = any(l.kind in ("session", "five_hour") for l in known)
    has_weekly = any(l.kind in ("weekly_all", "seven_day", "weekly_scoped") for l in known)
    return has_session and has_weekly and all(l.percent >= PLAN_EXHAUSTED_AT for l in known)


def credit_binding_limit(limits: list[Limit], now: datetime) -> tuple[Limit | None, bool]:
    """Return the reset-bearing limit for credits mode, plus whether it is API-bound."""
    known = [l for l in limits if l.kind in PLAN_LIMIT_KINDS and l.percent is not None]
    active = next(
        (l for l in known if l.is_active and l.percent >= PLAN_EXHAUSTED_AT and l.resets_at is not None),
        None,
    )
    if active is not None:
        return active, True
    maxed = [l for l in known if l.percent >= PLAN_EXHAUSTED_AT and l.resets_at is not None]
    if not maxed:
        return None, False
    # An already-elapsed reset is stale data — a live future one beats it, or the
    # countdown would sit on "reset now…" while a real deadline exists
    future = [l for l in maxed if l.resets_at > now]
    return min(future or maxed, key=lambda l: l.resets_at), False


def render_credit_spend(credits: Credits, today: float | None, limits: list[Limit],
                        now: datetime) -> Group:
    """Focal view shown once usage spills onto credits: today's credit spend,
    centred and prominent, in place of the (all-maxed) plan bars. A compact
    reset line stays so you can see when plan quota — and free usage — returns.
    """
    parts: list = [Align.center(Text("Drawing on usage credits", style=f"bold {CLAUDE}")), Text()]
    parts.append(Align.center(Text(fmt_money(today if today is not None else 0.0, credits.currency),
                                   style=f"bold {CLAUDE}")))
    parts.append(Align.center(Text("spent today", style=DIM)))
    parts.append(Text())

    if credits.used is not None:
        sub = Text()
        sub.append(f"{fmt_money(credits.used, credits.currency)} used this month", style=DIM)
        avail = credits.available
        if avail is not None:
            sub.append("  ·  ", style=DIM)
            sub.append(f"{fmt_money(avail, credits.currency)} left", style=DIM)
        parts.append(Align.center(sub))

    binding, api_bound = credit_binding_limit(limits, now)
    if binding is not None:
        countdown = fmt_delta(binding.resets_at, now)
        label = binding.label if api_bound else "Next plan reset"
        line = Text()
        line.append("⟳ ", style=CLAUDE)
        if countdown == "now":
            verb = "resetting" if api_bound else "reset"
            line.append(f"{label} {verb} now…", style=f"bold {CLAUDE}")
        else:
            line.append(f"{label} resets in " if api_bound else f"{label} in ", style=DIM)
            line.append(countdown, style="bold bright_white")
        parts.append(Align.center(line))
    return Group(*parts)


def account_limit_label(limit: Limit) -> str:
    short = ACCOUNT_LABELS.get(limit.kind)
    if short:
        return short
    return limit.label.split("·")[-1].strip().lower()  # "Week · Fable" -> "fable"


def _fit_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if Text(text).cell_len <= width:
        return text + " " * (width - Text(text).cell_len)
    if width == 1:
        return "…"
    out = ""
    for ch in text:
        if Text(out + ch + "…").cell_len > width:
            break
        out += ch
    return out + "…" + " " * max(0, width - Text(out + "…").cell_len)


def _account_status(row: AccountRow, now: datetime) -> tuple[str, str]:
    if row.is_live:
        return "live", "green"
    if row.note:
        return row.note, "yellow"
    if row.fetched_at is not None:
        return fmt_ago(datetime.fromtimestamp(row.fetched_at, tz=timezone.utc), now), DIM
    return "", DIM


def _account_limit_for_column(limits: list[Limit], column: str) -> Limit | None:
    if column == "session":
        return next((l for l in limits if l.kind in ("session", "five_hour") and l.percent is not None), None)
    if column == "weekly":
        return next((l for l in limits if l.kind in ("weekly_all", "seven_day") and l.percent is not None), None)
    if column == "scoped":
        return next(
            (l for l in limits if l.kind not in ("session", "five_hour", "weekly_all", "seven_day")
             and l.percent is not None),
            None,
        )
    return None


def _account_limit_plain(limit: Limit | None, now: datetime) -> str:
    if limit is None or limit.percent is None:
        return ""
    text = f"{account_limit_label(limit)} {limit.percent:.0f}%"
    if limit.kind in ("session", "five_hour") and limit.percent >= 90 and limit.resets_at:
        text += f" ⟳{fmt_delta(limit.resets_at, now)}"
    return text


def _account_limit_cell(limit: Limit | None, now: datetime, width: int) -> Text:
    cell = Text()
    if limit is None or limit.percent is None:
        # no data is not zero — and never another login's number (a freshly
        # switched-to live row has nothing yet): show a placeholder
        cell.append(_fit_cells("—", width), style=DIM)
        return cell

    pct_txt = f"{limit.percent:.0f}%"
    pct_len = Text(pct_txt).cell_len
    reset_txt = ""
    if limit.kind in ("session", "five_hour") and limit.percent >= 90 and limit.resets_at:
        reset_txt = f" ⟳{fmt_delta(limit.resets_at, now)}"
    reset_len = Text(reset_txt).cell_len

    if width <= pct_len:
        cell.append(_fit_cells(pct_txt, width), style=pct_color(limit.percent))
        return cell

    label_width = width - pct_len - 1
    if reset_txt and label_width - reset_len >= 2:
        label_width -= reset_len
    else:
        reset_txt = ""
        reset_len = 0
    label = _fit_cells(account_limit_label(limit), label_width).rstrip()
    cell.append(label, style=DIM)
    cell.append(" ")
    cell.append(pct_txt, style=pct_color(limit.percent))
    if reset_txt:
        cell.append(reset_txt, style=DIM)
    if cell.cell_len < width:
        cell.append(" " * (width - cell.cell_len))
    return cell


def _account_credit_text(credits: Credits) -> tuple[str, str]:
    if not (credits.enabled and (credits.available is not None or credits.used is not None)):
        return "", DIM
    if credits.available is not None:
        return f"{fmt_money(credits.available, credits.currency)} left", "#8fbf6f"
    return f"{fmt_money(credits.used, credits.currency)} used", DIM


def render_accounts(rows: list[AccountRow], live_limits: list[Limit], width: int, now: datetime,
                    autorotate: bool = False) -> Group | None:
    """One line per saved account — spare capacity at a glance.

    Hidden until there's a second account to compare against; the live row
    reuses the main panel's merged limits instead of its own poll. Credit
    headroom is shown for the non-live rows (the live login's credits are the
    main panel's headline, and repeating them here would overflow the row).
    """
    if not rows or all(r.is_live for r in rows):
        return None
    header = Text()
    header.append("Accounts", style="bold")
    header.append("  ·  usage per login", style=DIM)
    if autorotate and header.cell_len + Text("  ·  ⟳ auto-rotate").cell_len <= width:
        header.append("  ·  ⟳ auto-rotate", style=CLAUDE)
    lines: list[Text] = [header]

    row_limits = {row.name: (live_limits if row.is_live else row.limits) for row in rows}
    name_width = min(16, max(10, max(Text(row.name).cell_len for row in rows)))
    statuses = {row.name: _account_status(row, now) for row in rows}
    status_width = min(14, max(4, max(Text(text).cell_len for text, _ in statuses.values())))

    candidates: list[tuple[str, int, int, int]] = []
    for column, minimum, cap in (("session", 6, 17), ("weekly", 6, 8), ("scoped", 8, 14)):
        vals = [_account_limit_plain(_account_limit_for_column(row_limits[row.name], column), now) for row in rows]
        if any(vals):
            candidates.append((column, minimum, cap, min(cap, max(minimum, max(Text(v).cell_len for v in vals)))))
    credit_vals = [_account_credit_text(row.credits)[0] for row in rows if not row.is_live]
    if any(credit_vals):
        candidates.append(("credits", 10, 18, min(18, max(10, max(Text(v).cell_len for v in credit_vals)))))

    prefix_width = 2 + name_width + 2
    available = max(0, width - prefix_width - 2 - status_width)
    columns: list[tuple[str, int]] = []
    used = 0
    for column, _minimum, _cap, col_width in candidates:
        sep = 2 if columns else 0
        if not columns:
            # always show at least one column, but never wider than the row allows
            col_width = min(col_width, max(1, available))
        elif used + sep + col_width > available:
            break  # columns drop from the right when the pane narrows
        columns.append((column, col_width))
        used += sep + col_width

    for row in rows:
        limits = live_limits if row.is_live else row.limits
        left = Text()
        left.append("● " if row.is_live else "○ ", style=CLAUDE if row.is_live else DIM)
        left.append(_fit_cells(row.name, name_width), style="bold" if row.is_live else "none")
        left.append("  ")
        shown_any = False
        for j, (column, col_width) in enumerate(columns):
            if j:
                left.append("  ")
            if column == "credits":
                credit_txt, credit_style = ("", DIM) if row.is_live else _account_credit_text(row.credits)
                left.append(_fit_cells(credit_txt, col_width), style=credit_style)
                shown_any = shown_any or bool(credit_txt)
                continue
            lim = _account_limit_for_column(limits, column)
            left.append(_account_limit_cell(lim, now, col_width))
            shown_any = shown_any or lim is not None
        credit_txt, credit_style = ("", DIM) if row.is_live else _account_credit_text(row.credits)
        if not columns and credit_txt:
            left.append(_fit_cells(credit_txt, max(10, width - prefix_width - status_width - 2)), style=credit_style)
            shown_any = True
        if not columns and not shown_any and not row.note:
            left.append("waiting…", style=DIM)  # with columns, empty cells show "—" instead
        right = Text()
        status_text, status_style = statuses[row.name]
        right.append(status_text, style=status_style)
        pad = max(2, width - left.cell_len - right.cell_len)
        lines.append(Text.assemble(left, " " * pad, right))
    return Group(*lines)


def render(state: State, console: Console) -> Panel:
    now = datetime.now(timezone.utc)
    width = max(30, min(console.size.width - 10, 72))

    with state.lock:
        limits = list(state.limits)
        credits = state.credits
        credit_today = state.credit_today
        history = {k: list(v) for k, v in state.history.items()}
        account_rows = list(state.accounts)
        accounts_warning = state.accounts_warning
        autorotate = state.autorotate
        autorotate_note = state.autorotate_note
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

    if credits_active(limits, credits):
        # Usage has spilled onto credits: the three plan bars are all pinned at
        # 100%, so replace them with today's credit spend (see user request).
        body.append(render_credit_spend(credits, credit_today, limits, now))
        body.append(Text())
        body.append(Text())
    else:
        if not limits:
            body.append(Align.center(Text("waiting for first response…", style=DIM)))
            body.append(Text())
        for limit in limits:
            rate = compute_pace(history.get(limit_key(limit)), now_ts)
            body.append(render_limit(limit, now, width, rate))
            body.append(Text())

        session_hist = history.get("session") or history.get("five_hour")
        session_lim = next((l for l in limits if l.kind in ("session", "five_hour")), None)
        if session_hist and session_lim is not None and session_lim.resets_at is not None:
            spark = render_sparkline(session_hist, width, now_ts, session_lim.resets_at.timestamp())
            if spark is not None:
                body.append(spark)
                body.append(Text())

        if credits.enabled and credits.used is not None:
            line = Text()
            line.append("Extra usage credits  ", style="bold")
            line.append(f"{fmt_money(credits.used, credits.currency)} used", style=DIM)
            avail = credits.available
            if avail is not None:
                line.append(f"  ·  {fmt_money(avail, credits.currency)} left", style=DIM)
            elif credits.limit:
                line.append(f" of {fmt_money(credits.limit, credits.currency)}/mo", style=DIM)
            if credit_today:
                line.append(f"  ·  {fmt_money(credit_today, credits.currency)} today", style=DIM)
            body.append(line)
            body.append(Text())

    if accounts_warning:
        warn = Text()
        warn.append("⚠ ", style="yellow")
        warn.append(accounts_warning, style="yellow")
        body.append(warn)
        body.append(Text())

    accounts_section = render_accounts(account_rows, limits, width, now, autorotate)
    if accounts_section is not None:
        body.append(accounts_section)
        if autorotate and autorotate_note:
            note = Text()
            style = "yellow" if autorotate_note.startswith("⚠") else DIM
            note.append(autorotate_note, style=style)
            body.append(note)
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


def env_interval() -> int:
    try:
        return int(os.environ.get("CLAUDE_MONITOR_INTERVAL", "60"))
    except ValueError:
        return 60


def main() -> None:
    parser = argparse.ArgumentParser(description="Live monitor for Claude Code usage limits")
    parser.add_argument(
        "--interval",
        type=int,
        default=env_interval(),
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
    parser.add_argument(
        "--autorotate",
        action="store_true",
        help="when the live login hits 100%%, auto-switch to a saved account with headroom "
             "(claude-account), so running sessions resume on their next retry; needs >= 2 saved accounts",
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
    state.autorotate = args.autorotate
    load_history(state)
    load_credit_baseline(state)
    stop = threading.Event()
    thread = threading.Thread(
        target=poll_loop, args=(state, max(args.interval, 30), stop, args.notify), daemon=True
    )
    thread.start()
    threading.Thread(target=feed_loop, args=(state, stop, args.notify), daemon=True).start()
    threading.Thread(
        target=accounts_loop, args=(state, stop), kwargs={"autorotate": args.autorotate}, daemon=True
    ).start()

    def bye(*_):
        stop.set()
        state.poll_wake.set()  # the poll thread sleeps on this event
        raise SystemExit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)  # tmux pane closed

    try:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while not stop.is_set():
                try:
                    frame = Align.center(render(state, console), vertical="middle")
                except Exception as exc:  # one bad value must not kill the monitor
                    frame = Align.center(Text(f"render error: {exc}", style="red"), vertical="middle")
                live.update(frame)
                stop.wait(0.5)
    finally:
        save_history(state)
        save_credit_baseline(state)
        if created_flag is not None:
            created_flag.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
