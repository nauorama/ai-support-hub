"""Tool permissions (G6, P7-03): allow-list per node, identity injection, argument validation,
limits, approval for side effects. Pure; the recorded side is in test_tool_client.py."""

import json
import uuid
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hub.tools.permissions import (
    ALLOWLIST,
    MAX_CALLS_PER_RUN,
    MAX_NAME_LENGTH,
    Allowed,
    Approval,
    ApprovalBasis,
    Caller,
    Denied,
    DenyReason,
    ToolNode,
    ToolRequest,
    ToolScope,
    authorize,
    idempotency_key,
)
from hub.tools.registry import (
    IDEMPOTENCY_KEY,
    IDENTITY_ARGUMENTS,
    MODEL_TOOLS,
    TOOLS,
    model_tool_schema,
    model_tool_schemas,
)

RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000a001")
TICKET_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000a002")
OTHER_TICKET_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000a003")
OPERATOR_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000a004")
ISSUE = {"issue_type": "dispute", "summary": "Card payment dispute", "description": "Check it."}
ALERT = {"urgency": "high", "text": "Urgent handoff: suspected account takeover."}


def scope(node: ToolNode = ToolNode.RESEARCH, customer_ref: str = "CUS-0001") -> ToolScope:
    return ToolScope(run_id=RUN_ID, ticket_id=TICKET_ID, customer_ref=customer_ref, node=node)


def model(name: str, **arguments: object) -> ToolRequest:
    return ToolRequest(name=name, arguments=arguments, caller=Caller.MODEL)


def code(name: str, **arguments: object) -> ToolRequest:
    return ToolRequest(name=name, arguments=arguments, caller=Caller.CODE)


def decide(
    request: ToolRequest,
    where: ToolScope | None = None,
    *,
    calls: int = 0,
    done: int = 0,
    approval: Approval | None = None,
) -> Allowed | Denied:
    return authorize(
        request,
        where or scope(),
        calls_in_run=calls,
        side_effects_for_ticket=done,
        approval=approval,
    )


def operator_approval(**changes: object) -> Approval:
    values: dict[str, object] = {
        "tool": "jira_create_issue",
        "ticket_id": TICKET_ID,
        "basis": ApprovalBasis.OPERATOR_REVIEW,
        "operator_id": OPERATOR_ID,
        "arguments": ISSUE,
    }
    return Approval.model_validate(values | changes)


def alert_approval(**changes: object) -> Approval:
    values: dict[str, object] = {
        "tool": "slack_post_escalation",
        "ticket_id": TICKET_ID,
        "basis": ApprovalBasis.URGENT_HANDOFF,
        "arguments": ALERT,
    }
    return Approval.model_validate(values | changes)


# --- the schema the model sees ---------------------------------------------------------------


def test_model_tools_are_the_four_read_tools_of_ai_pipeline_4() -> None:
    assert set(MODEL_TOOLS) == {"get_account", "get_transactions", "get_transaction", "get_cards"}
    assert not any(spec.side_effect for spec in MODEL_TOOLS.values())


@pytest.mark.parametrize("name", sorted(MODEL_TOOLS))
def test_model_facing_schema_has_no_customer_argument(name: str) -> None:
    schema = model_tool_schema(MODEL_TOOLS[name])
    properties = schema["function"]["parameters"].get("properties", {})
    assert not (IDENTITY_ARGUMENTS | {IDEMPOTENCY_KEY}) & set(properties)
    text = json.dumps(schema)
    assert "customer_ref" not in text
    assert "ticket_id" not in text
    assert "CUS-" not in text


def test_model_tool_schemas_are_in_a_stable_order() -> None:
    schemas = model_tool_schemas(frozenset(MODEL_TOOLS))
    assert [item["function"]["name"] for item in schemas] == sorted(MODEL_TOOLS)


def test_a_code_only_tool_has_no_model_schema() -> None:
    with pytest.raises(ValueError, match="not a model tool"):
        model_tool_schema(TOOLS["jira_create_issue"])


def test_side_effect_tools_are_never_model_tools() -> None:
    side_effects = {name for name, spec in TOOLS.items() if spec.side_effect}
    assert side_effects == {"jira_create_issue", "slack_post_escalation"}
    assert all(TOOLS[name].model_name is None for name in side_effects)


# --- identity injection ----------------------------------------------------------------------


