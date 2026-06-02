"""재시도 정책을 담은 httpx 비동기 트랜스포트.

트레이딩 API 의 핵심 안전 규칙:
- **429 (rate limit)**: 요청이 거절(미실행)된 것이므로 **모든 메서드** 재시도 안전.
  `Retry-After` 를 우선 존중한다.
- **5xx / 네트워크 오류·타임아웃**: 서버측에서 이미 처리됐는데 응답만 유실됐을 수 있다.
  따라서 **안전한 메서드(GET/HEAD/OPTIONS)만** 재시도하고, 주문 변경 같은 POST 는
  **절대 재시도하지 않는다**(이중 주문 방지). 호출자가 getOrders/getOrder 로 조정한다.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR_MIN = 500


class RetryTransport(httpx.AsyncBaseTransport):
    """내부 트랜스포트를 감싸 위 정책대로 재시도한다."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_backoff: float = 30.0,
    ) -> None:
        self._transport = transport
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        is_safe = request.method in _SAFE_METHODS
        last_response: httpx.Response | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._transport.handle_async_request(request)
            except httpx.TransportError as exc:
                # 네트워크/타임아웃: 안전 메서드만 재시도, 그 외(주문 POST 등)는 그대로 전파
                if not is_safe or attempt >= self._max_retries:
                    raise
                wait = self._backoff(attempt)
                logger.warning(
                    "%s %s transport error (%s); retry %d/%d after %.2fs",
                    request.method,
                    request.url,
                    type(exc).__name__,
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            status = response.status_code
            retriable = status == _TOO_MANY_REQUESTS or (
                status >= _SERVER_ERROR_MIN and is_safe
            )
            if not retriable or attempt >= self._max_retries:
                return response

            wait = self._wait_for(response, attempt)
            logger.warning(
                "%s %s -> %d; retry %d/%d after %.2fs",
                request.method,
                request.url,
                status,
                attempt + 1,
                self._max_retries,
                wait,
            )
            await response.aclose()
            last_response = response
            await asyncio.sleep(wait)

        # 도달 시: 마지막 응답 반환(루프 구조상 일반적으로 위에서 return)
        assert last_response is not None
        return last_response

    def _wait_for(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(float(retry_after), self._max_backoff)
            except ValueError:
                # HTTP-date 형식 등은 단순화를 위해 백오프로 폴백
                pass
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_base * (2**attempt), self._max_backoff)

    async def aclose(self) -> None:
        await self._transport.aclose()
