"""The daily budget guard (G8, security §3; NFR-14).

Today's spend is the sum of `llm_calls.cost_usd` over the rows written since 00:00 UTC with
`mode = 'real'`, in every context: `online`, `memory` and `record` (`make record-llm`; owner
decision, P3-01). Fake-mode rows never count (owner decision, P3-05): they repeat the usage and
cost of the recording they answered from. Evaluation rows live in the evaluation database and
have their own limit (`EVAL_MAX_COST_USD`).

The limit is the `daily_budget_usd` setting (changed by the lead in the console); while the row
is missing, `DAILY_BUDGET_USD` gives it. A stored value that is not a finite number of zero or
more refuses every call: on doubt, hand off.

The gateway checks before every `online` call, and the run checks once at its start, so a new
ticket makes no model call once the budget is reached (ai-pipeline §6, row 2). The guard is
soft: one call can pass the limit by its own cost, and runs at the same moment can each pass the
check. The spending limit in the Anthropic console is the hard stop (ADR-0018).
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select

from hub.db.models import LlmCall
from hub.db.repositories import SettingsRepository
from hub.db.session import Database
from hub.domain.enums import LlmCallMode, SettingKey
from hub.llm.schemas import BudgetExceededError

logger = logging.getLogger(__name__)


class BudgetCheck(Protocol):
    async def check(self) -> None:
        """Raise `BudgetExceededError` when today's real-mode spend has reached the limit."""


def day_start(now: datetime) -> datetime:
    """00:00 UTC of the day `now` falls on (an aware datetime in any zone)."""
    return datetime.combine(now.astimezone(UTC).date(), time(), tzinfo=UTC)


def reached(spent: Decimal, limit: Decimal) -> bool:
    """A limit of 0 refuses every call."""
    return spent >= limit


def parse_limit(value: object, default: Decimal) -> Decimal:
    """The limit from the setting's JSON value; `None` (no row) gives `default`. Anything that is
    not a finite number of zero or more gives 0: every call is refused."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        logger.error("daily_budget_usd is not a number; every model call is refused")
        return Decimal(0)
    if not math.isfinite(value) or value < 0:
        logger.error("daily_budget_usd is not a finite number >= 0; every model call is refused")
        return Decimal(0)
    return Decimal(str(value))


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BudgetGuard:
    database: Database
    default_limit: Decimal
    now: Callable[[], datetime] = field(default=_now)

    async def spent_today(self) -> Decimal:
        async with self.database.sessions() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
                    LlmCall.mode == LlmCallMode.REAL,
                    LlmCall.created_at >= day_start(self.now()),
                )
            )
        return Decimal(total or 0)

    async def limit(self) -> Decimal:
        async with self.database.sessions() as session:
            value = await SettingsRepository(session).get(SettingKey.DAILY_BUDGET_USD)
        return parse_limit(value, self.default_limit)

    async def check(self) -> None:
        limit = await self.limit()
        spent = await self.spent_today()
        if reached(spent, limit):
            logger.warning(
                "daily budget reached", extra={"spent_usd": str(spent), "limit_usd": str(limit)}
            )
            raise BudgetExceededError(spent, limit)
