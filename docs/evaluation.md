# Benchmarking and real-model evaluation

SwarmOS ships two evaluation modes. They answer different questions and their
results must not be mixed.

| Mode | Provider | Question it answers | Reproducible |
| --- | --- | --- | --- |
| Scripted scenario suite | `ScenarioProvider` (in `app/benchmark_scenarios.py`) | Does the orchestration graph handle each failure mode correctly, and what does each policy cost in calls and rounds? | Yes, exactly |
| Task suite with a real model | `openai-compatible` endpoint | How do real planner, coder, and reviewer models behave under each policy? | No, statistically |

The scripted suite proves *mechanism*, not model quality. The task suite measures
real models but cannot inject deterministic faults, because a real model decides
its own output.

## Scripted scenario suite

```bash
swarmos bench --suite scenarios
swarmos bench --suite scenarios --policy fixed-fast adaptive \
  --scenario first_try_success escalation_recovery verification_timeout \
  --json-report benchmarks/results/policy-benchmark.json \
  --markdown benchmarks/results/policy-benchmark.md
```

By default the runs are stored in memory. Add `--bench-db benchmarks/results/scenarios.db`
to keep them inspectable with `swarmos serve` and the dashboard. Reports are
written to `benchmarks/results/`, which is git-ignored, so a run never modifies
tracked project files.

### Scenario expectations

Each scenario also has a test that pins these expectations
(`tests/test_benchmark_scenarios.py`). "Rejection" means the reviewer's verdict
is recorded; it is never converted into approval.

| Scenario | Expected status | Verification | Review | Budget constraint |
| --- | --- | --- | --- | --- |
| `first_try_success` | `completed` | gating, passed | approve | no revisions, no escalation |
| `revision_required` | `completed` | gating, passed | reject once, then approve | `max_revisions=1` |
| `genuine_blocker` | `completed_with_findings` | diagnostic, failed | request_changes (evidenced) | `max_revisions=1`, exhausted |
| `insufficient_evidence` | `completed_with_findings` | diagnostic, failed | request_changes (thin evidence) | `max_revisions=1`; evidence gap recorded |
| `malformed_review` | `review_invalid` | diagnostic, failed | request_changes with no finding | nothing dispatched |
| `planner_malformed` | `failed` | not run | not reached | planner repair bounded by `max_retries=1` |
| `coder_malformed` | `failed` | not run | not reached | coder retry bounded by `max_retries=1` |
| `reviewer_malformed` | `failed` | not run | parse failure, not approval | reviewer repair bounded by `max_retries=1` |
| `provider_timeout` | `completed` | gating, passed | approve | one transient failure, one retry |
| `verification_failure` | `failed` | gating, failed twice | approve | `max_retries=1` repair, then stop |
| `verification_timeout` | `failed` | gating, exit 124 | approve | `verification_timeout_seconds=1` |
| `execution_disabled` | `failed` | gating, exit 126 (refused) | approve | `execution_enabled=False` |
| `escalation_recovery` | `completed` under adaptive/role-routed, `completed_with_findings` under fixed-fast | gating | reject, then approve | `max_revisions=0`, `max_escalations=1` |
| `escalation_limit` | `completed_with_findings` | diagnostic, failed | request_changes | one escalation, then stop |
| `escalation_unavailable` | `completed_with_findings` | diagnostic, failed | request_changes | no strong model configured |
| `revision_limit_hit` | `completed_with_findings` | diagnostic, failed | request_changes | `max_revisions=0` |
| `call_limit` | `limit_exceeded` | not run | not reached | `max_agent_calls=2`, one refusal |

`verification` is "gating" when it decides the outcome. A rejected run gets a
*diagnostic* verification for evidence only; it never dispatches another coding
round and never raises the verification pass rate.

## Real-model evaluation

### 1. Configure the provider

```bash
export OPENAI_COMPAT_BASE_URL=http://127.0.0.1:11434/v1   # Ollama, vLLM, ...
export OPENAI_COMPAT_MODEL_FAST=qwen2.5-coder:7b
export OPENAI_COMPAT_MODEL_STRONG=qwen2.5-coder:32b       # optional, must differ
export OPENAI_COMPAT_API_KEY=                             # empty is normal for local
```

A hosted compatible endpoint works too; an API key is optional and is sent only
when configured. Nothing in this document requires a paid key.

### 2. Choose models per role

```bash
export SWARMOS_ROLE_MODELS="planner=strong,coder=fast,reviewer=strong"
```

`role_routed` and `config` honour that map. `adaptive` always starts every role on
the fast profile and escalates on failure. `fast_only` ignores the map and pins
every role to fast.

### 3. Configure escalation

