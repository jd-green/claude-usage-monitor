#!/usr/bin/env python3
"""Claude account router — save, list, and cycle between claude.ai logins.

Claude Code keeps one login at a time: the OAuth tokens live in the macOS
Keychain item "Claude Code-credentials" (or ~/.claude/.credentials.json on
Linux), and the account identity (email, accountUuid, org) lives in
~/.claude.json under oauthAccount/userID. This tool snapshots that pair into
named slots and swaps them back, so `claude-account next` rotates the live
login across subscriptions.

Only the claudeAiOauth key is swapped inside the keychain blob — MCP server
tokens (mcpOAuth) stay put. Slot secrets are stored in the Keychain on macOS
(service "claude-usage-monitor.account", one item per slot) or as 0600 files
elsewhere; the index file holds names/emails only, never tokens. Tokens are
never printed or logged.

Running Claude Code sessions keep their old login in memory — a switch
applies to sessions started afterwards.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LIVE_SERVICE = "Claude Code-credentials"
SLOT_SERVICE = "claude-usage-monitor.account"

CLAUDE_JSON = Path.home() / ".claude.json"
LIVE_CRED_FILE = Path.home() / ".claude" / ".credentials.json"
INDEX_FILE = Path.home() / ".claude" / "usage-monitor-accounts.json"
SLOTS_DIR = Path.home() / ".claude" / "usage-monitor-accounts"
# In-flight switch journal (slot name + uuids only, never tokens): lets the
# next invocation finish or safely abandon a switch that died between the
# credential write and the identity write.
MARKER_FILE = Path.home() / ".claude" / "usage-monitor-switch.json"

# Claude Code's public OAuth client — same endpoint + id the CLI itself uses.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_MARGIN = 300  # refresh when the access token is within 5 min of expiry

FEED_DIR = Path.home() / ".claude" / "usage-feed"

# ~/.claude.json fields that identify the login and must travel with it
IDENTITY_FIELDS = ("oauthAccount", "userID")


class AccountError(Exception):
    pass


class MissingLogin(AccountError):
    """No credential store exists at all — distinct from transient failures."""


def _write_private(path: Path, raw: str, mode: int = 0o600) -> None:
    """Atomic write that is never world-readable, not even transiently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(raw)
    tmp.replace(path)


# ------------------------------------------------------------------ keychain


def use_keychain() -> bool:
    return sys.platform == "darwin"


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["security", *args], capture_output=True, text=True, input=stdin, timeout=20
        )
    except subprocess.TimeoutExpired:
        raise AccountError("keychain call timed out — is the Keychain locked or showing a prompt?")


def _security_error_hint(proc: subprocess.CompletedProcess) -> str:
    """First stderr line, but only security's own diagnostics — a parse error
    from `security -i` can echo the command line, which contains the secret."""
    first = (proc.stderr or "").strip().splitlines()[:1]
    if first and first[0].startswith("security:") and "sk-ant" not in first[0]:
        return f" ({first[0][:120]})"
    return ""


