"""Loaded by pytest before every other conftest and test module of the API tests.

LiteLLM reads its switches when it is first imported, and in its default mode it copies the
nearest `.env` (the developer's real one, with API keys) into `os.environ` and fetches files from
GitHub. A test module that imports `litellm` before `hub.llm.gateway` would get that default. So
the gateway, which sets the switches (`LITELLM_SWITCHES`) before it imports LiteLLM, is imported
here, first. test_llm_isolation.py checks that a test file run on its own leaks nothing.
"""

import os

from hub.llm.gateway import LITELLM_SWITCHES

assert all(os.environ.get(name) == value for name, value in LITELLM_SWITCHES.items())
