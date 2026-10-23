# Milestone 1 design

## Problem and evidence

An agent update can preserve the command name and return code while changing
which instructions apply, which hooks fire, which runtime executes work, or what
a restore actually undoes. Acceptance should test these behaviors using invented
projects and inspectable evidence. The runner accepts no installer result as a
behavioral result.

The motivating research was captured on 2026-10-19. The links below are the public
sources behind that research's first ranked candidate and gaps G1, G2, G5, G8.
They are reported problems, not regressions reproduced by this milestone. Source
state was not refreshed during implementation, which had a no-network constraint
apart from local development dependency installation.

| Gap | Reported evidence | Milestone response |
| --- | --- | --- |
| G1: instruction loading | [Claude Code issue 90450](https://github.com/anthropics/claude-code/issues/90450) describes nested-rule differences across access methods | Two instruction sentinel fixtures, selected by client; nested overrides must affect bytes actually written |
| G2: restore ownership | [Claude Code issue 87575](https://github.com/anthropics/claude-code/issues/87575) reports rewind leaving shell changes behind | Four restore fixtures check whole-tree results and refuse conflicting later edits |
| G5: runtime drift | [Codex issue 48195](https://github.com/openai/codex/issues/48195) discusses separate updater/runtime copies | Require an explicit CLI pin; measure the resolved executable bytes and version on the exact invocation path |
| G8: hook delivery | [Claude Code issue 29716](https://github.com/anthropics/claude-code/issues/29716) discusses hooks across settings/session surfaces | Require positive Read coverage and one correlated PreToolUse receipt per call |

Related public approaches informed the scope, without copying implementation:

- [UHP conformance](https://github.com/HarnessRouter/harnessrouter/tree/main/protocol/conformance): explicit adapter contracts and case results.
- [Worktree reproduction](https://github.com/clayallsopp/worktree-desktop-repro): small synthetic reproductions for hook/session behavior.
- [Bash bridge](https://github.com/JasonGDG/claude-bash-bridge): shell-mediated edits and instruction delivery need distinct checks. A bridge's presence is not proof that instructions constrained an operation.
- [Time Machine](https://github.com/YeonwooSung/my-claude-code-mods/tree/main/time-machine): snapshots already exist; the useful addition here is a restore acceptance corpus, not a claim to invent snapshots.
- [Codex execpolicy documentation](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/execpolicy/README.md): offline command matching is a different contract from runtime authorization.
- [Hook harness](https://github.com/echo-lumen/claude-hooks-test) and [hook documentation](https://code.claude.com/docs/en/hooks): a hook runner must preserve event and exit semantics.

Earlier bounded evaluations supplied three design lessons: a validator can have
warnings despite a positive field; a policy command can exit zero while matching
nothing; and a synthetic hook harness can pass only part of its contract while
its reproduction script exits zero. This implementation does not import those
evaluations' code or configurations. It writes independent synthetic fixtures
and verifies structured results and filesystem artifacts.

## Result vocabulary and coverage

Every case has exactly one status:

- `pass`: positive evidence satisfies every required assertion, with completion
  and runtime evidence. Adapters cannot emit this verdict themselves.
- `fail`: a required behavior was contradicted. Examples include a changed denied
  target, wrong instruction sentinel, missing hook after a successful Read, a
  stale executable, or an incomplete restore.
- `unsupported`: an explicitly unimplemented or inapplicable adapter capability.
  This is never inferred from an arbitrary process error.
- `unknown`: missing executable/authentication, disabled live execution, dry run,
  malformed data, no terminal completion, timeout, or otherwise insufficient proof.

`exercised` means the client operation started and reached recognized terminal
completion. A known pin mismatch may fail before a case is exercised: it is
counterevidence, not a passing runtime test. Every case must be exercised for an
overall pass. Aggregate precedence is fail, unknown/incomplete coverage, then
pass; an all-unsupported selection is unsupported. An empty selection is unknown.
A mix of pass and unsupported is unknown, preserving the coverage gap.

The report also identifies `synthetic`, `dry_run`, selected/exercised counts,
per-state counts, and `live_verification`. Fake acceptance always leaves live
verification UNVERIFIED. An injected subprocess unit test is still synthetic
evidence; it does not validate a released vendor runtime.

## Fixture format

Fixtures are UTF-8 JSON, at most 64 KiB. The SHA-256 covers the exact file bytes,
including whitespace. Duplicate keys, unknown top-level fields, invalid types,
unsupported schema versions, symlinks, absolute/escaping paths, and hidden target
paths are rejected. The implementation in `aap/model.py` is the executable schema.
There is no YAML dependency or arbitrary executable field.

Required fields:

| Field | Type / meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `id` | Unique lowercase ASCII identifier, digits/hyphens allowed |
| `kind` | `instruction`, `denied_write`, `hook`, `runtime`, or `restore` |
| `clients` | Nonempty list containing `codex` and/or `claude` |
| `files` | Map of normalized project-relative paths to initial text |
| `expected` | Kind-specific assertions below |

Optional fields are `cwd` (instruction cases only, project-relative, default `.`),
`target` (required for instruction/denied_write/hook), and restore-only
`agent_edits` and `human_edits`. Edit maps contain
text or `null` for deletion. `$INSTRUCTIONS` as a setup path component expands to
`AGENTS.md` or `CLAUDE.md`; collisions after expansion are rejected. All content
must be synthetic. Paths use a restricted portable character set.

Expectation shapes:

```json
{
  "instruction": {"content": "NESTED_RULE\n"},
  "denied_write": {"content": "KEEP\n", "denial": true},
  "hook": {"event": "PreToolUse", "tool": "Read"},
  "runtime": {"matches_pin": true},
  "restore": {"files": {"draft.txt": "before\n", "added.txt": null}, "conflicts": []}
}
```

This block illustrates the five shapes, not a fixture itself. Full runnable
examples live in `aap/fixtures/`. For restore, `expected.files` overlays the
original tree. Every final file is checked, including unlisted additions.
For instructions, the prompt asks the agent to discover the sentinel from the
instruction files; it does not reveal the expected value.

The explicit runtime pin is separate from the behavioral fixture so the same
fixture can compare multiple client versions. Its fields are `executable`
(absolute path), `version` (exact semantic version), and `sha256` (64 lowercase
hex characters). Reports replace the executable path with a generic label and
retain its hashes. Automatic post-update repinning would defeat the drift check.

## Adapter interface

An adapter exposes `name`, `client`, `synthetic`, and
`run(fixture, workspace) -> Observation`. A live adapter optionally holds a Pin.
The workspace has isolated paths and an allowlisted child environment.

An Observation contains:

- `state`: absent, `unknown`, or `unsupported`; plus a bounded generic reason.
- `started`, `completed`, and the measured `version`.
- `runtime`: executable SHA-256 before/after, expected identity, generic path label.
- `events`: normalized tool ID, name, input, result success, denial evidence.
- `hooks`: synthetic hook event/name/tool-ID receipts.

The runner independently compares identity fields against the pin; a transport's
`matches_pin` boolean is not trusted. It snapshots artifacts, evaluates the case,
and writes the verdict. Tool input remains in memory for matching; archived
evidence uses booleans, allowlisted names and hashed IDs rather than raw commands.

Codex uses `exec --json`, an isolated CODEX_HOME, noninteractive approval policy,
ephemeral sessions, and the requested sandbox. The denied-write fixture uses
read-only mode and requires both an attempted tool call with a denial result and
unchanged bytes. `execpolicy check` alone would not satisfy this fixture. Codex's
PreToolUse coverage is explicitly unsupported in milestone 1.

Claude uses headless print mode, verbose stream JSON, a disposable config
directory, explicitly generated settings, disabled settings-source discovery,
empty strict MCP configuration, and no session persistence. Normal synthetic
edits have explicit tool allowances. The denied-write case explicitly denies
Bash/Write/Edit. The hook case registers a small original Python PreToolUse hook
which records only event type, tool name, and tool-use ID. The runner correlates
that receipt to successful Read telemetry. Duplicate receipts do not pass.

The fake adapter interprets the synthetic instruction sentinel, performs file
edits, and emits deterministic event envelopes. Tests inject missing hooks, wrong
instructions, stale runtimes, write bypasses, absent tool/completion events, and
transport errors. A separately injected bad restorer tests incomplete restore
and human-edit clobbering. Fake tests validate the runner's verdict logic, not the
clients' behavior.

## Isolation and process lifecycle

Each case creates fresh HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, XDG config/cache/data,
temporary, control, and project directories beneath a new system-temp directory.
The project is outside the source checkout. Only fixture text is staged. No real
agent settings, auth files, hooks, MCP definitions, sessions, or project files are
read or copied. The environment is constructed from scratch: no inherited API
keys, model settings, proxy variables, shell startup files, or runtime preload
variables. PATH is limited to system binary directories. Codex's credential store
is configured as file-only in the empty temporary home.

The runner invokes the explicit resolved binary, never a shell command assembled
from user paths. It limits process time and captured output, kills the process
group on completion/timeout, and deletes its disposable tree after verification.
Raw stdout/stderr are temporary and are not archived. Snapshot collection rejects
symlinks, special files and oversized trees. Evidence survives in a unique run
directory: `report.json`, exact `fixture.json` copies, and per-case `evidence.json`
with original/post-edit hashes, runtime measurements, normalized tool/hook
evidence, assertions and restore manifests. Evidence paths are relative to the
run directory; personal host paths are not part of the report format.

These measures isolate configuration and test artifacts. They do not sandbox an
untrusted executable, defeat a managed system policy, disable all vendor
keychain access, guarantee process containment against daemonization, or prevent
discovery outside the tree by a malicious client. Therefore live execution is
off by default; `--live` must only be used in an externally isolated disposable
host without personal mounts, credentials or config. Missing auth or an
incompatible CLI interface yields unknown, never an automatic fallback to the real
home directory.

Milestone 2 adds `aap/auth.py`: an explicit, per-case, auth-only provision. The
operator names one credential file by its client-specific name; only login fields
are kept; the result is written 0600 into the case's own config directory after
the minimal config and before the version probe, and removed in a `finally`
block. Near-refresh credentials are refused because a refresh inside a copy can
rotate the source login's refresh token. Receipts carry four booleans and no
values. See [LIVE-RESULTS.md](LIVE-RESULTS.md) for the first live runs.

## Restore contract

1. Capture the original whole tree, including existing dirty bytes.
2. Let the adapter perform requested edits. Verify the complete agent-post tree.
3. Apply synthetic human edits after that capture, representing another actor's
   work during the overall operation. Capture this state separately.
4. At a quiescent point, compare original, agent-post, and current bytes. For a file
   changed by the agent, restore only when current still equals agent-post.
   Otherwise report a conflict and preserve current bytes or absence.
5. Compare the complete final tree and the exact conflict set with the fixture's
   expectations. A restore receipt or success return alone is insufficient.

Renames are represented as deletion plus addition. Empty directories, file modes,
ownership, hard links, xattrs and binary fixture content are outside this version.
This helper runs only on disposable test trees. It requires writers to stop
before restore; it is not an atomic filesystem transaction and does not protect
against a writer racing between comparison and replacement. No native Codex or
Claude rewind acceptance is claimed.

## Explicit exclusions and next milestone

No install/update automation, scheduling, credential import, subscription proxy,
publishing, source-control mutation, production restore, or automatic approval.
No desktop/remote session, session-owner handoff, compaction/resume, MCP event,
arbitrary shell syntax, cross-platform managed-config, or model-quality coverage.
No claim that an instruction sentinel proves internal planning provenance, that
hook receipts resist deliberate forgery, or that CLI identity proves internal
daemon/child runtime identity. Reports are local evidence, not signed attestations.

Next milestone: run all nine fixtures per client in a disposable
credential-free host with the auth provision above, capture
redacted synthetic transport fixtures from real runs, and publish a versioned
capability matrix with measured unsupported/unknown boundaries. Then add native
snapshot adapters and a richer shell-mediated instruction/policy corpus. The
acceptance pack should integrate with an existing updater; it is not a competing
updater or installer.
