import json
from pathlib import Path
import sys
import subprocess

import pytest

from aap.adapters import ClaudeAdapter, CodexAdapter, Pin, decode_stream, invoke
from aap.cli import select
from aap.isolation import isolated
from aap.model import digest
from aap.runner import run


def codex_stream(command=None, code=0, output=""):
    events = [{"type": "thread.started", "thread_id": "synthetic"}, {"type": "turn.started"}]
    if command:
        events.append({"type": "item.completed", "item": {"type": "command_execution", "id": "t1",
                       "command": command, "exit_code": code, "aggregated_output": output}})
    events.append({"type": "turn.completed", "usage": {}})
    return "\n".join(map(json.dumps, events))


def claude_stream():
    return "\n".join(map(json.dumps, [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "read-me.txt"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "synthetic input"}]}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]))


def test_native_envelopes_decode():
    events, completed = decode_stream("codex", codex_stream("printf BYPASS > aap-denied-target.txt", 1, "Permission denied"))
    assert completed and events[0]["denied"] and not events[0]["ok"]
    events, completed = decode_stream("claude", claude_stream())
    assert completed and events[0]["ok"] and events[0]["id"] == "t1"


@pytest.mark.parametrize("client,raw", [
    ("codex", ""), ("codex", "installer success"), ("codex", "{}"),
    ("claude", '{"type":"result","subtype":"success","is_error":false}'),
    ("codex", '{"type":"thread.started"}\n{"type":"turn.failed"}'),
    ("claude", '{"type":"system","subtype":"init"}\n{"type":"result","subtype":"error","is_error":true}'),
])
def test_unrecognized_or_failed_stream_never_passes(client, raw):
    with pytest.raises(ValueError):
        decode_stream(client, raw)


def test_missing_terminal_completion():
    events, completed = decode_stream("codex", '{"type":"thread.started"}')
    assert not completed
    _, completed = decode_stream("codex", codex_stream() + '\n{"type":"turn.started"}')
    assert not completed


@pytest.mark.parametrize("cls", [CodexAdapter, ClaudeAdapter])
def test_commands_use_only_isolated_config(cls):
    adapter = cls()
    with isolated(select("policy-denied-write")[0], adapter.client) as workspace:
        fixture = select("policy-denied-write")[0]
        adapter.configure(fixture, workspace)
        argv = adapter.command(fixture, workspace, "/synthetic/agent")
        assert argv[0] == "/synthetic/agent"
        assert not any("dangerously" in value or "bypass" in value.lower() for value in argv[:-1])
        if adapter.client == "codex":
            assert argv[1] == "exec"
            assert "read-only" in argv
            assert 'approval_policy = "never"' in (workspace.root / "codex/config.toml").read_text()
        else:
            assert "--print" in argv
            assert argv[argv.index("--setting-sources") + 1] == ""
            config = json.loads((workspace.root / "control/settings.json").read_text())
            assert config["permissions"]["deny"] == ["Bash", "Write", "Edit"]


def test_claude_hook_settings_have_a_real_synthetic_script():
    fixture = select("hook-tool-call")[0]
    with isolated(fixture, "claude") as workspace:
        ClaudeAdapter().configure(fixture, workspace)
        config = json.loads((workspace.root / "control/settings.json").read_text())
        assert config["hooks"]["PreToolUse"][0]["matcher"] == "Read"
        assert (workspace.root / "control/hook.py").is_file()
        # Execute only our original script, with synthetic stdin, not a vendor client.
        receipt = workspace.root / "control/hooks.jsonl"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "synthetic-t1"}
        proc = subprocess.run([sys.executable, str(workspace.root / "control/hook.py"), str(receipt)],
                              input=json.dumps(event), text=True, capture_output=True,
                              cwd=workspace.cwd, env=workspace.env, timeout=5)
        assert proc.returncode == 0
        assert json.loads(receipt.read_text()) == event


@pytest.fixture
def pin(tmp_path):
    binary = tmp_path / "synthetic-binary"
    binary.write_bytes(b"synthetic executable identity; never executed")
    return Pin(str(binary), "1.2.3", digest(binary.read_bytes()))


