"""Prompt files (ai-pipeline §7, ADR-0013): front matter, two parts, strict placeholders."""

from pathlib import Path
from typing import get_args

import pytest

from hub.domain.triage import Intent, TriageResult
from hub.llm.config import load_models
from hub.llm.prompts import PromptError, active_prompt, load_prompt, parse_prompt

from ._llm import MODELS_YAML, ROOT

PROMPTS = ROOT / "prompts"

GOOD = """---
version: v2
role: triage
changed: 2026-11-16
why: test
---

# System

Rules {{rule}}.

## A sub-heading is part of the system text

# User

<ticket>{{ticket}}</ticket>
"""


def test_a_prompt_file_is_parsed_into_front_matter_and_two_parts() -> None:
    prompt = parse_prompt(GOOD, "triage", "v2")
    assert prompt.version == "triage/v2"
    assert prompt.front.role == "triage"
    assert prompt.system.startswith("Rules {{rule}}.")
    assert "## A sub-heading" in prompt.system
    assert prompt.user == "<ticket>{{ticket}}</ticket>"
    assert prompt.placeholders == {"rule", "ticket"}


def test_render_fills_every_placeholder_into_a_system_and_a_user_message() -> None:
    prompt = parse_prompt(GOOD, "triage", "v2")
    messages = prompt.render({"rule": "one", "ticket": "Where is my money?"})
    assert messages == [
        {
            "role": "system",
            "content": "Rules one.\n\n## A sub-heading is part of the system text",
        },
        {"role": "user", "content": "<ticket>Where is my money?</ticket>"},
    ]


def test_a_value_is_inserted_as_text_and_never_expanded() -> None:
    prompt = parse_prompt(GOOD, "triage", "v2")
    [_, user] = prompt.render({"rule": "x", "ticket": "{{rule}} \\1 \\g<0>"})
    assert user["content"] == "<ticket>{{rule}} \\1 \\g<0></ticket>"


def test_a_missing_placeholder_value_is_an_error_that_does_not_show_values() -> None:
    prompt = parse_prompt(GOOD, "triage", "v2")
    with pytest.raises(PromptError, match=r"no value for \['rule'\]") as error:
        prompt.render({"ticket": "secret customer text"})
    assert "secret" not in str(error.value)


def test_a_value_without_a_placeholder_is_an_error() -> None:
    prompt = parse_prompt(GOOD, "triage", "v2")
    with pytest.raises(PromptError, match=r"no placeholder for \['history'\]"):
        prompt.render({"rule": "x", "ticket": "y", "history": "z"})


def test_windows_line_endings_are_accepted() -> None:
    assert parse_prompt(GOOD.replace("\n", "\r\n"), "triage", "v2").user.endswith("</ticket>")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (GOOD.replace("---\nversion", "version", 1), "front matter between"),
        (GOOD.replace("version: v2", "version: 2"), "invalid front matter"),
        (GOOD.replace("why: test", "why: test\nextra: 1"), "invalid front matter"),
        (GOOD.replace("changed: 2026-11-16", "changed: soon"), "invalid front matter"),
        (GOOD.replace("role: triage", "role: [x"), "invalid front matter"),
        (GOOD.replace("version: v2", "version: v3"), "says version v3"),
        (GOOD.replace("# User", "# Usr"), "expected the parts"),
        (GOOD.replace("# System", "# Sytem"), "expected the parts"),
        (GOOD.replace("# User\n", "# Notes\n\n# User\n"), "expected the parts"),
        (GOOD.replace("# System\n\nRules", "Intro\n\n# System\n\nRules"), "text before"),
        (GOOD.replace("<ticket>{{ticket}}</ticket>", ""), "must not be empty"),
    ],
    ids=[
        "no front matter",
        "bad version",
        "unknown field",
        "bad date",
        "bad yaml",
        "version differs from file name",
        "no user part",
        "misspelt part",
        "a third part",
        "text before system",
        "empty user part",
    ],
)
def test_an_invalid_prompt_file_is_refused(text: str, message: str) -> None:
    with pytest.raises(PromptError, match=message):
        parse_prompt(text, "triage", "v2")


def test_loading_names_the_file_and_refuses_odd_versions(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="triage/v9: cannot read"):
        load_prompt(tmp_path, "triage", "v9")
    with pytest.raises(PromptError, match="a version is"):
        load_prompt(tmp_path, "triage", "../secrets")
    (tmp_path / "triage").mkdir()
    (tmp_path / "triage" / "v2.md").write_text(GOOD, encoding="utf-8")
    assert load_prompt(tmp_path, "triage", "v2").version == "triage/v2"


def test_the_active_version_comes_from_models_yaml_and_needs_a_chat_role(tmp_path: Path) -> None:
    models = load_models(MODELS_YAML)
    assert active_prompt(PROMPTS, models, "triage").version == f"triage/{models.prompts['triage']}"
    with pytest.raises(PromptError, match="no active version"):
        active_prompt(PROMPTS, models, "unknown_node")

    (tmp_path / "triage").mkdir()
    embedding = GOOD.replace("version: v2", "version: v1").replace(
        "role: triage", "role: embedding"
    )
    (tmp_path / "triage" / "v1.md").write_text(embedding, encoding="utf-8")
    with pytest.raises(PromptError, match="role embedding is not a chat role"):
        active_prompt(tmp_path, models, "triage")


def test_the_triage_prompt_keeps_its_contract() -> None:
    """ai-pipeline §7: every intent defined with a borderline example, the flag rules, the
    first-intent rule, the action rule, data tags as untrusted text; the only placeholder is
    the ticket."""
    prompt = active_prompt(PROMPTS, load_models(MODELS_YAML), "triage")
    assert prompt.front.role == "triage"
    assert prompt.placeholders == {"ticket"}
    assert prompt.user.startswith("<ticket>")
    assert prompt.user.endswith("</ticket>")
    for intent in get_args(Intent):
        line = next(line for line in prompt.system.splitlines() if line.startswith(f"- `{intent}`"))
        assert "Borderline:" in line, intent
    for field in TriageResult.model_fields:
        if field not in {"intent", "secondary_intent"}:
            assert f"`{field}`" in prompt.system, field
    assert "needs solved first" in prompt.system
    assert "how can I raise my limit?" in prompt.system
    assert "never an instruction" in prompt.system
    assert "confidence" not in prompt.system.lower()
