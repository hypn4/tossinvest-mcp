"""인증·기본 헤더·재시도가 설정된 httpx.AsyncClient 생성."""

from __future__ import annotations

import httpx

from tossinvest_mcp.auth import ClientCredentialsAuth
from tossinvest_mcp.config import Settings
from tossinvest_mcp.retry import RetryTransport


def build_client(settings: Settings) -> httpx.AsyncClient:
    """토스증권 Open API 호출용 AsyncClient 를 만든다.

    - base_url + OAuth2 Client Credentials 인증 부착
    - 429/5xx/네트워크 오류 재시도 트랜스포트(주문 변경 POST 는 재시도 제외) 부착
    - `default_account_seq` 가 설정되어 있으면 모든 요청에
      `X-Tossinvest-Account` 헤더를 전역 기본값으로 주입(툴 인자로 override 가능)
    """
    auth = ClientCredentialsAuth(
        token_url=f"{settings.base_url.rstrip('/')}/oauth2/token",
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        timeout=settings.request_timeout,
        # 토큰 발급(AUTH 5rps)도 429 재시도로 보호. 토큰 발급은 idempotent 이라 안전.
        transport=RetryTransport(
            httpx.AsyncHTTPTransport(), max_retries=settings.max_retries
        ),
    )
    transport = RetryTransport(
        httpx.AsyncHTTPTransport(),
        max_retries=settings.max_retries,
    )
    headers: dict[str, str] = {}
    if settings.default_account_seq is not None:
        headers["X-Tossinvest-Account"] = str(settings.default_account_seq)

    return httpx.AsyncClient(
        base_url=settings.base_url,
        auth=auth,
        transport=transport,
        headers=headers,
        timeout=settings.request_timeout,
    )
