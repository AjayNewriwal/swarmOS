"""Small command line entry point so the MVP is usable without the UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.benchmark import (
    POLICIES,
    load_tasks,
    request_for,
    result_from_run,
    summarise,
    write_results,
)
from app.config import settings
from app.models import Limits, RunRequest
from app.providers import resolve_provider
from app.providers.base import ProbeableProvider, ProviderConfigError, ProviderError
from app.service import RunService
from app.store import RunStore

BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks" / "tasks.jsonl"
ROLLING_POLICIES = ("fixed-fast", "adaptive", "multi-agent")


def _service(db_path: str) -> tuple[RunService, RunStore]:
    store = RunStore(db_path)
    return RunService(settings, store), store


def _warn_zero_budget(provider: str) -> None:
    """Warn when a zero budget would be enforced as a hard refusal.

    A zero cap means "unlimited" while no token rates are configured, but once
    rates exist the ledger refuses every call. Say so before the run fails, and
    do not change the requested limit.
    """
    uses_real_endpoint = provider != "demo" and settings.openai_configured
    if uses_real_endpoint and settings.pricing_known:
        print(
            "WARNING: --budget 0 is enforced as a hard cap when token rates are "
            "configured, so every model call will be refused. Pass a real cap "
            "such as --budget 0.50, or unset the rates to disable USD limits.",
            file=sys.stderr,
        )


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
    _warn_zero_budget(args.provider)
    request = RunRequest(
        goal=args.goal,
        provider=args.provider,
        model_policy=args.policy,
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
    except ProviderConfigError as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2
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
    _warn_zero_budget(args.provider)
    tasks = load_tasks(BENCHMARKS)
    if args.limit:
        tasks = tasks[: args.limit]
    selected = args.policy or list(ROLLING_POLICIES)
    results = []
    exit_code = 0
    try:
        for task in tasks:
            for policy_name in selected:
                policy = POLICIES[policy_name]
                request = request_for(task, policy, args.provider, args.budget)
                try:
                    record = asyncio.run(service.run_request(request))
                except ProviderConfigError as exc:
                    print(f"provider configuration error: {exc}", file=sys.stderr)
                    return 2
                events = store.get_events(record.run_id)
                results.append(
                    result_from_run(
                        task_id=task.id,
                        policy=policy.name,
                        record=record,
                        events=events,
                        escalation_available=settings.escalation_available,
                    )
                )
                if record.status != "completed":
                    exit_code = 1
    finally:
        store.close()

    print(
        f"{'task':<14} {'policy':<12} {'status':<20} {'calls':>5} {'retry':>5} "
        f"{'ms':>6} {'verified':>8} {'usage':>6}"
    )
    for item in results:
        print(
            f"{item.task_id:<14} {item.policy:<12} {item.status:<20} "
            f"{item.agent_calls:>5} {item.retries:>5} {item.duration_ms:>6} "
            f"{('yes' if item.verification_passed else 'no'):>8} "
            f"{('yes' if item.usage_available else 'no'):>6}"
        )

    summary = summarise(results)
    print()
    for warning in summary.get("warnings", []):
        print(f"WARNING: {warning}")
    if summary.get("warnings"):
        print()
    print(json.dumps(summary, indent=2))

    if args.out:
        write_results(Path(args.out), results)
        print(f"\nwrote {len(results)} result(s) to {args.out}")
    return exit_code


def _cmd_doctor(_: argparse.Namespace) -> int:
    """Report configuration and, if configured, probe the model endpoint.

    The probe lists models only; it never runs inference and never spends money.
    """
    info = settings.describe()
    print("SwarmOS doctor")
    print(f"  python:            {sys.version.split()[0]}")
    print(f"  provider mode:     {info['provider_mode']}")
    print(f"  provider kind:     {info['provider_kind']}")
    print(f"  endpoint:          {info['base_url'] or '(not configured)'}")
    print(f"  auth configured:   {info['auth_configured']}")
    print(f"  json mode:         {info['json_mode']}")
    print(f"  profiles:          {', '.join(info['profiles']) or '(none)'}")
    print(f"  role routing:      {info['role_routing']}")
    print(f"  escalation:        {info['escalation_available']}")
    print(f"  pricing known:     {info['pricing_known']}")
    print(f"  monetary enforced: {info['monetary_enforced']}")
    print(f"  execution enabled: {settings.execution_enabled}")
    print(f"  allowed commands:  {', '.join(sorted(settings.allowed_command_set))}")
    print(f"  repo checkout:     {settings.allow_repo_checkout}")
    print(f"  api token set:     {bool(settings.api_token)}")
    print(f"  database:          {settings.db_path}")

    if not settings.openai_configured:
        print("\nNo endpoint configured. Runs use the deterministic demo fixture.")
        return 0

    provider, name, warning = resolve_provider(
        "demo" if settings.provider == "demo" else "auto", settings
    )
    print(f"\nProbing {name} (model list only, no inference)…")
    if not isinstance(provider, ProbeableProvider):
        print("  provider does not support a probe")
        return 0
    try:
        result = asyncio.run(provider.health())
    except ProviderError as exc:
        print(f"  probe failed: {exc}")
        return 1
    print(f"  reachable: {result.get('ok')} - {result.get('detail')}")
    models = result.get("models") or []
    if models:
        print(f"  models: {', '.join(str(item) for item in models[:20])}")
        missing = [
            profile.model
            for profile in settings.configured_profiles().values()
            if profile.model not in models
        ]
        if missing:
            print(f"  WARNING: configured model(s) not listed: {', '.join(missing)}")
    if warning:
        print(f"  note: {warning}")
    return 0 if result.get("ok") else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback:
        # The run API executes test code. Never expose it on a public interface
        # without both an explicit opt-in and a token.
        if not settings.allow_remote_bind:
            print(
                "refusing to bind a non-loopback address: set "
                "SWARMOS_ALLOW_REMOTE_BIND=1 to override",
                file=sys.stderr,
            )
            return 2
        if not settings.api_token:
            print(
                "refusing to bind a non-loopback address without SWARMOS_API_TOKEN; "
                "the execution API would be unauthenticated",
                file=sys.stderr,
            )
            return 2
        print(
            "WARNING: exposing the execution API on a non-loopback interface. "
            "It can run project code in a temporary workspace."
        )

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmos")
    parser.add_argument("--db", default=str(settings.db_path))
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one coding task")
    run.add_argument("goal")
    run.add_argument("--provider", default="auto", choices=["auto", "demo", "openai"])
    run.add_argument(
        "--policy",
        default=None,
        choices=["config", "fast_only", "adaptive", "role_routed"],
        help="model-selection policy (default: from settings)",
    )
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

    bench = sub.add_parser("bench", help="run reproducible benchmark tasks")
    bench.add_argument(
        "--provider", default="demo", choices=["auto", "demo", "openai"]
    )
    bench.add_argument(
        "--policy", nargs="*", choices=list(ROLLING_POLICIES), default=None
    )
    bench.add_argument("--budget", type=float, default=0.0)
    bench.add_argument("--limit", type=int, default=0)
    bench.add_argument("--out", default=None, help="write JSONL results here")
    bench.set_defaults(func=_cmd_bench)

    doctor = sub.add_parser("doctor", help="report configuration and probe the endpoint")
    doctor.set_defaults(func=_cmd_doctor)

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