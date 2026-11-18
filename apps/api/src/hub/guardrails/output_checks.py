"""Output checks of an AI draft (G7, docs/security.md §3; ai-pipeline §5, P5-04).

The code checks of the verify step. All of them must pass before the LLM claim check is even
called (`hub.graph.nodes.verify`). Each failed check gives a violation code; codes never hold
text of the draft, so they can go to the audit log.

1. `can_answer` is true and the answer is not empty (`cannot_answer`, `empty_answer`). When it
   fails, the other checks are skipped: there is nothing to check.
2. Every `[n]` marker has a citation (`marker_without_citation`), and every citation points to
   a source of the evidence (`unknown_source`).
3. At least one citation (`no_citation`). Every paragraph with a number, date, fee or time has
   a marker (`unmarked_fact`): a digit outside the markers, or a currency sign.
4. No card data (`card_data`: G1 finds a card number, CVV, PIN or 2FA code, or the text repeats
   a masked card or a `[REDACTED]` code); no placeholder left in the text (`placeholder`: a G3
   placeholder the run cannot restore, a template `{{x}}` or `[customer name]`); no text from
   `internal` documents (`internal_text`): the writer never receives them, so this is the
   second check. It compares hashes of 8-word windows of the draft with those of the internal
   chunks, leaving out windows that an external chunk has too (shared wording is not a leak).
5. No forbidden promise (`forbidden_promise:<name>`): refund, limit increase, unfreeze, the
   reason of a restriction, legal or tax advice. Patterns for a first-person promise or a done
   deed ("we will refund", "your refund will be issued", "your limit has been raised"), matched
   on the folded view of G4 (`injection.fold_text`), so invisible characters and look-alike
   letters cannot hide one. A first filter: the LLM verifier checks the rest, because a promise
   no source makes is an unsupported claim. The patterns are `forbidden_promises` in
   `config/policy.yaml` (moved there in P6-02); every check takes them as an argument.
6. Length at most 1,200 characters (`too_long`); language English (`not_english`).

The other model-written strings of a draft (`missing_information`, each proposed action) get
the checks of groups 4 and 5, with codes `<field>:<code>` (e.g.
`proposed_action:forbidden_promise:refund`), also when the draft cannot answer.

The checks run on the model's answer as it wrote it, with the run's placeholders restored and
**before** the card mask and before markers without a citation are removed
(`hub.graph.nodes.verify`): a check must see what the model did, not what the floor repaired.

Canary strings and prompt fragments (security T5, T6) come with the attack suite (P8-02).
Pure functions; no imports from the rest of the project except the other guardrails.
"""

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from hub.guardrails.injection import fold_text
from hub.guardrails.masking import mask

MAX_LENGTH = 1200
# Words per window of the internal-text check: long enough that ordinary phrases do not match,
# short enough that a copied sentence does.
SHINGLE_WORDS = 8
# Language heuristic: below this many words the text is too short to tell.
MIN_WORDS_FOR_LANGUAGE = 8
# Share of letters that must be ASCII.
MIN_ASCII_LETTERS = 0.9


class Violation(StrEnum):
    CANNOT_ANSWER = "cannot_answer"
    EMPTY_ANSWER = "empty_answer"
    MARKER_WITHOUT_CITATION = "marker_without_citation"
    UNKNOWN_SOURCE = "unknown_source"
    NO_CITATION = "no_citation"
    UNMARKED_FACT = "unmarked_fact"
    CARD_DATA = "card_data"
    PLACEHOLDER = "placeholder"
    INTERNAL_TEXT = "internal_text"
    FORBIDDEN_PROMISE = "forbidden_promise"
    TOO_LONG = "too_long"
    NOT_ENGLISH = "not_english"


