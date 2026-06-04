"""핀(pin)된 OpenAPI 스펙으로부터 FastMCP 서버를 조립한다.

`spec/openapi.json`(v1.0.3) 한 개가 문서이자 툴 생성 엔진이다.
`FastMCP.from_openapi()` 의 기본 매핑(모든 라우트 -> Tool)을 사용하되,
`/oauth2/token` 은 인증 계층(ClientCredentialsAuth)이 담당하므로 툴에서 제외한다.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from tossinvest_mcp.analytics import register_analytics
from tossinvest_mcp.client import build_client
from tossinvest_mcp.config import Settings
from tossinvest_mcp.prompts import register_prompts

if TYPE_CHECKING:
    from fastmcp.utilities.openapi.models import HTTPRoute


def _packaged_spec_path() -> Path:
    """패키지에 포함된 spec/openapi.json 경로(editable/wheel 모두 동작)."""
    return Path(str(files("tossinvest_mcp").joinpath("spec", "openapi.json")))


# 토큰 발급 엔드포인트는 툴로 노출하지 않는다(인증 계층이 처리).
_EXCLUDE_TOKEN = RouteMap(
    methods=["POST"], pattern=r"^/oauth2/token$", mcp_type=MCPType.EXCLUDE
)
# 주문 변경(생성/정정/취소)은 모두 POST /api/v1/orders... 경로. 거래 비활성 시 제외.
_EXCLUDE_TRADING = RouteMap(
    methods=["POST"], pattern=r"^/api/v1/orders", mcp_type=MCPType.EXCLUDE
)


def _route_maps(*, enable_trading: bool) -> list[RouteMap]:
    maps = [_EXCLUDE_TOKEN]
    if not enable_trading:
        maps.append(_EXCLUDE_TRADING)
    return maps


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _annotate_component(route: HTTPRoute, component: object) -> None:
    """생성된 각 툴에 MCP 행동 힌트를 부여한다(HTTP 메서드 기반, 스펙추적 0).

    주석은 클라이언트가 참고하는 *힌트*(메타데이터 위생)일 뿐 강제 장치가 아니다.
    실제 안전은 `enable_trading=False` 일 때 주문 툴을 아예 노출하지 않음으로 확보된다.
    노출되는 비-GET 툴은 주문(생성/정정/취소)뿐이므로 메서드 판정으로 충분하다.
    """
    if not isinstance(component, Tool):  # 리소스/템플릿이면 스킵(현재는 전부 Tool)
        return
    read_only = route.method.upper() in _READ_METHODS
    component.annotations = ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=not read_only,  # 주문 POST(생성/정정/취소)는 파괴적
        idempotentHint=read_only,  # GET 멱등 / 주문 POST 비멱등(이중주문 위험)
        openWorldHint=True,  # 외부 토스 API/시장 상태와 상호작용
    )


def load_spec(path: Path | None = None) -> dict[str, Any]:
    """pin 된 스펙(패키지 동봉)을 로드하고 계좌 헤더를 보정한다.

    `X-Tossinvest-Account`(components.parameters.AccountSeq)에 대해:
    1. `required=False` 로 완화 -> 전역 기본값(클라이언트 헤더)이 있으면 생략 가능,
       필요 시 툴 인자로 호출별 override.
    2. schema 타입을 `integer` -> `string` 으로 보정. HTTP 헤더 값은 항상 문자열이며,
       정수로 두면 FastMCP 가 httpx 에 int 를 넘겨 `Header value must be str` 로 실패한다.
    """
    spec_path = path or _packaged_spec_path()
    with spec_path.open(encoding="utf-8") as f:
        spec: dict[str, Any] = json.load(f)

    account_seq = spec.get("components", {}).get("parameters", {}).get("AccountSeq")
    if account_seq is not None:
        account_seq["required"] = False
        account_seq["schema"] = {"type": "string", "pattern": r"^\d+$"}
    return spec


def build_server(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> FastMCP:
    """FastMCP 서버를 조립해 반환한다.

    토글(enable_trading/validate_output)은 `settings` 에서 읽는다. `settings` 없이
    `client` 만 주입하면 기본값(읽기 전용, 출력검증 on)을 쓰므로 자격증명 없이도
    구성할 수 있어 정적 테스트에 유용하다.
    """
    spec = load_spec()
    if settings is None and client is None:
        settings = Settings()  # type: ignore[call-arg]  # .env/환경변수에서 로드
    if client is None:
        assert settings is not None
        client = build_client(settings)

    enable_trading = settings.enable_trading if settings else False
    validate_output = settings.validate_output if settings else True

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="토스증권 MCP",
        route_maps=_route_maps(enable_trading=enable_trading),
        mcp_component_fn=_annotate_component,
        validate_output=validate_output,
    )
    register_prompts(mcp)
    register_analytics(
        mcp, client, db_path=settings.cache_db_path if settings else None
    )
    return mcp
