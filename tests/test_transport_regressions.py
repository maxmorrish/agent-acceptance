"""Invented transport envelopes: no captured account data or live clients."""
import json

import pytest

from aap.adapters import ClaudeAdapter, CodexAdapter, FakeAdapter, Pin, decode_stream
from aap.cli import select
from aap.model import digest
from aap.runner import run


def envelope(client, events):
    start = ({"type": "thread.started"} if client == "codex" else
             {"type": "system", "subtype": "init"})
    end = ({"type": "turn.completed"} if client == "codex" else
           {"type": "result", "subtype": "success", "is_error": False})
    return "\n".join(json.dumps(e) for e in [start, *events, end])


def call(ident="tool-1"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": "Read", "input": {"file_path": "sample.txt"}}]}}


def result(ident="tool-1", **kwargs):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ident, "content": "synthetic", **kwargs}]}}


def command(ident="command-1"):
    return {"type": "item.completed", "item": {
        "type": "command_execution", "id": ident, "command": "echo synthetic",
        "exit_code": 0, "aggregated_output": "synthetic"}}


BAD_STREAMS = [
    ("claude", envelope("claude", [result()])),
    ("claude", envelope("claude", [call(), result("other")])),
    ("claude", envelope("claude", [call(), result(), result(is_error=True)])),
    ("claude", envelope("claude", [call(), result(is_error=True), result()])),
    ("claude", envelope("claude", [call()])),
    ("claude", envelope("claude", [call(), result(is_error="false")])),
    ("claude", envelope("claude", [call(), result(is_error=0)])),
    ("claude", envelope("claude", [call("")])),
    ("claude", envelope("claude", []) + "\n" + json.dumps(call())),
    ("codex", envelope("codex", [command(), command()])),
    ("codex", envelope("codex", [command(None)])),
    ("codex", envelope("codex", [command("")])),
]


@pytest.mark.parametrize("client,raw", BAD_STREAMS)
def test_ambiguous_stream_is_unknown_in_full_runner(tmp_path, client, raw):
    binary = tmp_path / "binary"
    binary.write_bytes(b"synthetic identity only")
    pin = Pin(str(binary), "1.2.3", digest(binary.read_bytes()))

    def process(argv, workspace, timeout=45):
        return (0, "1.2.3", "") if argv[-1] == "--version" else (0, raw, "")

    adapter = {"claude": ClaudeAdapter, "codex": CodexAdapter}[client](
        pin=pin, enabled=True, executor=process)
    report = run(select("runtime-pin"), adapter, tmp_path / "reports")
    assert report["status"] == "unknown"
    assert report["exercised"] == 0
    assert report["synthetic"] is True
    assert report["live_verification"] == "UNVERIFIED"


def test_interleaved_claude_calls_correlate_by_id():
    events, completed = decode_stream("claude", envelope("claude", [
        call("one"), call("two"), result("two", is_error=True), result("one")]))
    assert completed
    assert [(e["id"], e["ok"]) for e in events] == [("one", True), ("two", False)]


def test_unknown_client_rejected():
    with pytest.raises(ValueError, match="unknown stream client"):
        decode_stream("typo", envelope("claude", []))


@pytest.mark.parametrize("adapter,expected", [
    (CodexAdapter(), {"unknown": 8, "unsupported": 1}),
    (ClaudeAdapter(), {"unknown": 9, "unsupported": 0}),
    (FakeAdapter(), {"pass": 9}),
])
def test_capability_matrix_preserves_measured_boundaries(tmp_path, adapter, expected):
    report = run(select(), adapter, tmp_path)
    for state, count in expected.items():
        assert report["counts"][state] == count
    matrix = report["capabilities"]
    assert matrix["schema_version"] == 1
    assert sum(row["selected"] for row in matrix["rows"]) == 9
    for row in matrix["rows"]:
        assert row["live_verification"] == "UNVERIFIED"
        assert sum(row["counts"].values()) == row["selected"]
    saved = json.loads((tmp_path / report["report"]).read_text())
    assert saved["capabilities"] == matrix


def test_unselected_capabilities_are_unknown_and_dry_run_stays_unverified(tmp_path):
    report = run(select("runtime-pin"), FakeAdapter(), tmp_path, dry_run=True)
    for row in report["capabilities"]["rows"]:
        assert row["status"] == "unknown"
        assert row["exercised"] == 0
        assert row["selected"] == (1 if row["kind"] == "runtime" else 0)
        assert row["live_verification"] == "UNVERIFIED"