Escalation only exists when `OPENAI_COMPAT_MODEL_STRONG` is set and differs from
the fast model. `max_escalations` in the run limits bounds how often it can
happen. If no strong model is configured, the report warns that escalation is
inert: equal results across policies are then *by construction*, not evidence of
equivalence.

Two implementation facts matter when reading results:

- `fast_only` can never escalate. `RunService` clamps `max_escalations` to zero,
  and routing enforces the policy independently, so a directly constructed run
  cannot escalate out of it either.
- Escalation latches the run to the strong tier. At most **one** escalation can
  happen per run, even if `max_escalations` is set higher; the limit is a ceiling,
  not a schedule.

### 4. Smoke test (one cheap run)

Preflight verifies configuration without any inference:

```bash
swarmos doctor
```

Check that it reports the base URL, the fast model, a distinct strong model if
you want escalation, the role routing, pricing state, and a successful model
list. A failure here is an **infrastructure/configuration** result, not a model
failure; the benchmark must not be run or interpreted until `doctor` is clean.

Then, and only with approval, run one small task with explicit limits:

```bash
swarmos run "Implement tag normalization so the failing tests pass" \
  --provider openai --policy fast_only --budget 0.05 \
  --max-calls 6 --max-retries 0 --max-escalations 0
```

Expected resource usage, before any model call: at most **6 model calls**, zero
retries, zero escalations, no more than the configured wall clock (default 120 s
per run), and at most the soft cap of **$0.05** once token rates are configured.
A one-step demo typically uses planner + coder + reviewer = 3 calls.

Inspect the record and trace: provider, `models_by_role`, `models_used`, the
review verdict, the verification command/exit code, `usage_available`, and
`cost_usd`. If the endpoint is unreachable, the run ends `failed` with a
provider-category error; report that as infrastructure, and do not treat it as
evidence about the models or the policy.

### 5. Limited policy comparison

```bash
swarmos bench --provider openai --policy fixed-fast adaptive multi-agent \
  --budget 0.50 --limit 1 --out benchmarks/results/real-openai.jsonl
```

`--limit 1` runs the first bundled task only. Pass `--budget` explicitly: a zero
cap is not "unlimited" once token rates are configured. Each policy runs as its
own run with its own limits.

### 6. Token usage and cost

Usage is recorded only when the endpoint returns `usage` (or Ollama's
`prompt_eval_count`/`eval_count`) with **both** prompt and completion counts. A
partial usage block is treated as unavailable. To turn cost into a real control:

```bash
export FAST_INPUT_USD_PER_MILLION=0.15
export FAST_OUTPUT_USD_PER_MILLION=0.60
export STRONG_INPUT_USD_PER_MILLION=0.60
export STRONG_OUTPUT_USD_PER_MILLION=2.40
```

Cost is reported only when usage was measured **and** every selectable profile is
priced; otherwise the benchmark writes `null` and withholds the cost comparison.

### 7. Record the configuration

Every run record carries `provider`, `provider_kind`, `models_by_role`, the
limits, and `usage_available`. The scenario report adds a `config_fingerprint`
with models, routing, pricing, limit ceilings, verification settings, and
allow-listed commands, plus `models_used` (the distinct model ids actually
invoked per role) and `escalation_triggers`. Use `models_used`, not
`models_by_role`, to prove that an escalation happened: `models_by_role` reports
the policy's starting selection. Keep the JSONL output next to the model ids; a
metric without its configuration is not comparable.

### 8. Comparison protocol

This is the protocol for a fair comparison of `fixed-fast`, `adaptive`, and
`multi-agent`.

**Controls — identical for every policy**

| Control | Requirement |
| --- | --- |
| Tasks | Same task ids, prompts, acceptance criteria, and seed files |
| Workspace | Same initial workspace for every run of a task |
| Models | Same fast/strong model ids and versions; record them |
| Sampling | Same endpoint and sampling configuration (the adapter sends `temperature: 0.1`) |
| Limits | Same `max_agent_calls`, `max_wall_seconds`, `max_retries`, `max_revisions`; `max_escalations` comes from the policy |
| Verification | Same command and timeout |
| Budget | Same per-run budget (or none, explicitly) |
| Trials | At least 3 runs per task/policy if budget allows; report the spread |

**Capability disclosure.** `fixed-fast` cannot use the strong model at all;
`adaptive` and `multi-agent` can only use it if a distinct strong model is
configured. If a comparison gives some policies a strong model and not others,
the difference measures *model access plus orchestration*, not orchestration
intelligence. Run one comparison with a strong model configured and (optionally)
one without, and label them separately.

**Metrics — keep separate, never collapse into one score**

