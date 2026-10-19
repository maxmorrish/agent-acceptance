import json
import os
from pathlib import Path

import pytest

from aap.cli import select
from aap.isolation import isolated, snapshot
from aap.model import Fixture


def test_environment_is_allowlisted_and_temporary(monkeypatch):
    monkeypatch.setenv("API_KEY", "synthetic-secret")
    monkeypatch.setenv("CODEX_HOME", "/forbidden/config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/forbidden/config")
    monkeypatch.setenv("BASH_ENV", "/forbidden/startup")
    with isolated(select("instructions-nested")[0], "codex") as workspace:
        root = workspace.root
        for key in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "TMPDIR"):
            assert Path(workspace.env[key]).is_relative_to(root)
        assert "API_KEY" not in workspace.env
        assert "BASH_ENV" not in workspace.env
        assert workspace.cwd == workspace.project / "nested"
        assert (workspace.cwd / "AGENTS.md").is_file()
        assert not (workspace.cwd / "CLAUDE.md").exists()
    assert not root.exists()


@pytest.mark.parametrize("path", ["../outside", "/absolute", "x/../outside", "x//y", "x\\y", ".git/config", "x/.config"])
def test_fixture_paths_cannot_escape(tmp_path, path):
    data = dict(select("runtime-pin")[0].data)
    data["files"] = {path: "synthetic"}
    fixture = tmp_path / "case.json"
    fixture.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        Fixture.load(fixture)


@pytest.mark.parametrize("raw", ['{"id":"first","id":"second"}', '[]', '{"schema_version":NaN}'])
def test_malformed_fixture_rejected(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        Fixture.load(path)


def test_snapshot_refuses_symlink(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        snapshot(tree)


def test_symlinked_fixture_rejected(tmp_path):
    source = tmp_path / "case.json"
    source.write_text('{}')
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError):
        Fixture.load(link)


@pytest.mark.parametrize("change", [
    {"expected": {"matches_pin": 1}}, {"kind": []}, {"schema_version": True},
    {"clients": []}, {"unexpected": True}, {"target": "ignored.txt"},
])
def test_strict_field_contract(tmp_path, change):
    data = {**select("runtime-pin")[0].data, **change}
    path = tmp_path / "case.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        Fixture.load(path)


def test_expanded_instruction_paths_cannot_collide(tmp_path):
    data = dict(select("instructions-root")[0].data)
    data["files"] = {"$INSTRUCTIONS": "one", "AGENTS.md": "two"}
    path = tmp_path / "case.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="collide"):
        with isolated(Fixture.load(path), "codex"):
            pass
