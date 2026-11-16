"""Structured output (ADR-0004): the schema a provider gets, the check of its answer, and the
repair request. Pure functions; `Gateway.complete_structured` puts them together.

The schema sent to a provider is a strict form of the Pydantic schema, the same for the model
and its fallback, so both get the same contract. Rules (checked 2026-11-15):
- Anthropic JSON outputs: every object needs `additionalProperties: false`; `minLength`,
  `maxLength`, `minimum`, `maximum`, `multipleOf` and `minItems` above 1 are rejected with a 400
  (https://platform.claude.com/docs/en/build-with-claude/structured-outputs, and LiteLLM
  1.108.6 `AnthropicConfig.filter_anthropic_output_schema`, which strips the same keywords).
- Every property is in `required` and an optional field is a nullable union. This came from
  OpenAI strict mode, which ADR-0018 removed; it is kept because it makes the contract stricter
  (a model cannot leave a field out). P3-03's first real calls confirm that Anthropic accepts it.
Constraints a provider may not enforce are moved into the field description, so the model still
reads them; Pydantic enforces them on the answer and a miss goes through the repair call.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, JsonValue, ValidationError

from hub.llm.schemas import LlmError, Message

# Keyword -> text added to the description when the keyword is taken out of the schema.
_MOVED_CONSTRAINTS = {
    "minLength": "at least {} characters",
    "maxLength": "at most {} characters",
    "minimum": "at least {}",
    "maximum": "at most {}",
    "exclusiveMinimum": "more than {}",
    "exclusiveMaximum": "less than {}",
    "multipleOf": "a multiple of {}",
    "minItems": "at least {} items",
    "maxItems": "at most {} items",
    "pattern": "matches the pattern {}",
}
# Keywords that only describe the schema for people, or that a strict schema must not carry.
_DROPPED = ("title", "default", "examples")
# Keywords the strict schema cannot express; a schema using them is refused, not loosened.
# `uniqueItems` comes from `set[...]`, `prefixItems` from `tuple[...]`: LiteLLM strips them for
# Anthropic, so the model would not see the rule.
_REFUSED = {
    "oneOf": "",
    "allOf": "",
    "not": "",
    "if": "",
    "then": "",
    "else": "",
    "patternProperties": "",
    "uniqueItems": " (a set[...] field; use list[...])",
    "prefixItems": " (a tuple[...] field; use list[...] or a model)",
}

MAX_ERRORS_SHOWN = 10


class SchemaNotSupportedError(LlmError):
    """The Pydantic model cannot be turned into a strict provider schema (an open object such as
    `dict[str, X]`, or a keyword strict mode does not support). A programming error."""


def provider_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The strict JSON schema sent to the provider for `schema`."""
    return _strict_object(schema.model_json_schema(), schema.__name__, "#")


def _strict(node: JsonValue, name: str, path: str) -> JsonValue:
    if isinstance(node, list):
        return [_strict(item, name, f"{path}/{index}") for index, item in enumerate(node)]
    if isinstance(node, dict):
        return _strict_object(node, name, path)
    return node


def _strict_object(node: dict[str, Any], name: str, path: str) -> dict[str, Any]:
    for keyword, hint in _REFUSED.items():
        if keyword in node:
            raise SchemaNotSupportedError(
                f"{name}: `{keyword}` at {path}{hint} is not supported in strict mode"
            )
    notes: list[str] = []
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED or key == "additionalProperties":  # the latter is set below
            continue
        if key in _MOVED_CONSTRAINTS:
            notes.append(_MOVED_CONSTRAINTS[key].format(value))
        elif key in ("properties", "$defs") and isinstance(value, dict):
            # Keys of these maps are names, not keywords: only their values are schemas.
            result[key] = {
                child: _strict(sub, name, f"{path}/{key}/{child}") for child, sub in value.items()
            }
        else:
            result[key] = _strict(value, name, f"{path}/{key}")
    if node.get("type") == "object":
        properties = node.get("properties")
        if not isinstance(properties, dict) or node.get("additionalProperties") not in (
            None,
            False,
        ):
            raise SchemaNotSupportedError(f"{name}: open object at {path}; use a model")
        result["additionalProperties"] = False
        result["required"] = list(properties)
    if notes:
        description = result.get("description")
        constraint = f"Constraint: {'; '.join(notes)}."
        result["description"] = f"{description} {constraint}" if description else constraint
    return result


@dataclass(frozen=True)
class Invalid:
    """Why an answer does not match the schema: location and message of each error, without the
    input values. A location can still hold model text: the name of an extra key the model made
    up (`extra="forbid"`). The errors go only into the repair request, which G2 scans;
    `llm_calls.error` gets only the count (`summary`)."""

    errors: tuple[str, ...]

    @property
    def summary(self) -> str:
        """What `llm_calls.error` stores for an invalid answer."""
        return f"ValidationError ({len(self.errors)})"


def check_answer[T: BaseModel](schema: type[T], content: str | None) -> T | Invalid:
    """The validated answer, or why it is invalid. Empty content (a refusal or a cut-off answer)
    and text that is not JSON are invalid like any schema mismatch."""
    if content is None or not content.strip():
        return Invalid(("(answer): the answer was empty; reply with one JSON object",))
    try:
        return schema.model_validate_json(content)
    except ValidationError as exc:
        return Invalid(
            tuple(
                f"{_location(error['loc'])}: {error['msg']}"
                for error in exc.errors(
                    include_url=False, include_input=False, include_context=False
                )
            )
        )


def _location(loc: Sequence[int | str]) -> str:
    return ".".join(str(part) for part in loc) or "(answer)"


def repair_messages(
    messages: Sequence[Message], content: str | None, invalid: Invalid
) -> list[Message]:
    """The original conversation, the invalid answer, and the validation errors (ADR-0004). The
    invalid answer is model output of this same request; the gateway scans the repair request
    with G2 like any other."""
    shown = invalid.errors[:MAX_ERRORS_SHOWN]
    more = len(invalid.errors) - len(shown)
    lines = "\n".join(f"- {error}" for error in shown)
    if more:
        lines += f"\n- and {more} more"
    # An empty answer is left out: Anthropic refuses an assistant turn with empty content.
    answer = [{"role": "assistant", "content": content}] if content and content.strip() else []
    return [
        *(dict(message) for message in messages),
        *answer,
        {
            "role": "user",
            "content": (
                "Your answer does not match the required JSON schema:\n"
                f"{lines}\n"
                "Reply again with only the corrected JSON object."
            ),
        },
    ]
