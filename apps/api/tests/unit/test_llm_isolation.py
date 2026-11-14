"""test_llm_providers.py imports `litellm` itself (ruff sorts it before `hub`). Run on its own,
it must still load no `.env`, reach no network and add no unexpected environment variables:
tests/conftest.py imports the gateway, which sets LiteLLM's switches, before the module loads.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from hub.llm.gateway import LITELLM_SWITCHES

UNIT = Path(__file__).resolve().parent
ROOT = UNIT.parents[3]

# Variables a run may add: LiteLLM's switches, pytest's own, and LiteLLM's tokenizer cache
# directory (set by LiteLLM at import, a path, no setting of the Hub).
EXPECTED_NEW = {*LITELLM_SWITCHES, "TIKTOKEN_CACHE_DIR"}


def test_the_providers_test_file_alone_loads_no_env_file_and_reaches_no_network(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    env = {k: v for k, v in os.environ.items() if k not in LITELLM_SWITCHES}
    env |= {"HUB_ISOLATION_REPORT": str(report), "PYTHONPATH": str(UNIT)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "_isolation_spy",
            "-p",
            "no:cacheprovider",
            "-q",
            str(UNIT / "test_llm_providers.py"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    found = json.loads(report.read_text(encoding="utf-8"))
    assert found["events"] == []
    unexpected = [
        name
        for name in found["new_env"]
        if name not in EXPECTED_NEW and not name.startswith("PYTEST_")
    ]
    assert unexpected == []
