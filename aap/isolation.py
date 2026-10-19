"""Disposable fixture trees. This is config isolation, not an OS sandbox."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile


@dataclass
class Workspace:
    root: Path
    project: Path
    cwd: Path
    env: dict


@contextmanager
def isolated(fixture, client):
    # An absolute system temp root avoids discovering instructions in the source repo.
    with tempfile.TemporaryDirectory(prefix="aap-", dir="/tmp") as name:
        root = Path(name).resolve()
        project = root / "project"
        project.mkdir()
        for folder in ("home", "codex", "claude", "config", "cache", "data", "tmp", "control"):
            (root / folder).mkdir()
        env = {"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
               "CLAUDE_CONFIG_DIR": str(root / "claude"), "XDG_CONFIG_HOME": str(root / "config"),
               "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
               "TMPDIR": str(root / "tmp"), "PATH": "/usr/bin:/bin",
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
               "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        written = set()
        for key, content in fixture.data["files"].items():
            key = key.replace("$INSTRUCTIONS", "AGENTS.md" if client == "codex" else "CLAUDE.md")
            if key in written:
                raise ValueError("fixture paths collide after instruction expansion")
            written.add(key)
            path = project / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cwd = project / fixture.data.get("cwd", ".")
        cwd.mkdir(parents=True, exist_ok=True)
        yield Workspace(root, project, cwd, env)


def snapshot(project):
    result = {}
    for path in sorted(project.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in fixture tree")
        if path.is_file():
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("oversized artifact")
            result[path.relative_to(project).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("special file in fixture tree")
        if len(result) > 1000 or sum(map(len, result.values())) > 4 * 1024 * 1024:
            raise ValueError("fixture tree limit exceeded")
    return result


def edit(project, edits):
    for name, content in edits.items():
        path = project / name
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def restore(project, before, after):
    """Conservative three-state restore at a quiescent point, preserving conflicts.

    No merge guesses: if current bytes differ from agent-post bytes, refuse the file.
    Callers must stop writers first; this is not a concurrent filesystem transaction.
    """
    current = snapshot(project)
    conflicts, restored = [], []
    for name in sorted(before.keys() | after.keys()):
        old, agent, now = before.get(name), after.get(name), current.get(name)
        if old == agent:
            continue
        if now != agent:
            conflicts.append(name)
            continue
        path = project / name
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
        restored.append(name)
    return {"conflicts": conflicts, "restored": restored}
