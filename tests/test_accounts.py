"""Tests for accounts.py — slot store, switching, rotation, token refresh.

Everything runs against the file backend in tmp_path (use_keychain forced
off); no real Keychain, network, or ~/.claude is touched. urlopen is stubbed
wherever a refresh could fire. One exception: a macOS-only integration test
does a real Keychain roundtrip under a dedicated test service name (skipped
on CI, which runs Linux).
"""

import io
import json
import subprocess
import sys
import time
import urllib.error

import pytest

import accounts as acc

UUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UUID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def oauth_blob(tag: str, expires_in: float = 3600) -> dict:
    return {
        "accessToken": f"sk-ant-oat01-{tag}",
        "refreshToken": f"sk-ant-ort01-{tag}",
        "expiresAt": int((time.time() + expires_in) * 1000),
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "rateLimitTier": "default",
    }


def identity_blob(email: str, uuid: str) -> dict:
    return {
        "oauthAccount": {"accountUuid": uuid, "emailAddress": email, "organizationUuid": "org-" + uuid},
        "userID": "hash-" + uuid,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every accounts.py path at tmp_path and force the file backend."""
    monkeypatch.setattr(acc, "use_keychain", lambda: False)
    monkeypatch.setattr(acc, "CLAUDE_JSON", tmp_path / ".claude.json")
    monkeypatch.setattr(acc, "LIVE_CRED_FILE", tmp_path / ".credentials.json")
    monkeypatch.setattr(acc, "INDEX_FILE", tmp_path / "accounts-index.json")
    monkeypatch.setattr(acc, "SLOTS_DIR", tmp_path / "slots")
    monkeypatch.setattr(acc, "FEED_DIR", tmp_path / "usage-feed")
    monkeypatch.setattr(acc, "MARKER_FILE", tmp_path / "switch-marker.json")
    monkeypatch.setattr(
        acc.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected network call")),
    )
    return tmp_path


def set_live(home, email: str, uuid: str, tag: str, expires_in: float = 3600, extra: dict | None = None):
    blob = {"claudeAiOauth": oauth_blob(tag, expires_in)}
    blob.update(extra or {})
    (home / ".credentials.json").write_text(json.dumps(blob))
    cfg = identity_blob(email, uuid)
    cfg["theme"] = "dark"  # unrelated config that must survive switches
    (home / ".claude.json").write_text(json.dumps(cfg))


# ------------------------------------------------------------------ store


def test_slot_roundtrip_and_permissions(home):
    acc.slot_write("work", {"claudeAiOauth": oauth_blob("w")})
    assert acc.slot_read("work")["claudeAiOauth"]["subscriptionType"] == "max"
    mode = (home / "slots" / "work.json").stat().st_mode & 0o777
    assert mode == 0o600
    acc.slot_delete("work")
    assert acc.slot_read("work") is None
    acc.slot_delete("work")  # deleting a missing slot is fine


def test_index_roundtrip_and_garbage(home):
    acc.save_index([acc.Account("a", "a@x.io", UUID_A)])
    assert acc.load_index()[0].email == "a@x.io"
    acc.INDEX_FILE.write_text("{broken")
    assert acc.load_index() == []


def test_token_expiry_ms_and_seconds():
    ms = {"expiresAt": int((time.time() + 600) * 1000)}
    s = {"expiresAt": int(time.time() + 600)}
    assert acc.token_fresh(ms) and acc.token_fresh(s)
    assert not acc.token_fresh({"expiresAt": int((time.time() - 10) * 1000)})
    assert not acc.token_fresh({})


# ------------------------------------------------------------------ save


def test_save_defaults_name_to_email_localpart(home, capsys):
    set_live(home, "james@jdgreen.io", UUID_A, "a")
    acc.cmd_save(None)
    assert "saved current login as 'james'" in capsys.readouterr().out
    idx = acc.load_index()
    assert idx[0].name == "james" and idx[0].account_uuid == UUID_A
    slot = acc.slot_read("james")
    assert slot["claudeAiOauth"]["accessToken"].endswith("-a")
    assert slot["identity"]["oauthAccount"]["emailAddress"] == "james@jdgreen.io"


def test_save_rejects_name_collision_and_duplicate_login(home):
    set_live(home, "a@x.io", UUID_A, "a")
    acc.cmd_save("main")
    set_live(home, "b@x.io", UUID_B, "b")
    with pytest.raises(acc.AccountError, match="already holds"):
        acc.cmd_save("main")
    acc.cmd_save("spare")
    with pytest.raises(acc.AccountError, match="already saved as 'spare'"):
        acc.cmd_save("other-name")


def test_save_resyncs_same_account(home):
    set_live(home, "a@x.io", UUID_A, "a1")
    acc.cmd_save("main")
    set_live(home, "a@x.io", UUID_A, "a2")
    acc.cmd_save("main")
    assert acc.slot_read("main")["claudeAiOauth"]["accessToken"].endswith("-a2")
    assert len(acc.load_index()) == 1


def test_save_requires_live_login(home):
    with pytest.raises(acc.AccountError, match="No Claude Code login"):
        acc.cmd_save("x")


# ------------------------------------------------------------------ switch


def two_accounts(home):
    set_live(home, "a@x.io", UUID_A, "a", extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})
    acc.cmd_save("main")
    set_live(home, "b@x.io", UUID_B, "b", extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})
    acc.cmd_save("spare")


def test_switch_swaps_tokens_identity_preserves_the_rest(home, capsys):
    two_accounts(home)
    (home / "usage-feed").mkdir()
    (home / "usage-feed" / "sess.json").write_text("{}")
    acc.cmd_switch("main")
    live = json.loads((home / ".credentials.json").read_text())
    assert live["claudeAiOauth"]["accessToken"].endswith("-a")
    assert live["mcpOAuth"]["figma"]["accessToken"] == "mcp-tok"  # MCP logins untouched
    cfg = json.loads((home / ".claude.json").read_text())
    assert cfg["oauthAccount"]["accountUuid"] == UUID_A
    assert cfg["userID"] == "hash-" + UUID_A
    assert cfg["theme"] == "dark"  # unrelated config survives
    assert not list((home / "usage-feed").glob("*.json"))  # stale feed cleared
    assert "switched to 'main'" in capsys.readouterr().out


def test_switch_syncs_live_tokens_back_first(home):
    two_accounts(home)
    # Claude Code refreshed spare's tokens since we saved it
    set_live(home, "b@x.io", UUID_B, "b-refreshed",
             extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})
    acc.cmd_switch("main")
    assert acc.slot_read("spare")["claudeAiOauth"]["accessToken"].endswith("-b-refreshed")


def test_switch_noop_when_already_active(home, capsys):
    two_accounts(home)
    acc.cmd_switch("spare")
    assert "already the active login" in capsys.readouterr().out
    assert json.loads((home / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"].endswith("-b")


def test_switch_unknown_name(home):
    two_accounts(home)
    with pytest.raises(acc.AccountError, match="known: main, spare"):
        acc.cmd_switch("nope")


def test_next_cycles_in_index_order(home):
    two_accounts(home)  # live is spare
    acc.cmd_next()
    assert acc.active_account_uuid() == UUID_A  # spare -> main
    acc.cmd_next()
    assert acc.active_account_uuid() == UUID_B  # main -> spare (wraps)


def test_next_needs_two_accounts(home):
    set_live(home, "a@x.io", UUID_A, "a")
    acc.cmd_save("main")
    with pytest.raises(acc.AccountError, match="at least two"):
        acc.cmd_next()


def test_remove_deletes_slot_but_not_live_login(home):
    two_accounts(home)
    acc.cmd_remove("main")
    assert acc.slot_read("main") is None
    assert [a.name for a in acc.load_index()] == ["spare"]
    assert (home / ".credentials.json").exists()
    with pytest.raises(acc.AccountError, match="no account named"):
        acc.cmd_remove("main")


# ------------------------------------------------------------------ refresh


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_ensure_fresh_slot_refreshes_and_persists(home, monkeypatch):
    set_live(home, "a@x.io", UUID_A, "a", expires_in=-60)  # already expired
    acc.cmd_save("main")
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(json.loads(req.data.decode()))
        return FakeResponse(json.dumps({
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "sk-ant-ort01-new",
            "expires_in": 7200,
        }).encode())

    monkeypatch.setattr(acc.urllib.request, "urlopen", fake_urlopen)
    slot = acc.ensure_fresh_slot("main")
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["client_id"] == acc.CLIENT_ID
    assert slot["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-new"
    stored = acc.slot_read("main")["claudeAiOauth"]
    assert stored["refreshToken"] == "sk-ant-ort01-new"  # rotated token persisted
    assert acc.token_fresh(stored)
    # fresh again: no second network call
    acc.ensure_fresh_slot("main")
    assert len(calls) == 1


def test_refresh_keeps_old_refresh_token_when_absent(home, monkeypatch):
    monkeypatch.setattr(
        acc.urllib.request, "urlopen",
        lambda req, timeout=0: FakeResponse(json.dumps({"access_token": "new", "expires_in": 60}).encode()),
    )
    updated = acc.refresh_oauth(oauth_blob("keep"))
    assert updated["refreshToken"] == "sk-ant-ort01-keep"
    assert updated["accessToken"] == "new"


def test_refresh_dead_token_is_terminal(home, monkeypatch):
    def dead(req, timeout=0):
        raise urllib.error.HTTPError(acc.TOKEN_URL, 401, "unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(acc.urllib.request, "urlopen", dead)
    with pytest.raises(acc.AccountError, match="refresh token rejected"):
        acc.refresh_oauth(oauth_blob("dead"))


def test_refresh_retries_transient_errors(home, monkeypatch):
    attempts = []

    def flaky(req, timeout=0):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(acc.TOKEN_URL, 503, "unavailable", {}, io.BytesIO(b"{}"))
        return FakeResponse(json.dumps({"access_token": "ok", "expires_in": 60}).encode())

    monkeypatch.setattr(acc.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(acc.time, "sleep", lambda s: None)
    assert acc.refresh_oauth(oauth_blob("flaky"))["accessToken"] == "ok"
    assert len(attempts) == 3


def test_refresh_expires_at_variants(home, monkeypatch):
    future_s = time.time() + 3600

    def with_expires_at(req, timeout=0):
        return FakeResponse(json.dumps({"access_token": "x", "expires_at": future_s}).encode())

    monkeypatch.setattr(acc.urllib.request, "urlopen", with_expires_at)
    updated = acc.refresh_oauth(oauth_blob("t"))
    assert abs(updated["expiresAt"] / 1000 - future_s) < 2  # seconds normalized to ms

    def with_expires_at_ms(req, timeout=0):
        return FakeResponse(
            json.dumps({"access_token": "x", "expires_at": future_s * 1000}).encode())

    monkeypatch.setattr(acc.urllib.request, "urlopen", with_expires_at_ms)
    updated = acc.refresh_oauth(oauth_blob("t"))
    assert abs(updated["expiresAt"] / 1000 - future_s) < 2  # ms passed through


def test_refresh_missing_access_token_is_account_error(home, monkeypatch):
    monkeypatch.setattr(
        acc.urllib.request, "urlopen",
        lambda req, timeout=0: FakeResponse(json.dumps({"token_type": "Bearer"}).encode()),
    )
    with pytest.raises(acc.AccountError, match="no access_token"):
        acc.refresh_oauth(oauth_blob("odd"))


def test_token_expires_at_garbage_is_zero():
    assert acc.token_expires_at({"expiresAt": "soon"}) == 0.0
    assert acc.token_expires_at({"expiresAt": None}) == 0.0
    assert acc.token_expires_at({}) == 0.0


# ------------------------------------------------------------------ rescue


def test_refresh_persist_failure_lands_in_rescue_file(home, monkeypatch, capsys):
    set_live(home, "a@x.io", UUID_A, "a", expires_in=-60)
    acc.cmd_save("main")
    monkeypatch.setattr(
        acc.urllib.request, "urlopen",
        lambda req, timeout=0: FakeResponse(json.dumps(
            {"access_token": "rescued-token", "refresh_token": "rescued-rt", "expires_in": 7200}
        ).encode()),
    )
    monkeypatch.setattr(acc.time, "sleep", lambda s: None)
    real_write = acc.slot_write
    monkeypatch.setattr(acc, "slot_write", lambda *a: (_ for _ in ()).throw(acc.AccountError("keychain down")))

    slot = acc.ensure_fresh_slot("main")  # must not lose the rotated tokens
    assert slot["claudeAiOauth"]["accessToken"] == "rescued-token"
    assert "could not persist" in capsys.readouterr().err
    # reads now prefer the rescue copy over the stale primary
    assert acc.slot_read("main")["claudeAiOauth"]["refreshToken"] == "rescued-rt"
    # a later successful primary write clears the rescue file
    monkeypatch.setattr(acc, "slot_write", real_write)
    acc.slot_write("main", slot)
    assert not (home / "slots" / "main.rescue.json").exists()
    assert acc.slot_read("main")["claudeAiOauth"]["accessToken"] == "rescued-token"


def test_remove_keeps_index_when_delete_fails(home, monkeypatch):
    set_live(home, "a@x.io", UUID_A, "a")
    acc.cmd_save("main")
    monkeypatch.setattr(acc, "slot_delete", lambda n: (_ for _ in ()).throw(acc.AccountError("locked")))
    with pytest.raises(acc.AccountError, match="locked"):
        acc.cmd_remove("main")
    assert [a.name for a in acc.load_index()] == ["main"]  # still managed


# ------------------------------------------------------------------ switch marker


def test_heal_completes_interrupted_switch(home, capsys):
    two_accounts(home)  # live is spare (B)
    slot = acc.slot_read("main")
    acc.write_marker("main", UUID_A, acc.active_account_uuid())
    acc.write_live_oauth(slot["claudeAiOauth"])  # "crash" before the identity write
    assert acc.active_account_uuid() == UUID_B   # mixed state: A tokens, B identity

    acc.heal_pending_switch()
    assert acc.active_account_uuid() == UUID_A
    live = json.loads((home / ".credentials.json").read_text())
    assert live["claudeAiOauth"]["accessToken"].endswith("-a")
    assert acc.read_marker() is None
    assert "finished interrupted switch" in capsys.readouterr().err


def test_heal_abandons_when_user_logged_in_elsewhere(home, capsys):
    two_accounts(home)
    acc.write_marker("main", UUID_A, UUID_B)
    third = "cccccccc-0000-0000-0000-000000000003"
    set_live(home, "c@x.io", third, "c")  # user ran /login in between

    acc.heal_pending_switch()
    assert acc.active_account_uuid() == third  # untouched
    assert acc.read_marker() is None
    assert "abandoned" in capsys.readouterr().err


def test_sync_back_skipped_while_marker_present(home):
    two_accounts(home)  # live is spare (B)
    set_live(home, "b@x.io", UUID_B, "b-newer",
             extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})
    acc.write_marker("main", UUID_A, UUID_B)
    assert acc.sync_back_live(acc.load_index()) is None
    assert acc.slot_read("spare")["claudeAiOauth"]["accessToken"].endswith("-b")  # not folded


def test_switch_leaves_no_marker(home):
    two_accounts(home)
    acc.cmd_switch("main")
    assert acc.read_marker() is None


# ------------------------------------------------------------------ keychain unit


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_kc_quote_escaping():
    assert acc._kc_quote('a"b\\c') == '"a\\"b\\\\c"'
    assert acc._kc_quote("plain") == '"plain"'


def test_keychain_write_builds_stdin_command(monkeypatch):
    seen = {}

    def fake_security(args, stdin=None):
        seen["args"], seen["stdin"] = args, stdin
        return proc()

    monkeypatch.setattr(acc, "_security", fake_security)
    acc._keychain_write("svc", "acct", 'secret-with-"quote"')
    assert seen["args"] == ["-i"]  # secret travels via stdin, never argv
    assert '\\"quote\\"' in seen["stdin"]
    assert seen["stdin"].count("\n") == 1 and seen["stdin"].endswith("\n")


def test_keychain_write_rejects_newlines(monkeypatch):
    monkeypatch.setattr(acc, "_security", lambda *a, **k: pytest.fail("must not reach security"))
    with pytest.raises(acc.AccountError, match="newline"):
        acc._keychain_write("svc", "acct", "line1\nline2")


def test_keychain_write_error_never_echoes_payload(monkeypatch):
    # a parse error from `security -i` echoes the command (with the secret);
    # only security's own "security:"-prefixed diagnostics may be surfaced
    echo = 'unknown command: add-generic-password -w "sk-ant-oat01-SECRET"'
    monkeypatch.setattr(acc, "_security", lambda *a, **k: proc(rc=1, err=echo))
    with pytest.raises(acc.AccountError) as exc_info:
        acc._keychain_write("svc", "acct", "sk-ant-oat01-SECRET")
    assert "SECRET" not in str(exc_info.value)

    monkeypatch.setattr(
        acc, "_security",
        lambda *a, **k: proc(rc=1, err="security: SecKeychainItemModifyContent failed"),
    )
    with pytest.raises(acc.AccountError, match="SecKeychainItemModifyContent"):
        acc._keychain_write("svc", "acct", "s")


def test_keychain_delete_tolerates_missing_flags_other_failures(monkeypatch):
    monkeypatch.setattr(
        acc, "_security",
        lambda *a, **k: proc(rc=44, err="security: The specified item could not be found in the keychain."),
    )
    acc._keychain_delete("svc", "acct")  # no raise
    monkeypatch.setattr(acc, "_security", lambda *a, **k: proc(rc=51, err="security: keychain locked"))
    with pytest.raises(acc.AccountError, match="delete failed"):
        acc._keychain_delete("svc", "acct")


def test_keychain_live_account_paths(monkeypatch):
    monkeypatch.setattr(acc, "_security", lambda *a, **k: proc(rc=44))
    assert acc._keychain_live_account()  # no item yet: safe default
    meta = 'attributes:\n    "acct"<blob>="james"\n    "svce"<blob>="Claude Code-credentials"'
    monkeypatch.setattr(acc, "_security", lambda *a, **k: proc(rc=0, out=meta))
    assert acc._keychain_live_account() == "james"
    # item exists but acct unparseable: writing blind would fork a duplicate
    monkeypatch.setattr(acc, "_security", lambda *a, **k: proc(rc=0, out='"acct"<blob>=<NULL>'))
    with pytest.raises(acc.AccountError, match="account attribute"):
        acc._keychain_live_account()


@pytest.mark.skipif(sys.platform != "darwin", reason="real Keychain only on macOS")
def test_real_keychain_roundtrip(tmp_path, monkeypatch):
    """End-to-end proof that the `security -i` escaping round-trips a token
    with quotes/backslashes — the one thing the fakes above can't prove."""
    monkeypatch.setattr(acc, "SLOT_SERVICE", "claude-usage-monitor.test-suite")
    monkeypatch.setattr(acc, "SLOTS_DIR", tmp_path / "slots")  # rescue files
    payload = {"claudeAiOauth": {"accessToken": 'tok-with-"quote"-and-\\slash',
                                 "expiresAt": 1234567890123}}
    try:
        acc.slot_write("kc-test", payload)
        assert acc.slot_read("kc-test") == payload
    finally:
        acc.slot_delete("kc-test")
    assert acc.slot_read("kc-test") is None


# --------------------------------------------------- review-fix regressions


def test_write_live_oauth_aborts_on_transient_read_failure(home, monkeypatch):
    """A keychain timeout mid-switch must not silently discard mcpOAuth."""
    set_live(home, "a@x.com", UUID_A, "a", extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})

    def flaky(require_oauth=True):
        raise acc.AccountError("keychain call timed out")

    monkeypatch.setattr(acc, "read_live_blob", flaky)
    with pytest.raises(acc.AccountError, match="timed out"):
        acc.write_live_oauth(oauth_blob("new"))
    # untouched: mcpOAuth still present in the original blob
    blob = json.loads((home / ".credentials.json").read_text())
    assert blob["mcpOAuth"]["figma"]["accessToken"] == "mcp-tok"


