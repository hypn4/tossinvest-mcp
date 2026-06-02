"""E2E 검증 — 실제 토스증권 API 호출(읽기 전용).

`.env` 또는 환경변수에 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 이 있어야 실행되며,
없으면 자동 skip 된다. `uv run pytest -m e2e` 로 명시 실행 권장.

주문 계열(createOrder/modifyOrder/cancelOrder)은 실거래·실자금이 발생하므로
여기서 절대 호출하지 않는다. 읽기 전용 엔드포인트만 검증한다.

프로덕션과 동일하게 클라이언트(=캐시 토큰) 하나를 여러 호출에 재사용한다.
테스트마다 새 토큰을 발급하면 AUTH rate-limit(5rps)에 걸려 불안정해진다.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from pydantic import ValidationError

from tossinvest_mcp.client import build_client
from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server

pytestmark = pytest.mark.e2e


def _settings_or_skip() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # .env/환경변수에서 로드
    except ValidationError:
        pytest.skip("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정 — E2E 생략")


async def test_read_only_rest_endpoints() -> None:
    """토큰 1회 발급 + 읽기 전용 REST 엔드포인트들을 한 클라이언트로 검증."""
    settings = _settings_or_skip()
    async with build_client(settings) as client:
        # 1) getPrices — 계좌 헤더 불필요, 토큰 발급 + Bearer 주입 확인
        prices = await client.get("/api/v1/prices", params={"symbols": "005930"})
        prices.raise_for_status()
        assert prices.json(), "getPrices 응답 본문이 있어야 한다"

        # 2) getAccounts — 계좌 목록
        accounts = await client.get("/api/v1/accounts")
        accounts.raise_for_status()
        assert accounts.json() is not None

        # 3) getHoldings — default_account_seq 설정 시에만 (전역 계좌 헤더 주입 라이브 확인)
        if settings.default_account_seq is not None:
            holdings = await client.get("/api/v1/holdings")
            holdings.raise_for_status()
            assert holdings.json() is not None


async def test_full_stack_tool_call_through_mcp() -> None:
    """전체 스택 검증: MCP 툴 호출 -> 인증 -> 라이브 엔드포인트 -> 출력 스키마 검증.

    `validate_output=True`(기본) 이므로, 토스 실응답이 스펙 응답 스키마와 어긋나면
    여기서 실패한다. 즉 실데이터의 스키마 정합성까지 확인한다.
    """
    _settings_or_skip()
    mcp = build_server()  # .env 의 실 creds 로 인증 클라이언트 생성(기본 읽기 전용)
    async with Client(mcp) as client:
        result = await client.call_tool("getPrices", {"symbols": "005930"})
        assert result.is_error is False
        assert result.content, "툴 응답 콘텐츠가 있어야 한다"
