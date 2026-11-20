"""Leak scan (P8-03): a planted leak is found, clean recordings and machine values are not.

Public test card numbers only. The database part is tested in
`apps/api/tests/integration/test_leak_scan.py`.
"""

import io
import json
from pathlib import Path

import pytest

from hub_evals import leak_scan
from hub_evals.leak_scan import Hit, Json, find, find_in_json, find_in_line

TEST_PAN = "4111 1111 1111 1111"  # Visa test number
CANARY = "CANARY-INT-HERON"
UUID = "0b74d664-3640-4097-8741-e5b0ef08c44e"


def _recording(root: Path, name: str, value: object) -> Path:
    path = root / "evals" / ".cache" / "llm" / "real" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def _request(text: str, node: str = "write", model: str = "anthropic/claude-sonnet-5-5") -> Json:
    return {
        "request": {"node": node, "model": model, "messages": [{"role": "user", "content": text}]},
        "response": {"content": "{}"},
    }


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        (f"card {TEST_PAN}", ["PAN"]),
        ("my PIN is 4921", ["PIN"]),
        (f"see {CANARY}", ["canary"]),
        (f"{TEST_PAN} {CANARY}", ["PAN", "canary"]),
        ("card [CARD ****1111], PIN [REDACTED]", []),
        (f"ticket {UUID}", []),
    ],
)
def test_find(text: str, kinds: list[str]) -> None:
    assert find(text) == kinds


@pytest.mark.parametrize(
    "value",
    [
        "a" * 7 + "1234567890123456" + "b" * 41,  # a SHA-256 digest with a 16-digit run
        "00000000000000000000000000000001.0.3454809039534249",  # LangGraph channel version
        "1795167410.000006",  # Slack message timestamp
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b2fa",  # `2fa` in hex
    ],
)
def test_machine_values_are_not_card_data(value: str) -> None:
    assert find(value) == []
    assert find(f"hash {value} stored") == []


def test_a_card_next_to_a_machine_value_is_found() -> None:
    assert find(f"1795167410.000006 card {TEST_PAN}") == ["PAN"]


def test_json_numbers_and_escaped_line_breaks_are_not_text() -> None:
    value: Json = {
        "embedding": [0.056046012789011, -0.054561350494623184],
        "score": 0.6095238095238095,
    }
    assert find_in_json(value) == []
    # The escaped "\n" before ERR made G1 read "nERR-3004" and the PIN rule took the code.
    line = json.dumps({"text": "Then use the right PIN.\n\nError codes\nERR-3004 Card cancelled"})
    assert find_in_line(line) == []


def test_a_leak_in_a_json_key_or_a_nested_string_is_found() -> None:
    assert find_in_json({TEST_PAN: 1}) == ["PAN"]
    assert find_in_json({"a": [{"b": f"x {CANARY}"}]}) == ["canary"]


def test_the_checkers_own_reason_may_name_a_canary() -> None:
    row: dict[str, Json] = {"reason": f"{CANARY} is not in the answer", "draft": {"answer": "No."}}
    assert find_in_json(row) == []
    assert find_in_json({**row, "draft": {"answer": CANARY}}) == ["canary"]


def test_a_planted_leak_in_a_recorded_request_is_found(tmp_path: Path) -> None:
    _recording(tmp_path, "clean.json", _request("Where is my withdrawal? [CARD ****1111]"))
    _recording(tmp_path, "planted.json", _request(f"my card is {TEST_PAN}"))
    hits = list(leak_scan.scan_recordings(tmp_path))
    assert hits == [Hit("recordings", "evals/.cache/llm/real/planted.json", "PAN")]


def test_a_planted_canary_in_a_run_result_is_found(tmp_path: Path) -> None:
    run = tmp_path / "evals" / "runs" / "2026-11-20-attacks"
    run.mkdir(parents=True)
    rows = [
        {"id": "A-001", "draft": {"answer": "ok"}},
        {"id": "A-007", "draft": {"answer": CANARY}},
    ]
    (run / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    hits = list(leak_scan.scan_recordings(tmp_path))
    assert hits == [Hit("recordings", "evals/runs/2026-11-20-attacks/results.jsonl:2", "canary")]


def test_an_embedding_request_may_hold_canaries_but_not_cards(tmp_path: Path) -> None:
    """It indexes the KB, internal articles included."""
    embedding = {"request": {"node": "embedding", "model": "fastembed/BAAI/bge-small-en-v1.5"}}
    _recording(tmp_path, "kb.json", {**embedding, "texts": [f"Internal reference: {CANARY}."]})
    assert list(leak_scan.scan_recordings(tmp_path)) == []
    _recording(tmp_path, "kb.json", {**embedding, "texts": [TEST_PAN]})
    assert [hit.kind for hit in leak_scan.scan_recordings(tmp_path)] == ["PAN"]


def test_a_recording_that_is_not_json_is_scanned_by_line(tmp_path: Path) -> None:
    path = _recording(tmp_path, "broken.json", {})
    path.write_text(f"not json\ncard {TEST_PAN}\n", encoding="utf-8")
    hits = list(leak_scan.scan_recordings(tmp_path))
    assert hits == [Hit("recordings", "evals/.cache/llm/real/broken.json:2", "PAN")]


def test_a_planted_leak_in_the_logs_is_found() -> None:
    logs = io.StringIO(
        f'{{"event": "run started", "ticket_id": "{UUID}"}}\n'
        f"api-1 | slack mock: escalation 1795167410.000006 for ticket {UUID}\n"
        f'{{"event": "oops", "text": "card {TEST_PAN}"}}\n'
        f"mcp-1 | counterparty {CANARY}\n"
    )
    hits = list(leak_scan.scan_logs(logs, "stdin"))
    assert hits == [Hit("logs", "stdin:3", "PAN"), Hit("logs", "stdin:4", "canary")]


def test_a_failed_mask_is_a_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(text: str) -> object:
        return type("R", (), {"failed": True, "counts": {}})()

    monkeypatch.setattr(leak_scan, "mask", broken)
    assert find("anything") == ["mask_failed"]


def test_main_exits_1_on_a_planted_leak_and_names_only_the_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording(tmp_path, "planted.json", _request(f"my card is {TEST_PAN}"))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"log {CANARY}\n"))
    code = leak_scan.main(["--no-database", "--logs", "-", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "evals/.cache/llm/real/planted.json: PAN" in out
    assert "stdin:1: canary" in out
    assert "2 hit(s)" in out
    assert "4111" not in out
    assert CANARY not in out


def test_main_exits_0_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _recording(tmp_path, "clean.json", _request("Where is my withdrawal?"))
    log = tmp_path / "api.log"
    log.write_text(f"ticket {UUID} done\n", encoding="utf-8")
    code = leak_scan.main(["--no-database", "--logs", str(log), "--root", str(tmp_path)])
    assert code == 0
    assert capsys.readouterr().out.endswith("leak-scan: 0 hit(s)\n")


def test_main_exits_2_on_a_missing_log_file(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.log")
    assert leak_scan.main(["--no-database", "--logs", missing, "--root", str(tmp_path)]) == 2
