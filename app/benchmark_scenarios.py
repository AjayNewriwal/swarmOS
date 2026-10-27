"""Deterministic, scripted benchmark scenarios.

The provider below drives the real orchestrator through the real planning,
coding, review, revision, escalation, verification, and ledger paths. Only the
model responses are scripted, so a matrix run is reproducible and needs no
network or API key.

The provider reports no token usage on purpose: the harness then records tokens
and cost as unavailable instead of inventing them.

The scenarios cover the outcomes the graph can produce: first-attempt success, a
revision round, a genuine blocker, an under-evidenced blocker, a verification
failure, escalation recovery, the revision limit, and the call limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from app.benchmark import (
    POLICIES,
    BenchmarkPolicy,
    BenchmarkResult,
    config_fingerprint,
    result_from_run,
)
from app.config import Settings
from app.models import Limits, RunRecord
from app.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderTimeoutError,
)
from app.service import RunService
from app.store import RunStore

FAST_MODEL = "bench-weak"
STRONG_MODEL = "bench-strong"

GOAL = "Implement add(a, b) in calc.py so tests/test_calc.py passes."

CALC_STUB = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    raise NotImplementedError
'''

CALC_CORRECT = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b
'''

CALC_WRONG = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the difference of two integers."""
    return a - b
'''

CALC_OFF_BY_ONE = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers, plus one."""
    return a + b + 1
'''

TESTS = '''from calc import add


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-4, 1) == -3
'''

# Used only by the timeout scenario: the verification timeout must fire before
# this test finishes, so it must sleep longer than the configured timeout.
SLOW_TESTS = "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"

FILES = {"calc.py": CALC_STUB, "tests/test_calc.py": TESTS}
SLOW_FILES = {
    "calc.py": CALC_STUB,
    "tests/test_calc.py": TESTS,
    "tests/test_slow.py": SLOW_TESTS,
}

PLAN = {
    "assumptions": ["The workspace is a synthetic arithmetic fixture."],
    "steps": [
        {
            "id": "fix_calc",
            "title": "Implement add in calc.py",
            "detail": "Implement add(a, b) in calc.py so tests/test_calc.py passes.",
            "acceptance_criteria": [
                "tests/test_calc.py passes",
                "add returns the arithmetic sum of its two arguments",
            ],
            "depends_on": [],
            "target_files": ["calc.py"],
        }
    ],
}

APPROVE = {
    "verdict": "approve",
    "rationale": "add returns the arithmetic sum.",
    "findings": [],
}

REJECT_EVIDENCED = {
    "verdict": "request_changes",
    "rationale": "The implementation returns the difference.",
    "findings": [
        {
            "severity": "blocker",
            "step_id": "fix_calc",
            "file": "calc.py",
            "message": (
                'calc.py line 6 returns a - b, so "tests/test_calc.py passes" fails: '
                "expected 5 for add(2, 3), actual -1."
            ),
        }
    ],
}

# A rejection that names no finding at all is malformed output: the orchestrator
# records it as review_invalid and dispatches nothing.
REJECT_MALFORMED = {
    "verdict": "request_changes",
    "rationale": "Not convinced.",
    "findings": [],
}

# A blocker with no code reference, no quoted criterion, and no expected/actual
# pair. It is a real finding with thin evidence, not malformed output.
REJECT_THIN = {
    "verdict": "request_changes",
    "rationale": "Not convinced.",
    "findings": [
        {
            "severity": "blocker",
            "step_id": "fix_calc",
            "file": "calc.py",
            "message": "The arithmetic helper is still wrong.",
        }
    ],
}


@dataclass(frozen=True)
class Scenario:
    id: str
    goal: str
    category: str
    notes: str
    files: dict[str, str]
    plan: dict
    limits: Limits
    weak_outputs: tuple[str, ...]
    strong_outputs: tuple[str, ...]
    reviews: tuple[dict, ...]
    # Settings this scenario needs beyond the shared scripted benchmark settings
    # (for example a disabled execution path or a one-second verification timeout).
    settings_overrides: dict[str, object] = field(default_factory=dict)
    # role -> response kinds per call ("ok", "malformed", "timeout"); the last
    # entry repeats. A fault is scripted for that role only.
    faults: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def review_at(self, index: int) -> dict:
        return self.reviews[min(index, len(self.reviews) - 1)]


def _limits(**overrides: int) -> Limits:
    base = {
        "max_agent_calls": 12,
        "max_budget_usd": 0.25,
        "max_wall_seconds": 120,
        "max_retries": 0,
        "max_revisions": 0,
        "max_escalations": 0,
    }
    base.update(overrides)
    return Limits(**base)