def test_write_live_oauth_preserves_mcp_oauth_normally(home):
    set_live(home, "a@x.com", UUID_A, "a", extra={"mcpOAuth": {"figma": {"accessToken": "mcp-tok"}}})
    acc.write_live_oauth(oauth_blob("new"))
    blob = json.loads((home / ".credentials.json").read_text())
    assert blob["mcpOAuth"]["figma"]["accessToken"] == "mcp-tok"
    assert "new" in blob["claudeAiOauth"]["accessToken"]


def test_write_live_oauth_starts_fresh_only_when_store_missing(home):
    acc.write_live_oauth(oauth_blob("first"))  # no credentials file at all: OK
    blob = json.loads((home / ".credentials.json").read_text())
    assert "first" in blob["claudeAiOauth"]["accessToken"]


def test_ensure_fresh_slot_concurrent_refreshes_once(home, monkeypatch):
    """Two overlapping refreshes of the same slot must produce ONE network call."""
    import threading

    acc.slot_write("race", {"claudeAiOauth": oauth_blob("stale", expires_in=-100),
                            "identity": identity_blob("r@x.com", UUID_A)})
    calls = {"n": 0}

    def fake_refresh(oauth):
        calls["n"] += 1
        time.sleep(0.2)  # hold the lock long enough for the second caller to queue
        fresh = dict(oauth)
        fresh["accessToken"] = "sk-ant-oat01-refreshed"
        fresh["expiresAt"] = int((time.time() + 3600) * 1000)
        return fresh

    monkeypatch.setattr(acc, "refresh_oauth", fake_refresh)
    results, errors = [], []

    def worker():
        try:
            results.append(acc.ensure_fresh_slot("race"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert calls["n"] == 1, f"refresh ran {calls['n']} times — single-use token burned"
    assert all(r["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-refreshed" for r in results)


def test_cmd_switch_rejects_slot_without_identity(home, capsys):
    set_live(home, "a@x.com", UUID_A, "a")
    acc.save_index([acc.Account("broken", "b@x.com", UUID_B)])
    acc.slot_write("broken", {"claudeAiOauth": oauth_blob("b")})  # no identity key
    with pytest.raises(acc.AccountError, match="no saved identity"):
        acc.cmd_switch("broken")
    # live login untouched, no marker left behind
    blob = json.loads((home / ".credentials.json").read_text())
    assert "-a" in blob["claudeAiOauth"]["accessToken"]
    assert acc.read_marker() is None


def test_load_index_wrong_shape_and_traversal_names(home):
    acc.INDEX_FILE.write_text("[]")
    assert acc.load_index() == []
    acc.INDEX_FILE.write_text("42")
    assert acc.load_index() == []
    acc.INDEX_FILE.write_text(json.dumps(
        {"accounts": [{"name": "../evil", "email": "e@x.com", "accountUuid": UUID_A},
                      {"name": "fine", "email": "f@x.com", "accountUuid": UUID_B}]}))
    names = [a.name for a in acc.load_index()]
    assert names == ["fine"]


def test_slot_paths_reject_bad_names(home):
    for bad in ("../evil", "a/b", "", ".hidden", "x y"):
        with pytest.raises(acc.AccountError, match="invalid account name"):
            acc._slot_file(bad)


def test_index_file_is_private(home):
    acc.save_index([acc.Account("a", "a@x.com", UUID_A)])
    assert (acc.INDEX_FILE.stat().st_mode & 0o777) == 0o600


def test_read_claude_json_non_dict(home):
    (home / ".claude.json").write_text("[1, 2, 3]")
    assert acc.read_claude_json() == {}
    # and a switch path hitting it gets a friendly error, not a TypeError
    with pytest.raises(acc.AccountError):
        acc.write_claude_json_identity(identity_blob("a@x.com", UUID_A))


def test_security_hint_never_echoes_token(monkeypatch):
    class P:
        stderr = 'security: parse error near "sk-ant-ort01-supersecret"'
    assert "sk-ant" not in acc._security_error_hint(P())
    class OK:
        stderr = "security: SecKeychainItemModifyContent failed"
    assert "SecKeychainItemModifyContent" in acc._security_error_hint(OK())


def test_save_requires_account_uuid(home):
    set_live(home, "a@x.com", UUID_A, "a")
    cfg = json.loads((home / ".claude.json").read_text())
    cfg["oauthAccount"]["accountUuid"] = ""
    (home / ".claude.json").write_text(json.dumps(cfg))
    with pytest.raises(acc.AccountError, match="accountUuid"):
        acc.cmd_save("x")
