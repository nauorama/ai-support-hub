"""Prompt files (docs/ai-pipeline.md §7, ADR-0013).

A prompt is `prompts/<node>/v<N>.md`: YAML front matter (`version`, `role`, `changed`, `why`),
then exactly two parts, `# System` and `# User`, with `{{placeholders}}`. The active version of
each node is named in `config/models.yaml` (`prompts`). Every model call made with a prompt
records its id (`<node>/v<N>`) in `llm_calls.prompt_version`.

A `{{` or `}}` that is not part of a valid placeholder makes the file invalid. Rendering is
strict both ways: a placeholder without a value and a value without a placeholder are errors,
so a renamed placeholder cannot silently send an empty or stale prompt. Values are inserted in
one pass, so text inside a value (a ticket that contains `{{x}}`) is never expanded. Every value
is untrusted data, so a data tag inside it (`</ticket>`, `<kb>`, ai-pipeline §7) is neutralised
before it is inserted and cannot close or open a tag of the prompt (`guardrails/injection.py`).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hub.guardrails.injection import neutralise_data_tags
from hub.llm.config import ModelsConfig
from hub.llm.schemas import LlmError, Message

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
_VERSION = re.compile(r"v[1-9][0-9]*")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_PART_HEADING = re.compile(r"^# (System|User)[ \t]*$", re.MULTILINE)
# Any other level-1 heading, `#System` without the space included: a typo such as `# Sytem`
# must not be swallowed into a part.
_LEVEL_1_HEADING = re.compile(r"^#(?!#).*$", re.MULTILINE)
# Braces left after the valid placeholders are taken out: a malformed placeholder such as
# `{{Ticket}}` or `{{ ticket-id }}` would otherwise reach the model as literal text.
_BRACES = re.compile(r"\{\{|\}\}")


class PromptError(LlmError):
    """A prompt file is invalid, or a render got the wrong values. A programming error: the
    message names the file and the placeholder, never a value (values hold customer text)."""


class FrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=_VERSION.pattern)
    role: str
    changed: date
    why: str = Field(min_length=1)


@dataclass(frozen=True)
class Prompt:
    node: str
    front: FrontMatter
    system: str
    user: str

    @property
    def version(self) -> str:
        """What `llm_calls.prompt_version` stores, e.g. `triage/v1`."""
        return f"{self.node}/{self.front.version}"

    @property
    def placeholders(self) -> frozenset[str]:
        return frozenset(PLACEHOLDER.findall(self.system) + PLACEHOLDER.findall(self.user))

    def render(self, values: Mapping[str, str]) -> list[Message]:
        """The system and user messages with every placeholder filled."""
        missing = self.placeholders - set(values)
        if missing:
            raise PromptError(f"{self.version}: no value for {sorted(missing)}")
        unused = set(values) - self.placeholders
        if unused:
            raise PromptError(f"{self.version}: no placeholder for {sorted(unused)}")

        safe = {name: neutralise_data_tags(value) for name, value in values.items()}

        def fill(text: str) -> str:
            return PLACEHOLDER.sub(lambda match: safe[match.group(1)], text)

        return [
            {"role": "system", "content": fill(self.system)},
            {"role": "user", "content": fill(self.user)},
        ]


def parse_prompt(text: str, node: str, version: str) -> Prompt:
    """Parse the text of `prompts/<node>/<version>.md`."""
    name = f"{node}/{version}"
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if match is None:
        raise PromptError(f"{name}: front matter between `---` lines is missing")
    try:
        front = FrontMatter.model_validate(yaml.safe_load(match.group(1)))
    except (yaml.YAMLError, ValidationError) as exc:
        raise PromptError(f"{name}: invalid front matter ({type(exc).__name__})") from None
    if front.version != version:
        raise PromptError(f"{name}: front matter says version {front.version}")

    body = text.replace("\r\n", "\n")[match.end() :]
    headings = [heading.group(0).strip() for heading in _LEVEL_1_HEADING.finditer(body)]
    if headings != ["# System", "# User"]:
        raise PromptError(f"{name}: expected the parts `# System` then `# User`, got {headings}")
    parts = _PART_HEADING.split(body)
    # split -> [before, "System", system text, "User", user text]
    if parts[0].strip():
        raise PromptError(f"{name}: text before `# System`")
    system, user = parts[2].strip(), parts[4].strip()
    if not system or not user:
        raise PromptError(f"{name}: the System and User parts must not be empty")
    for part, text in (("System", system), ("User", user)):
        if _BRACES.search(PLACEHOLDER.sub("", text)):
            raise PromptError(
                f"{name}: malformed placeholder in the {part} part "
                "(lowercase letters, digits and `_`, e.g. `{{ticket}}`)"
            )
    return Prompt(node=node, front=front, system=system, user=user)


def load_prompt(directory: Path, node: str, version: str) -> Prompt:
    if not _VERSION.fullmatch(version):
        raise PromptError(f"{node}/{version}: a version is `v` and a number")
    path = directory / node / f"{version}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"{node}/{version}: cannot read ({type(exc).__name__})") from None
    return parse_prompt(text, node, version)


def active_prompt(directory: Path, models: ModelsConfig, node: str) -> Prompt:
    """The version of `node` named in `config/models.yaml`. Its `role` must be a chat role there
    (a prompt need not share its node's name: `handoff_summary` runs on the `triage` role)."""
    version = models.prompts.get(node)
    if version is None:
        raise PromptError(f"{node}: no active version in models.yaml `prompts`")
    prompt = load_prompt(directory, node, version)
    role = models.roles.get(prompt.front.role)
    if role is None or role.kind != "chat":
        raise PromptError(f"{prompt.version}: role {prompt.front.role} is not a chat role")
    return prompt
