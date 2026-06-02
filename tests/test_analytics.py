"""분석 함수 단위 테스트 — 네트워크 없음(순수 함수).

핵심: **고정 기대값 오라클**. RSI/MACD/ATR/MFI 의 기대값은 권위 출처(TA-Lib 0.6.x)로 한 번 산출해
상수로 박았다(개발 중 `uv run --with TA-Lib --no-project` 일회성 사용, pyproject 엔 미포함).
구조/범위 검사는 "잘못된 관례"(예: Wilder 대신 SMA 평활)를 못 잡으므로, 이 정확값 대조가 talipp 의
표준 관례 준수를 실증한다. 동일 입력 시리즈에 대해 talipp + 자체 MFI 가 TA-Lib 와 일치(허용오차 내)함을
확인했다: RSI/MFI 는 사실상 정확 일치, ATR ~0.02%, MACD 는 동일 EMA 계열이나 시딩 차이로 ~1.5%.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from tossinvest_mcp.analytics import (
    _error_detail,
    _result,
    _session_bars,
    compute_indicators,
    compute_vwap,
    money_flow_index,
    parse_candles,
    summarize_microstructure,
)

# --- 결정적 테스트 시리즈(생성 스크립트와 동일 공식) ---
N = 60


def _series() -> list[dict[str, Any]]:
    out = []
    for i in range(N):
        close = 100 + 10 * math.sin(i / 3.0) + 0.5 * i
        out.append(
            {
                "timestamp": f"2026-04-{(i % 28) + 1:02d}T00:00:00+09:00",
                "open": close,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": float(1000 + 10 * i + 100 * (i % 5)),
            }
        )
    return out


# TA-Lib(권위 출처)로 산출한 고정 기대값 — 동일 시리즈 마지막 봉 기준
ORACLE_RSI14 = 73.44633
ORACLE_ATR14 = 4.721327
ORACLE_MACD = (3.711828, 2.582479, 1.12935)  # macd, signal, histogram
ORACLE_MFI14 = 59.862705


def test_rsi_matches_talib_oracle() -> None:
    d = compute_indicators(_series())
    # 정확 일치(Wilder 평활) — 타이트한 허용오차
    assert abs(d["momentum"]["rsi"] - ORACLE_RSI14) <= 0.05


def test_atr_matches_talib_oracle() -> None:
    d = compute_indicators(_series())
    assert abs(d["volatility"]["atr"] - ORACLE_ATR14) <= 0.01  # ~0.02% 시딩 차이


def test_macd_matches_talib_oracle() -> None:
    macd = compute_indicators(_series())["trend"]["macd"]
    # 동일 EMA 계열이나 시딩 관례 차이로 약간 느슨(잘못된 관례면 이보다 훨씬 크게 벌어짐)
    assert abs(macd["macd"] - ORACLE_MACD[0]) <= 0.05
    assert abs(macd["signal"] - ORACLE_MACD[1]) <= 0.08
    assert abs(macd["histogram"] - ORACLE_MACD[2]) <= 0.05


def test_mfi_matches_talib_oracle() -> None:
    s = _series()
    raw = money_flow_index(
        [c["high"] for c in s],
        [c["low"] for c in s],
        [c["close"] for c in s],
        [c["volume"] for c in s],
        14,
    )
    assert raw is not None
    assert abs(raw[-1] - ORACLE_MFI14) <= 1e-4  # 자체 구현이 TA-Lib 와 정확 일치


def test_mfi_hand_computed() -> None:
    # period=2, 4봉: tp 10->12(up)->11(down)->13(up), 거래량 100 고정
    highs = lows = closes = [10.0, 12.0, 11.0, 13.0]
    vols = [100.0, 100.0, 100.0, 100.0]
    out = money_flow_index(highs, lows, closes, vols, period=2)
    assert out is not None and len(out) == 2
    assert abs(out[-1] - 54.16667) <= 1e-3  # 수기 계산값


def test_mfi_insufficient_returns_none() -> None:
    assert (
        money_flow_index([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], [1.0, 1.0], period=2)
        is None
    )


def test_compute_indicators_empty() -> None:
    assert compute_indicators([])["bars"] == 0


def test_dashboard_basics() -> None:
    d = compute_indicators(_series())
    assert d["bars"] == N
    assert d["range"]["period_high"] >= d["range"]["period_low"]
    assert d["volume"]["spike_ratio"] is not None
    # 충분한 봉이면 장기 지표도 채워짐
    assert d["trend"]["sma60"] is not None
    assert d["trend"]["supertrend"]["trend"] in {"UP", "DOWN"}


def test_change_and_range_exact() -> None:
    # 단순 증가 시리즈로 등락률/기간 고저를 정확 검증
    candles = [
        {
            "timestamp": f"2026-01-{i + 1:02d}",
            "open": 10.0 + i,
            "high": 12.0 + i,
            "low": 8.0 + i,
            "close": 10.0 + i,
            "volume": 100.0,
        }
        for i in range(5)
    ]
    d = compute_indicators(candles)
    assert d["change"]["pct_1bar"] == round((14 / 13 - 1) * 100, 2)
    assert d["change"]["pct_period"] == round((14 / 10 - 1) * 100, 2)
    assert d["range"]["period_high"] == 16.0  # 12 + 4
    assert d["range"]["period_low"] == 8.0


def test_parse_candles_sorts_and_drops_bad() -> None:
    raw = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "openPrice": "11",
            "highPrice": "12",
            "lowPrice": "10",
            "closePrice": "11",
            "volume": "100",
        },
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "openPrice": "10",
            "highPrice": "11",
            "lowPrice": "9",
            "closePrice": "10",
            "volume": "90",
        },
        {
            "timestamp": "2026-01-03T00:00:00Z",
            "openPrice": None,
            "highPrice": "12",
            "lowPrice": "10",
            "closePrice": "11",
            "volume": "100",
        },  # 결측 -> 제외
    ]
    parsed = parse_candles(raw)
    assert [c["timestamp"] for c in parsed] == [
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
    ]
    assert parsed[0]["close"] == 10.0


# --- VWAP ---
def _vwap_bars() -> list[dict[str, Any]]:
    return [
        {
            "timestamp": f"2026-06-02T09:0{i}:00+09:00",
            "open": 9 + 2 * i,
            "high": 11 + 2 * i,
            "low": 9 + 2 * i,
            "close": 10 + 2 * i,
            "volume": float(100 * (i + 1)),
        }
        for i in range(3)
    ]


def test_vwap_typical_price() -> None:
    # tp=(h+l+c)/3 가중평균 = (10*100 + 12*200 + 14*300)/600 = 12.6667
    res = compute_vwap(_vwap_bars())
    assert res is not None
    assert abs(res["vwap"] - 12.6667) <= 1e-3
    assert res["bars_in_session"] == 3


def test_vwap_session_anchoring() -> None:
    # 전일 세션(야간 갭) 봉은 제외되어야 한다
    prev = [
        {
            "timestamp": f"2026-06-01T09:0{i}:00+09:00",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.0,
            "volume": 9999.0,
        }
        for i in range(2)
    ]
    assert len(_session_bars(prev + _vwap_bars())) == 3


# --- 마이크로구조 ---
def test_microstructure_orderbook_and_flow() -> None:
    facts = summarize_microstructure(
        warnings=[
            {
                "warningType": "INVESTMENT_RISK",
                "startDate": "2026-01-01",
                "endDate": None,
            }
        ],
        kr_detail={"liquidationTrading": False, "krxTradingSuspended": True},
        last_price="129",
        limits={"upperLimitPrice": "130", "lowerLimitPrice": "70"},
        orderbook={
            "asks": [{"price": "10", "volume": "100"}, {"price": "11", "volume": "50"}],
            "bids": [{"price": "9", "volume": "300"}, {"price": "8", "volume": "100"}],
        },
        trades=[
            {"price": "10", "volume": "100", "timestamp": "t1"},
            {"price": "11", "volume": "200", "timestamp": "t2"},
            {"price": "10", "volume": "50", "timestamp": "t3"},
        ],
        volume_ctx={"latest": 200.0, "avg": 100.0, "spike_ratio": 2.0},
    )
    assert facts["warnings"][0]["type"] == "INVESTMENT_RISK"
    assert facts["kr_flags"]["krx_trading_suspended"] is True
    assert facts["orderbook"]["bid_ask_ratio"] == round(400 / 150, 3)
    assert facts["limit"]["near_upper"] is True  # 129/130 >= 0.97
    assert facts["trade_flow"]["up_ratio"] == round(200 / 250, 3)
    assert "뉴스가 아님" in facts["note"]  # 데이터 파생 신호임을 명시


# --- 에러 처리(A): 토스 ApiError 엔벨로프 노출 ---
def test_error_detail_parses_envelope() -> None:
    resp = httpx.Response(
        404,
        json={
            "error": {
                "code": "INVALID_SYMBOL",
                "message": "유효하지 않은 종목",
                "requestId": "req-1",
            }
        },
    )
    detail = _error_detail(resp)
    assert "INVALID_SYMBOL" in detail
    assert "유효하지 않은 종목" in detail
    assert "req-1" in detail


def test_error_detail_fallback_plain_body() -> None:
    detail = _error_detail(httpx.Response(500, text="oops"))
    assert "500" in detail and "oops" in detail


async def test_result_raises_toolerror_with_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "BAD", "message": "잘못된 요청", "requestId": "x"}},
        )

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ToolError) as ei:
            await _result(client, "/api/v1/prices", {"symbols": "X"})
        assert "BAD" in str(ei.value) and "잘못된 요청" in str(ei.value)
    finally:
        await client.aclose()
