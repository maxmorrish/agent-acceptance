"""Command-line entry point; non-passing runs always exit nonzero."""
import argparse
import json
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter, FakeAdapter, Pin
from .model import Fixture
from .runner import run

FIXTURES = Path(__file__).parent / "fixtures"


def select(value=None):
    if value is None:
        paths = sorted(FIXTURES.glob("*.json"))
    else:
        bundled = FIXTURES / (value + ".json")
        paths = [bundled if "/" not in value and bundled.is_file() else Path(value)]
    return [Fixture.load(path) for path in paths]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aap")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run", help="run synthetic behavioral fixtures")
    command.add_argument("--client", choices=("codex", "claude", "fake"), default="codex")
    command.add_argument("--fixture", help="bundled ID or JSON file")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--json", action="store_true", dest="json_output")
    command.add_argument("--live", action="store_true", help="launch client; requires an externally isolated host")
    command.add_argument("--pin", type=Path, help="explicit executable/version/SHA-256 JSON pin")
    command.add_argument("--output", type=Path, default=Path("aap-results"))
    args = parser.parse_args(argv)
    try:
        fixtures = select(args.fixture)
        pin = Pin.load(args.pin) if args.pin else None
        adapter = (FakeAdapter() if args.client == "fake" else
                   {"codex": CodexAdapter, "claude": ClaudeAdapter}[args.client](pin=pin, enabled=args.live))
        report = run(fixtures, adapter, args.output, dry_run=args.dry_run)
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Stable output without echoing personal file paths or invalid fixture contents.
        report = {"schema_version": 1, "status": "unknown", "exercised": 0,
                  "live_verification": "UNVERIFIED", "reason": f"invalid or unavailable input ({type(error).__name__})"}
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        label = "SYNTHETIC" if report.get("synthetic") else "LIVE ADAPTER"
        print(f"{label}: {report['status']} | {report['exercised']} exercised | live {report['live_verification']}")
        for case in report.get("cases", []):
            print(f"{case['status']:11} {case['fixture']}: {case['reason']}")
        if "report" in report:
            print(f"Evidence: <output>/{report['report']}")
        elif "reason" in report:
            print(report["reason"])
    return {"pass": 0, "fail": 1, "unknown": 2, "unsupported": 3}[report["status"]]
