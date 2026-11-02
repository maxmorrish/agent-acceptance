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

Reports also include `capabilities` (schema version 1): five rows grouped by
fixture kind, with selected/exercised counts, verdict counts, fixture IDs, and
live-verification labels. These summarize only the selected fixtures; an
unselected kind is `unknown`, never implicitly supported. See
[offline M2 capability coverage](docs/CAPABILITIES.md). Injected process
executors, like the fake adapter, always produce synthetic evidence.

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
It does not copy installed settings and does not inherit API keys.
Missing authentication, unavailable executables, changed CLI flags, startup
errors, timeouts, and unrecognized output produce `unknown`.

### Test authentication

A live client needs a login. Name one credential file and the runner copies only
that, per case:

```sh
.venv/bin/aap run --client codex --live --pin codex-pin.json --auth-file /path/to/auth.json
.venv/bin/aap run --client claude --live --pin claude-pin.json --auth-token-file /path/to/token
```

- Codex accepts a file named `auth.json`. Claude Code accepts a file named
  `.credentials.json`, or `--auth-token-file` with one OAuth token (for example
  from `claude setup-token`), which is set only in that case's process
  environment as `CLAUDE_CODE_OAUTH_TOKEN`.
- Any other file name is refused, so config, settings, hooks, skills and memory
  files cannot be passed by mistake. Links, files not owned by you, files readable
  by group or others, and files over 64 KiB are refused.
- Only the login fields are kept (`auth_mode`, `OPENAI_API_KEY`, `tokens`,
  `last_refresh` for Codex; `claudeAiOauth` for Claude). They are written mode
  0600 into the case's fresh `CODEX_HOME` or `CLAUDE_CONFIG_DIR` after the minimal
  config and before the first launch, and removed when the case ends, including
  after errors.
- Credentials close to a refresh are refused (Codex: last refresh over 7 days
  ago; either client: access token expiring within 30 minutes). A refresh inside a
  throwaway copy can rotate the refresh token and sign out the original login. If
  a client rewrites the credential anyway, the report counts it under
  `auth.rotated` and the CLI prints a warning.
- Reports and evidence record only whether a credential was provisioned, removed
  or rotated. Values, paths and hashes of credentials are never written.

This is a convenience for a disposable host, not containment: a live client still
runs as your user. Measured live results are in [docs/LIVE-RESULTS.md](docs/LIVE-RESULTS.md).

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
