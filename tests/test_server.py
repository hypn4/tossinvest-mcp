"""정적 검증 — 자격증명 없이 동작. 핀된 스펙으로 서버가 올바르게 조립되는지 확인."""

from __future__ import annotations

import httpx
from fastmcp import Client, FastMCP
from mcp.types import GetPromptResult, TextContent
from pydantic import SecretStr

from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server, load_spec

# 거래 비활성(기본) 시 노출되는 읽기 전용 툴: 스펙 생성 17개 + 계산형 분석 3개 = 20개
READ_ONLY_TOOLS = {
    # Market Data
    "getOrderbook",
    "getPrices",
    "getTrades",
    "getPriceLimit",
    "getCandles",
    # Stock Info
    "getStocks",
    "getStockWarnings",
    # Market Info
    "getExchangeRate",
    "getKrMarketCalendar",
    "getUsMarketCalendar",
    # Account / Asset
    "getAccounts",
    "getHoldings",
    # Order History
    "getOrders",
    "getOrder",
    # Order Info
    "getBuyingPower",
    "getSellableQuantity",
    "getCommissions",
    # 계산형 분석 툴(analytics.py)
    "analyze_indicators",
    "intraday_vwap",
    "get_market_signals",
}
# 거래 활성 시 추가로 노출되는 주문 변경 툴 3개
TRADING_TOOLS = {"createOrder", "modifyOrder", "cancelOrder"}
ALL_TOOLS = READ_ONLY_TOOLS | TRADING_TOOLS


def _mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://openapi.tossinvest.com")


def _trading_settings(*, enable_trading: bool) -> Settings:
    return Settings(
        client_id="test",
        client_secret=SecretStr("test"),
        enable_trading=enable_trading,
    )


def _prompt_text(result: GetPromptResult) -> str:
    content = result.messages[0].content
    assert isinstance(content, TextContent)
    return content.text


async def test_default_is_read_only_tools() -> None:
    client = _mock_client()
    try:
        names = {t.name for t in await build_server(client=client).list_tools()}
        assert "issueOAuth2Token" not in names, "토큰 발급은 인증 계층 담당 -> 툴 제외"
        assert names.isdisjoint(TRADING_TOOLS), "기본은 읽기 전용 -> 주문 변경 툴 제외"
        assert names == READ_ONLY_TOOLS
        assert len(names) == 20  # 스펙 생성 17 + 계산형 분석 3
    finally:
        await client.aclose()


async def test_enable_trading_exposes_all_tools() -> None:
    client = _mock_client()
    try:
        mcp = build_server(_trading_settings(enable_trading=True), client=client)
        names = {t.name for t in await mcp.list_tools()}
        assert TRADING_TOOLS <= names, "거래 활성 시 주문 변경 툴 노출"
        assert names == ALL_TOOLS
        assert len(names) == 23  # 읽기 전용 20 + 주문 변경 3
    finally:
        await client.aclose()


async def test_read_tool_annotated_read_only() -> None:
    client = _mock_client()
    try:
        prices = await build_server(client=client).get_tool("getPrices")
        assert prices is not None
        ann = prices.annotations
        assert ann is not None
        assert ann.readOnlyHint is True
        assert ann.destructiveHint is False
    finally:
        await client.aclose()


async def test_order_tool_annotated_destructive() -> None:
    client = _mock_client()
    try:
        mcp = build_server(_trading_settings(enable_trading=True), client=client)
        create = await mcp.get_tool("createOrder")
        assert create is not None
        ann = create.annotations
        assert ann is not None
        assert ann.destructiveHint is True
        assert ann.readOnlyHint is False
        assert ann.idempotentHint is False  # 이중주문 위험 -> 비멱등
    finally:
        await client.aclose()


async def test_analytics_tool_annotated_read_only() -> None:
    client = _mock_client()
    try:
        tool = await build_server(client=client).get_tool("analyze_indicators")
        assert tool is not None
        ann = tool.annotations
        assert ann is not None
        assert ann.readOnlyHint is True
        assert ann.destructiveHint is False
    finally:
        await client.aclose()


