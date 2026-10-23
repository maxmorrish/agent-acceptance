import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from aap.adapters import ClaudeAdapter, CodexAdapter, Pin
from aap.auth import AuthError, AuthProvision
from aap.cli import main, select
from aap.model import digest
from aap.runner import run

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
MARK = "synthetic-credential-marker"


def jwt(exp):
    # Built at runtime so no token-shaped literal sits in the source tree.
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return ".".join([part({"alg": "none"}), part({"exp": int(exp.timestamp()), "note": MARK}), "sig"])


def codex_auth(tmp_path, *, refreshed=NOW - timedelta(days=1), expires=NOW + timedelta(days=2),
               extra=None, name="auth.json", mode=0o600):
    data = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": refreshed.isoformat(),
            "tokens": {"access_token": jwt(expires), "refresh_token": MARK}, **(extra or {})}
    path = tmp_path / name
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


def claude_auth(tmp_path, *, expires=NOW + timedelta(hours=5)):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": MARK, "expiresAt": int(expires.timestamp() * 1000)},
                                "mcpOAuth": {"server": MARK + "-mcp"}}))
    path.chmod(0o600)
    return path


def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text(MARK + "-token\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def pin(tmp_path):
    binary = tmp_path / "synthetic-binary"
    binary.write_bytes(b"synthetic executable identity; never executed")
    return Pin(str(binary), "1.2.3", digest(binary.read_bytes()))


def stream(client):
    if client == "codex":
        return "\n".join(map(json.dumps, [{"type": "thread.started"}, {"type": "turn.started"},
                                          {"type": "turn.completed"}]))
    return "\n".join(map(json.dumps, [{"type": "system", "subtype": "init"},
                                      {"type": "result", "subtype": "success", "is_error": False}]))


def all_output(root):
    return "".join(p.read_text(errors="replace") for p in Path(root).rglob("*") if p.is_file())


def test_codex_file_is_filtered_written_0600_and_removed(tmp_path, pin, monkeypatch):
    source = codex_auth(tmp_path, extra={"unexpected": {"profile": MARK + "-extra"}})
    original = source.read_bytes()
    credential = AuthProvision.load("codex", source, now=NOW)
    monkeypatch.setattr("aap.auth._now", lambda now: NOW)
    seen = {}

    def process(argv, workspace, timeout=45):
        target = workspace.root / "codex/auth.json"
        seen["mode"] = target.stat().st_mode & 0o777
        seen["data"] = json.loads(target.read_text())
        seen["root"] = workspace.root
        seen["home"] = workspace.env["HOME"]
        return (0, "codex-cli 1.2.3", "") if argv[-1] == "--version" else (0, stream("codex"), "")

    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=process, auth=credential),
                 tmp_path / "reports")
    assert report["status"] == "pass"
    assert seen["mode"] == 0o600
    assert set(seen["data"]) == {"auth_mode", "OPENAI_API_KEY", "last_refresh", "tokens"}
    assert seen["home"] != str(Path.home())
    assert not seen["root"].exists()
    assert report["auth"] == {"provisioned": 1, "removed": 1, "rotated": 0}
    assert MARK not in all_output(tmp_path / "reports")
    assert source.read_bytes() == original


@pytest.mark.parametrize("name", ["config.toml", "settings.json", "hooks.json", "AGENTS.md", "auth.json.bak"])
def test_only_the_credential_file_name_is_accepted(tmp_path, name):
    path = codex_auth(tmp_path, name=name)
    with pytest.raises(AuthError, match="unexpected name"):
        AuthProvision.load("codex", path, now=NOW)


def test_links_loose_modes_relative_and_oversized_sources_are_refused(tmp_path):
    (tmp_path / "real").mkdir()
    real = codex_auth(tmp_path / "real")
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / "auth.json").symlink_to(real)
    with pytest.raises(AuthError, match="link"):
        AuthProvision.load("codex", link_dir / "auth.json", now=NOW)
    loose = tmp_path / "loose"
    loose.mkdir()
    with pytest.raises(AuthError, match="0600"):
        AuthProvision.load("codex", codex_auth(loose, mode=0o644), now=NOW)
    with pytest.raises(AuthError, match="absolute"):
        AuthProvision.load("codex", Path("auth.json"), now=NOW)
    big = tmp_path / "big"
    big.mkdir()
    (big / "auth.json").write_text("{" + " " * 70000 + "}")
    (big / "auth.json").chmod(0o600)
    with pytest.raises(AuthError, match="too large"):
        AuthProvision.load("codex", big / "auth.json", now=NOW)


