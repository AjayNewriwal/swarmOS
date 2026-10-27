# Sample workspace

This fixture is what a SwarmOS run starts from when the request does not supply
its own files.

- `target.py` contains a stub that raises `NotImplementedError`.
- `tests/test_target.py` describes the required behaviour of `normalize_tags`.

The demo provider is scripted against this workspace: its Planner produces a
three-step plan, its Coding Agent implements `normalize_tags` and writes
`docs/change-notes.md`, and its Review Agent checks the implementation. The
Verification Agent is not a fixture -- it really runs the command in
`SWARMOS_VERIFY_COMMAND` (default `python -m pytest -q`) inside the run's
temporary workspace.

Anything you learn from a demo run about *orchestration* is real: routing,
parallel fan-out, retries, budget accounting, and the trace. What is simulated
is the model's reasoning.

## Constrained local-model demo

`swarmos demo --provider openai` runs this workspace through the
`sample_tag_normalization` recipe:

- **Plan:** one step, supplied by the recipe, targeting only `target.py`.
  The planner model is skipped, and the trace records
  `plan_seeded` with `planner_skipped: true`.
- **Coding, review, verification:** real. The coder proposes the file,
  the reviewer inspects it, and `python -m pytest -q` runs the three tests.
- **Read-only:** `tests/test_target.py`. Any attempted write to it (or to
  any path other than `target.py`) is rejected before changes are applied.
- **Limits:** 6 agent calls, 0 retries, 1 revision, 0 escalations,
  120-second wall clock.

The offline demo-provider path (`swarmos run ... --provider demo`) still uses
its three-step fixture plan. This recipe exists because a small local model can
be slow and may otherwise add steps, edit tests, or trigger a revision cycle.
