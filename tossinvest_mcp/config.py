"""환경설정 — `.env` 또는 환경변수(`TOSS_` 접두사)에서 로드."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """토스증권 MCP 실행에 필요한 설정.

    OpenAPI 콘솔에서 발급한 클라이언트 자격증명과 기본 계좌를 받는다.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOSS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id: str = Field(description="OpenAPI 콘솔에서 발급받은 client_id")
    client_secret: SecretStr = Field(
        description="OpenAPI 콘솔에서 발급받은 client_secret (repr/로그 노출 방지)"
    )
    base_url: str = Field(
        default="https://openapi.tossinvest.com",
        description="토스증권 Open API base URL",
    )
    default_account_seq: int | None = Field(
        default=None,
        description="기본 계좌 accountSeq. 계좌/자산/주문 API 요청 시 전역 기본값으로 주입된다. "
        "툴 호출 시 accountSeq 인자로 개별 override 가능.",
    )

    # --- 운영/안전 토글 ---
    enable_trading: bool = Field(
        default=False,
        description="주문 변경 툴(createOrder/modifyOrder/cancelOrder) 노출 여부. "
        "기본 False(읽기 전용). 실거래를 허용할 때만 명시적으로 True.",
    )
    validate_output: bool = Field(
        default=True,
        description="툴 응답을 OpenAPI 응답 스키마로 검증할지. True면 벤더 응답 드리프트를 "
        "감지하지만, 스펙과 어긋나면 데이터가 멀쩡해도 에러가 된다.",
    )

    # --- 네트워크/재시도 ---
    request_timeout: float = Field(
        default=30.0,
        description="HTTP 요청 타임아웃(초)",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="재시도 횟수. 429는 모든 메서드, 5xx/네트워크 오류는 안전한(GET 등) "
        "메서드에만 적용. 주문 변경 POST는 이중 주문 위험 때문에 재시도하지 않는다.",
    )
    log_level: str = Field(
        default="INFO",
        description="로그 레벨(DEBUG/INFO/WARNING/ERROR). 로그는 stderr 로 출력된다.",
    )
