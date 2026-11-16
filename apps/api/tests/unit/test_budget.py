"""The daily budget guard (G8, P3-06): the pure rules, the guard with a stub session, and the
gateway check before online calls. The real query is tested against Postgres in
tests/integration/test_budget_guard.py."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel

from hub.db.models import Setting
from hub.db.session import Database
from hub.domain.enums import LlmCallContext, LlmCallMode, LlmNode, SettingKey
from hub.llm.budget import BudgetGuard, day_start, parse_limit, reached
from hub.llm.schemas import BudgetExceededError, CallContext

from ._llm import CALL, MemoryRecorder, ScriptedProvider, make_gateway, ok, real_models

HAIKU = real_models().role("triage").model


class Answer(BaseModel):
    text: str


# --- pure rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "start"),
    [
        (datetime(2026, 11, 16, 0, 0, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC)),
        (datetime(2026, 11, 16, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC)),
        # 00:30 in Bratislava (UTC+1) is still the day before in UTC.
        (
            datetime(2026, 11, 16, 0, 30, tzinfo=timezone(timedelta(hours=1))),
            datetime(2026, 11, 15, tzinfo=UTC),
        ),
    ],
)
def test_the_day_starts_at_midnight_utc(now: datetime, start: datetime) -> None:
    assert day_start(now) == start
    assert day_start(now).tzinfo == UTC


@pytest.mark.parametrize(
    ("spent", "limit", "expected"),
    [
        ("0", "0", True),  # a limit of 0 refuses every call
        ("0.49", "0.50", False),
        ("0.50", "0.50", True),
        ("0.51", "0.50", True),
        ("0", "0.50", False),
    ],
)
def test_the_budget_is_reached_at_the_limit(spent: str, limit: str, expected: bool) -> None:
    assert reached(Decimal(spent), Decimal(limit)) is expected


@pytest.mark.parametrize(
    ("value", "limit"),
    [(None, "0.5"), (0, "0"), (0.25, "0.25"), (3, "3"), (0.1, "0.1")],
)
def test_the_limit_comes_from_the_setting_or_the_default(value: object, limit: str) -> None:
    assert parse_limit(value, Decimal("0.5")) == Decimal(limit)


@pytest.mark.parametrize(
    "value", [True, False, "0.5", [], {}, -0.01, -1, float("nan"), float("inf"), float("-inf")]
)
def test_a_broken_setting_refuses_every_call(
    value: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="hub.llm.budget"):
        assert parse_limit(value, Decimal("0.5")) == Decimal(0)
    assert "every model call is refused" in caplog.text


# --- the guard with a stub session -----------------------------------------------------------


@dataclass
class StubSession:
    setting: Any
    spent: Decimal | int | None
    statements: list[Any] = field(default_factory=list)

    async def get(self, model: type, key: SettingKey) -> Setting | None:
        assert model is Setting
        assert key == SettingKey.DAILY_BUDGET_USD
        return None if self.setting is None else Setting(key=key, value=self.setting)

    async def scalar(self, statement: Any) -> Decimal | int | None:  # noqa: ANN401
        self.statements.append(statement)
        return self.spent


@dataclass
class StubDatabase:
    session: StubSession

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[StubSession]:
        yield self.session

    def sessions(self) -> Any:  # noqa: ANN401 - stands in for async_sessionmaker
        return self._open()


def _guard(setting: Any, spent: Decimal | int | None) -> tuple[BudgetGuard, StubSession]:  # noqa: ANN401
    session = StubSession(setting, spent)
    guard = BudgetGuard(
        cast(Database, StubDatabase(session)),
        default_limit=Decimal("0.5"),
        now=lambda: datetime(2026, 11, 16, 12, tzinfo=UTC),
    )
    return guard, session


async def test_a_limit_of_zero_refuses_with_nothing_spent() -> None:
    guard, _ = _guard(0, 0)
    with pytest.raises(BudgetExceededError) as raised:
        await guard.check()
    assert (raised.value.spent_usd, raised.value.limit_usd) == (Decimal(0), Decimal(0))


async def test_spend_below_the_default_limit_passes() -> None:
    guard, _ = _guard(None, Decimal("0.499999"))
    await guard.check()
    assert await guard.limit() == Decimal("0.5")


async def test_spend_at_the_setting_limit_refuses() -> None:
    guard, _ = _guard(0.3, Decimal("0.300000"))
    with pytest.raises(BudgetExceededError, match=r"spent 0\.300000 of 0\.3 USD"):
        await guard.check()


async def test_no_rows_today_is_zero_spend() -> None:
    guard, _ = _guard(None, None)
    assert await guard.spent_today() == Decimal(0)


async def test_the_sum_counts_only_real_rows_since_midnight_utc() -> None:
    guard, session = _guard(None, 0)
    await guard.spent_today()
    [statement] = session.statements
    params = statement.compile().params
    assert LlmCallMode.REAL in params.values()
    assert datetime(2026, 11, 16, tzinfo=UTC) in params.values()
    sql = str(statement)
    assert "llm_calls.mode" in sql
    assert "llm_calls.created_at >=" in sql
    assert "context" not in sql  # every context counts, record included (P3-01)


def test_the_default_clock_is_utc() -> None:
    guard = BudgetGuard(cast(Database, None), default_limit=Decimal(0))
    assert guard.now().tzinfo == UTC


# --- the gateway check ------------------------------------------------------------------------


@dataclass
class CountingBudget:
    refuse_after: int | None = None  # None: never refuse
    checks: int = 0

    async def check(self) -> None:
        self.checks += 1
        if self.refuse_after is not None and self.checks > self.refuse_after:
            raise BudgetExceededError(Decimal("0.5"), Decimal("0.5"))


async def test_a_refused_online_call_reaches_no_model_and_writes_no_row() -> None:
    provider = ScriptedProvider({HAIKU: [ok()]})
    recorder = MemoryRecorder()
    budget = CountingBudget(refuse_after=0)
    gateway = make_gateway(provider, recorder, budget=budget)
    with pytest.raises(BudgetExceededError):
        await gateway.complete("triage", [{"role": "user", "content": "hi"}], call=CALL)
    assert provider.requests == []
    assert recorder.rows == []
    assert budget.checks == 1


async def test_the_budget_is_checked_before_the_card_scan() -> None:
    """A refused call is not an attempt: even a G2 hit writes no row and no audit record."""
    recorder = MemoryRecorder()
    gateway = make_gateway(ScriptedProvider({}), recorder, budget=CountingBudget(refuse_after=0))
    with pytest.raises(BudgetExceededError):
        await gateway.complete(
            "triage", [{"role": "user", "content": "card 4111 1111 1111 1111"}], call=CALL
        )
    assert recorder.rows == []
    assert recorder.blocked == []


@pytest.mark.parametrize(
    "context", [LlmCallContext.RECORD, LlmCallContext.EVAL, LlmCallContext.MEMORY]
)
async def test_only_online_calls_are_checked(context: LlmCallContext) -> None:
    budget = CountingBudget(refuse_after=0)
    gateway = make_gateway(ScriptedProvider({HAIKU: [ok()]}), budget=budget)
    call = CallContext(context=context, node=LlmNode.TRIAGE)
    await gateway.complete("triage", [{"role": "user", "content": "hi"}], call=call)
    assert budget.checks == 0


async def test_the_repair_call_is_checked_again() -> None:
    """The budget can run out between the first answer and its repair call."""
    provider = ScriptedProvider({HAIKU: [ok("not json")]})
    recorder = MemoryRecorder()
    budget = CountingBudget(refuse_after=1)
    gateway = make_gateway(provider, recorder, budget=budget)
    with pytest.raises(BudgetExceededError):
        await gateway.complete_structured(
            "triage", [{"role": "user", "content": "hi"}], Answer, call=CALL
        )
    assert budget.checks == 2
    assert len(provider.requests) == 1  # the repair call never reached the model
    assert len(recorder.rows) == 1


@pytest.mark.parametrize("mode", [LlmCallMode.REAL, LlmCallMode.FAKE])
async def test_every_row_names_the_mode(mode: LlmCallMode) -> None:
    recorder = MemoryRecorder()
    gateway = make_gateway(ScriptedProvider({HAIKU: [ok()]}), recorder, mode=mode)
    await gateway.complete("triage", [{"role": "user", "content": "hi"}], call=CALL)
    assert [row.mode for row in recorder.rows] == [mode]
