from dataclasses import replace
import json
from pathlib import Path

import pytest

from aap.adapters import FakeAdapter
from aap.cli import main, select
from aap.isolation import restore
from aap.model import overall
from aap.runner import run


def one(tmp_path, ident, adapter=None, **kwargs):
    report = run(select(ident), adapter or FakeAdapter(), tmp_path, **kwargs)
    return report, report["cases"][0]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_complete_synthetic_suite(tmp_path, client):
    report = run(select(), FakeAdapter(client), tmp_path)
    assert report["status"] == "pass"
    assert report["exercised"] == report["selected"] == 9
    assert report["live_verification"] == "UNVERIFIED"
    assert report["synthetic"]
    for case in report["cases"]:
        assert case["client"] == "fake"
        assert case["client_version"] == "1.0.0"
        assert len(case["fixture_sha256"]) == 64
        for evidence in case["evidence"]:
            path = tmp_path / report["run_id"] / evidence
            if path.name == "evidence.json":
                assert json.loads(path.read_text())["fixture_sha256"] == case["fixture_sha256"]
            else:
                from aap.model import digest
                assert digest(path.read_bytes()) == case["fixture_sha256"]


@pytest.mark.parametrize("ident,fault,status", [
    ("hook-tool-call", "missing_hook", "fail"),
    ("runtime-pin", "stale_runtime", "fail"),
    ("policy-denied-write", "denied_write_bypass", "fail"),
    ("runtime-pin", "unknown", "unknown"),
    ("runtime-pin", "unsupported", "unsupported"),
    ("runtime-pin", "crash", "unknown"),
    ("runtime-pin", "no_completion", "unknown"),
    ("policy-denied-write", "zero_tools", "unknown"),
    ("hook-tool-call", "zero_tools", "fail"),
])
def test_seeded_faults(tmp_path, ident, fault, status):
    report, case = one(tmp_path, ident, FakeAdapter(fault=fault))
    assert case["status"] == report["status"] == status


def test_incomplete_restore_is_failure(tmp_path):
    def incomplete(project, before, after):
        return {"restored": [], "conflicts": []}  # zero exit/receipt is not proof
    _, case = one(tmp_path, "restore-clean", restorer=incomplete)
    assert case["status"] == "fail"
    assert not case["observed"]["restore"]["final_matches"]


def test_clobbering_human_edit_is_failure(tmp_path):
    def clobber(project, before, after):
        receipt = restore(project, before, after)
        (project / "shared.txt").write_bytes(before["shared.txt"])
        return receipt
    _, case = one(tmp_path, "restore-conflict", restorer=clobber)
    assert case["status"] == "fail"


def test_wrong_instruction_precedence_is_failure(tmp_path):
    class Wrong(FakeAdapter):
        def run(self, fixture, workspace):
            result = super().run(fixture, workspace)
            (workspace.project / fixture.data["target"]).write_text("ROOT_RULE\n")
            return result
    _, case = one(tmp_path, "instructions-nested", Wrong())
    assert case["status"] == "fail"


@pytest.mark.parametrize("tamper", ["duplicate", "wrong_id", "wrong_event", "wrong_tool"])
def test_hook_receipts_must_correlate(tmp_path, tamper):
    class Wrong(FakeAdapter):
        def run(self, fixture, workspace):
            result = super().run(fixture, workspace)
            if tamper == "duplicate":
                result.hooks *= 2
            else:
                field = {"wrong_id": "tool_use_id", "wrong_event": "hook_event_name", "wrong_tool": "tool_name"}[tamper]
                result.hooks[0][field] = "wrong"
            return result
    _, case = one(tmp_path, "hook-tool-call", Wrong())
    assert case["status"] == "fail"


def test_runtime_boolean_is_not_trusted(tmp_path):
    class Liar(FakeAdapter):
        def run(self, fixture, workspace):
            result = super().run(fixture, workspace)
            result.runtime["matches_pin"] = True
            result.runtime["executable_sha256"] = "b" * 64
            return result
    _, case = one(tmp_path, "runtime-pin", Liar())
    assert case["status"] == "fail"


def test_bypass_is_failure_even_with_incomplete_run(tmp_path):
    class CrashAfterWrite(FakeAdapter):
        def run(self, fixture, workspace):
            super().run(fixture, workspace)
            raise RuntimeError("crash after bypass")
    _, case = one(tmp_path, "policy-denied-write", CrashAfterWrite(fault="denied_write_bypass"))
    assert case["status"] == "fail"


def test_no_cases_and_dry_run_never_pass(tmp_path):
    assert run([], FakeAdapter(), tmp_path)["status"] == "unknown"
    report = run(select(), FakeAdapter(), tmp_path, dry_run=True)
    assert report["exercised"] == 0
    assert report["status"] == "unknown"
    assert all(case["status"] == "unknown" for case in report["cases"])


@pytest.mark.parametrize("states,exercised,expected", [
    (["pass"], False, "unknown"), (["unsupported"], False, "unsupported"),
    (["pass", "unsupported"], True, "unknown"), (["pass", "unknown"], True, "unknown"),
    (["fail", "unknown"], True, "fail"), (["pass", "pass"], True, "pass"),
])
def test_conservative_aggregation(states, exercised, expected):
    assert overall([{"status": s, "exercised": exercised} for s in states]) == expected


def test_every_case_needs_exercise_for_overall_pass():
    assert overall([{"status": "pass", "exercised": True},
                    {"status": "pass", "exercised": False}]) == "unknown"


def test_fixture_hash_covers_exact_bytes(tmp_path):
    from aap.model import Fixture
    fixture = select("runtime-pin")[0]
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture.data))
    first = Fixture.load(path)
    path.write_text(json.dumps(fixture.data) + "\n")
    second = Fixture.load(path)
    assert first.hash != second.hash


def test_restorer_failure_becomes_unknown(tmp_path):
    def broken(*args):
        raise OSError("synthetic failure")
    _, case = one(tmp_path, "restore-clean", restorer=broken)
    assert case["status"] == "unknown"


def test_cli_json_and_exit_codes(tmp_path, capsys):
    assert main(["run", "--client", "fake", "--json", "--output", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert main(["run", "--dry-run", "--json", "--output", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["exercised"] == 0
    assert main(["run", "--fixture", "missing-id", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


def test_disabled_live_and_missing_pin(tmp_path):
    from aap.adapters import CodexAdapter, ClaudeAdapter
    for adapter in [CodexAdapter(), ClaudeAdapter(), CodexAdapter(enabled=True)]:
        _, case = one(tmp_path, "runtime-pin", adapter)
        assert case["status"] == "unknown"
        assert not case["exercised"]
    _, case = one(tmp_path, "hook-tool-call", CodexAdapter())
    assert case["status"] == "unsupported"
