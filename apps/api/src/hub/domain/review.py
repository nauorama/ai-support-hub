"""Rules of the operator review of a draft (docs/api.md §3, P3-05b): edit ratio and citation
markers in an edited text. Pure functions; the console endpoints call them.

`ReviewDecision` (ai-pipeline §5, P6-03) is what resumes a paused run: built by the API from the
operator's request after its transaction committed, never by a model.
"""

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

type ReviewKind = Literal["approve", "edit", "reject", "reply", "close"]


class ReviewDecision(BaseModel):
    """The operator's decision on a paused run (ai-pipeline §1, §5).

    The API has already written everything the decision changes (the message, the draft, the
    ticket and the audit records, in one transaction under the ticket row lock, with the
    `ticket_version` and `Idempotency-Key` checks), and then resumes the run with it. The run
    records `message_id` as its final message and ends (owner decision, P6-03). `body` is the
    G1-masked text the operator sent (edit, reply, reject with a reply), None otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ReviewKind
    operator_id: str
    body: str | None = None
    approved_actions: list[str] = []
    reject_reason: str | None = None
    # The message the decision sent; None for a reject without a reply and for a close.
    message_id: str | None = None


_MARKER = re.compile(r"\[(\d+)\]")
_RATIO_STEP = Decimal("0.001")


def edit_ratio(draft_body: str, final_body: str) -> Decimal:
    """How much of the draft the operator changed: 0 = unchanged, 1 = nothing in common.

    `1 - difflib.SequenceMatcher.ratio()`, rounded to 3 decimals (`drafts.edit_ratio` is
    numeric(4,3)). It approximates a normalised edit distance: the ratio counts matching blocks,
    not single-character edits, so it is not Levenshtein, but it needs no new dependency and is
    good enough for the dashboard's median (data model, `drafts.edit_ratio`). `autojunk` is off:
    its heuristic treats frequent characters in texts over 200 characters as junk and would make
    the number depend on the text length.
    """
    if draft_body == final_body:
        return Decimal("0.000")
    matcher = SequenceMatcher(None, draft_body, final_body, autojunk=False)
    ratio = Decimal(1) - Decimal(str(matcher.ratio()))
    return ratio.quantize(_RATIO_STEP, rounding=ROUND_HALF_UP)


def markers_in(body: str) -> list[int]:
    """The `[n]` citation markers of a text, in order of appearance, each once."""
    return list(dict.fromkeys(int(match.group(1)) for match in _MARKER.finditer(body)))


def unknown_markers(body: str, citations: Iterable[Mapping[str, Any]]) -> list[int]:
    """Markers in an edited text that have no citation in the draft. The console refuses such a
    text: a marker without a source would reach the customer as a dead reference."""
    known = {citation["marker"] for citation in citations}
    return [marker for marker in markers_in(body) if marker not in known]


def citations_kept(body: str, citations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The draft's citations whose marker is still in the edited text, in the draft's order. A
    citation whose marker the operator deleted is not sent."""
    present = set(markers_in(body))
    return [dict(citation) for citation in citations if citation["marker"] in present]