_MARKER = re.compile(r"\[(\d+)\]")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_FACT = re.compile(r"\d|[€$£¥]")
_CARD_ENTITIES = frozenset({"PAN", "CVV", "PIN", "2FA"})
# What masking writes in place of card data: the writer must not repeat it either.
_MASKED_CARD = re.compile(r"\[CARD \*{4}\d{0,4}\]|\[REDACTED\]")
_PLACEHOLDERS = re.compile(
    r"<(?:EMAIL|PHONE|IBAN)_\d+>"
    r"|\{\{[^{}]*\}\}"
    r"|\[(?:customer(?:'s)? name|your name|first name|last name|name|agent name|amount|date"
    r"|reference|transaction id)\]",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W_]+")


def _word_set(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Common English function words, for the language heuristic.
_ENGLISH = _word_set(
    """a about after all also an and any are as at be been before but by can could do does for
    from has have how i if in into is it its may more must no not of on once or our out over
    please should so than that the their them then there these they this to up us was we what
    when where which while will with within would you your"""
)
# Common function words of other languages written in Latin letters (German, Spanish, French,
# Italian, Portuguese, Dutch, Polish), none of them an English word.
_OTHER = _word_set(
    """aber auf bitte das dem der des dich ein eine einem einen einer haben habe ich ist kann mein
    meine mich mir mit nicht noch sie sind und uns von wie wir zu zum zur
    al como del el ella ellos en es esta este estoy hola la las los mi mis muy para pero
    por porque que se su sus tengo una y ya
    au aux avec ce cette dans des du est et je les leur mais mes mon nous ne ou pas pour qui
    sur tu une vous
    che della di gli il lo mio nel non sono un
    ao com da das dos em está meu minha não os uma você
    een het ik je mijn niet ook op van voor wat zijn
    ale co czy dla jak jest mam mnie nie od się w z że"""
)

# Forbidden promises: name -> patterns, from `forbidden_promises` in config/policy.yaml
# (`hub.guardrails.policy.Policy.promises`), matched on the G4-folded text (`promise_text`).
type Promises = Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class DraftToCheck:
    """A draft as the model wrote it. `answer`: the run's placeholders restored, not yet card
    masked, markers still in. `citations`: (marker, source id) pairs. `evidence_ids`: the ids
    the writer was given (chunk ids, account fact ids). `other_texts`: the other model-written
    strings as (field, text), `missing_information` and each proposed action; they get the
    checks that do not depend on the answer's shape (card data, placeholders, internal text,
    forbidden promises), with codes `<field>:<code>`, also when the draft cannot answer."""

    can_answer: bool
    answer: str
    citations: Sequence[tuple[int, str]]
    evidence_ids: frozenset[str]
    other_texts: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class InternalText:
    """Hashes of the 8-word windows of the internal chunks that no external chunk has."""

    hashes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(cls, internal: Iterable[str], external: Iterable[str] = ()) -> "InternalText":
        shared = {digest for text in external for digest in shingles(text)}
        return cls(frozenset(d for text in internal for d in shingles(text) if d not in shared))

    def found_in(self, text: str) -> bool:
        return not self.hashes.isdisjoint(shingles(text))


# --- helpers --------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Case-folded NFKC text with straight apostrophes and single spaces."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(text.split())


def words(text: str) -> list[str]:
    return _WORD.findall(fold(_MARKER.sub(" ", text)))


def shingles(text: str) -> set[str]:
    """SHA-256 of every window of `SHINGLE_WORDS` words (markers and punctuation ignored)."""
    tokens = words(text)
    return {
        hashlib.sha256(" ".join(tokens[i : i + SHINGLE_WORDS]).encode()).hexdigest()
        for i in range(len(tokens) - SHINGLE_WORDS + 1)
    }


def markers(text: str) -> list[int]:
    return [int(number) for number in _MARKER.findall(text)]


def paragraphs(text: str) -> list[str]:
    return [part for part in _PARAGRAPH_BREAK.split(text) if part.strip()]


def looks_english(text: str) -> bool:
    """A heuristic, no dependency: most letters are ASCII, and common English function words
    outnumber those of other languages (when there are any). Fee tables and short texts (under
    `MIN_WORDS_FOR_LANGUAGE` words) pass: they have few function words of any language."""
    letters = [c for c in _MARKER.sub(" ", text) if c.isalpha()]
    if not letters:
        return True
    if sum(c.isascii() for c in letters) / len(letters) < MIN_ASCII_LETTERS:
        return False
    tokens = words(text)
    if len(tokens) < MIN_WORDS_FOR_LANGUAGE:
        return True
    other = sum(t in _OTHER for t in tokens)
    return other == 0 or sum(t in _ENGLISH for t in tokens) > other


def has_card_data(text: str) -> bool:
    result = mask(text)
    if result.failed:
        return True
    found = any(count > 0 for kind, count in result.counts.items() if kind in _CARD_ENTITIES)
    return found or bool(_MASKED_CARD.search(text))


def promise_text(text: str) -> str:
    """NFKC, then the G4 folded view (`injection.fold_text`), straight apostrophes and one
    space: a zero-width space or a Cyrillic look-alike cannot hide a promise."""
    folded = fold_text(unicodedata.normalize("NFKC", text))
    folded = folded.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    return " ".join(folded.split())


def forbidden_promises(text: str, patterns: Promises) -> list[str]:
    """Names of the forbidden promises the text makes, in the order of `patterns`."""
    folded = promise_text(text)
    return [name for name, group in patterns.items() if any(re.search(p, folded) for p in group)]


# --- the checks -----------------------------------------------------------------------------


def check_draft(
    draft: DraftToCheck,
    *,
    promises: Promises,
    internal: InternalText | None = None,
) -> list[str]:
    """Violation codes in check order; an empty list when every code check passes. The checks
    of the answer come first, then those of the other model-written strings."""
    found = _answer_violations(draft, internal, promises)
    for name, text in draft.other_texts:
        found += [f"{name}:{code}" for code in content_violations(text, promises, internal)]
    return found


def content_violations(
    text: str,
    promises: Promises,
    internal: InternalText | None = None,
) -> list[str]:
    """The checks of check group 4 and 5 that apply to any model-written text."""
    found: list[str] = []
    if has_card_data(text):
        found.append(Violation.CARD_DATA)
    if _PLACEHOLDERS.search(text):
        found.append(Violation.PLACEHOLDER)
    if internal is not None and internal.found_in(text):
        found.append(Violation.INTERNAL_TEXT)
    found += [f"{Violation.FORBIDDEN_PROMISE}:{n}" for n in forbidden_promises(text, promises)]
    return found


def _answer_violations(
    draft: DraftToCheck,
    internal: InternalText | None,
    promises: Promises,
) -> list[str]:
    if not draft.can_answer:
        return [Violation.CANNOT_ANSWER]
    text = draft.answer
    if not text.strip():
        return [Violation.EMPTY_ANSWER]
    found: list[str] = []
    cited = {marker for marker, _ in draft.citations}
    if any(marker not in cited for marker in markers(text)):
        found.append(Violation.MARKER_WITHOUT_CITATION)
    if any(source not in draft.evidence_ids for _, source in draft.citations):
        found.append(Violation.UNKNOWN_SOURCE)
    if not draft.citations:
        found.append(Violation.NO_CITATION)
    if any(_FACT.search(_MARKER.sub("", part)) and not markers(part) for part in paragraphs(text)):
        found.append(Violation.UNMARKED_FACT)
    found += content_violations(text, promises, internal)
    if len(text) > MAX_LENGTH:
        found.append(Violation.TOO_LONG)
    if not looks_english(text):
        found.append(Violation.NOT_ENGLISH)
    return found


def operator_text_violations(text: str) -> list[str]:
    """Operator-edited text gets the card check only (security G7)."""
    return [Violation.CARD_DATA] if has_card_data(text) else []
