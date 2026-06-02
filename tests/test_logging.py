"""로깅 구성 검증 — stdio 프로토콜 보호.

stdout 은 JSON-RPC 채널이므로 로그가 stdout 으로 새면 프로토콜이 깨진다.
또한 우리 로거만 건드려 httpx/httpcore 가 DEBUG 로 토큰을 흘리지 않게 한다.
"""

from __future__ import annotations

import logging

import pytest

from tossinvest_mcp.__main__ import _configure_logging


def test_logs_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _configure_logging("INFO")
    logging.getLogger("tossinvest_mcp.auth").info("hello-from-auth")
    captured = capsys.readouterr()
    assert "hello-from-auth" in captured.err
    assert "hello-from-auth" not in captured.out


def test_does_not_enable_root_debug() -> None:
    """DEBUG 레벨이어도 우리 패키지 로거에만 적용되고 root 로 전파하지 않는다."""
    _configure_logging("DEBUG")
    pkg = logging.getLogger("tossinvest_mcp")
    assert pkg.level == logging.DEBUG
    assert pkg.propagate is False  # root(stdout 가능)/httpx 로 새지 않음
