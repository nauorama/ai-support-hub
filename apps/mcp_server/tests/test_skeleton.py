"""The support_tools package imports from the workspace."""

from pathlib import Path

import support_tools
import support_tools.adapters


def test_support_tools_is_installed_from_the_workspace() -> None:
    assert support_tools.__file__ is not None
    assert Path(support_tools.__file__).parent.parent.parent.name == "mcp_server"
    assert support_tools.adapters.__file__ is not None
