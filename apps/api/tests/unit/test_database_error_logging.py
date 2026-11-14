"""Database errors are logged as class and constraint name only (their text can hold rows)."""

import sys
from typing import Any

import psycopg
import pytest
import sqlalchemy.exc

from hub.observability.logging import DatabaseErrorSummary, database_error_processor

ROW_TEXT = "DETAIL:  Failing row contains (my private words, x)."


def _db_error() -> sqlalchemy.exc.IntegrityError:
    return sqlalchemy.exc.IntegrityError(
        "INSERT ...", {"body": "my private words"}, Exception(ROW_TEXT)
    )


def _process(exc_info: Any) -> dict[str, Any]:  # noqa: ANN401
    return dict(database_error_processor(None, "error", {"event": "x", "exc_info": exc_info}))


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def test_a_database_error_becomes_a_summary_with_the_frames_kept() -> None:
    error = _raised(_db_error())
    out = _process((type(error), error, error.__traceback__))
    exc_type, summary, traceback = out["exc_info"]
    assert exc_type is DatabaseErrorSummary
    assert str(summary) == "sqlalchemy.exc.IntegrityError (constraint: none)"
    assert summary.__cause__ is None
    assert summary.__suppress_context__
    assert traceback is error.__traceback__
    assert out["db_error_class"] == "sqlalchemy.exc.IntegrityError"
    assert out["db_constraint"] is None
    assert "private" not in repr(out)


def test_the_constraint_name_comes_from_the_psycopg_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig = psycopg.errors.CheckViolation(ROW_TEXT)

    class Diag:
        constraint_name = "ck_messages_masked_entities"

    monkeypatch.setattr(psycopg.errors.CheckViolation, "diag", property(lambda _self: Diag()))
    error = sqlalchemy.exc.IntegrityError("INSERT ...", None, orig)
    out = _process(error)
    assert out["db_constraint"] == "ck_messages_masked_entities"
    assert "ck_messages_masked_entities" in str(out["exc_info"][1])


def test_a_database_error_inside_a_group_or_as_a_cause_is_found() -> None:
    group = ExceptionGroup("task group", [ValueError("other"), _db_error()])
    assert _process(group)["db_error_class"] == "sqlalchemy.exc.IntegrityError"

    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = psycopg.OperationalError(ROW_TEXT)
    assert _process(wrapper)["db_error_class"] == "psycopg.OperationalError"


def test_exc_info_true_reads_the_current_exception() -> None:
    try:
        raise _db_error()
    except sqlalchemy.exc.IntegrityError:
        assert sys.exc_info()[0] is sqlalchemy.exc.IntegrityError
        out = _process(True)
    assert out["exc_info"][0] is DatabaseErrorSummary


@pytest.mark.parametrize("exc_info", [None, False, ValueError("not a database error")])
def test_other_records_pass_unchanged(exc_info: Any) -> None:  # noqa: ANN401
    event = {"event": "x", "exc_info": exc_info}
    assert database_error_processor(None, "error", dict(event)) == event


def test_a_cycle_in_the_chain_ends() -> None:
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__context__, second.__context__ = second, first
    assert "db_error_class" not in _process(first)
