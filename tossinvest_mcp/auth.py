"""OAuth2 Client Credentials Grant용 httpx 인증 흐름.

토스증권 Open API는 `POST /oauth2/token` 으로 access token 을 발급받아
모든 요청에 `Authorization: Bearer {access_token}` 헤더로 전달한다.
FastMCP 내장 `OAuth` 헬퍼는 브라우저 Authorization-Code 흐름용이므로
Client Credentials 에는 맞지 않아 직접 구현한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

_HTTP_UNAUTHORIZED = 401


class ClientCredentialsAuth(httpx.Auth):
    """토큰을 발급/캐시/갱신하여 Bearer 헤더를 주입하는 httpx.Auth.

    - 토큰 발급 요청은 이 Auth 가 **붙지 않은 별도** AsyncClient 로 보내므로
      `async_auth_flow` 재진입(재귀)이 발생하지 않는다.
    - 캐시 토큰이 로컬상 유효해도 서버가 401 을 주면(키 로테이션/시계 오차 등)
      **single-flight** 로 한 번 재발급 후 1회 재시도한다.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 30.0,
        leeway: float = 60.0,
        fallback_ttl: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._leeway = leeway
        self._fallback_ttl = fallback_ttl
        # 토큰 발급 클라이언트가 쓰는 transport.
        # 프로덕션: 재시도 트랜스포트 주입(429 보호) / 테스트: MockTransport / None: 기본 네트워크.
        self._transport = transport
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_valid(self) -> bool:
        return (
            self._access_token is not None
            and time.monotonic() < self._expires_at - self._leeway
        )

    async def _fetch_token(self) -> None:
        # 별도 클라이언트(auth 미부착)로 발급 -> 재귀 방지
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()

        self._access_token = payload["access_token"]
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            logger.warning(
                "token response missing/invalid expires_in (%r); "
                "falling back to %ss TTL",
                expires_in,
                self._fallback_ttl,
            )
            expires_in = self._fallback_ttl
        self._expires_at = time.monotonic() + float(expires_in)
        logger.debug("issued new access token (expires_in=%ss)", expires_in)

    async def _ensure_token(self) -> None:
        if self._is_valid():
            return
        async with self._lock:
            if not self._is_valid():  # 락 대기 중 다른 코루틴이 갱신했을 수 있음
                await self._fetch_token()

    async def _refresh_after_401(self, previous: str | None) -> None:
        # single-flight: 내가 쓰던 토큰이 아직 캐시값과 같을 때만 재발급한다.
        # (대량 401 시 thundering herd 및 AUTH rate-limit 초과 방지)
        async with self._lock:
            if self._access_token == previous:
                logger.warning("access token rejected (401); refreshing")
                await self._fetch_token()

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        await self._ensure_token()
        used = self._access_token
        request.headers["Authorization"] = f"Bearer {used}"
        response = yield request

        if response.status_code == _HTTP_UNAUTHORIZED:
            await self._refresh_after_401(used)
            request.headers["Authorization"] = f"Bearer {self._access_token}"
            yield request
