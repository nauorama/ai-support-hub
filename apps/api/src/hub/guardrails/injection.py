"""Injection heuristics (G4, docs/security.md §3) and data-tag neutralisation (T2, T3).

``score_injection`` gives a pattern score from 0 to 1 and the names of the pattern groups
that matched. It is a signal for routing, not the defence: the defences are the hard policy
rules in code, the writer without action tools, and untrusted text kept inside data tags.

``neutralise_data_tags`` keeps that last defence intact: a ``</ticket>`` written by a customer
would otherwise close the prompt's data tag early, and a ``<kb>`` would open a fake one. Every
opening or closing tag with a data-tag name inside untrusted text has its angle brackets
written as ``&lt;`` and ``&gt;``.

Both work on the same folded view of the text (``_fold``): NFKD, format characters such as
zero-width spaces and soft hyphens, combining marks such as variation selectors and Hangul
fillers removed, case-folded, and common look-alike letters and
angle brackets mapped to ASCII. So a tag or a phrase written with full-width letters, a
Cyrillic ``\u0456`` or a hidden zero-width space is still found.

Pure functions; no imports from the rest of the project.
"""

import re
import unicodedata

from pydantic import BaseModel, ConfigDict

# The data tags of the prompts (docs/ai-pipeline.md §7). A test checks that every tag used in
# `prompts/` is listed here. The last group belongs to the evaluation judges (`judge_*`, eval
# plan §5): the draft they judge is model text that can quote a customer, so it gets the same
# protection.
DATA_TAGS: tuple[str, ...] = (
    "ticket",
    "history",
    "customer",
    "kb",
    "account_facts",
    "memory",
    "draft",
    "key_facts",
    "evidence",
    "attack_goal",
    "rubric",
    "transcript",
    "tool_calls",
)

# Look-alikes NFKD does not fold: Cyrillic and Greek letters that look like Latin ones, and
# angle brackets (single guillemets, CJK and mathematical angle brackets, modifier letters).
_LOOKALIKES = str.maketrans(
    {
        **dict.fromkeys("\u2039\u3008\u27e8\u02c2\u1438\u276e\u29fc", "<"),
        **dict.fromkeys("\u203a\u3009\u27e9\u02c3\u1433\u276f\u29fd", ">"),
        # Cyrillic
        "\u0430": "a", "\u0441": "c", "\u0435": "e", "\u04bb": "h", "\u0456": "i",
        "\u0458": "j", "\u043a": "k", "\u043c": "m", "\u043e": "o", "\u0440": "p",
        "\u0455": "s", "\u0442": "t", "\u0443": "y", "\u0445": "x", "\u0457": "i",
        # Greek
        "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bf": "o",
        "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03bd": "v", "\u03c7": "x",
    }
)  # fmt: skip


# Invisible characters that are not format characters (Cf) or combining marks: Hangul fillers.
_INVISIBLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")
# Dropped in the folded view: format characters (zero-width space, soft hyphen, ...),
# combining marks (variation selectors, the combining grapheme joiner, accents) and enclosing
# marks. Tag names and pattern words are ASCII, and the original text is kept, so this only
# widens what is found.
_DROPPED_CATEGORIES = frozenset({"Cf", "Mn", "Me"})


def _fold_char(char: str) -> str:
    # NFKD, not NFKC: it splits a sign with a combining overlay (U+226E, "not less-than") into
    # `<` and the mark, and the mark is then dropped.
    decomposed = unicodedata.normalize("NFKD", char.translate(_LOOKALIKES))
    kept = "".join(
        part
        for part in decomposed
        if unicodedata.category(part) not in _DROPPED_CATEGORIES and part not in _INVISIBLE_LETTERS
    )
    return kept.casefold().translate(_LOOKALIKES)


def fold_text(text: str) -> str:
    """The folded view of G4 (look-alikes mapped, format characters, combining marks and
    invisible letters dropped, case-folded), for other guardrails that match words: the
    forbidden-promise check of `output_checks.py` (P5-04)."""
    return _fold(text)[0]


def _fold(text: str) -> tuple[str, list[int]]:
    """The folded text, and for each of its characters the index of the original one."""
    parts: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = _fold_char(char)
        parts.append(folded)
        origin.extend([index] * len(folded))
    return "".join(parts), origin


