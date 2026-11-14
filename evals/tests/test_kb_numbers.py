"""`make kb-numbers`: numbers in the knowledge base that are not in the world file."""

from pathlib import Path

import pytest

from hub_evals.kb_numbers import Finding, main, normalise, numbers, unknown_numbers

# The world file writes ranges with an en dash (U+2013).
WORLD = "Limits: €30,000 per month, €1.50 fee, 2.9% + €0.30, 3\u20135 days, 15:00 CET, 2FA."


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("€30,000 and 1,000", ["30,000", "1,000"]),
        ("2.9% + €0.30", ["2.9", "0.30"]),
        ("3\u20135 business days", ["3", "5"]),
        ("before 15:00 CET", ["15", "00"]),
        ("See ERR-2015.", []),  # error codes are checked by data-check
        ("Use 2FA and 3-D Secure", ["3"]),  # digits inside a word are not numbers
        ("1. Open the app\n2. Tap Cards\n  3. Done", []),  # list markers
        ("Step 7 of the list", ["7"]),
        ("A sentence ends with 10.", ["10"]),
    ],
)
def test_numbers(text: str, found: list[str]) -> None:
    assert [value for _, value in numbers(text)] == found


def test_equal_values_match() -> None:
    assert normalise("15.00") == normalise("15")
    assert normalise("30,000") == normalise("30000")
    assert normalise("0.3") == normalise("0.30")


def test_unknown_numbers_with_locations() -> None:
    article = "---\nid: a\nupdated_at: 2026-11-14\n---\n## Fees\nA fee of €1.50.\nAbout 7 days.\n"
    csv = "code,title\nERR-2015,Costs 15.00 or 99\n"
    findings = unknown_numbers(WORLD, {"kb/a.md": article, "kb/codes.csv": csv})
    assert findings == [Finding("7", "kb/a.md:7"), Finding("99", "kb/codes.csv:2")]


def test_main_prints_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("## A\nOnly 42 days.\n", encoding="utf-8")
    (kb / "notes.txt").write_text("99", encoding="utf-8")  # not a KB file type
    world = tmp_path / "world.md"
    world.write_text(WORLD, encoding="utf-8")
    assert main([str(kb), str(world)]) == 0
    out = capsys.readouterr().out
    assert f"{kb / 'a.md'}:2: 42" in out
    assert "99" not in out
    assert "1 number(s) not in" in out


def test_main_usage_and_unreadable_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
    assert main([str(tmp_path / "nope"), str(tmp_path / "world.md")]) == 2
    assert "cannot read" in capsys.readouterr().err
