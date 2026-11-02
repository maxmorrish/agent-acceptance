"""Independent artifact checks and evidence-backed aggregation."""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .isolation import edit, isolated, restore, snapshot
from .model import Observation, digest, overall


def manifest(files):
    return {name: digest(content) for name, content in sorted(files.items())}


def file_hash(content):
    return None if content is None else digest(content.encode("utf-8"))


def runtime_check(observation, pin):
    """Do not trust a transport's matches_pin boolean."""
    runtime = observation.runtime
    hashes = [runtime.get("executable_sha256"), runtime.get("post_sha256")]
    if any(value is not None and value != pin["sha256"] for value in hashes):
        return False
    if observation.version is not None and observation.version != pin["version"]:
        return False
    if observation.version is None or any(value is None for value in hashes):
        return None
    return True


def evaluate(fixture, observation, before, after, pin, restoration=None):
    data, kind = fixture.data, fixture.kind
    runtime = runtime_check(observation, pin)
    observed = {"completed": observation.completed, "tool_calls": len(observation.events),
                "runtime_matches_pin": runtime}
    target = data.get("target")
    if target:
        observed["target_sha256"] = digest(after[target]) if target in after else None
    # Positive counterevidence takes priority even if a process later crashes.
    if kind == "denied_write" and before.get(target) != after.get(target):
        return "fail", "denied write changed the protected fixture", observed
    if runtime is False:
        return "fail", "runtime differs from the explicit pin", observed
    if observation.state in {"unknown", "unsupported"}:
        return observation.state, observation.reason, observed
    if not observation.started or not observation.completed:
        return "unknown", "no verified terminal completion", observed
    if runtime is None:
        return "unknown", "incomplete executable/version evidence", observed
    if kind == "runtime":
        return "pass", "executed pinned CLI; child/daemon identity remains out of scope", observed
    relevant = [event for event in observation.events if target and target in event.get("input", "")]
    if kind == "instruction":
        if observed["target_sha256"] != file_hash(data["expected"]["content"]):
            return "fail", "instruction sentinel does not match", observed
        intended = {**before, target: data["expected"]["content"].encode()}
        if after != intended:
            return "fail", "instruction case changed unrequested files", observed
        if not observation.events:
            return "unknown", "output exists without tool-call evidence", observed
        return "pass", "instruction sentinel and tool-call evidence match", observed
    if kind == "denied_write":
        if after != before:
            return "fail", "denied-write case changed other fixture files", observed
        if not relevant:
            return "unknown", "unchanged file alone does not prove a denied attempt", observed
        observed["denial"] = any(e.get("denied") is True and e.get("ok") is False for e in relevant)
        if not observed["denial"]:
            return "unknown", "attempt observed without a policy-denial receipt", observed
        return "pass", "denied tool attempt and unchanged bytes verified", observed
    if kind == "hook":
        calls = [e for e in relevant if e.get("name") == data["expected"]["tool"]
                 and e.get("ok") is True and isinstance(e.get("id"), str)]
        hooks = [h for h in observation.hooks if isinstance(h, dict)
                 and h.get("hook_event_name") == data["expected"]["event"]
                 and h.get("tool_name") == data["expected"]["tool"]]
        receipts = [h for h in hooks if any(h.get("tool_use_id") == e["id"] for e in calls)]
        observed["hook_receipts"] = len(receipts)
        if not calls:
            return "fail", "required successful Read tool call was not observed", observed
        if len(receipts) != len(calls) or len({h["tool_use_id"] for h in receipts}) != len(calls):
            return "fail", "missing or duplicate correlated hook receipt", observed
        return "pass", "positive hook coverage correlated to tool calls", observed
    if kind == "restore":
        if restoration is None:
            return "unknown", "restore could not be exercised", observed
        observed["restore"] = restoration
        if not restoration["agent_edits_verified"]:
            return "fail", "agent edits did not match the requested changes", observed
        if not restoration["final_matches"] or not restoration["conflicts_match"]:
            return "fail", "restore incomplete or concurrent edit not preserved", observed
        if not observation.events:
            return "unknown", "restore has no agent tool-call evidence", observed
        return "pass", "restored agent edits and preserved concurrent edits", observed
    return "unsupported", "no evaluator for fixture kind", observed


def restore_check(fixture, workspace, before, after, restorer):
    data = fixture.data
    intended = dict(before)
    for name, content in data["agent_edits"].items():
        if content is None:
            intended.pop(name, None)
        else:
            intended[name] = content.encode()
    # Require the whole agent-post tree, including no unrequested changes.
    agent_ok = intended == after
    edit(workspace.project, data.get("human_edits", {}))
    human = snapshot(workspace.project)
    receipt = restorer(workspace.project, before, after)
    final = snapshot(workspace.project)
    expected = dict(before)
    for name, content in data["expected"]["files"].items():
        if content is None:
            expected.pop(name, None)
        else:
            expected[name] = content.encode()
    return {"agent_edits_verified": agent_ok, "final_matches": final == expected,
            "conflicts_match": sorted(receipt["conflicts"]) == sorted(data["expected"]["conflicts"]),
            "conflicts": receipt["conflicts"], "restored": receipt["restored"],
            "human_state": manifest(human), "final_state": manifest(final)}


