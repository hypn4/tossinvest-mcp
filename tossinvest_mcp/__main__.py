"""실행 진입점: stdio 트랜스포트로 MCP 서버를 띄운다."""

from __future__ import annotations

import logging
import sys

from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server


def _configure_logging(level: str) -> None:
    """우리 로거(tossinvest_mcp)에만 stderr 핸들러를 붙인다.

    - stdio 트랜스포트는 stdout 이 JSON-RPC 채널이므로 로그는 반드시 stderr 로.
    - root 가 아니라 패키지 로거에만 설정 -> httpx/httpcore 가 DEBUG 로 켜져
      `Authorization` 토큰을 와이어 로그로 흘리는 일을 막는다.
    - `basicConfig` 는 root 에 핸들러가 이미 있으면 무시되므로(no-op), 명시적으로 부착.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger = logging.getLogger("tossinvest_mcp")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False  # root(=stdout 가능) 로 전파 금지


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # .env/환경변수에서 로드
    _configure_logging(settings.log_level)
    build_server(settings).run()


if __name__ == "__main__":
    main()
