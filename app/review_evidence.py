"""Deterministic evidence checks for reviewer findings.

These checks validate the *shape* of the evidence a finding supplies. They do
not, and cannot, decide whether the claim is true:

* a finding can cite a real line, name a real criterion, and still be wrong;
* a passing check is format compliance, not proof of a defect;
* a failing check means the finding cannot be acted on as a blocker, not that
  the code is correct.

They exist because unsupported blockers recurred with a small local model: a
reviewer rejected correct code (run_478959ca3d86, run_9038fdabb1b9) by
restating the acceptance criteria, and once by reporting a fabricated
expected/actual pair. The orchestrator uses this module to avoid dispatching a
revision for a blocker that carries no minimum evidence. It never turns an
unsubstantiated blocker into approval.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.models import ReviewFinding, ReviewOutput

_MIN_CRITERION_CHARS = 15

_LINE_REF = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_FILE_LINE_REF = re.compile(r"\b[\w./\\-]+\.[A-Za-z0-9]{1,6}:\d+\b")
_BACKTICK = re.compile(r"`([^`\n]{2,})`")
_QUOTED = re.compile(r"[\"'`]([^\"'`\n]{%d,})[\"'`]" % _MIN_CRITERION_CHARS)
# Something expression-like inside backticks, e.g. `sorted(...)`, `x == y`, or
# `n = 0`. A path (`tests/test_target.py`) or a bare name (`normalize_tags`) must
# not qualify, so separators like `/`, `-`, and `*` are not enough on their own.
_EXPRESSION = re.compile(r"[()\[\]{}]|==|!=|<=|>=|->|(?<![=<>!])=(?!=)")
_RUNTIME_CLAIM = re.compile(
    r"\b(expected|actual|returns?|returned|output|outputs|raises?|raised|fails?|failed)\b",
    re.IGNORECASE,
)
_EXPECTED = re.compile(r"\bexpected\b", re.IGNORECASE)
_ACTUAL = re.compile(r"\bactual\b", re.IGNORECASE)


def _normalise(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .,\"'")


@dataclass(frozen=True)
class EvidenceCheck:
    """The evidence-shape assessment of a single finding."""

    finding: ReviewFinding
    has_code_reference: bool
    has_acceptance_criterion: bool
    has_relevant_file: bool
    claims_runtime_behaviour: bool
    has_expected_actual: bool

    @property
    def missing(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if not self.has_code_reference:
            gaps.append("code reference or line")
        if not self.has_acceptance_criterion:
            gaps.append("named acceptance criterion")
        if not self.has_relevant_file:
            gaps.append("relevant workspace file")
        if self.claims_runtime_behaviour and not self.has_expected_actual:
            gaps.append("expected/actual behaviour")
        return tuple(gaps)

    @property
    def is_supported(self) -> bool:
        """Format compliance only. True does not mean the finding is correct."""
        return not self.missing


@dataclass(frozen=True)
class ReviewEvidenceReport:
    checks: tuple[EvidenceCheck, ...]

    @property
    def unsupported_blockers(self) -> tuple[EvidenceCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if check.finding.severity == "blocker" and not check.is_supported
        )

    @property
    def supported_findings(self) -> tuple[EvidenceCheck, ...]:
        return tuple(check for check in self.checks if check.is_supported)

    @property
    def is_invalid_rejection(self) -> bool:
        """A rejection that rests on nothing but unsubstantiated blockers.

        An invalid rejection must never become approval, and it must not trigger
        a coding revision, because there is no actionable evidence to act on.
        """
        return bool(self.unsupported_blockers) and not self.supported_findings


def evaluate_finding(
    finding: ReviewFinding,
    *,
    acceptance_criteria: Iterable[str] = (),
    files: Mapping[str, str] | None = None,
) -> EvidenceCheck:
    message = finding.message or ""
    criteria = [criterion for criterion in acceptance_criteria if criterion]
    known_files = set(files or {})

    return EvidenceCheck(
        finding=finding,
        has_code_reference=_has_code_reference(message),
        has_acceptance_criterion=_has_acceptance_criterion(message, criteria),
        has_relevant_file=(
            finding.file is None or not known_files or finding.file in known_files
        ),
        claims_runtime_behaviour=bool(_RUNTIME_CLAIM.search(message)),
        has_expected_actual=bool(
            _EXPECTED.search(message) and _ACTUAL.search(message)
        ),
    )


def validate_review(
    review: ReviewOutput,
    *,
    acceptance_criteria: Iterable[str] = (),
    files: Mapping[str, str] | None = None,
) -> ReviewEvidenceReport:
    return ReviewEvidenceReport(
        checks=tuple(
            evaluate_finding(
                finding, acceptance_criteria=acceptance_criteria, files=files
            )
            for finding in review.findings
        )
    )


def _has_code_reference(message: str) -> bool:
    if _LINE_REF.search(message) or _FILE_LINE_REF.search(message):
        return True
    for span in _BACKTICK.findall(message):
        # A bare path (`target.py`) or a bare identifier (`normalize_tags`) is
        # not a code reference; require something expression-like.
        if _EXPRESSION.search(span):
            return True
    return False


def _has_acceptance_criterion(message: str, criteria: list[str]) -> bool:
    normalised_message = _normalise(message)
    for criterion in criteria:
        candidate = _normalise(criterion)
        if len(candidate) >= _MIN_CRITERION_CHARS and candidate in normalised_message:
            return True
    for span in _QUOTED.findall(message):
        quoted = _normalise(span)
        if len(quoted) < _MIN_CRITERION_CHARS:
            continue
        if any(quoted in _normalise(criterion) for criterion in criteria):
            return True
    return False