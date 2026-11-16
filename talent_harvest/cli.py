from __future__ import annotations

from argparse import ArgumentParser, Namespace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys

from .contracts import HarvestError, canonical
from .audit import Auditor
from .inventory import Inventory
from .lineage import Lineage
from .ops import Operations
from .analytics import Analytics
from .export import Delivery, verify_snapshot
from .ledger import Ledger
from .pipeline import Harvester


def parser() -> ArgumentParser:
    command = ArgumentParser(prog="talent_harvest", description="Local talent-signal mirror harvester")
    command.add_argument("--db", default="harvest.sqlite3", help="SQLite state path")
    actions = command.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register", help="register a source declaration")
    register.add_argument("declaration")
    register_rule = actions.add_parser("register-rule", help="register an official-source priority rule")
    register_rule.add_argument("declaration")
    actions.add_parser("rules", help="show priority rule registry and rejections")
    ingest = actions.add_parser("ingest", help="ingest or resume a manifest")
    ingest.add_argument("manifest")
    ingest.add_argument("--page", type=int)
    ingest.add_argument("--stop-on-error", action="store_true")
    status = actions.add_parser("status", help="inspect a batch")
    status.add_argument("source")
    status.add_argument("batch")
    withdraw = actions.add_parser("withdraw", help="withdraw a source")
    withdraw.add_argument("source")
    withdraw.add_argument("--reason", required=True)
    current = actions.add_parser("current", help="show current accepted records")
    current.add_argument("--quarantine", action="store_true")
    history = actions.add_parser("history", help="show raw history")
    history.add_argument("--source")
    history.add_argument("--identity")
    trace = actions.add_parser("trace", help="show record and event lineage")
    trace.add_argument("identity")
    actions.add_parser("summary", help="aggregate accepted quantities")
    actions.add_parser("audit", help="verify persisted state invariants")
    actions.add_parser("operations", help="summarize batch and page progress")
    report = actions.add_parser("source-report", help="inspect source intake and quality")
    report.add_argument("source")
    actions.add_parser("sources", help="summarize all sources")
    quality = actions.add_parser("quality", help="summarize quarantine reasons")
    quality.add_argument("--source")
    recovery = actions.add_parser("recovery", help="plan page retries")
    recovery.add_argument("--source")
    compare = actions.add_parser("compare", help="compare two complete source batches")
    compare.add_argument("source")
    compare.add_argument("left")
    compare.add_argument("right")
    lineage_record = actions.add_parser("record", help="inspect a source record's revisions")
    lineage_record.add_argument("source")
    lineage_record.add_argument("record_id")
    lineage_batch = actions.add_parser("batch-evidence", help="inspect batch provenance")
    lineage_batch.add_argument("source")
    lineage_batch.add_argument("batch")
    timeline = actions.add_parser("timeline", help="inspect source timeline")
    timeline.add_argument("source")
    preview = actions.add_parser("preview", help="validate local mirror without mutating state")
    preview.add_argument("source")
    preview.add_argument("manifest")
    replay = actions.add_parser("replay-check", help="compare stored observations with mirror")
    replay.add_argument("manifest")
    event_window = actions.add_parser("event-window", help="page events by stable cursor")
    event_window.add_argument("--start", type=int, default=0)
    event_window.add_argument("--limit", type=int, default=100)
    record_window = actions.add_parser("record-window", help="page observations by stable cursor")
    record_window.add_argument("--start", type=int, default=0)
    record_window.add_argument("--limit", type=int, default=100)
    actions.add_parser("consistency", help="inspect cross-source identity claims")
    actions.add_parser("database-info", help="inspect SQLite database status")
    actions.add_parser("checkpoint", help="capture stable intake cursors")
    actions.add_parser("distribution", help="aggregate current supply by dimensions")
    actions.add_parser("coverage", help="show source contribution")
    age = actions.add_parser("age", help="group current records by publication age")
    age.add_argument("reference_date")
    actions.add_parser("trend", help="summarize capture months")
    actions.add_parser("conflicts", help="summarize active disputes")
    actions.add_parser("health", help="show delivery readiness")
    events = actions.add_parser("events", help="show event history")
    events.add_argument("--source")
    snapshot = actions.add_parser("snapshot", help="write delivery snapshot")
    snapshot.add_argument("output")
    snapshot.add_argument("--quarantine", action="store_true")
    verify = actions.add_parser("verify", help="verify delivery snapshot")
    verify.add_argument("snapshot")
    verify.add_argument("sha256")
    demo = actions.add_parser("demo", help="run an isolated fixture demonstration")
    demo.add_argument("--workspace", default="fixtures")
    return command


