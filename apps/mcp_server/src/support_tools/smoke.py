"""Smoke test of the support-tools server with the official MCP Python client (`make mcp-smoke`).

Starts the server twice as a real subprocess, the way its clients do: over streamable HTTP with a
random bearer token (as the Hub), and over stdio (as Claude Code). On each it lists the tools and
calls one core tool, and prints what it saw. Exit code 0 only if both pass. Mock data only, no
network beyond 127.0.0.1, and the mock store goes to a temporary folder.
"""

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

EXPECTED_TOOLS = {
    "core_get_customer",
    "core_list_transactions",
    "core_get_transaction",
    "core_get_cards",
    "jira_create_issue",
    "jira_get_issue",
    "slack_post_escalation",
}
ANCHOR = "2026-11-12"
CALL = ("core_get_transaction", {"customer_ref": "CUS-0001", "reference": "TX-000011"})


async def _exercise(client: Client, transport: str) -> None:
    names = {tool.name for tool in (await client.list_tools()).tools}
    print(f"[{transport}] list_tools: {len(names)} tools: {', '.join(sorted(names))}")
    if names != EXPECTED_TOOLS:
        raise SystemExit(f"[{transport}] unexpected tool list")
    name, arguments = CALL
    result = await client.call_tool(name, arguments)
    content = result.structured_content
    if result.is_error or content is None:
        raise SystemExit(f"[{transport}] {name} failed")
    print(
        f"[{transport}] {name}({arguments['reference']}): type={content['type']} "
        f"status={content['status']} amount={content['amount']} {content['currency']} "
        f"requested_at={content['requested_at']} as_of={content['as_of']}"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _env(store: Path, token: str = "") -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "CORE_ANCHOR_DATE": ANCHOR,
        "MCP_MOCK_DB": str(store),
        "MCP_SERVER_TOKEN": token,
    }


async def _http(folder: Path) -> None:
    token = secrets.token_urlsafe(32)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    command = [sys.executable, "-m", "support_tools", "--transport", "http", "--port", str(port)]
    server = subprocess.Popen(  # noqa: S603 - our own module, fixed arguments
        command,
        env=_env(folder / "http.sqlite", token),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx2.AsyncClient() as plain:
            deadline = time.monotonic() + 20
            while True:
                try:
                    refused = await plain.post(url, json={})
                    break
                except httpx2.TransportError:
                    if time.monotonic() > deadline:
                        raise SystemExit("[http] the server did not start") from None
                    await asyncio.sleep(0.1)
        print(f"[http] request without a token: HTTP {refused.status_code}")
        if refused.status_code != 401:
            raise SystemExit("[http] a request without a token was not refused")
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            await _exercise(client, "http")
    finally:
        server.terminate()
        server.wait(timeout=10)


async def _stdio(folder: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "support_tools", "--transport", "stdio"],
        env=_env(folder / "stdio.sqlite"),
    )
    async with Client(params) as client:
        await _exercise(client, "stdio")


async def _main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        await _http(Path(folder))
        await _stdio(Path(folder))
    print("mcp smoke: ok")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