def _kc_quote(s: str) -> str:
    """Quote for the `security -i` command parser (backslash escapes)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_read(service: str, account: str | None = None) -> str | None:
    args = ["find-generic-password", "-s", service, "-w"]
    if account is not None:
        args[1:1] = ["-a", account]
    proc = _security(args)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _keychain_write(service: str, account: str, secret: str) -> None:
    # `security -i` reads the command from stdin, keeping the secret out of
    # argv (visible to `ps`).
    cmd = " ".join(
        ["add-generic-password", "-U", "-s", _kc_quote(service), "-a", _kc_quote(account),
         "-w", _kc_quote(secret)]
    )
    if "\n" in cmd or "\r" in cmd:  # would break the line protocol and echo back
        raise AccountError("refusing keychain write: payload contains a newline")
    proc = _security(["-i"], stdin=cmd + "\n")
    if proc.returncode != 0:
        raise AccountError(f"keychain write failed, security exited {proc.returncode}"
                           + _security_error_hint(proc))


def _keychain_delete(service: str, account: str) -> None:
    proc = _security(["delete-generic-password", "-s", service, "-a", account])
    if proc.returncode != 0 and "could not be found" not in (proc.stderr or ""):
        raise AccountError(f"keychain delete failed, security exited {proc.returncode}"
                           + _security_error_hint(proc))


def _keychain_live_account() -> str:
    """The live item's account attribute — writing under a different one would
    create a second item with the same service and break lookups."""
    proc = _security(["find-generic-password", "-s", LIVE_SERVICE])
    if proc.returncode != 0:  # no item yet: any account attribute is safe
        return os.environ.get("USER", "") or "claude"
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith('"acct"') and '="' in line:
            return line.split('="', 1)[1].rstrip('"')
    # An item exists but we can't tell its account: writing blind would fork a
    # duplicate item and make every later read non-deterministic.
    raise AccountError("could not read the live keychain item's account attribute")


# ---------------------------------------------------------------- slot store


def _slot_path(name: str, suffix: str) -> Path:
    # cmd_save sanitizes new names, but the index file is plain user-editable
    # JSON — never let a hand-edited name escape SLOTS_DIR
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise AccountError(f"invalid account name {name!r}")
    return SLOTS_DIR / f"{name}{suffix}"


def _slot_file(name: str) -> Path:
    return _slot_path(name, ".json")


def _rescue_file(name: str) -> Path:
    """Fallback persistence for refreshed tokens when the primary store fails.

    Refresh tokens are single-use: once the endpoint rotates them, losing the
    response kills the slot permanently. If the keychain write fails after a
    refresh, the new tokens land here (0600) instead, and every read prefers
    this file until a later primary write succeeds and clears it.
    """
    return _slot_path(name, ".rescue.json")


def slot_read(name: str) -> dict | None:
    try:
        rescued = json.loads(_rescue_file(name).read_text())
        if isinstance(rescued, dict) and rescued.get("claudeAiOauth"):
            return rescued  # strictly newer than the primary copy
    except (OSError, ValueError):
        pass
    if use_keychain():
        raw = _keychain_read(SLOT_SERVICE, name)
    else:
        try:
            raw = _slot_file(name).read_text()
        except OSError:
            raw = None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def slot_write(name: str, data: dict) -> None:
    raw = json.dumps(data)
    if use_keychain():
        _keychain_write(SLOT_SERVICE, name, raw)
    else:
        _write_private(_slot_file(name), raw)
    _rescue_file(name).unlink(missing_ok=True)  # primary now holds the newest data


def slot_write_with_rescue(name: str, data: dict) -> None:
    """Persist a slot whose tokens MUST NOT be lost (post-refresh)."""
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            slot_write(name, data)
            return
        except (AccountError, OSError) as exc:
            last_err = exc
            time.sleep(0.3 * (attempt + 1))
    _write_private(_rescue_file(name), json.dumps(data))
    print(f"warning: could not persist refreshed tokens to the primary store ({last_err}); "
          f"kept in {_rescue_file(name)} until the next successful write", file=sys.stderr)


def slot_delete(name: str) -> None:
    if use_keychain():
        _keychain_delete(SLOT_SERVICE, name)
    else:
        _slot_file(name).unlink(missing_ok=True)
    _rescue_file(name).unlink(missing_ok=True)


# -------------------------------------------------------------------- index


@dataclass
class Account:
    name: str
    email: str
    account_uuid: str


def load_index() -> list[Account]:
    try:
        data = json.loads(INDEX_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for entry in data.get("accounts", []):
        try:
            name = str(entry["name"])
            account = Account(name, str(entry.get("email", "")), str(entry.get("accountUuid", "")))
        except (TypeError, KeyError):
            continue
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            continue  # a tampered name could point slot paths outside SLOTS_DIR
        out.append(account)
    return out


def save_index(accounts: list[Account]) -> None:
    data = {"accounts": [{"name": a.name, "email": a.email, "accountUuid": a.account_uuid} for a in accounts]}
    _write_private(INDEX_FILE, json.dumps(data, indent=2))  # emails/uuids are PII: 0600


# --------------------------------------------------------------- live login


def read_live_blob(require_oauth: bool = True) -> tuple[dict, str]:
    """Return (parsed keychain/credential blob, source) for the live login."""
    if use_keychain():
        raw = _keychain_read(LIVE_SERVICE)
        source = "keychain"
    else:
        try:
            raw = LIVE_CRED_FILE.read_text()
        except OSError:
            raw = None
        source = "file"
    if not raw:
        raise MissingLogin("No Claude Code login found — run `claude` and /login first")
    try:
        blob = json.loads(raw)
    except ValueError as exc:
        raise AccountError(f"Could not parse stored credentials ({exc})") from exc
    if not isinstance(blob, dict):
        raise AccountError("Stored credentials are not a JSON object")
    if require_oauth and "claudeAiOauth" not in blob:
        raise AccountError("Stored credentials have no claudeAiOauth — run /login first")
    return blob, source


def write_live_oauth(oauth: dict) -> None:
    """Swap claudeAiOauth in the live credential blob, preserving mcpOAuth etc.

    Only a genuinely missing store may start a fresh blob. Any other read
    failure (keychain timeout, parse error) aborts the write — proceeding
    would silently discard unrelated keys like MCP server logins.
    """
    try:
        blob, _ = read_live_blob(require_oauth=False)
    except MissingLogin:
        blob = {}
    blob["claudeAiOauth"] = oauth
    raw = json.dumps(blob)
    if use_keychain():
        _keychain_write(LIVE_SERVICE, _keychain_live_account(), raw)
        return
    _write_private(LIVE_CRED_FILE, raw)


def read_claude_json() -> dict:
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_claude_json_identity(identity: dict) -> None:
    """Update only the login-identity fields of ~/.claude.json, atomically.

    Running Claude Code sessions rewrite this file too; if it changes between
    our read and our replace, retry the whole read-modify-write so their
    update isn't silently discarded. The window left is the replace() itself.
    """
    for _ in range(5):
        try:
            before = CLAUDE_JSON.stat().st_mtime_ns
        except OSError:
            before = None
        data = read_claude_json()
        if not data:
            raise AccountError(f"{CLAUDE_JSON} missing or unreadable")
        for key in IDENTITY_FIELDS:
            if key in identity:
                data[key] = identity[key]
        mode = (CLAUDE_JSON.stat().st_mode & 0o777) if CLAUDE_JSON.exists() else 0o600
        tmp = CLAUDE_JSON.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp, mode)
        try:
            unchanged = before == CLAUDE_JSON.stat().st_mtime_ns
        except OSError:
            unchanged = before is None
        if unchanged:
            tmp.replace(CLAUDE_JSON)
            return
        tmp.unlink(missing_ok=True)  # somebody wrote meanwhile — merge again
        time.sleep(0.05)
    raise AccountError(f"{CLAUDE_JSON} kept changing underneath us — try again")


def active_account_uuid() -> str:
    return (read_claude_json().get("oauthAccount") or {}).get("accountUuid", "")


def snapshot_live() -> dict:
    """Capture the live login as a slot payload: tokens + identity."""
    blob, _ = read_live_blob()
    cfg = read_claude_json()
    identity = {k: cfg[k] for k in IDENTITY_FIELDS if k in cfg}
    if not (identity.get("oauthAccount") or {}).get("accountUuid"):
        raise AccountError("~/.claude.json has no oauthAccount.accountUuid — is Claude Code logged in?")
    return {"claudeAiOauth": blob["claudeAiOauth"], "identity": identity}


# ------------------------------------------------------------ token refresh


def token_expires_at(oauth: dict) -> float:
    """expiresAt as epoch seconds (Claude Code stores JS milliseconds)."""
    try:
        exp = float(oauth.get("expiresAt") or 0)
    except (TypeError, ValueError):
        return 0.0
    return exp / 1000 if exp > 1e12 else exp


def token_fresh(oauth: dict, margin: float = REFRESH_MARGIN) -> bool:
    return token_expires_at(oauth) - time.time() > margin


def refresh_oauth(oauth: dict) -> dict:
    """Exchange the refresh token for new tokens; returns an updated oauth dict.

    Retries transient failures; 400/401/403 mean the refresh token itself is
    dead (rotated away or revoked) and only a fresh /login can fix that.
    """
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise AccountError("slot has no refresh token — re-save it from a live login")
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    ).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                raise AccountError(
                    "refresh token rejected — log into this account (`claude` → /login) "
                    "and run `claude-account save` again"
                )
            last_err = exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_err = exc
        time.sleep(0.5 * (2**attempt))
    else:
        raise AccountError(f"token refresh failed ({last_err})")
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AccountError("token endpoint returned no access_token")
    updated = dict(oauth)
    updated["accessToken"] = payload["access_token"]
    if payload.get("refresh_token"):
        updated["refreshToken"] = payload["refresh_token"]
    try:
        exp = float(payload["expires_at"])
        updated["expiresAt"] = int(exp if exp > 1e12 else exp * 1000)
    except (KeyError, TypeError, ValueError):
        try:
            ttl = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        updated["expiresAt"] = int((time.time() + ttl) * 1000)
    return updated


def ensure_fresh_slot(name: str) -> dict:
    """Return the slot with a valid access token, refreshing + persisting if stale.

    Persistence uses the rescue path: the rotated refresh token is single-use,
    so it must land somewhere durable before anyone uses the new tokens. The
    refresh itself is serialized across processes with a per-slot flock —
    two concurrent refreshes (CLI switch vs a monitor's accounts poll, or two
    monitors) would replay a consumed refresh token, which providers may
    answer by revoking the whole token family.
    """
    slot = slot_read(name)
    if slot is None:
        raise AccountError(f"no stored account named {name!r}")
    if token_fresh(slot.get("claudeAiOauth") or {}):
        return slot
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_slot_path(name, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            slot = slot_read(name)  # re-check: the lock winner may have refreshed already
            if slot is None:
                raise AccountError(f"no stored account named {name!r}")
            oauth = slot.get("claudeAiOauth") or {}
            if token_fresh(oauth):
                return slot
            slot["claudeAiOauth"] = refresh_oauth(oauth)
            slot_write_with_rescue(name, slot)
            return slot
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ---------------------------------------------------------------- operations


def read_marker() -> dict | None:
    try:
        data = json.loads(MARKER_FILE.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_marker(target: str, target_uuid: str, source_uuid: str) -> None:
    _write_private(
        MARKER_FILE,
        json.dumps({"target": target, "targetUuid": target_uuid,
                    "sourceUuid": source_uuid, "at": time.time()}),
    )


def clear_marker() -> None:
    MARKER_FILE.unlink(missing_ok=True)


def heal_pending_switch() -> None:
    """Finish or abandon a switch that died between its two writes.

    A crash there leaves the keychain holding the target's tokens while
    ~/.claude.json still names the source — and a later sync-back would then
    fold the target's tokens into the source's slot, killing it. Resolve the
    marker before any command runs.
    """
    marker = read_marker()
    if marker is None:
        return
    live = active_account_uuid()
    target, target_uuid = marker.get("target", ""), marker.get("targetUuid", "")
    if live == target_uuid or not target:
        clear_marker()  # both writes landed (or the marker is unusable)
        return
    if live == marker.get("sourceUuid"):
        slot = slot_read(target)
        if slot is not None:
            write_live_oauth(slot["claudeAiOauth"])
            write_claude_json_identity(slot.get("identity") or {})
            print(f"finished interrupted switch to {target!r}", file=sys.stderr)
        clear_marker()
        return
    # A third account is live — someone ran /login in between. Leave it alone.
    clear_marker()
    print(f"note: an interrupted switch to {target!r} was abandoned; "
          f"the current login was left untouched", file=sys.stderr)


def sync_back_live(accounts: list[Account]) -> str | None:
    """Fold the live login's (possibly newer) tokens back into its slot.

    Claude Code refreshes tokens as it runs; without this, a slot saved hours
    ago could hold a stale, already-rotated refresh token.
    """
    if read_marker() is not None:  # half-applied switch: live state is mixed
        return None
    uuid = active_account_uuid()
    if not uuid:
        return None
    match = next((a for a in accounts if a.account_uuid == uuid), None)
    if match is None:
        return None
    try:
        slot_write(match.name, snapshot_live())
    except (AccountError, OSError):
        return None
    return match.name


def cmd_save(name: str | None) -> None:
    payload = snapshot_live()
    oa = payload["identity"]["oauthAccount"]
    email = oa.get("emailAddress", "")
    uuid = oa.get("accountUuid", "")
    if not name:
        name = (email.split("@")[0] or "account").lower()
    name = "".join(c for c in name.lower() if c.isalnum() or c in "-_") or "account"
    accounts = load_index()
    existing = next((a for a in accounts if a.name == name), None)
    if existing and existing.account_uuid and uuid and existing.account_uuid != uuid:
        raise AccountError(f"slot {name!r} already holds {existing.email} — pick another name")
    same_uuid = next((a for a in accounts if a.account_uuid == uuid and a.name != name), None)
    if same_uuid:
        raise AccountError(f"this login is already saved as {same_uuid.name!r}")
    slot_write(name, payload)
    if existing:
        existing.email, existing.account_uuid = email, uuid
    else:
        accounts.append(Account(name, email, uuid))
    save_index(accounts)
    print(f"saved current login as {name!r} ({email})")


def cmd_list() -> None:
    accounts = load_index()
    if not accounts:
        print("no saved accounts — log in and run: claude-account save")
        return
    uuid = active_account_uuid()
    for a in accounts:
        marker = "●" if a.account_uuid == uuid else "○"
        slot = slot_read(a.name)
        oauth = (slot or {}).get("claudeAiOauth") or {}
        sub = oauth.get("subscriptionType", "")
        if slot is None:
            state = "missing credentials"
        elif a.account_uuid == uuid:
            state = "active"
        elif token_fresh(oauth):
            state = "ready"
        else:
            state = "token stale (auto-refreshes)"
        print(f"{marker} {a.name:<14} {a.email:<28} {sub:<8} {state}")


def cmd_switch(name: str) -> None:
    accounts = load_index()
    target = next((a for a in accounts if a.name == name), None)
    if target is None:
        known = ", ".join(a.name for a in accounts) or "none saved"
        raise AccountError(f"no account named {name!r} (known: {known})")
    if target.account_uuid and target.account_uuid == active_account_uuid():
        print(f"{name!r} is already the active login")
        return
    sync_back_live(accounts)
    slot = ensure_fresh_slot(name)
    if not (slot.get("identity") or {}).get("oauthAccount"):
        # writing the token without the identity would leave ~/.claude.json
        # naming account A while requests authenticate as account B
        raise AccountError(f"slot {name!r} has no saved identity — log into it and run `claude-account save` again")
    write_marker(name, target.account_uuid, active_account_uuid())
    write_live_oauth(slot["claudeAiOauth"])
    write_claude_json_identity(slot.get("identity") or {})
    clear_marker()
    clear_usage_feed()
    print(f"switched to {name!r} ({target.email})")
    print("new `claude` sessions use this login; running sessions keep the old one until restarted")


def clear_usage_feed() -> None:
    """Drop passive-feed snapshots — they describe the previous account."""
    try:
        for f in FEED_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_next() -> None:
    accounts = load_index()
    if len(accounts) < 2:
        raise AccountError("need at least two saved accounts to rotate")
    uuid = active_account_uuid()
    idx = next((i for i, a in enumerate(accounts) if a.account_uuid == uuid), -1)
    cmd_switch(accounts[(idx + 1) % len(accounts)].name)


def cmd_remove(name: str) -> None:
    accounts = load_index()
    if not any(a.name == name for a in accounts):
        raise AccountError(f"no account named {name!r}")
    slot_delete(name)
    save_index([a for a in accounts if a.name != name])
    print(f"removed {name!r} (the live Claude Code login is untouched)")


def cmd_status() -> None:
    uuid = active_account_uuid()
    email = (read_claude_json().get("oauthAccount") or {}).get("emailAddress", "?")
    accounts = load_index()
    match = next((a for a in accounts if a.account_uuid == uuid), None)
    slot_note = f"slot {match.name!r}" if match else "not saved as a slot (run: claude-account save)"
    print(f"active login: {email} — {slot_note}")
    print(f"saved accounts: {len(accounts)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-account", description="Cycle Claude Code across multiple claude.ai logins"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_save = sub.add_parser("save", help="save the current live login as a named slot")
    p_save.add_argument("name", nargs="?", help="slot name (default: email local part)")
    sub.add_parser("list", help="list saved accounts")
    p_switch = sub.add_parser("switch", help="make a saved account the live login")
    p_switch.add_argument("name")
    sub.add_parser("next", help="rotate to the next saved account")
    p_rm = sub.add_parser("remove", help="delete a saved slot (not the account)")
    p_rm.add_argument("name")
    sub.add_parser("status", help="show the active login")
    args = parser.parse_args()
    try:
        heal_pending_switch()
        if args.cmd == "save":
            cmd_save(args.name)
        elif args.cmd == "list":
            cmd_list()
        elif args.cmd == "switch":
            cmd_switch(args.name)
        elif args.cmd == "next":
            cmd_next()
        elif args.cmd == "remove":
            cmd_remove(args.name)
        elif args.cmd == "status":
            cmd_status()
    except AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
