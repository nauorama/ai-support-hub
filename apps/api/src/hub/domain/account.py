"""What the Hub keeps from the core system's tools (ai-pipeline §2, §4; P7-04).

- `CustomerProfile`: the part of `core_get_customer` that intake keeps for the run: tier,
  country and the `restricted` flag (row 1, `customer_restricted`). Never the reason of a
  restriction: the core mock has no such field, and the Hub would drop one (tipping-off rule,
  world file §6, AGENTS.md never-do 5).
- `AccountFact`: one fact about the customer's account that the writer may cite (`A1`, `A2`,
  ...), built in code from a tool result (`hub.tools.facts`), never written by a model.
"""

import re

from pydantic import BaseModel, ConfigDict, Field

FACT_ID = re.compile(r"^A[1-9]\d*$")


class CustomerProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str
    country: str
    restricted: bool


class AccountFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(pattern=FACT_ID.pattern)
    source_tool: str  # the MCP tool name, e.g. `core_get_transaction`
    text: str  # masked (G1)
