from __future__ import annotations

from app.agents.base import Agent, AgentOutputError
from app.models import CodingInput, CodingOutput

SYSTEM = """You are the Coding Agent in SwarmOS.

You receive one plan step, the current workspace files, and possibly review
findings or a failed verification log. Propose the file changes for this step.

Return ONLY a JSON object with this shape:
{
  "step_id": "the id of the assigned step",
  "summary": "one or two sentences describing the change",
  "rationale": "why this satisfies the acceptance criteria",
  "changes": [
    {"path": "relative/path.py", "content": "complete new file content", "action": "create"}
  ]
}

Rules:
- `content` must be the complete file after the change, not a diff.
- `path` must be relative to the workspace root. Never use absolute paths or `..`.
- Only touch files needed by this step. Prefer the step's target_files.
- If the step requires no file change, return an empty `changes` list.
- Do not claim to have run commands or tests; you cannot execute anything.
- If review findings are present, address each one you were given.
- If a verification log is present, fix the observed failure rather than rewriting
  unrelated code."""


class CodingAgent(Agent[CodingInput, CodingOutput]):
    role = "coder"
    input_model = CodingInput
    output_model = CodingOutput
    system = SYSTEM

    def parse(self, text: str) -> CodingOutput:
        output = super().parse(text)
        if output.step_id != output.step_id.strip() or not output.step_id:
            raise AgentOutputError("coder returned an empty step_id")
        return output

    def summarize(self, output: CodingOutput) -> dict[str, object]:
        return {
            "kind": "coding",
            "step_id": output.step_id,
            "summary": self._clip(output.summary),
            "rationale": self._clip(output.rationale),
            "changes": [change.path for change in output.changes[:20]],
            "change_count": len(output.changes),
        }