def test_identity_is_injected_from_the_scope() -> None:
    decision = decide(model("get_transaction", reference="TX-000012"))
    assert isinstance(decision, Allowed)
    assert decision.spec.name == "core_get_transaction"
    assert decision.arguments == {"reference": "TX-000012", "customer_ref": "CUS-0001"}


@pytest.mark.parametrize("argument", ["customer_ref", "ticket_id", "idempotency_key"])
@pytest.mark.parametrize("caller", list(Caller))
def test_a_supplied_identity_argument_is_denied(argument: str, caller: Caller) -> None:
    name = "get_account" if caller is Caller.MODEL else "core_get_customer"
    request = ToolRequest(name=name, arguments={argument: "CUS-0002"}, caller=caller)
    decision = decide(request)
    assert decision == Denied("core_get_customer", DenyReason.IDENTITY_ARGUMENT, (argument,))


def test_a_model_supplied_customer_ref_is_denied_even_when_it_is_the_right_one() -> None:
    decision = decide(model("get_account", customer_ref="CUS-0001"))
    assert isinstance(decision, Denied)
    assert decision.reason is DenyReason.IDENTITY_ARGUMENT


@pytest.mark.parametrize("bad", ["", "CUS-1", "0193a0c2-7d5e-7a01-8b02-00000000c001", "CUS-0001 "])
def test_a_broken_scope_identity_is_denied(bad: str) -> None:
    decision = decide(model("get_cards"), scope(customer_ref=bad))
    assert decision == Denied("core_get_cards", DenyReason.INVALID_IDENTITY, ("customer_ref",))


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner),
    max_leaves=6,
)


@given(
    name=st.sampled_from(sorted(MODEL_TOOLS)),
    arguments=st.dictionaries(
        st.sampled_from(["customer_ref", "reference", "type", "status", "limit", "ticket_id"])
        | st.text(max_size=12),
        _JSON | st.just("CUS-0002") | st.just("TX-000012"),
        max_size=4,
    ),
)
def test_no_model_arguments_reach_another_customer(name: str, arguments: dict[str, Any]) -> None:
    decision = decide(ToolRequest(name=name, arguments=arguments, caller=Caller.MODEL))
    if isinstance(decision, Allowed):
        assert decision.arguments["customer_ref"] == "CUS-0001"
        assert not IDENTITY_ARGUMENTS & set(arguments)


# --- allow-list ------------------------------------------------------------------------------


def test_the_allow_lists() -> None:
    assert ALLOWLIST[ToolNode.RESEARCH] == {
        "core_get_customer",
        "core_list_transactions",
        "core_get_transaction",
        "core_get_cards",
    }
    assert ALLOWLIST[ToolNode.INTAKE] == {"core_get_customer"}
    assert ALLOWLIST[ToolNode.HANDOFF_PREP] == {"slack_post_escalation"}
    assert ALLOWLIST[ToolNode.ACT] == {"jira_create_issue"}
    assert set(ALLOWLIST) == set(ToolNode) == set(MAX_CALLS_PER_RUN)


@pytest.mark.parametrize(
    ("node", "name"),
    [
        (ToolNode.RESEARCH, "jira_create_issue"),
        (ToolNode.RESEARCH, "slack_post_escalation"),
        (ToolNode.RESEARCH, "jira_get_issue"),
        (ToolNode.INTAKE, "core_get_transaction"),
        (ToolNode.ACT, "core_get_customer"),
        (ToolNode.HANDOFF_PREP, "jira_create_issue"),
    ],
)
def test_a_tool_outside_the_allow_list_is_denied(node: ToolNode, name: str) -> None:
    decision = decide(code(name), scope(node), approval=operator_approval())
    assert decision == Denied(name, DenyReason.NOT_ALLOWED)


@pytest.mark.parametrize("name", ["jira_create_issue", "core_get_customer", "delete_everything"])
def test_a_model_can_name_only_model_tools(name: str) -> None:
    assert decide(model(name)) == Denied(name, DenyReason.UNKNOWN_TOOL)


def test_an_unknown_name_is_shortened() -> None:
    decision = decide(model("x" * 500))
    assert isinstance(decision, Denied)
    assert len(decision.tool) == MAX_NAME_LENGTH


