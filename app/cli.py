"""Small command line entry point so the MVP is usable without the UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import settings
from app.models import Limits, RunRequest
from app.service import RunService
from app.store import RunStore

BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks" / "tasks.jsonl"


def _service(db_path: str) -> tuple[RunService, RunStore]:
    store = RunStore(db_path)
    return RunService(settings, store), store


def _print_run(record) -> None:
    print(record.summary)
    print()
    print(f"run_id: {record.run_id}")
    print(f"status: {record.status}")
    print(f"changes: {', '.join(record.changed_files) or 'none'}")
    if record.error:
        print(f"error: {record.error}")


def _cmd_run(args: argparse.Namespace) -> int:
    service, store = _service(args.db)
    request = RunRequest(
        goal=args.goal,
        provider=args.provider,
        limits=Limits(
            max_budget_usd=args.budget,
            max_agent_calls=args.max_calls,
            max_retries=args.max_retries,
            max_revisions=args.max_revisions,
            max_escalations=args.max_escalations,
        ),
    )
    try:
        record = asyncio.run(service.run_request(request))
        events = (
            [event.model_dump(mode="json") for event in store.get_events(record.run_id)]
            if args.json == "events"
            else []
        )
    finally:
        store.close()
    if args.json:
        print(record.model_dump_json(indent=2))
        if events:
            print(json.dumps(events, indent=2))
    else:
        _print_run(record)
    return 0 if record.status in {"completed", "completed_with_findings"} else 1


def _cmd_trace(args: argparse.Namespace) -> int:
    store = RunStore(args.db)
    try:
        record = store.get_run(args.run_id)
        if record is None:
            print(f"run {args.run_id} not found", file=sys.stderr)
            return 1
        print(f"{record.status}  {record.goal}")
        for event in store.get_events(args.run_id):
            print(f"{event.seq:>3}  {event.kind:<18} {event.message}")
    finally:
        store.close()
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    service, store = _service(args.db)
    rows: list[tuple[str, str, int, int, str]] = []
    try:
        for line in BENCHMARKS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            task = json.loads(line)
            request = RunRequest(goal=task["goal"], provider="demo", limits=Limits())
            record = asyncio.run(service.run_request(request))
            rows.append(
                (
                    task["id"],
                    record.status,
                    record.agent_calls,
                    record.duration_ms,
                    record.changed_files[0] if record.changed_files else "-",
                )
            )
    finally:
        store.close()
    print(f"{'task':<12} {'status':<24} {'calls':>5} {'ms':>6}  first change")
    for row in rows:
        print(f"{row[0]:<12} {row[1]:<24} {row[2]:>5} {row[3]:>6}  {row[4]}")
    return 0 if all(row[1] == "completed" for row in rows) else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmos")
    parser.add_argument("--db", default=str(settings.db_path))
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one coding task")
    run.add_argument("goal")
    run.add_argument("--provider", default="auto", choices=["auto", "demo", "openai"])
    run.add_argument("--budget", type=float, default=0.25)
    run.add_argument("--max-calls", type=int, default=12)
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--max-revisions", type=int, default=1)
    run.add_argument("--max-escalations", type=int, default=1)
    run.add_argument("--json", nargs="?", const="record", choices=["record", "events"])
    run.set_defaults(func=_cmd_run)

    trace = sub.add_parser("trace", help="print the trace for a run")
    trace.add_argument("run_id")
    trace.set_defaults(func=_cmd_trace)

    bench = sub.add_parser("bench", help="run the bundled benchmark tasks in demo mode")
    bench.set_defaults(func=_cmd_bench)

    serve = sub.add_parser("serve", help="start the API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())