# A data tag in the folded text: the bracket, an optional slash, the name, then attributes up
# to the closing bracket. Quoted attribute values may hold brackets (`<kb id="<x>">`). A tag
# left without its `>` is matched too.
_DATA_TAG = re.compile(
    rf"<\s*/?\s*(?:{'|'.join(DATA_TAGS)})(?![\w-])(?:\"[^\"]*\"|'[^']*'|[^<>\"'])*>?"
)


def neutralise_data_tags(text: str) -> str:
    """Write the angle brackets of every data tag in ``text`` as ``&lt;`` and ``&gt;``.

    Matches opening and closing tags, in any case, with spaces or attributes, look-alike
    letters or brackets, hidden format characters, and a tag left without its ``>``. Other
    characters stay as they are; other markup is not changed.
    """
    folded, origin = _fold(text)
    parts: list[str] = []
    position = 0
    for match in _DATA_TAG.finditer(folded):
        # max(): two matches could meet inside one original character that folds to several.
        start, end = max(origin[match.start()], position), origin[match.end() - 1] + 1
        parts.append(text[position:start])
        for char in text[start:end]:
            bracket = _fold_char(char)
            parts.append("&lt;" if bracket == "<" else "&gt;" if bracket == ">" else char)
        position = end
    parts.append(text[position:])
    return "".join(parts)


class InjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float
    patterns: list[str]


# Pattern groups and their weights. A single strong group reaches the high score of
# config/policy.yaml (`injection.high_score`, 0.5, which hands the ticket off); a weak
# one (0.3, 0.4) needs a second group. All groups but `fake_markup` run on the folded text
# with every run of whitespace made one space; `fake_markup` keeps the line breaks.
_W = r"\W+"  # between two words: spaces, line breaks, dashes, quotes
_WORDS = r"(?:(?!my\b|our\b)\w+\W+)"  # filler words, but not the customer's own ("my rules")
_RULES = r"(?:instructions?|rules|prompts?|directions|guidelines|guardrails|programming|context)"
_QUALIFIER = r"(?:previous|prior|above|earlier|preceding|all|your|system|original|initial)"
_AI = r"(?:ai|assistant|bot|chatbot|model|persona|character|admin|administrator|developer|dan)"
_SENTENCE_START = r"(?:^|[.!?:;]\s*|\byou\W+(?:\w+\W+)?|\bplease\W+|\bnow\W+|\blet's\W+)"
_GROUPS: tuple[tuple[str, float, bool, re.Pattern[str]], ...] = (
    (
        "override",
        0.6,
        False,
        re.compile(
            rf"\b(?:ignore|disregard|forget|override|bypass)\b{_W}{_WORDS}{{0,4}}?"
            rf"{_QUALIFIER}\b{_W}(?:\w+{_W}){{0,2}}?{_RULES}\b"
            rf"|\b(?:ignore|disregard|forget)\b{_W}(?:the{_W}|everything{_W}|all{_W})?"
            r"(?:above|before|prior context|previous context|what you were told)\b"
            r"|\boverride\s*:"
            rf"|\b(?:override|bypass|disable|turn off|deactivate)\b{_W}(?:\w+{_W}){{0,2}}?"
            r"(?:safety|guardrails|filters|content filter)\b"
            r"|\bnew (?:instructions|rules)\s*:"
            r"|\b(?:do not|don't|stop) (?:follow|obey)(?:ing)? (?:your|the|any|these) "
            r"(?:\w+ )?(?:instructions|rules|guidelines|programming)\b"
            r"|\byou (?:must|will) now (?:obey|follow|comply)\b"
        ),
    ),
    (
        "role_play",
        0.5,
        False,
        re.compile(
            rf"\byou are now (?:an? |my |the )?(?:\w+ )?{_AI}\b"
            r"|\bfrom now on,? you (?:will |must |shall )?"
            r"(?:act|respond|answer|reply|speak|only (?:reply|answer|respond|say))\b"
            rf"|\bfrom now on,? you are (?:an? |my |the )?(?:\w+ )?{_AI}\b"
            rf"|{_SENTENCE_START}pretend (?:to be|you are|you're|that you)\b"
            rf"|{_SENTENCE_START}act as\b"
            rf"|\bact (?:as|like) (?:if you|an? (?:unrestricted|unfiltered|different|new) {_AI})\b"
            rf"|\byou are no longer (?:an? |my |the )?(?:\w+ ){{0,2}}{_AI}\b"
            r"|\byour new (?:task|role|job|instructions?) (?:is|are)\b"
            r"|\brole[- ]?play\b"
            r"|\b(?:enter|enable|activate|switch to|you are (?:now )?in) "
            r"(?:developer|god|dan|jailbreak|unrestricted) mode\b"
            r"|\bjailbreak (?:mode|prompt)\b|\bdo anything now\b"
        ),
    ),
    (
        "prompt_extraction",
        0.6,
        False,
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|display|tell me|give me|leak|dump|share|"
            r"write out|copy|list|what (?:is|are|was|were))\b"
            r"(?: me)?(?: all| the| of)* (?:your )?(?:\w+ )?"
            r"(?:system prompt|prompt|instructions|configuration|guidelines)\b"
            r"(?! (?:for|on|about|to|regarding|when|if|how|in)\b)"
            r"|\b(?:initial|hidden|original|secret) (?:system )?prompt\b"
            r"|\b(?:hidden|secret) instructions\b"
            r"|\b(?:everything|all the text|the text|words) (?:above|before) (?:this|my)\b"
        ),
    ),
    (
        "fake_markup",
        0.4,
        True,
        re.compile(
            rf"<\s*/?\s*(?:{'|'.join(DATA_TAGS)}|system|assistant|developer|instructions?)"
            r"(?![\w-])[^<>]*>"
            r"|(?:^|\n)\s*(?:system|developer)\s*:"
            r"|\[/?inst\]|<\|im_(?:start|end)\|>|<<sys>>|#{2,} ?(?:system|instruction)"
        ),
    ),
    (
        "encoded_block",
        0.4,
        False,
        re.compile(r"(?:\\x[0-9a-f]{2}){4,}|(?:\\u[0-9a-f]{4}){4,}|(?:&#x?[0-9a-f]+;){4,}"),
    ),
    (
        "decode_request",
        0.3,
        False,
        re.compile(
            r"\b(?:base64|rot13|rot-13)\b"
            r"|\b(?:decode|decipher|translate) (?:this|it|the following|that)\b"
            r"(?:\W+\w+){0,4}?\W+(?:and|then) (?:follow|do|execute|obey|run|apply)\b"
        ),
    ),
    (
        "forced_output",
        0.3,
        False,
        re.compile(
            r"\b(?:reply|respond|answer) only with\b"
            r"|\b(?:reply|respond|answer) with (?:only |just )?(?:the word|[\"']|yes\b|approved\b)"
            r"|\byou (?:must|have to|will) (?:say|reply|respond|confirm|answer|write|promise) "
            r"(?:that|with|only|yes|[\"'])"
            r"|\b(?:say|write|confirm|promise)(?: in writing)? that (?:my|the|this) "
            r"(?:\w+ ){0,3}(?:is|was|has been|will be) "
            r"(?:approved|guaranteed|refunded|unblocked|unfrozen)\b"
        ),
    ),
)
_WEIGHTS = {name: weight for name, weight, _, _ in _GROUPS}
# Base64 or hex blocks are checked on the NFKC text, not the folded one: case matters.
_BASE64_BLOCK = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_BLOCK = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,}(?![0-9A-Fa-f])")