# --- arguments and limits --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "fields"),
    [
        ({"reference": "TX-1"}, ("reference",)),
        ({}, ("reference",)),
        ({"reference": "TX-000001", "note": "x"}, ("note",)),
    ],
)
def test_invalid_arguments_are_denied_with_field_names(
    arguments: dict[str, Any], fields: tuple[str, ...]
) -> None:
    decision = decide(model("get_transaction", **arguments))
    assert decision == Denied("core_get_transaction", DenyReason.INVALID_ARGUMENTS, fields)


def test_list_arguments_are_validated_and_none_is_left_out() -> None:
    decision = decide(model("get_transactions", type="withdrawal_sepa", limit=5))
    assert isinstance(decision, Allowed)
    assert decision.arguments == {
        "type": "withdrawal_sepa",
        "limit": 5,
        "customer_ref": "CUS-0001",
    }
    assert isinstance(decide(model("get_transactions", limit=21)), Denied)


def test_the_research_loop_stops_at_five_calls() -> None:
    assert isinstance(decide(model("get_cards"), calls=4), Allowed)
    assert decide(model("get_cards"), calls=5) == Denied("core_get_cards", DenyReason.RUN_LIMIT)


def test_a_second_side_effect_for_the_ticket_is_denied() -> None:
    decision = decide(code("jira_create_issue", **ISSUE), scope(ToolNode.ACT), done=1)
    assert decision == Denied("jira_create_issue", DenyReason.TICKET_LIMIT)


# --- approval --------------------------------------------------------------------------------


def test_a_side_effect_tool_without_approval_is_denied() -> None:
    decision = decide(code("jira_create_issue", **ISSUE), scope(ToolNode.ACT))
    assert decision == Denied("jira_create_issue", DenyReason.APPROVAL_REQUIRED)
    alert = decide(code("slack_post_escalation", **ALERT), scope(ToolNode.HANDOFF_PREP))
    assert alert == Denied("slack_post_escalation", DenyReason.APPROVAL_REQUIRED)


def test_an_approved_jira_issue_is_allowed_with_identity_and_key() -> None:
    decision = decide(
        code("jira_create_issue", **ISSUE), scope(ToolNode.ACT), approval=operator_approval()
    )
    assert isinstance(decision, Allowed)
    key = idempotency_key("jira_create_issue", TICKET_ID)
    assert decision.arguments == ISSUE | {
        "customer_ref": "CUS-0001",
        "ticket_id": str(TICKET_ID),
        "idempotency_key": key,
    }
    assert decision.approved_by == OPERATOR_ID
    assert decision.idempotency_key == key


def test_an_urgent_handoff_alert_is_allowed_without_an_operator() -> None:
    decision = decide(
        code("slack_post_escalation", **ALERT),
        scope(ToolNode.HANDOFF_PREP),
        approval=alert_approval(),
    )
    assert isinstance(decision, Allowed)
    assert decision.arguments == ALERT | {
        "ticket_id": str(TICKET_ID),
        "idempotency_key": idempotency_key("slack_post_escalation", TICKET_ID),
    }
    assert decision.approved_by is None


@pytest.mark.parametrize(
    "approval",
    [
        operator_approval(ticket_id=OTHER_TICKET_ID),
        operator_approval(tool="slack_post_escalation"),
        operator_approval(basis=ApprovalBasis.URGENT_HANDOFF),
        operator_approval(operator_id=None),
        operator_approval(arguments=ISSUE | {"summary": "Something else"}),
        operator_approval(arguments={"issue_type": "nonsense"}),
    ],
    ids=["other-ticket", "other-tool", "wrong-basis", "no-operator", "other-args", "bad-args"],
)
def test_an_approval_for_something_else_is_denied(approval: Approval) -> None:
    decision = decide(code("jira_create_issue", **ISSUE), scope(ToolNode.ACT), approval=approval)
    assert decision == Denied("jira_create_issue", DenyReason.APPROVAL_MISMATCH)


def test_an_operator_approval_does_not_cover_the_slack_alert() -> None:
    approval = alert_approval(basis=ApprovalBasis.OPERATOR_REVIEW, operator_id=OPERATOR_ID)
    decision = decide(
        code("slack_post_escalation", **ALERT), scope(ToolNode.HANDOFF_PREP), approval=approval
    )
    assert decision == Denied("slack_post_escalation", DenyReason.APPROVAL_MISMATCH)
