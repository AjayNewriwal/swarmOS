# Audit and decisions

The starting point was `swarmos_mvp.zip`: a 15-file prototype with a FastAPI shell, a Streamlit viewer, an OpenAI-compatible adapter, and demo output. The target scope is an adaptive multi-agent coding orchestrator. This note records what was kept, what changed, and why.

## What existed

| Area | Prior state |
| --- | --- |
| Orchestration | One async function with `fixed` / `adaptive` / `parallel` policies |
| Agents | A planner and a critic prompt; no coding, review, or verification roles |
| State | Plain dictionaries built inline; no shared or typed state |
| Routing | One complexity heuristic and an `if` block; no conditional graph |
| Parallelism | Two fixed prompts via `asyncio.gather`; no dependency model |
| Retries | `max_retries` was accepted by the API and never used |
| Budget | Escalation was skipped when cost exceeded the budget; spend was not capped |
| Trace | A list of dicts in one response; nothing persisted |
| Persistence | None (the README listed SQLite as a roadmap item) |
| Verification | A simulated demo string; no command was executed |
| Frontend | Streamlit |
| Tests | Two assertions against two helpers |

## Gap analysis

| Requirement | Prior state | Now |
| --- | --- | --- |
| Four roles: plan, code, review, verify | Missing | Four typed agents with a shared call runner |
| LangGraph | Missing | `StateGraph` with conditional edges and `Send` fan-out |
| Explicit shared state | Missing | `SwarmState` with documented single writers and reducers |
| Typed agent I/O | Missing | Pydantic models at every agent boundary |
| Conditional routing | Missing | Explicit routing table with bounded outcomes |
| Parallel only for independent subtasks | Fixed prompt pair | Dependency waves; conflict detection rejects unsafe overlap |
| Bounded retries | Unused field | Per-step attempts, review revisions, repair rounds |
| Escalation | Model choice only | One bounded tier switch after retries, recorded |
| Execution limits | None | Call count, wall clock, and per-run ceilings |
| Budget controls | Escalation guard only | Reserve/settle ledger denies calls that would overshoot |
| Trace of everything | Inline dicts | Append-only SQLite events for calls, routing, verification, failures |
| Provider abstraction | OpenAI-compatible only | Provider protocol + demo fixture + compatible transport |
| No assumption about host model APIs | Implicit | Explicit: no endpoint means fixture, labelled everywhere |
| SQLite | Missing | Runs and trace events with foreign keys |
| React | Streamlit | Vite + React + TypeScript trace viewer |
| Tests for routing/failures/budget | Two helper tests | 59 tests across contracts, routing, limits, failures, verification, API |

## Decisions

**Rebuild rather than extend.** The prototype's single-function orchestrator could not host dependency-aware waves, per-step retries, or a typed state machine without becoming an unmaintainable branch pile. The `app/` layout, provider concept, demo-mode honesty, and sample intent were preserved; the execution core was replaced.

**Keep the demo fixture, rename its promise.** The prototype's demo mode returned canned text. The fixture now drives the same graph as a real provider, so orchestration and verification are real while model reasoning is clearly simulated. This keeps the MVP runnable with zero credentials without overstating what happened.

**Waves instead of a general scheduler.** A step is dispatched only when all dependencies are complete. Steps in one wave are independent by construction, which satisfies the parallel-execution constraint and makes conflict detection tractable. Conflicting writes are rejected rather than silently merged.

**Completion is owned by the merge step.** Coding branches report `coded_steps`; `advance` marks a step complete only after applying its changes. An early draft marked completion inside the branch, which masked rejected conflicts — the failing test for conflicting writes caught it.

**Reserve before spend.** The ledger reserves a conservative amount per call and settles actual cost afterwards. This bounds parallel overshoot and makes refusals deterministic. When token rates are zero the reservation is zero, because inventing a cost would be dishonest; call and time limits remain.

**Verification is deterministic.** The Verification Agent does not call a model. It runs a command, captures exit code and output, and parses counts. An empty suite is not a pass.

**Containment, not a sandbox.** The test runner uses a temp workspace, path validation, an allow-listed environment, and a timeout. It does not claim kernel isolation and is documented as unsuitable for untrusted code.

## Verified

- `pytest`: 59 passed.
- Demo run: plan 3 steps → two parallel coding branches → dependent documentation step → reviewer approval → real `pytest` run with 3 passed.
- Trace: `run_started`, planner routing, plan acceptance, two dispatches, merges, a review invocation, verification, and `run_finished`.
- API: inline and background runs, polling, config, and 404/422/400 paths.
- Frontend: `tsc --noEmit` and `vite build` succeed; FastAPI serves the built dashboard and the API from one process.
- Budget test: a priced provider with a `$0.05` budget halts at `budget_exceeded` after the first settled call.
- Escalation test: with `max_retries=1`, a failing coder makes three attempts — initial, retry, escalated — then the run stops.

## Open risks

- Model prompts are not yet evaluated against a benchmark of real repositories; the fixture proves the plumbing, not coding quality.
- The plan is a single model call. If it schedules two steps that touch one file, the run fails on conflict rather than repairing the plan.
- Verification trusts the configured command. A malicious project could commit a `conftest.py` that does damage; the environment filter and timeout limit leakage but are not a sandbox.
- Background runs are in-process. A restart abandons an in-flight run; durable resume is the next milestone.