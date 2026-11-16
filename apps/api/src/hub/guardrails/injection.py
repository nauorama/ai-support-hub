"""Injection heuristics (G4, docs/security.md §3) and data-tag neutralisation (T2, T3).

``score_injection`` gives a pattern score from 0 to 1 and the names of the pattern groups
that matched. It is a signal for routing, not the defence: the defences are the hard policy
rules in code, the writer without action tools, and untrusted text kept inside data tags.

``neutralise_data_tags`` keeps that last defence intact: a ``</ticket>`` written by a customer
would otherwise close the prompt's data tag early, and a ``<kb>`` would open a fake one. Every
opening or closing tag with a data-tag name inside untrusted text has its angle brackets
written as ``&lt;`` and ``&gt;``.

Pure functions; no imports from the rest of the project.
"""

import re
import unicodedata

from pydantic import BaseModel, ConfigDict

# The data tags of the prompts (docs/ai-pipeline.md §7).
DATA_TAGS: tuple[str, ...] = ("ticket", "history", "customer", "kb", "account_facts", "memory")

# Starting value (docs/security.md §3, G4); P6-02 moves it into config/policy.yaml.
HIGH_INJECTION_SCORE = 0.5

# ASCII and full-width or small angle brackets: NFKC folds them to `<` and `>`, and a model
# may read them the same way.
_OPEN = "<\uff1c\ufe64"
_CLOSE = ">\uff1e\ufe65"
_DATA_TAG = re.compile(
    rf"[{_OPEN}]\s*/?\s*(?:{'|'.join(DATA_TAGS)})(?![\w-])[^{_OPEN}{_CLOSE}]*[{_CLOSE}]?",
    re.IGNORECASE,
)
_BRACKETS = str.maketrans({char: "&lt;" for char in _OPEN} | {char: "&gt;" for char in _CLOSE})


def neutralise_data_tags(text: str) -> str:
    """Write the angle brackets of every data tag in ``text`` as ``&lt;`` and ``&gt;``.

    Matches opening and closing tags, in any case, with spaces or attributes, and also a tag
    left without its ``>``. Other markup stays as it is.
    """
    return _DATA_TAG.sub(lambda match: match.group(0).translate(_BRACKETS), text)


class InjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float
    patterns: list[str]

    @property
    def high(self) -> bool:
        return self.score >= HIGH_INJECTION_SCORE


