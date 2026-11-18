"""First step of `make dev`: stop before anything starts when a required setting is empty.

`.env.example` leaves these empty on purpose (no shared demo secrets in the repository), so a
fresh clone must set them in `.env`. All missing names are listed at once, so one edit fixes
them all. Reads the settings the way the app does (process environment, then `.env`), names
variables only, never values, and writes nothing.
"""

import sys

from hub.config import Settings, SettingsError, load_settings

# Needed by `make seed` (operator passwords), by the demo chat (customer session, P2-06) and by
# the compose `mcp` service, whose HTTP transport refuses to start without a token (P7-01).
REQUIRED_FOR_DEV = (
    "SEED_OPERATOR_PASSWORD",
    "SESSION_SECRET",
    "DEMO_ACCESS_CODE",
    "MCP_SERVER_TOKEN",
)


def missing_settings(settings: Settings) -> list[str]:
    return [
        name for name in REQUIRED_FOR_DEV if not getattr(settings, name.lower()).get_secret_value()
    ]


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"make dev: {exc}", file=sys.stderr)
        return 1
    missing = missing_settings(settings)
    if missing:
        print(
            "make dev: these settings are empty: "
            + ", ".join(missing)
            + ". Set them in .env (see the comments in .env.example), then run `make dev` "
            "again. Nothing was started or written.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
