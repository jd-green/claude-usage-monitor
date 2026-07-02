# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13.7"]
# ///
"""Claude Usage Monitor — a live TUI for Claude Code usage limits.

Reads the OAuth token for the currently authenticated claude.ai login
(macOS Keychain item "Claude Code-credentials", or ~/.claude/.credentials.json
on Linux) and polls the same usage endpoint Claude Code's /usage screen uses.
The token is never printed, logged, or written anywhere.

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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    limits: list[Limit] = field(default_factory=list)
    credits: Credits = field(default_factory=Credits)
    subscription: str = ""
    fetched_at: datetime | None = None
    status: str = "starting…"
    status_style: str = DIM
    lock: threading.Lock = field(default_factory=threading.Lock)


# ------------------------------------------------------------------- polling


def poll_loop(state: State, interval: int, stop: threading.Event) -> None:
    backoff = 0
    while not stop.is_set():
        delay = interval
        try:
            token, sub = read_access_token()
            payload = fetch_usage(token)
            limits = parse_limits(payload)
            credits = parse_credits(payload)
            with state.lock:
                state.limits = limits
                state.credits = credits
                state.subscription = sub
                state.fetched_at = datetime.now(timezone.utc)
                state.status = "live"
                state.status_style = "green"
            backoff = 0
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
                state.status = f"rate limited, retrying in {delay}s{cached}"
                state.status_style = "yellow"
        except AuthError as exc:
            delay = max(interval, 60)
            with state.lock:
                state.status = str(exc)
                state.status_style = "red"
        except Exception as exc:  # network blips etc.
            backoff = min(backoff * 2, 300) if backoff else 15
            delay = backoff
            with state.lock:
                state.status = f"error: {exc}"
                state.status_style = "red"
        stop.wait(delay)


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


def util_color(limit: Limit) -> str:
    if limit.severity in SEVERITY_STYLES:
        return SEVERITY_STYLES[limit.severity]
    pct = limit.percent
    if pct is None:
        return DIM
    if pct >= 90:
        return "#e05252"
    if pct >= 75:
        return "#d97757"
    if pct >= 50:
        return "#d9a765"
    return "#8fbf6f"


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


def render_limit(limit: Limit, now: datetime, width: int) -> Group:
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

    return Group(line1, usage_bar(limit.percent, color, width), reset)


def render(state: State, console: Console) -> Panel:
    now = datetime.now(timezone.utc)
    width = max(30, min(console.size.width - 10, 72))

    with state.lock:
        limits = list(state.limits)
        credits = state.credits
        subscription = state.subscription
        status = state.status
        status_style = state.status_style
        fetched = state.fetched_at

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
        body.append(render_limit(limit, now, width))
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
    dot = {"green": "●", "yellow": "◐", "red": "✗"}.get(status_style, "●")
    foot.append(f"{dot} {status}", style=status_style)
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
    args = parser.parse_args()

    console = Console()

    if args.once:
        import time

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
    thread = threading.Thread(target=poll_loop, args=(state, max(args.interval, 30), stop), daemon=True)
    thread.start()

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