SCENARIOS: dict[str, Scenario] = {
    scenario.id: scenario
    for scenario in (
        Scenario(
            id="first_try_success",
            goal=GOAL,
            category="success",
            notes="The first coding attempt satisfies the tests and review approves.",
            files=FILES,
            plan=PLAN,
            limits=_limits(),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
        ),
        Scenario(
            id="revision_required",
            goal=GOAL,
            category="revision",
            notes="The reviewer rejects once with evidence; the revision passes.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_revisions=1),
            weak_outputs=(CALC_WRONG, CALC_CORRECT),
            strong_outputs=(CALC_CORRECT,),
            reviews=(REJECT_EVIDENCED, APPROVE),
        ),
        Scenario(
            id="genuine_blocker",
            goal=GOAL,
            category="blocker",
            notes="A substantiated blocker keeps the run rejected, never approved.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_revisions=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_EVIDENCED,),
        ),
        Scenario(
            id="insufficient_evidence",
            goal=GOAL,
            category="evidence",
            notes="A thin blocker is recorded as an evidence gap, not as approval.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_revisions=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_THIN,),
        ),
        Scenario(
            id="malformed_review",
            goal=GOAL,
            category="evidence",
            notes="A rejection that names no finding is review_invalid, never approval.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_revisions=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_MALFORMED,),
        ),
        Scenario(
            id="planner_malformed",
            goal=GOAL,
            category="reliability",
            notes="The planner never returns valid JSON; the bounded repair fails.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_retries=1),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            settings_overrides={"retry_backoff_seconds": 0.0},
            faults={"planner": ("malformed",)},
        ),
        Scenario(
            id="coder_malformed",
            goal=GOAL,
            category="reliability",
            notes="The coder never returns valid JSON; the retry is bounded.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_retries=1),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            faults={"coder": ("malformed",)},
        ),
        Scenario(
            id="reviewer_malformed",
            goal=GOAL,
            category="reliability",
            notes="The reviewer never returns valid JSON; a parse failure is not approval.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_retries=1),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            settings_overrides={"retry_backoff_seconds": 0.0},
            faults={"reviewer": ("malformed",)},
        ),
        Scenario(
            id="provider_timeout",
            goal=GOAL,
            category="reliability",
            notes="A transient provider timeout is retried once and then recovers.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_retries=1),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            faults={"coder": ("timeout", "ok")},
        ),
        Scenario(
            id="verification_failure",
            goal=GOAL,
            category="verification",
            notes="Review approves code that fails the tests; repair rounds cannot fix it.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_retries=1),
            weak_outputs=(CALC_OFF_BY_ONE,),
            strong_outputs=(CALC_OFF_BY_ONE,),
            reviews=(APPROVE,),
        ),
        Scenario(
            id="verification_timeout",
            goal=GOAL,
            category="limits",
            notes="The verification command exceeds its timeout and is recorded as failed.",
            files=SLOW_FILES,
            plan=PLAN,
            limits=_limits(),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            settings_overrides={"verification_timeout_seconds": 1},
        ),
        Scenario(
            id="execution_disabled",
            goal=GOAL,
            category="limits",
            notes="Execution is disabled, so verification is refused rather than approved.",
            files=FILES,
            plan=PLAN,
            limits=_limits(),
            weak_outputs=(CALC_CORRECT,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(APPROVE,),
            settings_overrides={"execution_enabled": False},
        ),
        Scenario(
            id="escalation_recovery",
            goal=GOAL,
            category="escalation",
            notes="The fast model cannot fix the code; the strong model can.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_escalations=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_CORRECT,),
            reviews=(REJECT_EVIDENCED, APPROVE),
        ),
        Scenario(
            id="escalation_limit",
            goal=GOAL,
            category="limits",
            notes="The strong model is also wrong; escalation happens once and stops.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_escalations=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_EVIDENCED,),
        ),
        Scenario(
            id="escalation_unavailable",
            goal=GOAL,
            category="limits",
            notes="No distinct strong model is configured, so escalation is inert.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_escalations=1),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_EVIDENCED,),
            settings_overrides={"strong_model": ""},
        ),
        Scenario(
            id="revision_limit_hit",
            goal=GOAL,
            category="limits",
            notes="A rejection with no revision budget ends as completed_with_findings.",
            files=FILES,
            plan=PLAN,
            limits=_limits(),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(REJECT_EVIDENCED,),
        ),
        Scenario(
            id="call_limit",
            goal=GOAL,
            category="limits",
            notes="A two-call budget is exhausted before review can run.",
            files=FILES,
            plan=PLAN,
            limits=_limits(max_agent_calls=2),
            weak_outputs=(CALC_WRONG,),
            strong_outputs=(CALC_WRONG,),
            reviews=(APPROVE,),
        ),
    )
}


class ScenarioProvider:
    """Deterministic provider scripted per scenario; reports no usage.

    ``faults`` lets a scenario script a malformed reply or a transient provider
    error for one role, so the run exercises the real parse, repair, and bounded
    retry paths instead of a special-case branch.
    """

    name = "scripted"

    def __init__(self, scenario: Scenario, *, fast_model: str, strong_model: str) -> None:
        self.scenario = scenario
        self.fast_model = fast_model
        self.strong_model = strong_model
        self.calls: list[tuple[str, str]] = []
        self._calls_by_role: dict[str, int] = {}
        self._coder_calls: dict[str, int] = {}
        self._review_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = str(request.role)
        self.calls.append((role, request.model))
        kind = self._next_kind(role)
        if kind == "timeout":
            raise ProviderTimeoutError("scripted provider timeout (deterministic)")

        if kind == "malformed":
            text = "this is not the JSON object the agent asked for"
        elif role == "planner":
            text = json.dumps(self.scenario.plan)
        elif role == "coder":
            text = json.dumps(self._coding(request))
        elif role == "reviewer":
            text = json.dumps(self.scenario.review_at(self._review_calls))
            self._review_calls += 1
        else:
            raise ProviderError(f"scripted benchmark has no script for {role!r}")
        return ModelResponse(
            text=text,
            model=request.model,
            provider=self.name,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            usage_available=False,
        )

    def _next_kind(self, role: str) -> str:
        index = self._calls_by_role.get(role, 0)
        self._calls_by_role[role] = index + 1
        script = self.scenario.faults.get(role)
        if not script:
            return "ok"
        return script[min(index, len(script) - 1)]

    def _coding(self, request: ModelRequest) -> dict:
        payload = json.loads(request.user)
        step = payload.get("step") or {}
        step_id = str(step.get("id", ""))
        index = self._coder_calls.get(step_id, 0)
        self._coder_calls[step_id] = index + 1
        outputs = self.scenario.weak_outputs
        if request.model == self.strong_model and self.scenario.strong_outputs:
            outputs = self.scenario.strong_outputs
        content = outputs[min(index, len(outputs) - 1)]
        return {
            "step_id": step_id,
            "summary": f"Scripted change for {step_id} from {request.model}.",
            "rationale": "Deterministic benchmark script.",
            "changes": [{"path": "calc.py", "content": content, "action": "update"}],
        }


def benchmark_settings(base: Settings) -> Settings:
    """Scripted settings with a distinct fast/strong profile and no endpoint."""
    return replace(
        base,
        provider="scripted",
        base_url="",
        api_key="",
        fast_model=FAST_MODEL,
        strong_model=STRONG_MODEL,
        fast_input_rate=0.0,
        fast_output_rate=0.0,
        strong_input_rate=0.0,
        strong_output_rate=0.0,
        role_models="planner=strong,reviewer=strong",
        model_policy="config",
    )


async def run_scenario(
    scenario: Scenario,
    policy: BenchmarkPolicy,
    *,
    settings: Settings,
    store: RunStore,
) -> BenchmarkResult:
    scenario_settings = (
        replace(settings, **scenario.settings_overrides)
        if scenario.settings_overrides
        else settings
    )
    service = RunService(scenario_settings, store)
    record = RunRecord(
        goal=scenario.goal,
        limits=service.clamp_limits(scenario.limits, policy.model_policy),
        provider="scripted",
        provider_kind="unknown",
        model_policy=policy.model_policy,
    )
    store.save_run(record)
    provider = ScenarioProvider(
        scenario,
        fast_model=scenario_settings.fast_model,
        strong_model=scenario_settings.strong_model,
    )
    finished = await service.execute(
        record, provider, dict(scenario.files), policy=policy.model_policy
    )
    return result_from_run(
        task_id=scenario.id,
        policy=policy.name,
        record=finished,
        events=store.get_events(record.run_id),
        escalation_available=scenario_settings.escalation_available,
        simulated=True,
    )


async def run_matrix(
    *,
    settings: Settings,
    store: RunStore,
    scenario_ids: list[str] | None = None,
    policy_names: list[str] | None = None,
) -> list[BenchmarkResult]:
    """Run every selected scenario under every selected policy, in order."""
    scenarios = [
        SCENARIOS[name] for name in (scenario_ids or list(SCENARIOS)) if name in SCENARIOS
    ]
    policies = [POLICIES[name] for name in (policy_names or list(POLICIES))]
    results: list[BenchmarkResult] = []
    for scenario in scenarios:
        for policy in policies:
            results.append(
                await run_scenario(scenario, policy, settings=settings, store=store)
            )
    return results


def scenario_report_config(
    settings: Settings,
    *,
    policies: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    store: str = "memory",
) -> dict:
    """Everything a stored result must be read against."""
    selected = [name for name in (scenario_ids or list(SCENARIOS)) if name in SCENARIOS]
    return {
        "suite": "scripted-scenarios",
        "provider": ScenarioProvider.name,
        "simulated": True,
        "usage_reporting": "not reported by the scripted provider",
        "store": store,
        "policies": policies or list(POLICIES),
        "scenarios": {
            name: {
                "goal": SCENARIOS[name].goal,
                "category": SCENARIOS[name].category,
                "notes": SCENARIOS[name].notes,
                "limits": SCENARIOS[name].limits.model_dump(),
                "settings_overrides": SCENARIOS[name].settings_overrides,
                "faults": {
                    role: list(kinds)
                    for role, kinds in SCENARIOS[name].faults.items()
                },
                "review_script": [review["verdict"] for review in SCENARIOS[name].reviews],
                "weak_outputs": len(SCENARIOS[name].weak_outputs),
                "strong_outputs": len(SCENARIOS[name].strong_outputs),
            }
            for name in selected
        },
        "settings": config_fingerprint(settings),
    }
