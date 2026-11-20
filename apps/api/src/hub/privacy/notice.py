"""The short privacy text of the chat (security §6 "Transparency", api.md `GET /privacy`, P8-05).

What the AI sees, who processes the data, how to reach a human. Plain sentences, no Markdown:
the chat shows them as text. Kept in code next to the functions it describes, so a change to
masking, providers or retention changes this text in the same pull request.
"""

from dataclasses import dataclass

UPDATED_AT = "2026-11-20"
TITLE = "How the Tallybridge AI assistant uses your data"


@dataclass(frozen=True)
class Section:
    heading: str
    text: str


SECTIONS: tuple[Section, ...] = (
    Section(
        "What the AI sees",
        "The assistant reads the messages of this conversation and, to answer questions about "
        "your account, your account profile, cards and transactions. Card numbers, security "
        "codes, PINs and one-time codes are removed before anything is stored or sent to the "
        "AI model. Email addresses, phone numbers and IBANs are replaced by placeholders "
        "before the model sees them.",
    ),
    Section(
        "Who processes the data",
        "Answers are written by a model of Anthropic, our only AI provider; under its terms "
        "for API use, this data is not used to train its models. Help-centre search runs on "
        "our own servers.",
    ),
    Section(
        "How long we keep it",
        "Support conversations are deleted 24 months after the last message. Short summaries "
        "of closed conversations, which help us answer your next question, are deleted after "
        "12 months. You can ask us to delete your support data at any time.",
    ),
    Section(
        "Talking to a person",
        "The assistant can make mistakes. You can ask for a human at any time with the "
        '"Talk to a human" button.',
    ),
)
