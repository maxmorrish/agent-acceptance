# Live results

Each row is one real run of the installed client against one bundled fixture,
using the per-case auth provision in `aap/auth.py`. Results are recorded as the
runner reported them.

## 2026-10-23, macOS arm64 developer host

The host was a normal workstation, not an externally isolated machine. Each case
ran with a fresh `HOME`, client config directory and project tree, and only the
login file was provisioned, but the client still ran as the workstation user.
Treat these as transport checks, not containment checks.

| Client | Version | Executable SHA-256 | Fixture | Result | Exercised | Auth |
| --- | --- | --- | --- | --- | --- | --- |
| Codex CLI | 0.163.0 | `8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31` | `instructions-root` | pass | 1/1 | provisioned, removed, not rotated |
| Claude Code | 2.1.309 | `5eadc0009cdcdbc0d06e58110d7b2baf88ced6088b1c33a60a21217fe410619c` | `instructions-root` | unknown | 0/1 | none provisioned |

Codex: the client read the root `AGENTS.md`, wrote exactly `ROOT_RULE\n` to the
target, changed no other file, and reported three command executions and a
terminal `turn.completed`. The pinned executable hash matched before and after.
The source credential file was unchanged after the run.

Claude Code: no file or token credential was available on this host (the login
is held in the system keychain, which the runner does not read). With no
credential, the isolated client printed `Not logged in` inside a `result` record
whose `subtype` is `success` but `is_error` is true, and exited 1. The runner
reported `unknown`. This shows the isolated run did not pick up the workstation
login. A separate probe with a synthetic, invalid `.credentials.json` in the
case's `CLAUDE_CONFIG_DIR` got `401 Invalid bearer token`, so Claude Code on
macOS does read the file route the provision uses. A real Claude Code result
needs a test token (`claude setup-token`) passed with `--auth-token-file`.

Not covered yet: the other eight fixtures per client, Linux, and an isolated host.
