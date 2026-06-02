"""ClientCredentialsAuth 단위 테스트 — httpx mock 으로 발급/캐시/갱신/재귀없음 검증."""

from __future__ import annotations

import httpx

from tossinvest_mcp.auth import ClientCredentialsAuth

TOKEN_URL = "https://api.test/oauth2/token"


def _token_handler(calls: list[int]):
    def handler(request: httpx.Request) -> httpx.Response:
        # 토큰 요청은 항상 토큰 경로로만 가야 한다(재귀/오라우팅 없음)
        assert request.url.path == "/oauth2/token"
        assert request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        calls.append(1)
        n = len(calls)
        return httpx.Response(
            200,
            json={"access_token": f"T{n}", "token_type": "Bearer", "expires_in": 3600},
        )

    return handler


async def test_token_fetched_once_and_cached() -> None:
    calls: list[int] = []
    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        transport=httpx.MockTransport(_token_handler(calls)),
    )

    seen: list[str | None] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        await client.get("/api/v1/prices")
        await client.get("/api/v1/prices")

    assert seen == ["Bearer T1", "Bearer T1"]  # 두 번째 호출은 캐시 토큰 재사용
    assert len(calls) == 1  # 토큰은 한 번만 발급


async def test_token_refreshed_when_expired() -> None:
    calls: list[int] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        n = len(calls)
        return httpx.Response(
            200,
            json={"access_token": f"T{n}", "token_type": "Bearer", "expires_in": 100},
        )

    # leeway(200s) > ttl(100s) -> 항상 만료로 간주되어 매 호출 재발급
    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        leeway=200.0,
        transport=httpx.MockTransport(token_handler),
    )

    seen: list[str | None] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        await client.get("/x")
        await client.get("/x")

    assert seen == ["Bearer T1", "Bearer T2"]  # 만료로 매번 갱신
    assert len(calls) == 2


async def test_missing_expires_in_uses_fallback_ttl_not_refetch() -> None:
    """expires_in 누락/0 이면 fallback TTL 을 써서 매 요청 재발급(AUTH rps 위험)을 막는다."""
    calls: list[int] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # expires_in 누락
        return httpx.Response(200, json={"access_token": "T1", "token_type": "Bearer"})

    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        leeway=0.0,
        fallback_ttl=300.0,
        transport=httpx.MockTransport(token_handler),
    )

    def resource_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        await client.get("/x")
        await client.get("/x")

    assert len(calls) == 1  # fallback TTL 덕에 재발급 안 함


def _sequential_token_handler(calls: list[int]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        n = len(calls)
        return httpx.Response(
            200,
            json={"access_token": f"T{n}", "token_type": "Bearer", "expires_in": 3600},
        )

    return handler


async def test_reauth_on_401_retries_once() -> None:
    """캐시 토큰이 401 을 받으면 재발급 후 1회 재시도한다."""
    token_calls: list[int] = []
    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        transport=httpx.MockTransport(_sequential_token_handler(token_calls)),
    )

    seen: list[str | None] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Authorization")
        seen.append(header)
        # 첫 토큰(T1)은 거부, 재발급된 토큰은 통과
        if header == "Bearer T1":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        resp = await client.get("/api/v1/holdings")

    assert seen == ["Bearer T1", "Bearer T2"]  # 401 후 재발급 토큰으로 재시도
    assert resp.status_code == 200
    assert len(token_calls) == 2  # 최초 발급 + 401 재발급


async def test_persistent_401_does_not_loop() -> None:
    """지속적 401 에도 무한 재시도하지 않고 한 번만 재시도 후 401 을 반환한다."""
    token_calls: list[int] = []
    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        transport=httpx.MockTransport(_sequential_token_handler(token_calls)),
    )

    attempts: list[int] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401)

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        resp = await client.get("/api/v1/holdings")

    assert resp.status_code == 401
    assert len(attempts) == 2  # 최초 + 재시도 1회뿐 (루프 없음)
    assert len(token_calls) == 2


async def test_concurrent_401_single_flight() -> None:
    """동시 401 다발 시 토큰 재발급이 single-flight 로 1회만 일어난다."""
    import asyncio

    token_calls: list[int] = []
    auth = ClientCredentialsAuth(
        token_url=TOKEN_URL,
        client_id="id",
        client_secret="sec",
        transport=httpx.MockTransport(_sequential_token_handler(token_calls)),
    )

    def resource_handler(request: httpx.Request) -> httpx.Response:
        # 최초 토큰(T1)만 거부 -> 재발급(T2) 후 통과
        if request.headers.get("Authorization") == "Bearer T1":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url="https://api.test",
        auth=auth,
        transport=httpx.MockTransport(resource_handler),
    ) as client:
        results = await asyncio.gather(*(client.get("/x") for _ in range(8)))

    assert all(r.status_code == 200 for r in results)
    # 최초 발급 1 + 401 재발급 1 = 2 (요청 수와 무관)
    assert len(token_calls) == 2
