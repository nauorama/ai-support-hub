"""`llm_calls` and the G2 audit record against Postgres, written by `DbCallRecorder` (app role).

The recorder commits (a failed call must stay recorded when the caller rolls back), so the tests
use a database of their own, like test_audit_log.py.
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import psycopg
import pytest
from psycopg import sql
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.audit import ChainCheck, verify_chain
from hub.db.models import AuditLog, LlmCall
from hub.db.session import Database, connect
from hub.domain.enums import ActorType, LlmCallContext, LlmCallStatus, LlmNode
from hub.llm.config import load_models
from hub.llm.gateway import FixtureProvider, Gateway
from hub.llm.pricing import load_pricing
from hub.llm.recorder import BLOCKED_ACTION, DbCallRecorder
from hub.llm.schemas import (
    CallContext,
    LlmCallRecord,
    OutboundCardDataError,
    OutboundIbanError,
    ProviderRequest,
    ProviderResponse,
    StructuredOutputError,
    Usage,
)
from hub.observability.readiness import libpq_url

from ._compose import ROOT
from ._db import app_url, as_user, migrate

DATABASE = "hub_llm_calls_test"
VISA = "4111 1111 1111 1111"  # public test card number
# Fixed run ids for the tests that check the audit details for card or IBAN digits: the run id
# is in those details, and a random uuid can contain "4111" (it did in a CI run).
BLOCKED_RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000c0de")
IBAN_RUN_ID = uuid.UUID("0193a0c2-7d5e-7a01-8b02-00000000beef")


@pytest.fixture(scope="module")
def llm_db(postgres_url: str) -> Iterator[tuple[str, str]]:
    """(migrator URL, app URL) of a migrated database of its own."""
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    migrator = as_user(postgres_url, "hub", "hub", database=DATABASE)
    migrate(migrator)
    yield migrator, app_url(postgres_url, database=DATABASE)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


@pytest.fixture
async def database(llm_db: tuple[str, str]) -> AsyncIterator[Database]:
    with psycopg.connect(libpq_url(llm_db[0]), autocommit=True) as connection:
        connection.execute("TRUNCATE llm_calls, audit_log RESTART IDENTITY")
    db = connect(llm_db[1])
    yield db
    await db.dispose()


def _record(**changes: object) -> LlmCallRecord:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "context": LlmCallContext.ONLINE,
        "node": LlmNode.TRIAGE,
        "model": "anthropic/claude-haiku-5-5",
        "prompt_version": "v1",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cached_tokens": 400,
        "cost_usd": Decimal("0.000164"),
        "latency_ms": 250,
        "status": LlmCallStatus.OK,
        "error": None,
    }
    return LlmCallRecord.model_validate(values | changes)


async def test_a_call_row_round_trips_as_the_app_role(database: Database) -> None:
    row = _record()
    await DbCallRecorder(database).record(row)
    async with database.sessions() as session:
        stored = await session.get(LlmCall, row.id)
    assert stored is not None
    assert LlmCallRecord.model_validate(stored, from_attributes=True) == row
    assert stored.created_at is not None


async def test_an_embedding_row_has_no_prompt_version(database: Database) -> None:
    row = _record(node=LlmNode.EMBEDDING, prompt_version=None, run_id=None)
    await DbCallRecorder(database).record(row)
    async with database.sessions() as session:
        stored = await session.get(LlmCall, row.id)
    assert stored is not None
    assert stored.prompt_version is None


@pytest.mark.parametrize(
    "changes",
    [{"input_tokens": -1}, {"cost_usd": Decimal("-0.01")}, {"latency_ms": -5}],
    ids=["tokens", "cost", "latency"],
)
async def test_the_database_refuses_negative_values(
    database: Database, changes: dict[str, object]
) -> None:
    row = _record().model_copy(update=changes)
    with pytest.raises(IntegrityError):
        await DbCallRecorder(database).record(row)


async def test_a_blocked_call_writes_its_row_and_a_chained_audit_record(
    database: Database,
) -> None:
    gateway = Gateway(
        models=load_models(ROOT / "config" / "models.yaml"),
        pricing=load_pricing(ROOT / "config" / "pricing.yaml"),
        provider=FixtureProvider(ROOT / "apps" / "api" / "tests" / "fixtures" / "llm"),
        recorder=DbCallRecorder(database),
    )
    run_id = BLOCKED_RUN_ID
    assert "4111" not in str(run_id)  # else the leak check below could fail on the id itself
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.WRITE, run_id=run_id)

    with pytest.raises(OutboundCardDataError):
        await gateway.complete(
            "write", [{"role": "user", "content": f"my card is {VISA}"}], call=call
        )

    async with database.sessions() as session:
        [row] = (await session.scalars(select(LlmCall))).all()
        [audit] = (await session.scalars(select(AuditLog))).all()
        chain = await verify_chain(session)

    assert row.status is LlmCallStatus.ERROR
    assert row.error == "blocked: G2"
    assert row.run_id == run_id
    assert row.cost_usd == 0
    assert audit.action == BLOCKED_ACTION
    assert audit.actor_type is ActorType.SYSTEM
    assert audit.entity_type == "llm_call"
    assert audit.entity_id == str(row.id)
    assert audit.details["guardrail"] == "G2"
    assert audit.details["node"] == "write"
    assert audit.details["entities"] == {"PAN": 1}
    assert audit.details["reason"] == "card_data"
    assert audit.details["run_id"] == str(run_id)
    assert "4111" not in str(audit.details)
    assert chain == ChainCheck(ok=True, checked=1)


async def test_a_call_blocked_for_a_raw_iban_says_so_in_its_row_and_audit_record(
    database: Database,
) -> None:
    gateway = Gateway(
        models=load_models(ROOT / "config" / "models.yaml"),
        pricing=load_pricing(ROOT / "config" / "pricing.yaml"),
        provider=FixtureProvider(ROOT / "apps" / "api" / "tests" / "fixtures" / "llm"),
        recorder=DbCallRecorder(database),
    )
    assert "37040044" not in str(IBAN_RUN_ID)  # same reason as in the test above
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.WRITE, run_id=IBAN_RUN_ID)

    with pytest.raises(OutboundIbanError):
        await gateway.complete(
            "write", [{"role": "user", "content": "refund to DE89370400440532013000"}], call=call
        )

    async with database.sessions() as session:
        [row] = (await session.scalars(select(LlmCall))).all()
        [audit] = (await session.scalars(select(AuditLog))).all()

    assert row.error == "blocked: G2 IBAN"
    assert audit.action == BLOCKED_ACTION
    assert audit.details["reason"] == "iban"
    assert audit.details["entities"] == {"IBAN": 1}
    assert "37040044" not in str(audit.details)


# --- structured output (P3-02): invalid answers are rows with status invalid_output ----------


class _Verdict(BaseModel):
    urgency: Literal["low", "normal", "high"]


@dataclass
class _Answers:
    """Answers the chat calls in order, whatever the model."""

    contents: list[str]

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content=self.contents.pop(0), usage=Usage(input_tokens=100, output_tokens=20)
        )

    async def embed(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("no embeddings here")


def _structured_gateway(database: Database, *contents: str) -> Gateway:
    return Gateway(
        models=load_models(ROOT / "config" / "models.yaml"),
        pricing=load_pricing(ROOT / "config" / "pricing.yaml"),
        provider=_Answers(list(contents)),
        recorder=DbCallRecorder(database),
    )


async def _rows(database: Database) -> list[LlmCall]:
    async with database.sessions() as session:
        return list((await session.scalars(select(LlmCall).order_by(LlmCall.id))).all())


async def test_an_invalid_answer_and_its_repair_are_both_recorded(database: Database) -> None:
    run_id = uuid.uuid4()
    call = CallContext(
        context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, run_id=run_id, prompt_version="v1"
    )
    gateway = _structured_gateway(
        database, json.dumps({"urgency": "panic"}), json.dumps({"urgency": "high"})
    )

    result = await gateway.complete_structured(
        "triage", [{"role": "user", "content": "help"}], _Verdict, call=call
    )

    assert result.value.urgency == "high"
    invalid, valid = await _rows(database)
    assert (invalid.status, valid.status) == (LlmCallStatus.INVALID_OUTPUT, LlmCallStatus.OK)
    assert invalid.error == "ValidationError (1)"
    assert "panic" not in (invalid.error or "")
    assert invalid.cost_usd > 0  # the invalid answer was paid for and counts in the budget
    assert invalid.input_tokens == 100
    assert {row.run_id for row in (invalid, valid)} == {run_id}
    assert {row.prompt_version for row in (invalid, valid)} == {"v1"}
    assert result.cost_usd == invalid.cost_usd + valid.cost_usd


async def test_two_invalid_answers_leave_two_invalid_output_rows(database: Database) -> None:
    call = CallContext(context=LlmCallContext.ONLINE, node=LlmNode.TRIAGE, run_id=uuid.uuid4())
    gateway = _structured_gateway(database, "not json", json.dumps({"urgency": "panic"}))

    with pytest.raises(StructuredOutputError):
        await gateway.complete_structured(
            "triage", [{"role": "user", "content": "help"}], _Verdict, call=call
        )

    rows = await _rows(database)
    assert [row.status for row in rows] == [LlmCallStatus.INVALID_OUTPUT] * 2
    assert all(row.error and row.error.startswith("ValidationError") for row in rows)