async def test_analytics_tool_params_documented() -> None:
    # MCP best practice: 파라미터 설명은 스키마에 있어야 한다(docstring 산문만으로는 부족).
    client = _mock_client()
    try:
        mcp = build_server(client=client)
        for name in ("analyze_indicators", "intraday_vwap", "get_market_signals"):
            tool = await mcp.get_tool(name)
            assert tool is not None, name
            props = tool.parameters.get("properties", {})
            assert props and all(p.get("description") for p in props.values()), name
        analyze = await mcp.get_tool("analyze_indicators")
        assert analyze is not None
        lookback = analyze.parameters["properties"]["lookback"]
        assert lookback.get("minimum") == 30 and lookback.get("maximum") == 252
    finally:
        await client.aclose()


async def test_market_signals_resilient_to_partial_failure() -> None:
    # 호가만 실패시켜도 전체 신호가 죽지 않고, 성공 신호 + unavailable 표기를 반환해야 한다.
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/warnings"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "warningType": "OVERHEATED",
                            "startDate": "2026-05-01",
                            "endDate": None,
                        }
                    ]
                },
            )
        if path == "/api/v1/orderbook":
            return httpx.Response(
                500,
                json={
                    "error": {"code": "OB", "message": "호가 오류", "requestId": "r"}
                },
            )
        if path == "/api/v1/candles":
            return httpx.Response(
                200, json={"result": {"candles": [], "nextBefore": None}}
            )
        if path == "/api/v1/price-limits":
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(200, json={"result": []})  # stocks/prices/trades

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        async with Client(build_server(client=client)) as c:
            data = (await c.call_tool("get_market_signals", {"symbol": "005930"})).data
        assert "orderbook" in data["unavailable"]  # 실패 항목 표기
        assert data["warnings"][0]["type"] == "OVERHEATED"  # 성공 신호는 살아있음
    finally:
        await client.aclose()


# 등록되는 워크플로 프롬프트(거래 활성화와 무관하게 항상 노출)
PROMPTS = {
    "포트폴리오_분석",
    "오늘_장_열리나",
    "매수_전_체크리스트",
    "종합_시세_브리핑",
    "차트_분석",
    "호재악재_신호_점검",
}


async def test_prompts_registered() -> None:
    client = _mock_client()
    try:
        names = {p.name for p in await build_server(client=client).list_prompts()}
        assert names == PROMPTS
    finally:
        await client.aclose()


async def test_pre_buy_prompt_takes_symbol() -> None:
    client = _mock_client()
    try:
        prompt = await build_server(client=client).get_prompt("매수_전_체크리스트")
        assert prompt is not None
        assert {arg.name for arg in (prompt.arguments or [])} == {"symbol"}
    finally:
        await client.aclose()


async def test_overseas_prompts_reference_exchange_rate() -> None:
    # 해외(외화) 종목은 환율 확인이 필수 -> 관련 프롬프트가 getExchangeRate 를 안내해야 한다.
    http = _mock_client()
    try:
        async with Client(build_server(client=http)) as client:
            pre_buy = await client.get_prompt("매수_전_체크리스트", {"symbol": "AAPL"})
            assert "getExchangeRate" in _prompt_text(pre_buy)
            portfolio = await client.get_prompt("포트폴리오_분석")
            assert "getExchangeRate" in _prompt_text(portfolio)
    finally:
        await http.aclose()


def test_account_header_relaxed_to_optional() -> None:
    spec = load_spec()
    assert spec["components"]["parameters"]["AccountSeq"]["required"] is False


async def test_account_scoped_tool_exposes_optional_account_header() -> None:
    client = _mock_client()
    try:
        mcp: FastMCP = build_server(client=client)
        holdings = await mcp.get_tool("getHoldings")
        assert holdings is not None
        props = holdings.parameters.get("properties", {})
        required = holdings.parameters.get("required", [])
        assert "X-Tossinvest-Account" in props
        assert "X-Tossinvest-Account" not in required  # 전역 기본 + 호출시 override

        # 비계좌 엔드포인트엔 계좌 헤더가 없어야 한다
        prices = await mcp.get_tool("getPrices")
        assert prices is not None
        assert "X-Tossinvest-Account" not in prices.parameters.get("properties", {})
        assert "symbols" in prices.parameters.get("required", [])
    finally:
        await client.aclose()
