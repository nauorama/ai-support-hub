"""pytest plugin for test_llm_isolation.py, loaded with `-p _isolation_spy` before anything else.

Records every `load_dotenv` call and every `httpx.get` to a host other than 127.0.0.1 (both are
refused), and the environment variables that appeared during the session. Writes them as JSON to
the file named in HUB_ISOLATION_REPORT.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import dotenv
import httpx
import pytest

_BEFORE = set(os.environ)
_EVENTS: list[str] = []


def _no_dotenv(*args: object, **kwargs: object) -> bool:
    _EVENTS.append("load_dotenv")
    return False


_real_get = httpx.get


def _local_get_only(url: str, *args: object, **kwargs: object) -> httpx.Response:
    if urlsplit(str(url)).hostname != "127.0.0.1":
        _EVENTS.append(f"httpx.get {url}")
        raise httpx.ConnectError("network refused by _isolation_spy")
    return _real_get(url, *args, **kwargs)  # type: ignore[arg-type]


setattr(dotenv, "load_dotenv", _no_dotenv)  # noqa: B010 - replaced for the whole run on purpose
setattr(httpx, "get", _local_get_only)  # noqa: B010


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    report = {"events": _EVENTS, "new_env": sorted(set(os.environ) - _BEFORE)}
    Path(os.environ["HUB_ISOLATION_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
