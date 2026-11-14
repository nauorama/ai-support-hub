"""`config/models.yaml`: roles → models (docs/ai-pipeline.md §8).

The loader refuses a configuration that could fail at run time in a way a test would not see:
a role without a price, a role that may get tools while its model or fallback cannot take them,
a parameter that is not on the allowlist, an embedding role with a fallback.
"""

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

# Parameters a role may pass to the provider. Short on purpose: request-body parameters such as
# `api_base` or credentials must never come from a file (LiteLLM GHSA-2rwg-2wh4-7c4f).
ALLOWED_PARAMS = frozenset({"thinking", "reasoning_effort"})


class ConfigError(Exception):
    """The models or pricing file is invalid. Names the problem, never a secret."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelInfo(_Strict):
    tools: bool
    # Parameters the model needs before it accepts tools (GPT-6 Luna: reasoning_effort none).
    tools_require: dict[str, JsonValue] = Field(default_factory=dict)


class ModelChoice(_Strict):
    model: str
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_params(self) -> Self:
        unknown = set(self.params) - ALLOWED_PARAMS
        if unknown:
            raise ValueError(f"parameters not allowed: {sorted(unknown)}")
        return self


class RoleConfig(ModelChoice):
    kind: Literal["chat", "embedding"] = "chat"
    fallback: ModelChoice | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_s: float = Field(gt=0, le=300)
    tools: bool = False

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        if self.kind == "embedding":
            if self.fallback is not None:
                raise ValueError("an embedding role has no fallback (vectors cannot be mixed)")
            if self.tools:
                raise ValueError("an embedding role takes no tools")
        elif self.max_tokens is None:
            raise ValueError("a chat role needs max_tokens")
        return self

    def choices(self) -> list[ModelChoice]:
        """The model, then the fallback."""
        return [self, *([self.fallback] if self.fallback is not None else [])]


class ModelsConfig(_Strict):
    models: dict[str, ModelInfo]
    roles: dict[str, RoleConfig]
    prompts: dict[str, str]

    @model_validator(mode="after")
    def _roles_use_known_models(self) -> Self:
        for name, role in self.roles.items():
            for choice in role.choices():
                info = self.models.get(choice.model)
                if info is None:
                    raise ValueError(f"role {name}: model {choice.model} is not under `models`")
                if role.tools and not info.tools:
                    raise ValueError(
                        f"role {name} may receive tools, but {choice.model} cannot call tools"
                    )
                if role.tools:
                    for key, value in info.tools_require.items():
                        if choice.params.get(key) != value:
                            raise ValueError(
                                f"role {name}: {choice.model} needs {key}={value} for tools"
                            )
        return self

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError:
            raise ConfigError(f"unknown model role: {name}") from None


def read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path.name}: {type(exc).__name__}") from None


def parse_models(data: object) -> ModelsConfig:
    try:
        return ModelsConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid models config: {problems}") from None


def load_models(path: Path) -> ModelsConfig:
    return parse_models(read_yaml(path))