# Pattern groups and their weights. A single strong group reaches HIGH_INJECTION_SCORE; a weak
# one (0.3) needs a second group. Patterns run on normalised text (lower case, NFKC, one space).
_ROLES = r"(?:system|assistant|developer|admin|administrator|root|ai|model|bot|chatbot)"
_RULES = (
    r"(?:instructions?|rules|prompts?|directions|guidelines|guardrails|polic(?:y|ies)"
    r"|programming|restrictions|filters)"
)
_GROUPS: tuple[tuple[str, float, re.Pattern[str]], ...] = (
    (
        "override",
        0.6,
        re.compile(
            rf"\b(?:ignore|disregard|forget)\b(?: \w+){{0,3}} {_RULES}\b"
            rf"|\b(?:override|bypass|disable|turn off|deactivate) (?:all |any )?(?:of )?your "
            rf"(?:\w+ )?{_RULES}\b"
            r"|\bbypass (?:the |all |any )?(?:safety|guardrails|filters)\b"
            r"|\bnew (?:instructions|rules)\s*:"
            rf"|\b(?:do not|don't|stop) (?:follow|obey|apply)(?:ing)? (?:your|the|any) {_RULES}\b"
            r"|\byou (?:must|will) now (?:obey|follow|comply)\b"
        ),
    ),
    (
        "role_play",
        0.5,
        re.compile(
            r"\byou are now (?:an? |my |in |the )?(?:\w+ )?"
            r"(?:ai|assistant|bot|model|mode|persona|character|admin|developer)\b"
            r"|\bfrom now on,? you (?:are|act|respond|answer|reply|speak|only)\b"
            r"|\bpretend (?:to be|you are|you're|that you)\b"
            r"|\bact as (?:if you|an? (?:unrestricted|unfiltered|different|new) "
            rf"|(?:an? |the )?{_ROLES}\b)"
            r"|\brole[- ]?play\b"
            r"|\b(?:developer|god|jailbreak|dan) mode\b|\bjailbreak"
            r"|\bdo anything now\b"
        ),
    ),
    (
        "prompt_extraction",
        0.6,
        re.compile(
            r"\bsystem prompt\b"
            r"|\b(?:reveal|show|print|repeat|output|display|tell me|give me|leak|dump|share|"
            r"what (?:is|are|were)|list)\b(?: me)? (?:all )?(?:of )?your "
            r"(?:\w+ )?(?:prompt|instructions|configuration)\b"
            r"|\b(?:initial|hidden|original|secret) (?:system )?prompt\b"
            r"|\b(?:hidden|secret) instructions\b"
            r"|\b(?:everything|all the text|the text|words) (?:above|before) (?:this|my)\b"
        ),
    ),
    (
        "fake_markup",
        0.4,
        re.compile(
            rf"<\s*/?\s*(?:{'|'.join(DATA_TAGS)}|{_ROLES}|instructions?)(?![\w-])[^<>]*>"
            r"|(?:^|\n)\s*(?:system|assistant|developer)\s*:"
            r"|\[/?inst\]|<\|im_(?:start|end)\|>|<<sys>>|#{2,} ?(?:system|instruction)"
        ),
    ),
    (
        "encoded_block",
        0.4,
        re.compile(r"(?:\\x[0-9a-f]{2}){4,}|(?:\\u[0-9a-f]{4}){4,}|(?:&#x?[0-9a-f]+;){4,}"),
    ),
    (
        "decode_request",
        0.3,
        re.compile(
            r"\b(?:base64|rot13|rot-13|hex)\b(?: \w+){0,3} (?:decode|decoded|encoded)"
            r"|\b(?:decode|decipher) (?:this|the following|it|that)\b"
        ),
    ),
    (
        "forced_output",
        0.3,
        re.compile(
            r"\b(?:reply|respond|answer)(?: only)? with\b"
            r"|\byou (?:must|have to|will) "
            r"(?:say|reply|respond|confirm|answer|write|tell|promise)\b"
            r"|\b(?:say|write|confirm|promise)(?: in writing)? that (?:my|the|this) "
            r"(?:\w+ ){0,3}(?:is|was|has been|will be) "
            r"(?:approved|guaranteed|refunded|unblocked|unfrozen)\b"
        ),
    ),
)
# Base64 or hex blocks are checked on the original text: case matters for base64.
_BASE64_BLOCK = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")
_HEX_BLOCK = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,}(?![0-9A-Fa-f])")


def _normalise(text: str) -> str:
    """NFKC (full-width letters become ASCII), drop format characters such as zero-width
    spaces, case-fold, and collapse spaces and tabs (line breaks stay, for the role prefix)."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = text.casefold().replace("\u2019", "'")
    return re.sub(r"[^\S\n]+", " ", text)


def _encoded_block(text: str) -> bool:
    # A long hex block mixes letters and digits; a long number or "aaaa\u2026" does not.
    for match in _HEX_BLOCK.finditer(text):
        if any(char.isalpha() for char in match.group(0)) and any(
            char.isdigit() for char in match.group(0)
        ):
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
    normal = _normalise(text)
    patterns = [name for name, _, pattern in _GROUPS if pattern.search(normal)]
    if "encoded_block" not in patterns and _encoded_block(unicodedata.normalize("NFKC", text)):
        patterns.append("encoded_block")
    weights = {name: weight for name, weight, _ in _GROUPS}
    score = min(1.0, round(sum(weights[name] for name in patterns), 2))
    return InjectionResult(score=score, patterns=sorted(patterns))
