"""RetryTransport 정책 검증.

핵심: 429 는 모든 메서드 재시도(거절=미실행), 5xx/네트워크는 안전 메서드(GET 등)만
재시도하고 주문 변경 POST 는 절대 재시도하지 않는다(이중 주문 방지).
"""

from __future__ import annotations

import httpx
import pytest

from tossinvest_mcp.retry import RetryTransport


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """호출마다 미리 정한 status 또는 예외를 반환/발생시키는 내부 트랜스포트."""

    def __init__(self, steps: list[int | Exception]) -> None:
        self._steps = steps
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, json={"ok": True})


def _client(
    steps: list[int | Exception],
) -> tuple[httpx.AsyncClient, _ScriptedTransport]:
    inner = _ScriptedTransport(steps)
    transport = RetryTransport(inner, max_retries=3, backoff_base=0.0, max_backoff=0.0)
    return httpx.AsyncClient(base_url="https://api.test", transport=transport), inner


async def test_429_retried_for_get() -> None:
    client, inner = _client([429, 429, 200])
    async with client:
        resp = await client.get("/api/v1/prices")
    assert resp.status_code == 200
    assert inner.calls == 3


async def test_429_retried_even_for_post() -> None:
    """429 는 거절(미실행)이므로 POST(주문)도 재시도 안전."""
    client, inner = _client([429, 200])
    async with client:
        resp = await client.post("/api/v1/orders", json={})
    assert resp.status_code == 200
    assert inner.calls == 2


async def test_5xx_retried_for_get() -> None:
    client, inner = _client([503, 503, 200])
    async with client:
        resp = await client.get("/api/v1/prices")
    assert resp.status_code == 200
    assert inner.calls == 3


async def test_5xx_NOT_retried_for_post() -> None:
    """주문 POST 의 5xx 는 재시도 금지(서버측 체결 후 응답 유실 시 이중 주문)."""
    client, inner = _client([503, 200])
    async with client:
        resp = await client.post("/api/v1/orders", json={})
    assert resp.status_code == 503  # 재시도 없이 즉시 5xx 반환
    assert inner.calls == 1


async def test_network_error_retried_for_get() -> None:
    client, inner = _client([httpx.ConnectError("boom"), 200])
    async with client:
        resp = await client.get("/api/v1/prices")
    assert resp.status_code == 200
    assert inner.calls == 2


async def test_network_error_NOT_retried_for_post() -> None:
    """주문 POST 의 네트워크 오류·타임아웃은 재시도 금지(이중 주문 방지)."""
    client, inner = _client([httpx.ReadTimeout("timeout"), 200])
    async with client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post("/api/v1/orders", json={})
    assert inner.calls == 1


async def test_max_retries_exhausted_returns_last_response() -> None:
    client, inner = _client([429, 429, 429, 429, 429])
    async with client:
        resp = await client.get("/api/v1/prices")
    assert resp.status_code == 429
    assert inner.calls == 4  # 최초 1 + 재시도 3


async def test_retry_after_header_respected() -> None:
    captured: list[float] = []

    class _T(RetryTransport):
        def _wait_for(self, response: httpx.Response, attempt: int) -> float:
            wait = super()._wait_for(response, attempt)
            captured.append(wait)
            return wait

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    transport = _T(httpx.MockTransport(handler), max_retries=3, backoff_base=99.0)
    async with httpx.AsyncClient(base_url="https://api.test", transport=transport) as c:
        resp = await c.get("/api/v1/prices")
    assert resp.status_code == 200
    assert captured == [0.0]  # backoff(99s) 대신 Retry-After(0s) 사용
