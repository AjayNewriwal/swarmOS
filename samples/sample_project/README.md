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