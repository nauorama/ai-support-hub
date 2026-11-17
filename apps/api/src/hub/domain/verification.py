"""The result of the verify step (docs/ai-pipeline.md §5, P5-04).

The model fills in `ClaimsAnswer` only: the claims of the draft, each with the source it rests
on. `Verification` is built in code from that answer and the code checks
(`hub.guardrails.output_checks`), so the model never sets `groundedness` or `passed`. Field
descriptions go to the model as part of the strict schema (`hub.llm.structured`).
"""

from pydantic import BaseModel, ConfigDict, Field


class ClaimCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(description="One statement of fact of the answer, in a few words.")
    supported: bool = Field(description="True only when the named source states the claim.")
    source_id: str | None = Field(
        description="The source id that states the claim, exactly as written after `Source id:`;"
        " null when no source states it."
    )


class ClaimsAnswer(BaseModel):
    """What the verify model returns."""

    # `extra="forbid"`: a made-up field is a schema mismatch and goes through the repair call.
    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: list[ClaimCheck] = Field(description="Every claim of the answer, in order.")


class Verification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: list[ClaimCheck]
    # Supported claims / all claims; None when the LLM check did not run (a code check failed).
    groundedness: float | None
    policy_violations: list[str]  # codes from the code checks, never text of the draft
    passed: bool

    @property
    def unsupported(self) -> list[ClaimCheck]:
        return [claim for claim in self.claims if not claim.supported]
