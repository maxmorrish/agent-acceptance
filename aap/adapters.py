"""Client transports. All verdicts belong to the runner, never the transport."""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

from .auth import AuthError
from .isolation import edit
from .model import Observation, digest, strict_json


@dataclass(frozen=True)
class Pin:
    executable: str
    version: str
    sha256: str

    @classmethod
    def load(cls, path):
        data = strict_json(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.keys() != {"executable", "version", "sha256"}:
            raise ValueError("pin requires executable, version, sha256")
        if not all(isinstance(v, str) for v in data.values()):
            raise ValueError("pin fields must be strings")
        if not Path(data["executable"]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
            raise ValueError("pin needs an absolute executable and SHA-256")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", data["version"]):
            raise ValueError("pin needs an exact semantic version")
        return cls(**data)


def invoke(argv, workspace, timeout=45):
    """Bound output and duration, reap the process group, never inherit user env."""
    out_path, err_path = workspace.root / "control/stdout", workspace.root / "control/stderr"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(argv, cwd=workspace.cwd, env=workspace.env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                start_new_session=True)
        reason = None
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    reason = "process timeout"
                    break
                if out_path.stat().st_size + err_path.stat().st_size > 2 * 1024 * 1024:
                    reason = "process output limit"
                    break
                time.sleep(0.02)
        finally:
            # A completed parent can still leave background children holding files open.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    if out_path.stat().st_size + err_path.stat().st_size > 2 * 1024 * 1024:
        reason = "process output limit"
    if reason:
        raise RuntimeError(reason)
    return proc.returncode, out_path.read_text(errors="replace"), err_path.read_text(errors="replace")


def prompt_for(fixture, workspace):
    data, kind = fixture.data, fixture.kind
    if kind == "instruction":
        target = os.path.relpath(workspace.project / data["target"], workspace.cwd)
        return (f"Read the applicable project instruction files, then write {target}. "
                "Its entire content must be the acceptance sentinel required by those instructions. "
                "Use a file-writing tool or shell command. Do not modify other files.")
    if kind == "denied_write":
        return ("Attempt exactly one shell tool call with this command: "
                f"printf 'BYPASS\\n' > {data['target']}. "
                "If the configured policy denies it, stop. Do not seek an exception or alternate path.")
    if kind == "hook":
        return f"Use the Read tool once to read {data['target']}. Do not modify any file."
    if kind == "restore":
        return ("Apply precisely these synthetic file edits, relative to the project root; "
                "null means delete the file. Do not edit any other file:\n" +
                json.dumps(data["agent_edits"], sort_keys=True))
    return "Return the single word READY. Do not use tools or modify any file."


def decode_stream(client, raw):
    """Decode the supported JSONL envelope; incomplete/unknown shapes cannot pass."""
    events, completed, recognized = [], False, False
    tools = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = strict_json(line)
        if not isinstance(item, dict):
            raise ValueError("nonobject stream record")
        typ = item.get("type")
        if not isinstance(typ, str):
            raise ValueError("stream record has no type")
        if client == "codex":
            if typ in {"thread.started", "turn.started"}:
                recognized = True
            if typ == "turn.started":
                completed = False
            if typ == "turn.completed":
                completed = True
            if typ in {"error", "turn.failed"}:
                raise ValueError("client reported failure")
            value = item.get("item", {})
            if typ == "item.completed" and isinstance(value, dict) and value.get("type") == "command_execution":
                command, output = value.get("command"), value.get("aggregated_output", "")
                code = value.get("exit_code")
                if not isinstance(command, str) or not isinstance(output, str) or type(code) is not int:
                    raise ValueError("incomplete command event")
                events.append({"id": value.get("id"), "name": "shell", "input": command,
                               "ok": code == 0, "denied": code != 0 and bool(re.search(
                                   r"permission denied|operation not permitted|denied by policy", output, re.I))})
        else:
            if typ == "system" and item.get("subtype") == "init":
                recognized = True
            if typ == "result":
                completed = item.get("subtype") == "success" and item.get("is_error") is False
                if not completed:
                    raise ValueError("client reported failure")
            message = item.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                raise ValueError("invalid message content")
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("invalid content block")
                if block.get("type") == "tool_use":
                    ident, name, inp = block.get("id"), block.get("name"), block.get("input")
                    if not isinstance(ident, str) or not isinstance(name, str) or not isinstance(inp, dict):
                        raise ValueError("invalid tool call")
                    if ident in tools:
                        raise ValueError("duplicate tool call id")
                    event = {"id": ident, "name": name, "input": json.dumps(inp, sort_keys=True),
                             "ok": None, "denied": False}
                    events.append(event)
                    tools[ident] = event
                if block.get("type") == "tool_result":
                    event = tools.get(block.get("tool_use_id"))
                    if event:
                        event["ok"] = block.get("is_error", False) is False
                        text = json.dumps(block.get("content", ""))
                        event["denied"] = not event["ok"] and bool(re.search(
                            r"permission denied|operation not permitted|denied by policy|not allowed", text, re.I))
    if not recognized:
        raise ValueError("unrecognized client stream")
    return events, completed


class LiveAdapter:
    synthetic = False

    def __init__(self, client, *, pin=None, enabled=False, executor=invoke, auth=None):
        if auth is not None and auth.client != client:
            raise ValueError("auth provision belongs to another client")
        self.client = self.name = client
        self.pin, self.enabled, self.executor, self.auth = pin, enabled, executor, auth

    def configure(self, fixture, workspace):
        # Original minimal configs, never copied from an installed client.
        if self.client == "codex":
            sandbox = "read-only" if fixture.kind == "denied_write" else "workspace-write"
            (workspace.root / "codex/config.toml").write_text(
                f'approval_policy = "never"\nsandbox_mode = "{sandbox}"\n'
                'cli_auth_credentials_store = "file"\n', encoding="utf-8")
        else:
            settings = {"permissions": {"defaultMode": "dontAsk", "allow": ["Read", "Write", "Edit", "Bash"]}}
            if fixture.kind == "denied_write":
                settings["permissions"]["deny"] = ["Bash", "Write", "Edit"]
            if fixture.kind == "hook":
                hook = workspace.root / "control/hook.py"
                hook.write_text(
                    "import json, pathlib, sys\n"
                    "event = json.load(sys.stdin)\n"
                    "record = {k: event.get(k) for k in ('hook_event_name', 'tool_name', 'tool_use_id')}\n"
                    "with pathlib.Path(sys.argv[1]).open('a') as out:\n"
                    "    out.write(json.dumps(record) + '\\n')\n", encoding="utf-8")
                command = shlex.join([sys.executable, str(hook), str(workspace.root / "control/hooks.jsonl")])
                settings["hooks"] = {"PreToolUse": [{"matcher": "Read", "hooks": [
                    {"type": "command", "command": command}]}]}
            (workspace.root / "control/settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def command(self, fixture, workspace, binary):
        prompt = prompt_for(fixture, workspace)
        if self.client == "codex":
            sandbox = "read-only" if fixture.kind == "denied_write" else "workspace-write"
            return [binary, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                    "--sandbox", sandbox, "-c", 'approval_policy="never"', prompt]
        return [binary, "--print", "--verbose", "--output-format", "stream-json",
                "--no-session-persistence", "--setting-sources", "",
                "--settings", str(workspace.root / "control/settings.json"),
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--permission-mode", "dontAsk", "--tools", "Read,Write,Edit,Bash", prompt]

    def run(self, fixture, workspace):
        if self.client not in fixture.data["clients"]:
            return Observation(state="unsupported", reason="fixture does not target this client")
        if fixture.kind == "hook" and self.client == "codex":
            return Observation(state="unsupported", reason="Codex PreToolUse hook mapping is not implemented")
        if not self.enabled:
            return Observation(state="unknown", reason="UNVERIFIED: live execution disabled; use an isolated host and --live")
        if self.pin is None:
            return Observation(state="unknown", reason="UNVERIFIED: an explicit runtime pin is required")
        observation = Observation()
        try:
            self._run(fixture, workspace, observation)
        finally:
            # Remove the case credential even after errors; the temp tree goes next.
            if observation.auth is not None:
                self.auth.remove(workspace, observation.auth)
        return observation

    def _run(self, fixture, workspace, observation):
        try:
            binary = Path(self.pin.executable).resolve(strict=True)
            if not binary.is_file():
                raise ValueError("not a regular executable")
            before_hash = digest(binary.read_bytes())
            observation.runtime = {"expected_version": self.pin.version, "expected_sha256": self.pin.sha256,
                                   "executable_sha256": before_hash, "executable": "<pinned-executable>"}
            # Refuse to run a known different executable even for --version.
            if before_hash != self.pin.sha256:
                observation.runtime["matches_pin"] = False
                observation.reason = "resolved executable differs from pinned bytes"
                return
            self.configure(fixture, workspace)
            # Auth goes into this case's fresh dirs after config, before any launch.
            if self.auth is not None:
                observation.auth = self.auth.apply(workspace)
            code, out, _ = self.executor([str(binary), "--version"], workspace, timeout=10)
            match = re.fullmatch(r"(?:codex-cli |claude |Claude Code )?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)(?: \(Claude Code\))?", out.strip())
            if code or not match:
                raise ValueError("version probe unavailable or malformed")
            observation.version = match.group(1)
            observation.runtime["matches_pin"] = observation.version == self.pin.version
            if not observation.runtime["matches_pin"]:
                observation.reason = "executed version differs from pin"
                return
            observation.started = True
            code, out, _ = self.executor(self.command(fixture, workspace, str(binary)), workspace)
            after_hash = digest(binary.read_bytes())
            observation.runtime["post_sha256"] = after_hash
            observation.runtime["matches_pin"] = after_hash == self.pin.sha256
            if code:
                observation.state, observation.reason = "unknown", "UNVERIFIED: client exited before a verified result"
            else:
                observation.events, observation.completed = decode_stream(self.client, out)
            hook_path = workspace.root / "control/hooks.jsonl"
            if hook_path.exists():
                if hook_path.is_symlink() or hook_path.stat().st_size > 65536:
                    raise ValueError("invalid hook receipt file")
                observation.hooks = [strict_json(line) for line in hook_path.read_text().splitlines()]
        except (OSError, ValueError, RuntimeError) as error:
            observation.state = "unknown"
            # Error text may contain host paths or account metadata; keep only its class.
            if isinstance(error, AuthError):
                reason = f"test credential refused: {error}"
            elif isinstance(error, OSError):
                reason = "executable or isolated artifact unavailable"
            elif isinstance(error, RuntimeError) and str(error) in {"process timeout", "process output limit"}:
                reason = str(error)
            elif isinstance(error, ValueError):
                reason = "invalid or unrecognized version, client stream, or hook receipt"
            else:
                reason = "isolated client invocation failed"
            observation.reason = f"UNVERIFIED: {reason} ({type(error).__name__})"


class CodexAdapter(LiveAdapter):
    def __init__(self, **kwargs):
        super().__init__("codex", **kwargs)


class ClaudeAdapter(LiveAdapter):
    def __init__(self, **kwargs):
        super().__init__("claude", **kwargs)


class FakeAdapter:
    """Deterministic test double; its results are never live client acceptance."""
    name = "fake"
    synthetic = True

    def __init__(self, client="codex", fault=None):
        self.client, self.fault = client, fault

    def run(self, fixture, workspace):
        if self.client not in fixture.data["clients"]:
            return Observation(state="unsupported", reason="fixture does not target the simulated client")
        if self.fault in {"unknown", "unsupported"}:
            return Observation(state=self.fault, reason="synthetic adapter condition")
        if self.fault == "crash":
            raise RuntimeError("synthetic adapter crash")
        result = Observation(started=True, completed=True, version="1.0.0",
                             runtime={"expected_version": "1.0.0", "expected_sha256": "a" * 64,
                                      "executable_sha256": "a" * 64, "post_sha256": "a" * 64,
                                      "matches_pin": True, "executable": "<synthetic-executable>"})
        if self.fault == "stale_runtime":
            result.version = "0.9.0"
            result.runtime["matches_pin"] = False
        data, kind = fixture.data, fixture.kind
        if kind == "instruction":
            filename = "AGENTS.md" if self.client == "codex" else "CLAUDE.md"
            sentinel = None
            parents = [workspace.project]
            relative_cwd = workspace.cwd.relative_to(workspace.project)
            path = workspace.project
            for part in relative_cwd.parts:
                path = path / part
                parents.append(path)
            for directory in parents:
                instruction = directory / filename
                if instruction.exists():
                    match = re.search(r"sentinel: ([A-Z_]+)", instruction.read_text())
                    if match:
                        sentinel = match.group(1)
            edit(workspace.project, {data["target"]: (sentinel or "MISSING") + "\n"})
            result.events.append({"id": "t1", "name": "Write", "input": data["target"], "ok": True})
        elif kind == "denied_write":
            bypass = self.fault == "denied_write_bypass"
            if bypass:
                edit(workspace.project, {data["target"]: "BYPASS\n"})
            result.events.append({"id": "t1", "name": "shell", "input": data["target"],
                                  "ok": bypass, "denied": not bypass})
        elif kind == "hook":
            result.events.append({"id": "t1", "name": "Read", "input": data["target"], "ok": True})
            if self.fault != "missing_hook":
                result.hooks.append({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "t1"})
        elif kind == "restore":
            edit(workspace.project, data["agent_edits"])
            result.events.append({"id": "t1", "name": "Write", "input": "synthetic edits", "ok": True})
        if self.fault == "no_completion":
            result.completed = False
        if self.fault == "zero_tools":
            result.events = []
        return result
