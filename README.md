# Claude Usage Monitor

A live tmux-pane TUI for Claude Code usage limits — the 5-hour session window,
the weekly window, model-scoped weekly limits, and when each one resets. Styled
to match the Claude Code terminal theme so it looks at home next to your other
panes.

![Claude Usage Monitor](docs/preview.svg)

## How it works

The monitor reads the OAuth token for the claude.ai login that Claude Code is
currently authenticated with (macOS Keychain item `Claude Code-credentials`,
or `~/.claude/.credentials.json` on Linux) and polls the same endpoint the
`/usage` screen in Claude Code uses (`api.anthropic.com/api/oauth/usage`).
Data comes straight from Anthropic — no estimating from local logs.

The token is only ever held in memory; it is never printed, logged, or written
anywhere.

## Requirements

- [uv](https://docs.astral.sh/uv/) — Python dependencies resolve automatically
  from the script header
- A Claude Code login (`claude` → `/login`)
- `jq` and [ccstatusline](https://github.com/sirmalloc/ccstatusline) for the
  statusline lite mode

## Install

```sh
git clone https://github.com/jd-green/claude-usage-monitor.git
cd claude-usage-monitor
ln -sf "$(pwd)/claude-usage" ~/.local/bin/claude-usage
ln -sf "$(pwd)/statusline-toggle" ~/.local/bin/claude-statusline
```

For the statusline modes (optional), point the Claude Code statusline at the
dispatcher in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/absolute/path/to/claude-usage-monitor/statusline-dispatch",
    "padding": 0,
    "refreshInterval": 10
  }
}
```

## Run it

```sh
claude-usage
```

That's the whole command — it launches the monitor with the lite statusline
(see below) and works from any directory, including tmux:

```sh
tmux split-window -h claude-usage
```

Direct invocation without the wrapper:

```sh
uv run monitor.py
```

## Options

| Flag | Default | What it does |
| --- | --- | --- |
| `--interval N` | 60 | Poll interval in seconds (min 30) |
| `--once` | — | Fetch once, print the raw JSON payload, exit |
| `--notify` | — | Send a notification (macOS + tmux message) on threshold crossings (80%/95%), window resets, and `◂ limiting` changes |
| `--lite-statusline` | — | Switch the statusline to lite (context info only, no usage polling) while the monitor runs; restored on exit |
| `--mute-statusline` | — | Hide the Claude Code statusline entirely while the monitor runs; restored on exit |

`CLAUDE_MONITOR_INTERVAL` sets the default interval via the environment.

## What the display means

- **Session · 5h window** — the rolling 5-hour usage window, with reset
  countdown and local reset time.
- **Week · all models** — the weekly window across all models.
- **Week · [model]** — a model-scoped weekly limit (e.g. Fable/Opus) when your
  plan has one. The `◂ limiting` tag marks the limit Anthropic currently
  considers the active constraint.
- **▸ pace** — burn rate over the last 45 minutes, plus a projection: either
  `~64% at reset` (you'll clear it) or `hits 100% ~3:40 pm, before reset`
  (you won't). Appears once a few minutes of samples accumulate; measurement
  restarts automatically after a window reset.
- **Session history** — a sparkline of the 5h window over the last few hours
  (up to 6h, in memory only), colored by utilization; resets show as drops.
- Bar colors shift green → yellow → orange → red as utilization climbs (and
  follow the API's severity flag when it escalates).

## Notifications

With `--notify` (included in the `claude-usage` wrapper by default), the
monitor sends a macOS notification and flashes a tmux status message — but
only on state *transitions*, never repeatedly:

- a window crosses 80% or 95% (upward),
- a window resets,
- the `◂ limiting` tag moves to a different window.

Remove `--notify` from the `claude-usage` wrapper if you'd rather it stay
quiet.

## Statusline modes

`statusline-dispatch` picks a mode based on flag files, so every Claude Code
session switches together (within ~10s, one statusline refresh):

- **full** (default) — ccstatusline exactly as configured, including the
  usage-limit widgets that poll the rate-limited API from every session.
- **lite** — the same ccstatusline, minus the network widgets (`reset-timer`,
  `weekly-usage`, `weekly-reset-timer`). Model, context window, tokens, and
  cost all stay, and are computed locally — zero API calls. Implemented by
  running ccstatusline with `HOME` pointed at a shadow home
  (`~/.claude/ccstatusline-lite/`) whose config is auto-generated from the
  real one, so theme/layout edits carry over.
- **off** — nothing.

```sh
claude-statusline lite     # context info only, no usage polling
claude-statusline full     # everything back
claude-statusline off      # blank
claude-statusline status   # check
```

Or let the monitor drive it: `claude-usage` passes `--lite-statusline` by
default (you keep per-session context, the monitor is the only thing polling
usage); `claude-usage --mute-statusline` hides the statusline entirely. Both
restore the previous state on exit, and leave the flag alone if you had
already set it manually. If the monitor dies to SIGKILL a flag can linger —
`claude-statusline full` clears it.

## Rate limiting

The usage endpoint is aggressively rate-limited, and in full statusline mode
every running Claude Code session polls it too. The monitor backs off
automatically on 429 (honoring `Retry-After` when present, otherwise
30s → 300s exponential) and keeps showing the last good data, marked as
cached, with a live countdown to the next retry. Running the statusline in lite mode removes the
per-session polling entirely, which makes the monitor's own refreshes far more
reliable.

## Files

| File | Purpose |
| --- | --- |
| `monitor.py` | The TUI itself (single-file, PEP 723 inline deps) |
| `claude-usage` | Launcher wrapper — `uv run monitor.py --lite-statusline` from anywhere |
| `statusline-dispatch` | Statusline entrypoint for `~/.claude/settings.json`; picks full/lite/off |
| `statusline-toggle` | Flips the mode flag files (installed as `claude-statusline`) |

## License

[MIT](LICENSE). Not affiliated with or endorsed by Anthropic; "Claude" and
"Claude Code" are Anthropic trademarks, used here only to describe
compatibility.
