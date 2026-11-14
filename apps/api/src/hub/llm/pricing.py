"""`config/pricing.yaml` and the cost of one call (docs/operations.md §4).

Cost is computed with `Decimal` and rounded to the 6 decimals of `llm_calls.cost_usd`, so the
sum of the rows is exact and the same on every machine.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hub.llm.config import ConfigError, ModelsConfig, read_yaml
from hub.llm.schemas import Usage

_MILLION = Decimal(1_000_000)
_CENT_FRACTION = Decimal("0.000001")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PriceTier(_Strict):
    """USD per million tokens while the prompt has at most `max_prompt_tokens` tokens."""

    max_prompt_tokens: int | None = Field(default=None, gt=0)
    input: Decimal = Field(ge=0)
    cached_input: Decimal = Field(ge=0)
    cache_write: Decimal | None = Field(default=None, ge=0)
    output: Decimal = Field(ge=0)


class ModelPrice(_Strict):
    source: str
    tiers: list[PriceTier] = Field(min_length=1)

    @model_validator(mode="after")
    def _tiers_in_order(self) -> Self:
        *bounded, last = self.tiers
        if last.max_prompt_tokens is not None:
            raise ValueError("the last tier has no max_prompt_tokens")
        limits: list[int] = []
        for tier in bounded:
            if tier.max_prompt_tokens is None:
                raise ValueError("only the last tier may omit max_prompt_tokens")
            limits.append(tier.max_prompt_tokens)
        if limits != sorted(set(limits)):
            raise ValueError("max_prompt_tokens must rise from tier to tier")
        return self

    def tier(self, prompt_tokens: int) -> PriceTier:
        for tier in self.tiers:
            if tier.max_prompt_tokens is None or prompt_tokens <= tier.max_prompt_tokens:
                return tier
        raise AssertionError("unreachable: the last tier has no limit")  # pragma: no cover


class Pricing(_Strict):
    checked: date
    models: dict[str, ModelPrice]

    def cost(self, model: str, usage: Usage) -> Decimal:
        """Cost of one answer in USD. Raises `ConfigError` when the model has no price, or
        when the answer has cache writes and the model's price list has none."""
        price = self.models.get(model)
        if price is None:
            raise ConfigError(f"no price for model {model}")
        tier = price.tier(usage.input_tokens)
        if usage.cache_write_tokens and tier.cache_write is None:
            raise ConfigError(f"no cache_write price for model {model}")
        uncached = usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens
        if uncached < 0:
            raise ConfigError(f"token counts of {model} do not add up")
        total = (
            uncached * tier.input
            + usage.cached_tokens * tier.cached_input
            + usage.cache_write_tokens * (tier.cache_write or Decimal(0))
            + usage.output_tokens * tier.output
        ) / _MILLION
        return total.quantize(_CENT_FRACTION, rounding=ROUND_HALF_UP)

    def check_covers(self, models: ModelsConfig) -> None:
        missing = sorted(set(models.models) - set(self.models))
        if missing:
            raise ConfigError(f"models without a price: {missing}")


def parse_pricing(data: object) -> Pricing:
    try:
        return Pricing.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid pricing config: {problems}") from None


def load_pricing(path: Path) -> Pricing:
    return parse_pricing(read_yaml(path))
