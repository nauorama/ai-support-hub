"""The hub package imports and its module layout matches docs/design.md §8.

Only `hub` is imported here: apps/api must not import apps/mcp_server (design §8).
"""

import importlib
from pathlib import Path

import pytest

import hub

HUB_MODULES = [
    "api",
    "auth",
    "db",
    "domain",
    "guardrails",
    "kb",
    "llm",
    "graph",
    "graph.nodes",
    "tools",
    "memory",
    "audit",
    "metrics",
    "privacy",
    "observability",
]


@pytest.mark.parametrize("module", HUB_MODULES)
def test_hub_module_is_a_package(module: str) -> None:
    imported = importlib.import_module(f"hub.{module}")
    assert imported.__file__ is not None
    assert Path(imported.__file__).name == "__init__.py"


def test_hub_is_installed_from_the_workspace() -> None:
    # Guards against a same-named package from PyPI shadowing the workspace member.
    assert hub.__file__ is not None
    assert Path(hub.__file__).parent.parent.parent.name == "api"
