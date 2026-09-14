"""Query/artifact generation with a bounded validate-and-repair loop
(Phase 4 + Phase 5 auto-repair).

Given a generated artifact (SQL / VRL / PromQL / regex), validate it. If it
fails, feed the concrete validator error back to the model for a bounded number
of repair attempts. If it still fails, DOWNGRADE: return the last attempt marked
as unvalidated, never presenting it as valid.

This closes the "generation -> validation -> repair" loop so grounding and
correctness are enforced on generated artifacts, not just trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .llm import LLMProvider
from .validator import (
    PromQLValidator,
    RegexValidator,
    SQLValidator,
    VRLValidator,
    ValidationResult,
)


class ArtifactKind(str, Enum):
    SQL = "sql"
    VRL = "vrl"
    PROMQL = "promql"
    REGEX = "regex"


class GenStatus(str, Enum):
    VALIDATED = "validated"
    REPAIRED = "repaired"
    UNVALIDATED = "unvalidated"  # downgraded after repair budget exhausted


@dataclass
class GenerationResult:
    kind: ArtifactKind
    artifact: str
    status: GenStatus
    attempts: int
    last_error: str | None = None
    warnings: list[str] | None = None


_REPAIR_PROMPT = (
    "The following {kind} failed validation. Fix it and return ONLY the corrected "
    "{kind}, with no explanation, no code fences.\n\n"
    "{kind} :\n{artifact}\n\nValidator error:\n{error}"
)


class GenerationWorkflow:
    """Validate-and-repair for a single artifact. The caller supplies the
    initial artifact (usually produced by the model); this loop enforces it."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        sql_validator: SQLValidator | None = None,
        vrl_validator: VRLValidator | None = None,
        promql_validator: PromQLValidator | None = None,
        max_repairs: int = 2,
    ):
        self.llm = llm
        self.sqlv = sql_validator
        self.vrlv = vrl_validator
        self.promqlv = promql_validator
        self.max_repairs = max_repairs

    def run(
        self,
        kind: ArtifactKind,
        artifact: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        stream_type: str = "logs",
        sample_events: list[dict] | None = None,
    ) -> GenerationResult:
        attempts = 0
        last_error: str | None = None
        current = artifact.strip()

        while attempts <= self.max_repairs:
            vr = self._validate(kind, current, start_time, end_time, stream_type, sample_events)
            if vr.ok:
                status = GenStatus.VALIDATED if attempts == 0 else GenStatus.REPAIRED
                return GenerationResult(
                    kind=kind, artifact=current, status=status, attempts=attempts,
                    warnings=vr.warnings or None,
                )
            last_error = vr.as_message()
            if attempts == self.max_repairs:
                break
            current = self._repair(kind, current, last_error)
            attempts += 1

        # downgraded: never claim validity
        return GenerationResult(
            kind=kind, artifact=current, status=GenStatus.UNVALIDATED,
            attempts=attempts, last_error=last_error,
        )

    def _validate(
        self, kind: ArtifactKind, artifact: str,
        start_time: int | None, end_time: int | None,
        stream_type: str, sample_events: list[dict] | None,
    ) -> ValidationResult:
        if kind is ArtifactKind.SQL:
            if not self.sqlv:
                return ValidationResult(ok=False, errors=["no SQL validator configured"])
            return self.sqlv.validate(artifact, start_time=start_time,
                                      end_time=end_time, stream_type=stream_type)
        if kind is ArtifactKind.VRL:
            if not self.vrlv:
                return ValidationResult(ok=False, errors=["no VRL validator configured"])
            return self.vrlv.validate(artifact, sample_events)
        if kind is ArtifactKind.PROMQL:
            if not self.promqlv:
                return ValidationResult(ok=False, errors=["no PromQL validator configured"])
            return self.promqlv.validate(artifact)
        if kind is ArtifactKind.REGEX:
            return RegexValidator.validate(artifact)
        return ValidationResult(ok=False, errors=[f"unknown artifact kind: {kind}"])

    def _repair(self, kind: ArtifactKind, artifact: str, error: str) -> str:
        prompt = _REPAIR_PROMPT.format(kind=kind.value, artifact=artifact, error=error)
        resp = self.llm.chat(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1024
        )
        fixed = (resp.content or "").strip()
        # strip accidental code fences
        if fixed.startswith("```"):
            fixed = fixed.strip("`")
            if "\n" in fixed:
                fixed = fixed.split("\n", 1)[1]
        return fixed.strip() or artifact
