"""통합 검증: 툴 호출이 FastMCP -> 인증 -> 실제 HTTP 요청까지 올바르게 흐르는지.

creds 없이 MockTransport 로 결정적으로 검증한다. 핵심은 "전역 기본 + 호출시 override"
계좌 헤더 로직: 인자를 생략하면 클라이언트 기본값이 fall-through 되는지(빈 값으로
덮어쓰지 않는지)를 HTTP 레벨에서 확인한다.
"""

from __future__ import annotations

import httpx
from fastmcp import Client, FastMCP

from tossinvest_mcp.auth import ClientCredentialsAuth
from tossinvest_mcp.server import build_server

BASE = "https://openapi.tossinvest.com"
DEFAULT_ACCOUNT = "42"


def _build() -> tuple[FastMCP, list[httpx.Request], httpx.AsyncClient]:
    captured: list[httpx.Request] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    def token_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "T1", "token_type": "Bearer", "expires_in": 3600},
        )

    auth = ClientCredentialsAuth(
        token_url=f"{BASE}/oauth2/token",
        client_id="x",
        client_secret="y",
        transport=httpx.MockTransport(token_handler),
    )
    http = httpx.AsyncClient(
        base_url=BASE,
        auth=auth,
        headers={"X-Tossinvest-Account": DEFAULT_ACCOUNT},  # config 전역 기본
        transport=httpx.MockTransport(resource_handler),
    )
    return build_server(client=http), captured, http


async def test_tool_call_injects_bearer_and_query() -> None:
    mcp, captured, http = _build()
    try:
        async with Client(mcp) as c:
            # 응답 본문이 스펙 스키마와 다른 mock 이므로 출력검증은 무시; 요청만 검사
            await c.call_tool("getPrices", {"symbols": "005930"}, raise_on_error=False)
    finally:
        await http.aclose()

    req = captured[-1]
    assert req.headers.get("Authorization") == "Bearer T1"
    assert req.url.path == "/api/v1/prices"
    assert req.url.params.get("symbols") == "005930"


async def test_account_header_default_falls_through_when_omitted() -> None:
    """핵심: accountSeq 인자를 생략하면 전역 기본값(42)이 그대로 전송되어야 한다."""
    mcp, captured, http = _build()
    try:
        async with Client(mcp) as c:
            await c.call_tool("getHoldings", {}, raise_on_error=False)
    finally:
        await http.aclose()

    req = captured[-1]
    assert req.url.path == "/api/v1/holdings"
    assert req.headers.get("X-Tossinvest-Account") == DEFAULT_ACCOUNT, (
        "생략 시 전역 기본 계좌 헤더가 fall-through 되어야 함 "
        "(빈 값/누락으로 덮어쓰면 안 됨)"
    )


async def test_account_header_override_when_provided() -> None:
    """인자로 주면 해당 호출만 다른 계좌로 override 되어야 한다."""
    mcp, captured, http = _build()
    try:
        async with Client(mcp) as c:
            # 스키마가 string 으로 보정되었으므로 호출자는 문자열 accountSeq 를 전달
            await c.call_tool(
                "getHoldings", {"X-Tossinvest-Account": "999"}, raise_on_error=False
            )
    finally:
        await http.aclose()

    req = captured[-1]
    assert req.headers.get("X-Tossinvest-Account") == "999"
