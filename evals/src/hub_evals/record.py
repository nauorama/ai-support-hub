"""`make record-llm`, judge part: record the judge test cases (P4-05) with real calls.

The same tool as `hub.llm.record` (gateway in record mode, G2 scan, `llm_calls` rows with context
`record`, models that already have a real recording are skipped), with the judge schemas and
its own folder `evals/tests/fixtures/llm/`, so the app never imports the evaluation package and
its fake mode never serves a judge answer. Needs real API keys and the database; costs money.
"""

from pathlib import Path

from hub.llm import record
from hub_evals.judges import JUDGE_SCHEMAS

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "evals" / "tests" / "fixtures" / "llm"


def main() -> None:
    record.main(FIXTURES_DIR, JUDGE_SCHEMAS, command="hub_evals.record")


if __name__ == "__main__":
    main()