@pytest.mark.parametrize("kwargs,message", [
    ({"refreshed": NOW - timedelta(days=7, hours=1)}, "due for refresh"),
    ({"expires": NOW + timedelta(minutes=10)}, "expires too soon"),
])
def test_codex_credentials_near_refresh_are_refused(tmp_path, kwargs, message):
    # A refresh inside the case could rotate the source login's refresh token.
    with pytest.raises(AuthError, match=message):
        AuthProvision.load("codex", codex_auth(tmp_path, **kwargs), now=NOW)


def test_claude_file_keeps_only_oauth_entry_and_refuses_near_expiry(tmp_path):
    credential = AuthProvision.load("claude", claude_auth(tmp_path), now=NOW)
    assert set(json.loads(credential.payload)) == {"claudeAiOauth"}
    assert MARK not in repr(credential)
    with pytest.raises(AuthError, match="expires too soon"):
        AuthProvision.load("claude", claude_auth(tmp_path, expires=NOW + timedelta(minutes=5)), now=NOW)


def test_claude_token_is_set_for_the_case_process_only(tmp_path, pin):
    credential = AuthProvision.load("claude", token_file(tmp_path), kind="token")
    seen = []

    def process(argv, workspace, timeout=45):
        seen.append((workspace.env.get("CLAUDE_CODE_OAUTH_TOKEN"), workspace.env))
        return (0, "1.2.3 (Claude Code)", "") if argv[-1] == "--version" else (0, stream("claude"), "")

    report = run(select("runtime-pin"), ClaudeAdapter(pin=pin, enabled=True, executor=process, auth=credential),
                 tmp_path / "reports")
    assert report["status"] == "pass"
    assert [value for value, _ in seen] == [MARK + "-token"] * 2
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen[-1][1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert report["auth"] == {"provisioned": 1, "removed": 1, "rotated": 0}
    assert MARK not in all_output(tmp_path / "reports")


def test_rewritten_credential_is_reported_as_rotated(tmp_path, pin, monkeypatch):
    credential = AuthProvision.load("codex", codex_auth(tmp_path), now=NOW)
    monkeypatch.setattr("aap.auth._now", lambda now: NOW)

    def process(argv, workspace, timeout=45):
        if argv[-1] == "--version":
            return 0, "codex-cli 1.2.3", ""
        (workspace.root / "codex/auth.json").write_text('{"refreshed": true}')
        return 0, stream("codex"), ""

    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=process, auth=credential),
                 tmp_path / "reports")
    assert report["auth"] == {"provisioned": 1, "removed": 1, "rotated": 1}


def test_credential_removed_when_the_client_fails(tmp_path, pin, monkeypatch):
    credential = AuthProvision.load("codex", codex_auth(tmp_path), now=NOW)
    monkeypatch.setattr("aap.auth._now", lambda now: NOW)
    roots = []

    def process(argv, workspace, timeout=45):
        roots.append(workspace.root)
        if argv[-1] == "--version":
            return 0, "codex-cli 1.2.3", ""
        raise RuntimeError("process timeout")

    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=process, auth=credential),
                 tmp_path / "reports")
    assert report["status"] == "unknown"
    assert report["auth"]["removed"] == 1
    assert not roots[0].exists()


def test_stale_pin_never_receives_a_credential(tmp_path, pin):
    credential = AuthProvision.load("codex", codex_auth(tmp_path), now=NOW)
    stale = Pin(pin.executable, pin.version, "b" * 64)
    report = run(select("runtime-pin"), CodexAdapter(pin=stale, enabled=True, executor=None, auth=credential),
                 tmp_path / "reports")
    assert report["status"] == "fail"
    assert report["auth"]["provisioned"] == 0


def test_provision_bound_to_one_client(tmp_path):
    credential = AuthProvision.load("codex", codex_auth(tmp_path), now=NOW)
    with pytest.raises(ValueError):
        ClaudeAdapter(auth=credential)
    with pytest.raises(AuthError):
        AuthProvision.load("codex", token_file(tmp_path), kind="token")


def test_cli_refuses_credentials_outside_live_runs_without_echoing_paths(tmp_path, capsys):
    source = codex_auth(tmp_path)
    assert main(["run", "--client", "fake", "--auth-file", str(source), "--json",
                 "--output", str(tmp_path / "out")]) == 2
    printed = capsys.readouterr().out
    assert "credential refused" in printed and str(tmp_path) not in printed
    assert main(["run", "--client", "codex", "--auth-file", str(source), "--json",
                 "--output", str(tmp_path / "out")]) == 2
    wrong = tmp_path / "config.toml"
    wrong.write_text("synthetic")
    assert main(["run", "--client", "codex", "--live", "--auth-file", str(wrong), "--json",
                 "--output", str(tmp_path / "out")]) == 2
    printed = capsys.readouterr().out
    assert "unexpected name" in printed and str(tmp_path) not in printed