@pytest.mark.parametrize("cls,version,stream", [
    (CodexAdapter, "codex-cli 1.2.3\n", codex_stream()),
    (ClaudeAdapter, "1.2.3 (Claude Code)\n", claude_stream()),
])
def test_live_transport_with_injected_process_only(tmp_path, pin, cls, version, stream):
    calls = []
    def process(argv, workspace, timeout=45):
        calls.append(argv)
        assert workspace.env["HOME"] != str(Path.home())
        return (0, version, "") if argv[-1] == "--version" else (0, stream, "")
    adapter = cls(pin=pin, enabled=True, executor=process)
    report = run(select("runtime-pin"), adapter, tmp_path / "reports")
    assert report["status"] == "pass"
    assert report["cases"][0]["client_version"] == "1.2.3"
    assert len(calls) == 2
    assert report["synthetic"] is True
    assert report["live_verification"] == "UNVERIFIED"
    assert all(row["live_verification"] == "UNVERIFIED"
               for row in report["capabilities"]["rows"])
    # This test is synthetic; it does not establish live vendor behavior.


@pytest.mark.parametrize("failure", ["missing", "startup", "timeout", "malformed", "installer_zero", "terminal_missing"])
def test_live_transport_failures_are_unknown(tmp_path, pin, failure):
    def process(argv, workspace, timeout=45):
        if failure == "startup":
            return 1, "", "synthetic startup error"
        if failure == "timeout":
            raise RuntimeError("process timeout")
        if argv[-1] == "--version":
            return 0, "codex-cli 1.2.3", ""
        return 0, {"malformed": "{", "installer_zero": "installed successfully",
                   "terminal_missing": '{"type":"thread.started"}'}.get(failure, ""), ""
    if failure == "missing":
        pin = Pin(str(tmp_path / "not-present"), pin.version, pin.sha256)
    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=process), tmp_path / "reports")
    assert report["status"] == "unknown"


def test_stale_binary_refused_before_launch(tmp_path, pin):
    def never(*args, **kwargs):
        raise AssertionError("should not launch stale executable")
    pin = Pin(pin.executable, pin.version, "b" * 64)
    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=never), tmp_path / "reports")
    assert report["status"] == "fail"
    assert not report["cases"][0]["exercised"]


def test_runtime_replaced_during_execution_is_failure(tmp_path, pin):
    def process(argv, workspace, timeout=45):
        if argv[-1] == "--version":
            return 0, "codex-cli 1.2.3", ""
        Path(pin.executable).write_bytes(b"replacement")
        return 0, codex_stream(), ""
    report = run(select("runtime-pin"), CodexAdapter(pin=pin, enabled=True, executor=process), tmp_path / "reports")
    assert report["status"] == "fail"


def test_local_subprocess_isolation_and_output_limits():
    with isolated(select("runtime-pin")[0], "codex") as workspace:
        code, out, err = invoke([sys.executable, "-c", "import os; print(os.environ['HOME'])"], workspace)
        assert code == 0 and out.strip() == workspace.env["HOME"] and not err
        with pytest.raises(RuntimeError, match="timeout"):
            invoke([sys.executable, "-c", "import time; time.sleep(5)"], workspace, timeout=0.05)
        with pytest.raises(RuntimeError, match="output limit"):
            invoke([sys.executable, "-c", "print('x' * 2200000)"], workspace)


def test_pin_validation(tmp_path):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps({"executable": "/opt/agents/codex", "version": "1.2.3", "sha256": "a" * 64}))
    assert Pin.load(path).version == "1.2.3"
    path.write_text(json.dumps({"executable": "relative", "version": "1.2.3", "sha256": "a" * 64}))
    with pytest.raises(ValueError):
        Pin.load(path)


def test_observed_claude_not_logged_in_shape_is_never_a_pass():
    # Shape observed from Claude Code 2.1.309 with no credential: "success" subtype, is_error true.
    raw = "\n".join(map(json.dumps, [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Not logged in"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"},
    ]))
    with pytest.raises(ValueError):
        decode_stream("claude", raw)
