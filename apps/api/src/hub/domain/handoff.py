"""The operator summary of a handoff (docs/ai-pipeline.md §3 and §7, P6-02).

The model fills in `HandoffSummary`; the code joins the three fields into three lines, G1-masks
them and stores them in `tickets.summary` for operators only (data model §1). Field descriptions
go to the model as part of the strict schema (`hub.llm.structured`).
"""

from pydantic import BaseModel, ConfigDict, Field

LINE_MAX_CHARS = 200


class HandoffSummary(BaseModel):
    # `extra="forbid"`: a made-up field is a schema mismatch and goes through the repair call.
    model_config = ConfigDict(frozen=True, extra="forbid")

    problem: str = Field(
        max_length=LINE_MAX_CHARS, description="What the customer needs, with the key facts."
    )
    found: str = Field(
        max_length=LINE_MAX_CHARS, description="What the assistant found that helps."
    )
    missing: str = Field(
        max_length=LINE_MAX_CHARS, description="What the operator still has to check or ask."
    )

    def lines(self) -> str:
        return f"Problem: {self.problem}\nFound: {self.found}\nMissing: {self.missing}"
