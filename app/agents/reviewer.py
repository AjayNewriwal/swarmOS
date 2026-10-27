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
- Judge only the actual implementation in `files` against the plan's acceptance
  criteria and any tests present in the workspace. Do not invent requirements
  that are not in the acceptance criteria or the tests.
- Every finding MUST contain, inside its `message`:
  1. the specific file and the line number or exact code expression at fault;
  2. the acceptance criterion it violates, quoted from the plan;
  3. a concrete input and the expected versus actual behaviour, when the claim is
     about runtime behaviour;
  4. why that evidence demonstrates a real defect.
- Never reject work by restating an acceptance criterion. "The function does not
  do X" is not a finding unless you can point at the line that fails X and show
  the failing input and output.
- If you cannot substantiate an issue with that evidence, do not report it as a
  blocker. Omit it, or at most record it as a nit.
- Approve only when you have no substantiated blocking defect: either every
  acceptance criterion is satisfied, or you cannot substantiate a violation with
  the evidence above. Do not request changes because you would have written the
  code differently.
- Any blocker finding forces a request_changes verdict.
- Do not speculate about code you were not shown.
- Point at the responsible plan step when you can."""

_MAX_FILE_CHARS = 12_000


class ReviewAgent(Agent[ReviewInput, ReviewOutput]):
    role = "reviewer"
    input_model = ReviewInput
    output_model = ReviewOutput
    system = SYSTEM

    def summarize(self, output: ReviewOutput) -> dict[str, object]:
        return {
            "kind": "review",
            "verdict": output.verdict,
            "rationale": self._clip(output.rationale),
            "findings": [
                {
                    "severity": finding.severity,
                    "step_id": finding.step_id,
                    "file": finding.file,
                    "message": self._clip(finding.message),
                }
                for finding in output.findings[: self.summary_finding_limit]
            ],
            "finding_count": len(output.findings),
        }

    def build_request(
        self, payload: ReviewInput, model: str, repair_note: str | None = None
    ) -> ModelRequest:
        data = payload.model_dump(mode="json")
        files: dict[str, str] = data.get("files") or {}
        rendered = {
            path: (content[:_MAX_FILE_CHARS] + "\n... (truncated)")
            if len(content) > _MAX_FILE_CHARS
            else content
            for path, content in files.items()
        }
        data["files"] = rendered
        data["note"] = (
            "files is the complete workspace snapshot after the changes were "
            "applied. It includes the project's tests: judge the implementation "
            "against those tests and the acceptance criteria, and do not invent "
            "requirements beyond them."
        )
        if repair_note:
            data["_output_repair"] = (
                "Your previous reply could not be parsed. Return only the JSON "
                f"object described by the system prompt. Parser error: {repair_note}"
            )
        return ModelRequest(
            role=self.role,
            system=self.system,
            user=json.dumps(data, indent=2),
            model=model,
            json_mode=True,
        )