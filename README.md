# Claude Usage Monitor

A live tmux-pane TUI for Claude Code usage limits — the 5-hour session window,
the weekly window, model-scoped weekly limits, and when each one resets. Styled
to match the Claude Code terminal theme so it looks at home next to your other
panes.

![Claude Usage Monitor](docs/preview.svg)

## How it works

Two data sources, merged:

1. **Passive feed (primary).** Claude Code pipes a `rate_limits` object to
   every statusline refresh (~10s) in every session. `statusline-dispatch`
   snapshots it to `~/.claude/usage-feed/<session>.json`, and the monitor
   reads those files — so while any session is running, session/weekly
   numbers are seconds-fresh with **zero API calls**. Sessions can report
   slightly stale numbers, so the merge takes the newest reset instance and
   the max percent within it (usage is monotonic inside a window).
2. **API poll (supplement).** The monitor also polls the endpoint the
   `/usage` screen uses (`api.anthropic.com/api/oauth/usage`) with the OAuth
   token of the current Claude Code login (macOS Keychain item
   `Claude Code-credentials`, or `~/.claude/.credentials.json` on Linux) —
   for what the feed lacks: model-scoped weekly limits, the `◂ limiting`
   flag, severity, and usage credits. While the feed is fresh this poll
   relaxes to every 5 minutes, so it rarely gets rate-limited at all.

The token is only ever held in memory; it is never printed, logged, or written
anywhere. The feed files contain only percentages and reset times.

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
- **Session history** — a 3-row chart of the 5h window over the last few
  hours (up to 6h), height on a fixed 0–100% scale, colored by utilization;
  resets show as cliffs. History persists across restarts
  (`~/.claude/usage-monitor-history.json`), so a fresh monitor keeps the
  day's shape.
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
- **native** — a minimal one-liner (model, context used/size/%, session cost)
  rendered directly from the JSON Claude Code pipes in — no npx startup, no
  transcript parsing, zero network.
- **off** — nothing.

In every mode, the dispatcher also writes the passive usage feed the monitor
reads (see *How it works*).

```sh
claude-statusline lite     # ccstatusline without usage polling
claude-statusline native   # minimal, zero-dependency line
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
every running Claude Code session polls it too. The passive feed makes this
mostly moot: session and weekly numbers keep streaming regardless, the footer
shows both source ages (`● live · feed 6s ago · api 3m ago`), and the API
poll relaxes to every 5 minutes while the feed is fresh. When the API does
429, the monitor backs off (honoring `Retry-After` when present, otherwise
30s → 300s exponential) with a live countdown, and only the scoped-limit /
`◂ limiting` details go briefly stale. Running the statusline in lite mode removes the
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
