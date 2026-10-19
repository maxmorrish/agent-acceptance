# Agent Update Acceptance Pack

Check whether the behavior you depend on still holds after a coding-agent update.
An installer returning zero is not acceptance evidence.

Milestone 1 provides nine synthetic JSON fixtures, a Python runner, Codex and
Claude Code adapters, and a deterministic fake adapter for testing the runner.
Python 3.11+ is required; the runner uses only the standard library. The process
runner currently targets POSIX systems.

Run without installing anything:

```sh
python3 -m aap run --client fake
```

Example output, shortened:

```text
SYNTHETIC: pass | 9 exercised | live UNVERIFIED
pass        hook-tool-call: positive hook coverage correlated to tool calls
pass        policy-denied-write: denied tool attempt and unchanged bytes verified
pass        restore-conflict: restored agent edits and preserved concurrent edits
Evidence: <output>/<run-id>/report.json
```

This checks the runner with a fake client. It does not establish that a released
Codex or Claude Code version passes these fixtures.

To install the `aap` command and run tests, keep dependencies in a local venv:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/aap run --client fake --json
.venv/bin/python -m pytest
```

What it checks:

| Case | Required evidence |
| --- | --- |
| Root and nested instructions | Correct sentinel bytes, a tool call, and no unrelated edits; `AGENTS.md` for Codex, `CLAUDE.md` for Claude |
| Denied write | A tool attempt with denial evidence and unchanged files |
| Hook coverage | A successful Read call matched to a `PreToolUse` receipt by tool ID |
| Runtime drift | Exact pinned CLI version and executable hash before/after execution |
| Restore | Agent edits actually happened, expected original bytes return, and later human edits survive |

The restore cases cover an existing dirty file, a conflict in a shared file, a
human-created file, agent additions/deletions/renames, a human deletion, and a
human recreation of a deleted file. They exercise the pack's conservative restore
helper, not either client's native rewind feature.

Use `--fixture instructions-nested` to select a bundled fixture, or supply a path
to your own synthetic JSON fixture. `--dry-run` stages and validates cases without
launching a client; its result is `unknown`. `--json` prints the report as JSON.
Evidence is saved under `aap-results/<run-id>/`, ignored by Git. Each case records
client, client version, exact fixture hash, expected/observed values, and relative
evidence paths. Observed file contents are represented by SHA-256 hashes.

```sh
.venv/bin/aap run --client codex --dry-run --json
.venv/bin/aap run --client claude --fixture hook-tool-call --json
```

Neither command launches a client. Live execution is disabled by default and
reports `unknown` with a reason. Codex's hook case reports `unsupported` because
this milestone has no verified mapping for that hook contract.

Live adapter implementation is **UNVERIFIED**. Run it only inside an externally
isolated, disposable host with no personal configuration, credentials, keychain,
or mounted home directories. Temporary environment variables alone are not an OS
sandbox. The adapter needs an explicit JSON pin:

```json
{
  "executable": "/opt/agents/codex",
  "version": "1.2.3",
  "sha256": "<64 lowercase hex characters from the reviewed executable>"
}
```

The version above is illustrative. Record the intended executable's hash and
version independently before the update; do not generate a fresh passing pin
automatically from whatever happens to be installed after it.

```sh
.venv/bin/aap run --client codex --live --pin pin.json --json
```

For Claude, use `--client claude` and its own pin. The runner creates fresh
`HOME`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, XDG directories, and project directories.
It does not copy installed settings or credentials and does not inherit API keys.
Missing authentication, unavailable executables, changed CLI flags, startup
errors, timeouts, and unrecognized output produce `unknown`. Authentication
provisioning is not implemented in this milestone. No live vendor session was
used to validate these adapters.

Result meanings:

| Result | Meaning | Exit code |
| --- | --- | --- |
| `pass` | Every selected case has exercised, matching evidence | 0 |
| `fail` | A check found contradictory evidence | 1 |
| `unknown` | Evidence or coverage is incomplete | 2 |
| `unsupported` | All selected cases are explicitly outside adapter coverage | 3 |

A mixed pass/unsupported run is `unknown`; failures take precedence. Zero
exercised cases can never produce an overall pass. Fake results are always
labelled synthetic, even when their exit code is zero.

Known gaps: native rewind, arbitrary edits racing during restore, internal
daemon/child-process identity, MCP tools, desktop/remote sessions, compaction,
session handoff, shell syntax matrices, platform-specific managed settings, and
hostile clients. Hook receipts are coverage evidence, not tamper-proof proof.
Restoration requires writers to be stopped; conflicts are left untouched rather
than automatically merged. There is no installer, updater, scheduler, or deploy
step. See [the design and evidence](docs/DESIGN.md) for the exact boundaries.

All bundled code and fixtures were written for this project. No third-party
implementation or installed agent configuration is bundled. MIT licensed.
