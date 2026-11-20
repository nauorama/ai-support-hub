"""Leak scan of a database (P8-03): a planted card number or canary is found in text, JSON and
binary columns; canaries are allowed in the KB tables; machine values are not hits.

A database of its own, so leftovers of other tests cannot make a hit. Public test numbers only.
"""

from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy.engine import make_url

from hub_evals.leak_scan import Hit, scan_database

TEST_PAN = "4111 1111 1111 1111"  # Visa test number
CANARY = "CANARY-INT-HERON"
UUID = "0b74d664-3640-4097-8741-e5b0ef08c44e"
DATABASE = "leak_scan_test"


def _libpq(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def scan_db(migrated_db_url: str) -> Iterator[str]:
    """An empty database with a few tables of the shapes the scan reads."""
    with psycopg.connect(_libpq(migrated_db_url), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
        admin.execute(f"CREATE DATABASE {DATABASE}")
    url = make_url(migrated_db_url).set(database=DATABASE).render_as_string(hide_password=False)
    with psycopg.connect(_libpq(url), autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE messages (id int PRIMARY KEY, body text);"
            "CREATE TABLE drafts (id int PRIMARY KEY, answer varchar(2000), evidence jsonb);"
            "CREATE TABLE checkpoint_blobs (id int PRIMARY KEY, version text, blob bytea);"
            "CREATE TABLE kb_chunks (id int PRIMARY KEY, text text, content_hash text);"
            "CREATE TABLE audit_log (id int PRIMARY KEY, hash text, details jsonb);"
        )
        rows: list[tuple[str, tuple[object, ...]]] = [
            ("messages", ("my card is [CARD ****1111], PIN [REDACTED]",)),
            (
                "drafts",
                (
                    "Your withdrawal is pending [1].",
                    '{"score": 0.6095238095238095, "ids": ["' + UUID + '"]}',
                ),
            ),
            (
                "checkpoint_blobs",
                ("00000000000000000000000000000001.0.3454809039534249", b"\x92\xa4text masked"),
            ),
            (
                "kb_chunks",
                (
                    f"Internal reference: {CANARY}.",
                    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b2fa",
                ),
            ),
            ("audit_log", ("a" * 7 + "1234567890123456" + "b" * 41, f'{{"ticket_id": "{UUID}"}}')),
        ]
        for table, values in rows:
            marks = ", ".join(["%s"] * len(values))
            connection.execute(f"INSERT INTO {table} VALUES (1, {marks})", values)  # noqa: S608
    yield url
    with psycopg.connect(_libpq(migrated_db_url), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")


def _plant(url: str, statement: str, value: object) -> None:
    with psycopg.connect(_libpq(url), autocommit=True) as connection:
        connection.execute(statement, (value,))


async def test_a_clean_database_has_no_hit(scan_db: str) -> None:
    """Masked text, JSON numbers, UUIDs, hashes, versions and KB canaries are no hits."""
    assert await scan_database(scan_db, "db") == []


@pytest.mark.parametrize(
    ("statement", "value", "where", "kind"),
    [
        ("UPDATE messages SET body = %s", f"card {TEST_PAN}", "db.messages.body", "PAN"),
        ("UPDATE drafts SET answer = %s", f"see {CANARY}", "db.drafts.answer", "canary"),
        (
            "UPDATE drafts SET evidence = %s::jsonb",
            '{"quote": "PIN 4921"}',
            "db.drafts.evidence",
            "PIN",
        ),
        (
            "UPDATE checkpoint_blobs SET blob = %s",
            b"\x92\xa4text\xb3card " + TEST_PAN.encode(),
            "db.checkpoint_blobs.blob",
            "PAN",
        ),
        ("UPDATE kb_chunks SET text = %s", f"card {TEST_PAN}", "db.kb_chunks.text", "PAN"),
    ],
)
async def test_a_planted_leak_is_found(
    scan_db: str, statement: str, value: object, where: str, kind: str
) -> None:
    _plant(scan_db, statement, value)
    assert await scan_database(scan_db, "db") == [Hit("database", f"{where} row (0,2)", kind)]
