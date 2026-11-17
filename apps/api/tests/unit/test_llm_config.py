"""`config/models.yaml` and `config/pricing.yaml`: the real files load, the loaders refuse what
would fail at run time, and the cost of a known token count matches the pricing file."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from hub.config import Settings
from hub.domain.enums import LlmCallMode
from hub.llm.config import ALLOWED_PARAMS, ConfigError, load_models, parse_models
from hub.llm.gateway import FixtureProvider, LiteLLMProvider, build_gateway
from hub.llm.local_embedding import LocalEmbeddingProvider
from hub.llm.pricing import load_pricing, parse_pricing
from hub.llm.schemas import Usage

from ._llm import MODELS_YAML, PRICING_YAML, MemoryRecorder, real_models, real_pricing

type Change = Callable[[dict[str, Any]], object]


def _models_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    return data


def _pricing_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(PRICING_YAML.read_text(encoding="utf-8"))
    return data


# --- the real files ------------------------------------------------------------------------------


def test_the_real_files_load_and_every_model_has_a_price() -> None:
    models = real_models()
    real_pricing().check_covers(models)
    assert set(models.roles) == {"triage", "research", "write", "verify", "judge", "embedding"}


def test_roles_follow_the_pipeline_document() -> None:
    roles = real_models().roles
    # ai-pipeline §8, ADR-0018: one provider; every chat role on Haiku with a Sonnet fallback,
    # the judge included; embeddings from a local model, never an API.
    for name, role in roles.items():
        if role.kind == "chat":
            assert role.model == "anthropic/claude-haiku-5-5", name
            assert role.fallback is not None
            assert role.fallback.model == "anthropic/claude-sonnet-5-5", name
    assert roles["embedding"].model.startswith("fastembed/")
    assert roles["embedding"].fallback is None
    every_model = {choice.model for role in roles.values() for choice in role.choices()}
    assert {model.split("/", 1)[0] for model in every_model} == {"anthropic", "fastembed"}
    # Only research may receive tools (research node, ai-pipeline §4).
    assert [name for name, role in roles.items() if role.tools] == ["research"]


def test_no_role_sends_temperature() -> None:
    # Claude Haiku 5.5 and Sonnet 5.5 reject temperature (provider docs, 2026-11-14).
    for role in real_models().roles.values():
        for choice in role.choices():
            assert "temperature" not in choice.params


def test_build_gateway_uses_fixtures_in_fake_mode_and_litellm_in_real_mode() -> None:
    fake = build_gateway(
        Settings(_env_file=None, models_config=MODELS_YAML), MemoryRecorder(), None
    )
    # Embeddings come from the local model in both modes (ADR-0018); chat calls do not.
    assert isinstance(fake.provider, LocalEmbeddingProvider)
    assert isinstance(fake.provider.inner, FixtureProvider)
    assert fake.mode == LlmCallMode.FAKE
    real = build_gateway(
        Settings(_env_file=None, models_config=MODELS_YAML, llm_mode="real"), MemoryRecorder(), None
    )
    assert isinstance(real.provider, LocalEmbeddingProvider)
    assert isinstance(real.provider.inner, LiteLLMProvider)
    assert real.mode == LlmCallMode.REAL


# --- the loaders refuse --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["roles"]["triage"].update(model="anthropic/unknown"), "is not under"),
        (lambda d: d["roles"]["triage"]["params"].update(api_base="http://x"), "not allowed"),
        (lambda d: d["roles"]["triage"]["params"].update(temperature=0), "not allowed"),
        (
            lambda d: d["roles"]["embedding"].update(
                fallback={"model": "anthropic/claude-haiku-5-5"}
            ),
            "no fallback",
        ),
        (lambda d: d["roles"]["embedding"].update(tools=True), "takes no tools"),
        (lambda d: d["roles"]["triage"].pop("max_tokens"), "needs max_tokens"),
        (lambda d: d["roles"]["triage"].update(timeout_s=0), "greater than 0"),
        (lambda d: d["roles"]["triage"].update(colour="red"), "Extra inputs"),
        (
            lambda d: d["roles"]["research"].update(
                fallback={"model": "fastembed/BAAI/bge-small-en-v1.5"}
            ),
            "cannot call tools",
        ),
    ],
)
def test_models_config_refuses(change: Change, message: str) -> None:
    data = _models_data()
    change(data)
    with pytest.raises(ConfigError, match=message):
        parse_models(data)


def test_allowlist_holds_no_connection_parameters() -> None:
    assert {"thinking", "reasoning_effort"} == ALLOWED_PARAMS


def test_unreadable_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_models(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("roles: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read"):
        load_pricing(broken)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["models"]["anthropic/claude-haiku-5-5"]["tiers"].pop(), "last tier has no"),
        (
            lambda d: d["models"]["anthropic/claude-haiku-5-5"]["tiers"][0].pop(
                "max_prompt_tokens"
            ),
            "only the last tier",
        ),
        (
            lambda d: d["models"]["anthropic/claude-haiku-5-5"]["tiers"].insert(
                1, {"max_prompt_tokens": 50, "input": 1, "cached_input": 1, "output": 1}
            ),
            "must rise",
        ),
        (
            lambda d: d["models"]["anthropic/claude-haiku-5-5"]["tiers"][0].update(input=-1),
            "greater than",
        ),
        (lambda d: d.pop("checked"), "Field required"),
    ],
)
def test_pricing_config_refuses(change: Change, message: str) -> None:
    data = _pricing_data()
    change(data)
    with pytest.raises(ConfigError, match=message):
        parse_pricing(data)


def test_a_model_without_price_is_refused() -> None:
    data = _pricing_data()
    del data["models"]["anthropic/claude-sonnet-5-5"]
    with pytest.raises(ConfigError, match="without a price"):
        parse_pricing(data).check_covers(real_models())


# --- cost ----------------------------------------------------------------------------------------


def test_cost_of_a_known_token_count_matches_the_pricing_file() -> None:
    usage = Usage(
        input_tokens=12_345, cached_tokens=2_000, cache_write_tokens=1_000, output_tokens=678
    )
    tier = _pricing_data()["models"]["anthropic/claude-sonnet-5-5"]["tiers"][0]
    by_hand = (
        (12_345 - 2_000 - 1_000) * Decimal(str(tier["input"]))
        + 2_000 * Decimal(str(tier["cached_input"]))
        + 1_000 * Decimal(str(tier["cache_write"]))
        + 678 * Decimal(str(tier["output"]))
    ) / 1_000_000
    cost = real_pricing().cost("anthropic/claude-sonnet-5-5", usage)
    assert cost == by_hand.quantize(Decimal("0.000001"))
    assert cost == Decimal("0.028170")  # 9,345 x 2 + 2,000 x 0.10 + 1,000 x 2.50 + 678 x 10


@pytest.mark.parametrize(
    ("prompt_tokens", "expected"),
    [
        (100_000, Decimal("0.010000")),  # at the limit: base tier, 0.10 per million
        (100_001, Decimal("0.050001")),  # over it: every token at 0.50 per million
    ],
)
def test_haiku_long_prompt_tier_applies_to_the_whole_request(
    prompt_tokens: int, expected: Decimal
) -> None:
    cost = real_pricing().cost("anthropic/claude-haiku-5-5", Usage(input_tokens=prompt_tokens))
    assert cost == expected


def test_cache_reads_count_toward_the_prompt_length_tier() -> None:
    usage = Usage(input_tokens=100_001, cached_tokens=100_000)
    # Over the threshold even though most of the prompt is a cache hit (Anthropic pricing page).
    assert real_pricing().cost("anthropic/claude-haiku-5-5", usage) == Decimal("0.005001")


def test_the_local_embedding_model_costs_nothing() -> None:
    usage = Usage(input_tokens=1_000_000)
    assert real_pricing().cost("fastembed/BAAI/bge-small-en-v1.5", usage) == Decimal(0)


def test_cost_errors() -> None:
    pricing = real_pricing()
    with pytest.raises(ConfigError, match="no price"):
        pricing.cost("anthropic/unknown", Usage())
    with pytest.raises(ConfigError, match="no cache_write price"):
        pricing.cost(
            "fastembed/BAAI/bge-small-en-v1.5", Usage(input_tokens=10, cache_write_tokens=5)
        )
    with pytest.raises(ConfigError, match="do not add up"):
        pricing.cost("anthropic/claude-haiku-5-5", Usage(input_tokens=5, cached_tokens=10))