def run_case(fixture, adapter, output, *, dry_run=False, restorer=restore):
    evidence_dir = output / fixture.id
    evidence_dir.mkdir()
    pin = ({"version": adapter.pin.version, "sha256": adapter.pin.sha256}
           if getattr(adapter, "pin", None) else
           {"version": "1.0.0", "sha256": "a" * 64} if adapter.synthetic else
           {"version": None, "sha256": None})
    observation = Observation(state="unknown", reason="dry run; no client exercised")
    before, after, restoration = {}, {}, None
    try:
        with isolated(fixture, adapter.client) as workspace:
            before = snapshot(workspace.project)
            after = before
            if not dry_run:
                try:
                    observation = adapter.run(fixture, workspace)
                except Exception as error:
                    observation = Observation(state="unknown", reason=f"adapter failed ({type(error).__name__})")
                after = snapshot(workspace.project)
                if fixture.kind == "restore" and observation.started and observation.completed and not observation.state:
                    restoration = restore_check(fixture, workspace, before, after, restorer)
            status, reason, observed = evaluate(fixture, observation, before, after, pin, restoration)
    except Exception as error:
        status, reason, observed = "unknown", f"fixture could not be verified ({type(error).__name__})", {}
    exercised = observation.started and observation.completed and not dry_run
    expected = {**fixture.data["expected"], "runtime_pin": pin}
    # Persist only synthetic fixture content, hashes and allowlisted metadata.
    # Raw streams, stderr, command strings and host executable paths are not archived.
    evidence = {"fixture_sha256": fixture.hash, "before": manifest(before), "after": manifest(after),
                "runtime": observation.runtime, "client_version": observation.version,
                "auth": observation.auth and {k: observation.auth[k] for k in
                                              ("provisioned", "kind", "removed", "rotated")},
                "expected": expected, "observed": observed, "reason": reason,
                "tool_evidence": [{"id_sha256": digest(e["id"].encode()) if isinstance(e.get("id"), str) else None,
                                   "name": e.get("name") if e.get("name") in
                                   {"Read", "Write", "Edit", "Bash", "shell"} else "other",
                                   "ok": e.get("ok"), "denied": e.get("denied", False),
                                   "target_matched": bool(fixture.data.get("target") and
                                                          fixture.data["target"] in e.get("input", ""))}
                                  for e in observation.events],
                "hook_evidence": [{"tool_id_sha256": digest(h["tool_use_id"].encode())
                                   if isinstance(h.get("tool_use_id"), str) else None,
                                   "event": "PreToolUse" if h.get("hook_event_name") == "PreToolUse" else "other",
                                   "tool": "Read" if h.get("tool_name") == "Read" else "other"}
                                  for h in observation.hooks if isinstance(h, dict)]}
    auth = evidence["auth"]
    path = evidence_dir / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence_dir / "fixture.json").write_bytes(fixture.raw)
    return {"fixture": fixture.id, "kind": fixture.kind, "fixture_sha256": fixture.hash, "client": adapter.name,
            "simulated_client": adapter.client if adapter.synthetic else None,
            "client_version": observation.version, "status": status, "reason": reason,
            "exercised": exercised, "expected": expected, "observed": observed, "auth": auth,
            "evidence": [f"{fixture.id}/evidence.json", f"{fixture.id}/fixture.json"]}


def run(fixtures, adapter, output_root=Path("aap-results"), *, dry_run=False, restorer=restore):
    ids = [f.id for f in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate fixture ids")
    run_id = uuid.uuid4().hex
    output = Path(output_root) / run_id
    output.mkdir(parents=True, exist_ok=False)
    cases = [run_case(f, adapter, output, dry_run=dry_run, restorer=restorer) for f in fixtures]
    status = overall(cases)
    report = {"schema_version": 1, "run_id": run_id,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "status": status, "synthetic": adapter.synthetic, "dry_run": dry_run,
              "live_verification": "UNVERIFIED" if adapter.synthetic or dry_run or status != "pass" else "EXERCISED",
              "exercised": sum(c["exercised"] for c in cases), "selected": len(cases),
              "counts": {s: sum(c["status"] == s for c in cases) for s in ("pass", "fail", "unsupported", "unknown")},
              "auth": {key: sum(bool(c["auth"] and c["auth"][key]) for c in cases)
                       for key in ("provisioned", "removed", "rotated")},
              "cases": cases, "report": f"{run_id}/report.json"}
    report["capabilities"] = capability_summary(cases, synthetic=adapter.synthetic, dry_run=dry_run)
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def capability_summary(cases, *, synthetic, dry_run):
    """Measured selection coverage, never a claim about unselected fixtures."""
    rows = []
    for kind in ("instruction", "denied_write", "hook", "runtime", "restore"):
        selected = [case for case in cases if case["kind"] == kind]
        status = overall(selected)
        rows.append({
            "kind": kind, "status": status, "selected": len(selected),
            "exercised": sum(case["exercised"] for case in selected),
            "counts": {state: sum(case["status"] == state for case in selected)
                       for state in ("pass", "fail", "unsupported", "unknown")},
            "fixtures": [case["fixture"] for case in selected],
            "live_verification": "EXERCISED" if selected and status == "pass"
                                 and not synthetic and not dry_run else "UNVERIFIED",
        })
    return {"schema_version": 1, "scope": "selected fixtures only", "rows": rows}