def load_declaration(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarvestError("source_read", str(exc)) from None
    return data


def execute(args: Namespace) -> dict:
    if args.action == "demo":
        return demo(Path(args.workspace))
    with Ledger(args.db) as ledger:
        harvester = Harvester(ledger)
        delivery = Delivery(ledger)
        if args.action == "register":
            return harvester.register_source(load_declaration(args.declaration))
        if args.action == "register-rule":
            return harvester.register_rule(load_declaration(args.declaration))
        if args.action == "rules":
            return {"rules": ledger.rules(), "rejections": ledger.rule_rejections()}
        if args.action == "ingest":
            return harvester.ingest(args.manifest, stop_on_error=args.stop_on_error, only_page=args.page)
        if args.action == "status":
            return harvester.inspect(args.source, args.batch)
        if args.action == "withdraw":
            return harvester.withdraw(args.source, args.reason)
        if args.action == "current":
            return delivery.current(include_quarantine=args.quarantine)
        if args.action == "history":
            return delivery.history(source=args.source, identity=args.identity)
        if args.action == "trace":
            return delivery.trace(args.identity)
        if args.action == "summary":
            return delivery.summary()
        if args.action == "audit":
            return Auditor(ledger).verify_state()
        if args.action == "operations":
            return Auditor(ledger).operational()
        if args.action == "source-report":
            return Inventory(ledger).source_report(args.source)
        if args.action == "sources":
            return Inventory(ledger).records_by_source()
        if args.action == "quality":
            return Inventory(ledger).quality_report(source=args.source)
        if args.action == "recovery":
            return Inventory(ledger).recovery_plan(source=args.source)
        if args.action == "compare":
            return Inventory(ledger).batch_comparison(args.source, args.left, args.right)
        if args.action == "record":
            return Lineage(ledger).record(args.source, args.record_id)
        if args.action == "batch-evidence":
            return Lineage(ledger).batch(args.source, args.batch)
        if args.action == "timeline":
            return Lineage(ledger).source_timeline(args.source)
        if args.action == "preview":
            return Operations(ledger).preview(args.source, args.manifest)
        if args.action == "replay-check":
            return Operations(ledger).replay_check(args.manifest)
        if args.action == "event-window":
            return Operations(ledger).event_window(args.start, args.limit)
        if args.action == "record-window":
            return Operations(ledger).record_window(args.start, args.limit)
        if args.action == "consistency":
            return Operations(ledger).consistency_report()
        if args.action == "database-info":
            return Operations(ledger).database_info()
        if args.action == "checkpoint":
            return Operations(ledger).checkpoint()
        if args.action == "distribution":
            return Analytics(ledger).distribution()
        if args.action == "coverage":
            return Analytics(ledger).coverage()
        if args.action == "age":
            return Analytics(ledger).age_profile(args.reference_date)
        if args.action == "trend":
            return Analytics(ledger).trend()
        if args.action == "conflicts":
            return Analytics(ledger).conflict_report()
        if args.action == "health":
            return Analytics(ledger).delivery_health()
        if args.action == "events":
            return {"events": ledger.events(source=args.source)}
        if args.action == "snapshot":
            return delivery.write_snapshot(args.output, include_quarantine=args.quarantine)
        if args.action == "verify":
            return verify_snapshot(args.snapshot, args.sha256)
    raise HarvestError("action", "unsupported command")


def demo(workspace: Path) -> dict:
    declaration = load_declaration(str(workspace / "source.json"))
    manifest = workspace / "manifest.json"
    with TemporaryDirectory(prefix="talent-harvest-") as directory:
        with Ledger(Path(directory) / "demo.sqlite3") as ledger:
            harvester = Harvester(ledger)
            harvester.register_source(declaration)
            result = harvester.ingest(manifest)
            current = Delivery(ledger).current()
            history = Delivery(ledger).history()
            return {"ingest": result, "counts": current["counts"],
                    "history_count": len(history["observations"]),
                    "accepted": current["accepted"]}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (HarvestError, OSError) as exc:
        print(canonical({"error": getattr(exc, "code", "io_error"), "detail": str(exc)}), file=sys.stderr)
        return 2
    print(canonical(result))
    if args.action == "ingest" and result["state"] != "complete":
        return 1
    if args.action == "demo" and result["ingest"]["state"] != "complete":
        return 1
    return 0
