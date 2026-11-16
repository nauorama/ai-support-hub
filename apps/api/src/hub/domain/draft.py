"""The result of the write step (docs/ai-pipeline.md §5).

Field descriptions go to the model as part of the strict schema (`hub.llm.structured`), so they
are short and say what the field means. In baseline B0 (P3-05) a citation points to a knowledge
base document id (`withdrawal-times`, `error-codes`); from phase 5 it points to a chunk id or an
account fact id.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IssueType = Literal["dispute", "fraud", "bug", "limit_request"]


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    marker: int = Field(ge=1, description="The number n of a [n] marker in the answer.")
    source_id: str = Field(description="The id of the source document the marker points to.")


class ProposedAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["jira_create_issue"]
    issue_type: IssueType
    summary: str = Field(description="One line for the support operator.")
    description: str = Field(description="What the operator should check or do.")


class DraftAnswer(BaseModel):
    # `extra="forbid"`: a made-up field is a schema mismatch and goes through the repair call.
    model_config = ConfigDict(frozen=True, extra="forbid")

    can_answer: bool = Field(description="False when the sources do not contain the answer.")
    missing_information: str | None = Field(
        description="What is missing to answer; null when can_answer is true."
    )
    answer: str = Field(
        description="The reply to the customer with [n] markers; empty when can_answer is false."
    )
    citations: list[Citation] = Field(description="One entry for every [n] marker in the answer.")
    proposed_actions: list[ProposedAction] = Field(
        description="Issues a support operator should open; usually empty."
    )
