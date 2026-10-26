from __future__ import annotations

import json

from app.agents.base import Agent
from app.models import ReviewInput, ReviewOutput
from app.providers.base import ModelRequest

SYSTEM = """You are the Review Agent in SwarmOS.

Inspect the proposed changes against the plan's acceptance criteria and the final
workspace contents. Look for correctness bugs, unhandled edge cases, and
requirement mismatches. Do not rewrite the code yourself.

Return ONLY a JSON object with this shape:
{
  "verdict": "approve" | "request_changes",
  "rationale": "why you reached this verdict",
  "findings": [
    {
      "severity": "blocker" | "major" | "minor" | "nit",
      "step_id": "id of the step responsible",
      "file": "relative/path.py",
      "message": "specific, actionable description"
    }
  ]
}

Rules:
- Approve only if every acceptance criterion is satisfied by the workspace.
- Any blocker finding forces a request_changes verdict.
- Findings must be specific; do not speculate about code you were not shown.
- Point at the responsible plan step when you can."""

_MAX_FILE_CHARS = 12_000


class ReviewAgent(Agent[ReviewInput, ReviewOutput]):
    role = "reviewer"
    input_model = ReviewInput
    output_model = ReviewOutput
    system = SYSTEM

    def build_request(self, payload: ReviewInput, model: str) -> ModelRequest:
        data = payload.model_dump(mode="json")
        files: dict[str, str] = data.get("files") or {}
        rendered = {
            path: (content[:_MAX_FILE_CHARS] + "\n... (truncated)")
            if len(content) > _MAX_FILE_CHARS
            else content
            for path, content in files.items()
        }
        data["files"] = rendered
        data["note"] = "files is the complete workspace snapshot after the changes were applied."
        return ModelRequest(
            role=self.role,
            system=self.system,
            user=json.dumps(data, indent=2),
            model=model,
            json_mode=True,
        )