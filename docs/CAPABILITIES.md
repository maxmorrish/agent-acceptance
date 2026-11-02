# Offline M2 transport and capability coverage

Verified 2026-10-29 against this source tree. This is an offline milestone;
released client behavior and containment remain unverified.

## Reproduce

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m aap run --client fake --json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m aap run --client codex --json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m aap run --client claude --json
```

The latter two commands intentionally omit `--live` and exit 2. No client or
credential is accessed. Fake exits 0. All three leave live verification
UNVERIFIED. The test suite has 110 passing tests, including 12 malformed or
ambiguous stream cases exercised through the full adapter/runner path.

| Kind | Bundled cases | Fake | Codex disabled | Claude disabled |
| --- | ---: | --- | --- | --- |
| instruction | 2 | pass | unknown | unknown |
| denied_write | 1 | pass | unknown | unknown |
| hook | 1 | pass | unsupported | unknown |
| runtime | 1 | pass | unknown | unknown |
| restore | 4 | pass | unknown | unknown |

Codex hook unsupported is an explicit adapter limitation, not a statement about
all Codex releases. Restore checks the pack's helper, not native rewind.

## Report contract

The existing report schema receives additive `kind` fields on cases and a
`capabilities` object with its own `schema_version: 1`. Each of five rows has
`kind`, `status`, `selected`, `exercised`, `counts`, `fixtures`, and
`live_verification`. Aggregation uses the same fail/unknown/unsupported/pass
rules as the overall report. Zero selected cases are unknown. The scope is
explicitly `selected fixtures only`; passing one instruction fixture does not
claim that every instruction behavior works. Version and executable identity
remain in the associated case evidence and runtime pin.

Injected executors now set `synthetic: true` even when using a live adapter class.
They cannot produce an EXERCISED live-verification label. Custom transport
wrappers are conservatively treated as synthetic; the built-in executor is the
only live execution path in this milestone.

## Transport regressions

Claude tool results must reference a known nonempty call ID, arrive only once,
and have a boolean error flag when present. A terminal success with unresolved
calls, or subsequent assistant/user messages, is rejected. Interleaved distinct
calls can finish in either order and remain correlated by ID. Codex completed
commands require nonempty unique IDs. Malformed streams become unknown through
the adapter; unchanged bytes or exit zero cannot turn them into a pass.

The envelopes in tests/test_transport_regressions.py are invented synthetic
examples, not captured vendor traces. Existing missing-completion, timeout,
process-exit, runtime-replacement, and login-error tests remain in place. This
is not a complete event-protocol validator: unfamiliar auxiliary records remain
ignored and additional CLI versions require isolated live evidence.

## Research and remaining work

Public sources checked 2026-10-29:

- [Codex event types](https://github.com/openai/codex/blob/main/sdk/typescript/src/events.ts):
  typed turn and item lifecycle envelopes motivate explicit terminal evidence.
- [Claude streaming](https://code.claude.com/docs/en/agent-sdk/streaming-output):
  message types and nested blocks require type-aware parsing.
- [ACP initialization](https://agentclientprotocol.com/protocol/v1/initialization):
  advertised version/capabilities are separate from this pack's measured outcomes.

GitHub searches for agent CLI conformance and recent agent conformance projects
found no drop-in replacement for this existing artifact-verifying runner. No
third-party code or new dependency was added. Protocol sources describe interfaces;
they do not establish installed-client acceptance.

Full M2 still needs all nine fixtures per client on an externally isolated
throwaway host with reviewed test authentication, redacted observed transport
fixtures, and a version/hash-specific live matrix. Historical transport checks
remain in LIVE-RESULTS.md; this change does not upgrade their evidentiary status.
