"""The guarded tool client (P7-03): every call is decided by the permissions and recorded, and
no call can reach another customer's data. A fake transport stands in for the MCP server; the
real server is used in tests/integration/test_tool_calls.py."""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from hub.domain.enums import ToolCallStatus
from hub.tools.mcp_client import (
    MAX_ARGUMENTS_JSON,
    ToolCallRow,
    ToolClient,
    TransportResult,
    audit_details,
    external_ref,
    result_summary,
    stored_arguments,
)
from hub.tools.permissions import (
    Approval,
    ApprovalBasis,
    Caller,
    ToolNode,
    ToolScope,
)

RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000b001")
TICKET_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000b002")
OPERATOR_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000b003")
VISA = "4111 1111 1111 1111"  # public test card number
ISSUE = {"issue_type": "dispute", "summary": "Card payment dispute", "description": "Check it."}

# The core mock's ownership, in small: whose transaction is whose.
OWNERS = {"TX-000001": "CUS-0001", "TX-000012": "CUS-0002"}


@dataclass
class FakeCore:
    """Answers like the server: another customer's transaction is "transaction not found"."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: Exception | None = None

    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float) -> TransportResult:
        self.calls.append((name, arguments))
        if self.fail is not None:
            raise self.fail
        customer = arguments.get("customer_ref")
        if name == "core_get_transaction":
            if OWNERS.get(arguments["reference"]) != customer:
                return TransportResult(True, None, "transaction not found")
            return TransportResult(False, {"reference": arguments["reference"], "status": "ok"})
        if name == "core_get_customer":
            return TransportResult(False, {"customer_ref": customer, "note": f"card {VISA}"})
        if name == "jira_create_issue":
            return TransportResult(False, {"key": "TB-1", "url": "u", "created": True})
        return TransportResult(False, {"customer_ref": customer})


@dataclass
class MemoryRecorder:
    rows: list[ToolCallRow] = field(default_factory=list)
    operators: set[uuid.UUID] = field(default_factory=lambda: {OPERATOR_ID})

    async def operator_exists(self, operator_id: uuid.UUID) -> bool:
        return operator_id in self.operators

    async def side_effects_done(self, ticket_id: uuid.UUID, tool: str) -> int:
        return sum(
            1
            for row in self.rows
            if row.scope.ticket_id == ticket_id
            and row.tool == tool
            and row.status is ToolCallStatus.OK
        )

    async def record(self, row: ToolCallRow) -> None:
        self.rows.append(row)


def scope(node: ToolNode = ToolNode.RESEARCH, customer_ref: str = "CUS-0001") -> ToolScope:
    return ToolScope(run_id=RUN_ID, ticket_id=TICKET_ID, customer_ref=customer_ref, node=node)


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
def recorder() -> MemoryRecorder:
    return MemoryRecorder()


@pytest.fixture
def client(core: FakeCore, recorder: MemoryRecorder) -> ToolClient:
    return ToolClient(core, recorder, timeout_s=1.0)


# --- no call can reach another customer's data ------------------------------------------------


async def test_a_model_supplied_customer_ref_is_denied_recorded_and_never_sent(
    client: ToolClient, core: FakeCore, recorder: MemoryRecorder
) -> None:
    session = client.session(scope())
    outcome = await session.call(
        "get_transaction",
        {"reference": "TX-000012", "customer_ref": "CUS-0002"},
        caller=Caller.MODEL,
    )
    assert outcome.status is ToolCallStatus.DENIED
    assert outcome.error == "identity_argument"
    assert core.calls == []
    [row] = recorder.rows
    assert row.status is ToolCallStatus.DENIED
    assert row.reason == "identity_argument"
    assert row.fields == ("customer_ref",)
    # Names only: the other customer's reference is not stored.
    assert row.arguments == {"names": ["customer_ref", "reference"]}
    details = audit_details(row, uuid.uuid4())
    assert details["reason"] == "identity_argument"
    assert "CUS-0002" not in repr(details)


async def test_another_customers_transaction_is_not_found(
    client: ToolClient, core: FakeCore
) -> None:
    session = client.session(scope())
    other = await session.call("get_transaction", {"reference": "TX-000012"}, caller=Caller.MODEL)
    own = await session.call("get_transaction", {"reference": "TX-000001"}, caller=Caller.MODEL)
    assert other.status is ToolCallStatus.ERROR
    assert other.result is None
    assert own.status is ToolCallStatus.OK
    assert [arguments["customer_ref"] for _, arguments in core.calls] == ["CUS-0001", "CUS-0001"]


@pytest.mark.parametrize("name", ["get_account", "get_transactions", "get_cards"])
async def test_every_model_tool_is_sent_with_the_scope_customer(
    client: ToolClient, core: FakeCore, name: str
) -> None:
    session = client.session(scope(customer_ref="CUS-0003"))
    outcome = await session.call(name, caller=Caller.MODEL)
    assert outcome.status is ToolCallStatus.OK
    assert core.calls[0][1]["customer_ref"] == "CUS-0003"


# --- allow-list, limits, approval -------------------------------------------------------------


async def test_a_tool_outside_the_allow_list_is_denied_and_recorded(
    client: ToolClient, core: FakeCore, recorder: MemoryRecorder
) -> None:
    outcome = await client.session(scope()).call("jira_create_issue", ISSUE)
    assert outcome.status is ToolCallStatus.DENIED
    assert outcome.error == "not_allowed"
    assert core.calls == []
    assert recorder.rows[0].reason == "not_allowed"
    assert recorder.rows[0].tool == "jira_create_issue"


async def test_a_side_effect_tool_without_approval_is_denied(
    client: ToolClient, core: FakeCore
) -> None:
    outcome = await client.session(scope(ToolNode.ACT)).call("jira_create_issue", ISSUE)
    assert outcome.error == "approval_required"
    assert core.calls == []


async def test_an_approved_issue_runs_once_per_ticket(
    client: ToolClient, core: FakeCore, recorder: MemoryRecorder
) -> None:
    approval = Approval(
        tool="jira_create_issue",
        ticket_id=TICKET_ID,
        basis=ApprovalBasis.OPERATOR_REVIEW,
        operator_id=OPERATOR_ID,
        arguments=ISSUE,
    )
    first = await client.session(scope(ToolNode.ACT)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert first.status is ToolCallStatus.OK
    row = recorder.rows[0]
    assert row.side_effect
    assert row.approved_by == OPERATOR_ID
    assert row.external_ref == "TB-1"
    assert row.idempotency_key == f"jira_create_issue:{TICKET_ID}"
    details = audit_details(row, uuid.uuid4())
    assert details["approved_by"] == OPERATOR_ID
    assert details["external_ref"] == "TB-1"
    assert details["caller"] == "code"
    assert "summary" not in details
    # A later run of the same ticket cannot create a second issue.
    second = await client.session(scope(ToolNode.ACT)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert second.error == "ticket_limit"
    assert len(core.calls) == 1


async def test_the_session_counts_denied_calls_towards_the_limit(
    client: ToolClient, core: FakeCore
) -> None:
    session = client.session(scope())
    for _ in range(4):
        await session.call("nope", caller=Caller.MODEL)
    assert (await session.call("get_cards", caller=Caller.MODEL)).status is ToolCallStatus.OK
    assert (await session.call("get_cards", caller=Caller.MODEL)).error == "run_limit"
    assert session.calls == 6
    assert len(core.calls) == 1


# --- errors and what is stored ----------------------------------------------------------------


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError("CUS-0002 secret text")])
async def test_a_transport_failure_is_an_error_outcome_with_the_class_only(
    core: FakeCore, recorder: MemoryRecorder, failure: Exception
) -> None:
    core.fail = failure
    session = ToolClient(core, recorder).session(scope())
    outcome = await session.call("get_cards", caller=Caller.MODEL)
    assert outcome.status is ToolCallStatus.ERROR
    assert outcome.error == type(failure).__name__
    row = recorder.rows[0]
    assert row.status is ToolCallStatus.ERROR
    assert row.idempotency_key is None
    assert "secret" not in repr(row)


async def test_a_slow_transport_is_cut_by_the_timeout(recorder: MemoryRecorder) -> None:
    class Slow:
        async def call(
            self, name: str, arguments: dict[str, Any], timeout_s: float
        ) -> TransportResult:
            await asyncio.sleep(timeout_s * 10)
            raise AssertionError("not cut")

    client = ToolClient(Slow(), recorder, timeout_s=0.01)
    outcome = await client.session(scope()).call("get_cards", caller=Caller.MODEL)
    assert outcome.error == "TimeoutError"


async def test_an_error_without_structured_content_is_an_error(
    recorder: MemoryRecorder,
) -> None:
    class Empty:
        async def call(
            self, name: str, arguments: dict[str, Any], timeout_s: float
        ) -> TransportResult:
            return TransportResult(False, None)

    outcome = await ToolClient(Empty(), recorder).session(scope()).call("core_get_cards")
    assert outcome.error == "no structured result"


async def test_the_stored_summary_is_masked_but_the_result_is_not(
    client: ToolClient, recorder: MemoryRecorder
) -> None:
    outcome = await client.session(scope()).call("get_account", caller=Caller.MODEL)
    assert outcome.result is not None
    summary = recorder.rows[0].result_summary
    assert summary is not None
    assert "4111 1111" not in summary
    assert "[CARD ****1111]" in summary


def test_stored_arguments_are_masked_or_reduced_to_names() -> None:
    assert stored_arguments({"text": f"card {VISA}"}) == {"text": "card [CARD ****1111]"}
    assert stored_arguments({"text": "a\x00b"}) == {"names": ["text"]}
    assert stored_arguments({"text": "x" * MAX_ARGUMENTS_JSON}) == {"names": ["text"]}
    assert stored_arguments({"value": object()}) == {"names": ["value"]}


def test_result_helpers() -> None:
    assert result_summary(None) is None
    assert result_summary({"a": "x" * 1000}) is not None
    assert len(result_summary({"a": "x" * 1000}) or "") == 300
    assert external_ref(None) is None
    assert external_ref({"key": "TB-7"}) == "TB-7"
    assert external_ref({"ts": "1.2", "created": True}) == "1.2"
    assert external_ref({"created": True}) is None


async def test_a_card_number_as_tool_name_is_never_stored(
    client: ToolClient, core: FakeCore, recorder: MemoryRecorder
) -> None:
    outcome = await client.session(scope()).call(VISA, caller=Caller.MODEL)
    assert outcome.tool == "unknown"
    assert outcome.error == "unknown_tool"
    assert recorder.rows[0].tool == "unknown"
    assert "4111" not in repr(recorder.rows[0])
    assert core.calls == []


async def test_an_approval_by_an_unknown_operator_is_denied(
    client: ToolClient, core: FakeCore, recorder: MemoryRecorder
) -> None:
    approval = Approval(
        tool="jira_create_issue",
        ticket_id=TICKET_ID,
        basis=ApprovalBasis.OPERATOR_REVIEW,
        operator_id=uuid.uuid4(),
        arguments=ISSUE,
    )
    outcome = await client.session(scope(ToolNode.ACT)).call(
        "jira_create_issue", ISSUE, approval=approval
    )
    assert outcome.error == "unknown_operator"
    assert core.calls == []