def _encoded_block(text: str) -> bool:
    # A long hex block mixes letters and digits; a long number or "aaaa…" does not.
    for match in _HEX_BLOCK.finditer(text):
        block = match.group(0)
        if any(char.isalpha() for char in block) and any(char.isdigit() for char in block):
            return True
    for match in _BASE64_BLOCK.finditer(text):
        block = match.group(0)
        # Real base64 mixes cases and digits; a long word or a long number does not.
        if (
            any(char.islower() for char in block)
            and any(char.isupper() for char in block)
            and any(char.isdigit() for char in block)
        ):
            return True
    return False


def score_injection(text: str) -> InjectionResult:
    """Score from 0 (no pattern) to 1, the sum of the weights of the matched groups, capped."""
    folded, _ = _fold(text.replace("\u2019", "'"))
    lined = re.sub(r"[^\S\n]+", " ", folded)
    flat = re.sub(r"\s+", " ", folded)
    patterns = [
        name for name, _, lines, pattern in _GROUPS if pattern.search(lined if lines else flat)
    ]
    if "encoded_block" not in patterns and _encoded_block(unicodedata.normalize("NFKC", text)):
        patterns.append("encoded_block")
    score = min(1.0, round(sum(_WEIGHTS[name] for name in patterns), 2))
    return InjectionResult(score=score, patterns=sorted(patterns))
