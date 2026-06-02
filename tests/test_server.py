"""정적 검증 — 자격증명 없이 동작. 핀된 스펙으로 서버가 올바르게 조립되는지 확인."""

from __future__ import annotations

import httpx
from fastmcp import FastMCP
from pydantic import SecretStr

from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server, load_spec

# 거래 비활성(기본) 시 노출되는 읽기 전용 툴 17개
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


async def test_default_is_read_only_17_tools() -> None:
    client = _mock_client()
    try:
        names = {t.name for t in await build_server(client=client).list_tools()}
        assert "issueOAuth2Token" not in names, "토큰 발급은 인증 계층 담당 -> 툴 제외"
        assert names.isdisjoint(TRADING_TOOLS), "기본은 읽기 전용 -> 주문 변경 툴 제외"
        assert names == READ_ONLY_TOOLS
        assert len(names) == 17
    finally:
        await client.aclose()


async def test_enable_trading_exposes_20_tools() -> None:
    client = _mock_client()
    try:
        mcp = build_server(_trading_settings(enable_trading=True), client=client)
        names = {t.name for t in await mcp.list_tools()}
        assert TRADING_TOOLS <= names, "거래 활성 시 주문 변경 툴 노출"
        assert names == ALL_TOOLS
        assert len(names) == 20
    finally:
        await client.aclose()


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