| Metric | Where it comes from |
| --- | --- |
| Task success (approved) | `status == "completed"` |
| Gating verification pass rate | runs with a gating verification that passed |
| Reviewer rejections | `reviewer_rejections` (recorded rejections, including at the revision limit) |
| Invalid reviews / evidence gaps | `invalid_reviews`, `evidence_gap_events` |
| Model calls and revisions | `agent_calls`, per-role counts, `revisions` |
| Escalation frequency and reason | `escalations`, `escalation_triggers` |
| Latency | `duration_ms` (wall clock, variable) |
| Tokens and cost | only when measured; otherwise `null` |

Reviewer false positives and false negatives can only be measured against an
independent ground truth — for example running the gating verification on the
same workspace after a rejection. The harness does not claim that number; if you
need it, compute it separately from the run's trace and say so.

**Report template.** For each task and policy: approved/total, gating
verification pass rate, mean calls, mean revisions, rejection count, escalation
count with reasons, and measured cost where available. Report the number of
trials and the spread. Do not compare a single run per cell.


## Orchestration decision audit

The graph makes decisions from run state, not from a score. The signals it
actually uses:

| Situation | Signal used | Decision |
| --- | --- | --- |
| Transient provider failure (timeout, connection, 429, 5xx) | `ProviderError.retryable` | Bounded retry: `AgentRunner` retries within `max_attempts`; a coding failure is re-dispatched by routing while `attempts <= max_retries`. Every attempt reserves a ledger call, so retries cannot escape the call or wall-clock limits. |
| Genuine implementation defect | Reviewer verdict `request_changes` | Revision round while the revision budget allows; otherwise escalation (if allowed), otherwise finalise as `completed_with_findings`. |
| Failed gating verification | `verification.passed` false | Repair round while verification attempts allow; then escalation (if allowed); then `failed`. |
| Reviewer rejection with insufficient evidence | Evidence-shape report | Recorded as an advisory `evidence_gap` event. The rejection still stands and still consumes the revision budget; routing never reads the evidence report, so it can never turn a rejection into approval or silently drop a blocker. |
| Budget or call limit exhausted | Ledger refusal (`BudgetExceeded`/`CallLimitExceeded`/`DeadlineExceeded`) | Halt with `budget_exceeded` or `limit_exceeded`; no revision and no escalation are dispatched. |

Escalation records a machine-readable `trigger`, so a real evaluation can
separate the reasons:

- `coding_failures_exhausted_retries`
- `revision_budget_exhausted`
- `verification_failed`
- `unclassified` (defensive; no current path produces it)

Gaps between intent and implementation, stated plainly:

- **Difficulty is not a signal.** Adaptive does not estimate whether a task is
  hard; it escalates only after a failure signal (retries, revisions, or repairs
  exhausted). A model that is slow but correct will never escalate.
- **One escalation per run.** The first escalation latches the run to the strong
  tier, so `max_escalations > 1` has no effect today.
- **Evidence quality does not drive routing.** A rejection that rests on thin
  evidence consumes the same revision or escalation budget as an evidenced one.
  This is deliberate: making evidence shape route a rejection away could
  silently ignore a real blocker. The gap is visible in the metrics, not hidden.
- **Retries vs revisions.** `retries` counts every attempt index above one,
  including revisions and repairs. `revisions` is published separately so the two
  can be told apart.

No scoring system or additional agent role is introduced; the audit found no
need for one.

## What real models can and cannot reproduce

Reproducible with a real model: approval and rejection, verification outcomes,
revision and repair rounds, escalation, call/revision/limit exhaustion, token and
cost capture, and the per-role call mix.

Not reproducible: injected malformed planner/coder/reviewer output, scripted
provider timeouts, deterministic disabled execution, and exact revision counts.
Those stay in the scripted suite, which exists to prove that the *handling* is
correct.

## Known limitations

- The scenario fixture is a single-step task over one file. It exercises review,
  revision, escalation, verification, and limits, but not multi-step fan-out,
  wave merging, or cross-step conflict detection. The task suite in
  `benchmarks/tasks.jsonl` runs the demo project, not a parallel plan.
- Wall-clock duration is measured, not deterministic; repeated scripted runs
  match on outcomes and counts, not on milliseconds.
- `retries` counts every invocation whose attempt index is above one, including
  revision and repair rounds; the report also publishes revisions separately.
- The scripted provider reports no token usage on purpose, so the scripted matrix
  never exercises the cost path. Priced real-model runs do.
- At most one escalation happens per run, even if `max_escalations` is higher.
- `models_by_role` is the policy's starting selection. Use `models_used` to see
  every model id actually invoked, including the escalated one.
- Reviewer false positives/negatives need an independent ground-truth run; the
  harness reports rejections and invalid reviews, not correctness of the review.
