"""Strict, bounded JSON fixtures and result vocabulary."""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

STATES = {"pass", "fail", "unsupported", "unknown"}
KINDS = {"instruction", "denied_write", "hook", "runtime", "restore"}


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def relative(name: str, *, dot=False) -> str:
    if dot and name == ".":
        return name
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("invalid relative path")
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in name.split("/")):
        raise ValueError("path must be normalized and relative")
    if any(x.startswith(".") for x in p.parts):
        raise ValueError("hidden paths are not fixture targets")
    if not re.fullmatch(r"[A-Za-z0-9_.$/-]+", name):
        raise ValueError("unsupported path characters")
    return name


def strict_json(raw: str):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


@dataclass(frozen=True)
class Fixture:
    data: dict
    hash: str
    raw: bytes

    @property
    def id(self):
        return self.data["id"]

    @property
    def kind(self):
        return self.data["kind"]

    @classmethod
    def load(cls, path: Path):
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("fixture must be a small regular JSON file")
        raw = path.read_bytes()
        data = strict_json(raw.decode("utf-8"))
        required = {"schema_version", "id", "kind", "clients", "files", "expected"}
        optional = {"cwd", "target", "agent_edits", "human_edits"}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
            raise ValueError("invalid fixture fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported fixture schema")
        if not isinstance(data["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", data["id"]):
            raise ValueError("invalid fixture id")
        if not isinstance(data["kind"], str) or data["kind"] not in KINDS:
            raise ValueError("unsupported fixture kind")
        if not isinstance(data["clients"], list) or not data["clients"] or any(
            c not in ("codex", "claude") for c in data["clients"]
        ) or len(set(data["clients"])) != len(data["clients"]):
            raise ValueError("invalid clients")
        for key in ("files", "agent_edits", "human_edits"):
            values = data.get(key, {})
            if not isinstance(values, dict):
                raise ValueError("file maps must be objects")
            for name, content in values.items():
                relative(name)
                if "$" in name and (key != "files" or any(
                        "$" in part and part != "$INSTRUCTIONS" for part in name.split("/"))):
                    raise ValueError("instruction placeholder only allowed in setup file names")
                if not isinstance(content, str) and not (key != "files" and content is None):
                    raise ValueError("file content must be text or an edit deletion")
        relative(data.get("cwd", "."), dot=True)
        if not isinstance(data["expected"], dict):
            raise ValueError("expected must be an object")
        kind, expected = data["kind"], data["expected"]
        allowed_optional = {"instruction": {"cwd", "target"}, "denied_write": {"target"},
                            "hook": {"target"}, "runtime": set(),
                            "restore": {"agent_edits", "human_edits"}}[kind]
        if (data.keys() & optional) - allowed_optional:
            raise ValueError("field does not apply to this fixture kind")
        fields = {"instruction": {"content"}, "denied_write": {"content", "denial"},
                  "hook": {"event", "tool"}, "runtime": {"matches_pin"},
                  "restore": {"files", "conflicts"}}[kind]
        if expected.keys() != fields:
            raise ValueError("invalid expectation fields")
        if kind in {"instruction", "denied_write", "hook"}:
            relative(data.get("target", ""))
        if kind in {"instruction", "denied_write"} and not isinstance(expected["content"], str):
            raise ValueError("content expectation must be text")
        if kind == "denied_write" and (expected["denial"] is not True or
                data["files"].get(data["target"]) != expected["content"]):
            raise ValueError("denied target must start with expected content")
        if kind == "hook" and expected != {"event": "PreToolUse", "tool": "Read"}:
            raise ValueError("unsupported hook contract")
        if kind == "runtime" and expected["matches_pin"] is not True:
            raise ValueError("runtime must require a pin match")
        if kind == "restore":
            if not data.get("agent_edits") or not isinstance(expected["files"], dict):
                raise ValueError("restore needs edits and expected files")
            if not isinstance(expected["conflicts"], list):
                raise ValueError("conflicts must be a list")
            for name in expected["conflicts"]:
                relative(name)
            for name, content in expected["files"].items():
                relative(name)
                if content is not None and not isinstance(content, str):
                    raise ValueError("invalid restore expectation")
        return cls(data, digest(raw), raw)


@dataclass
class Observation:
    state: str | None = None  # only unknown/unsupported; adapters never grant pass
    reason: str = ""
    started: bool = False
    completed: bool = False
    version: str | None = None
    runtime: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    hooks: list = field(default_factory=list)
    auth: dict | None = None  # value-free provisioning receipt


def overall(cases):
    states = [c["status"] for c in cases]
    if "fail" in states:
        return "fail"
    if not cases or "unknown" in states:
        return "unknown"
    if all(s == "unsupported" for s in states):
        return "unsupported"
    if "unsupported" in states or not all(c["exercised"] for c in cases):
        return "unknown"
    return "pass"
