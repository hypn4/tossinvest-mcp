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
    CandleCache,
    _error_detail,
    _merge_for_lookback,
    _resolve_session,
    _result,
    _session_bars,
    _session_windows,
    compute_indicators,
    compute_vwap,
    money_flow_index,
    parse_candles,
    session_context,
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


# --- 세션 컨텍스트(1m 분봉 장중 고저 보강) ---
def test_session_context_excludes_prior_session() -> None:
    # 전일 세션의 극단 고저(9999/0.1)는 당일 세션 컨텍스트에서 제외되어야 한다.
    prev = [
        {
            "timestamp": f"2026-06-01T09:0{i}:00+09:00",
            "open": 1.0,
            "high": 9999.0,
            "low": 0.1,
            "close": 1.0,
            "volume": 1.0,
        }
        for i in range(2)
    ]
    cur = [
        {
            "timestamp": f"2026-06-02T09:0{i}:00+09:00",
            "open": 10.0,
            "high": 12.0 + i,
            "low": 9.0 - i,
            "close": 10.0,
            "volume": 100.0,
        }
        for i in range(3)
    ]
    ctx = session_context(prev + cur)
    assert ctx is not None
    assert ctx["bars_in_session"] == 3
    assert ctx["session_start"] == "2026-06-02T09:00:00+09:00"
    assert ctx["session_high"] == 14.0  # 당일 최고(12+2), 9999 아님
    assert ctx["session_low"] == 7.0  # 당일 최저(9-2), 0.1 아님


def test_session_context_empty_returns_none() -> None:
    assert session_context([]) is None


def test_session_bars_threshold_20min_no_split_40min_split() -> None:
    # 임계 max(step*5, 1800s)=30분. step=1분일 때 20분 갭은 미분리, 40분 갭은 분리.
    def bar(t: str) -> dict[str, Any]:
        return {
            "timestamp": t,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }

    base = [bar(f"2026-06-02T09:0{i}:00+09:00") for i in range(3)]  # step=1분
    assert len(_session_bars(base + [bar("2026-06-02T09:22:00+09:00")])) == 4  # 20분 갭
    assert len(_session_bars(base + [bar("2026-06-02T09:42:00+09:00")])) == 1  # 40분 갭


# --- 정규장 캘린더 앵커(US 24h 거래 — 갭 휴리스틱이 못 잡는 경우) ---
_CAL = {
    "previousBusinessDay": {
        "regularMarket": {
            "startTime": "2026-06-02T22:30:00+09:00",
            "endTime": "2026-06-03T05:00:00+09:00",
        }
    },
    "today": {
        "regularMarket": {
            "startTime": "2026-06-03T22:30:00+09:00",
            "endTime": "2026-06-04T05:00:00+09:00",
        }
    },
    "nextBusinessDay": {"regularMarket": None},
}

_CAL4 = {
    "previousBusinessDay": {
        "dayMarket": {"startTime": "2026-06-02T09:00:00+09:00", "endTime": "2026-06-02T17:00:00+09:00"},
        "preMarket": {"startTime": "2026-06-02T17:00:00+09:00", "endTime": "2026-06-02T22:30:00+09:00"},
        "regularMarket": {"startTime": "2026-06-02T22:30:00+09:00", "endTime": "2026-06-03T05:00:00+09:00"},
        "afterMarket": {"startTime": "2026-06-03T05:00:00+09:00", "endTime": "2026-06-03T08:50:00+09:00"},
    },
    "today": {
        "dayMarket": {"startTime": "2026-06-03T09:00:00+09:00", "endTime": "2026-06-03T17:00:00+09:00"},
        "preMarket": {"startTime": "2026-06-03T17:00:00+09:00", "endTime": "2026-06-03T22:30:00+09:00"},
        "regularMarket": {"startTime": "2026-06-03T22:30:00+09:00", "endTime": "2026-06-04T05:00:00+09:00"},
        "afterMarket": None,
    },
    "nextBusinessDay": {},
}


def test_session_windows_collects_and_sorts() -> None:
    w = _session_windows(_CAL4)
    assert [x["name"] for x in w] == [
        "dayMarket", "preMarket", "regularMarket", "afterMarket",  # 전일 4
        "dayMarket", "preMarket", "regularMarket",                 # 당일 3(after=None 제외)
    ]
    assert w[0]["start"] == "2026-06-02T09:00:00+09:00"
    assert w[3]["end"] == "2026-06-03T08:50:00+09:00"


def test_session_windows_skips_nondict_and_missing() -> None:
    assert _session_windows(None) == []
    assert _session_windows(
        {"today": None, "previousBusinessDay": {"regularMarket": None}, "nextBusinessDay": {}}
    ) == []


def test_resolve_session_active_when_ref_in_window() -> None:
    chosen, active = _resolve_session(_session_windows(_CAL4), "2026-06-03T03:50:00+09:00", "auto")
    assert active == "regularMarket"  # 03:50 은 전일 정규장(22:30~05:00) 안
    assert chosen["name"] == "regularMarket"
    assert chosen["start"] == "2026-06-02T22:30:00+09:00"


def test_resolve_session_auto_falls_back_to_recent_when_no_active() -> None:
    # 08:55 는 애프터(08:50 종료)~데이(09:00 시작) 사이 공백 → active 없음 → 직전 시작 세션
    chosen, active = _resolve_session(_session_windows(_CAL4), "2026-06-03T08:55:00+09:00", "auto")
    assert active is None
    assert chosen["name"] == "afterMarket"
    assert chosen["start"] == "2026-06-03T05:00:00+09:00"


def test_resolve_session_explicit_picks_latest_started() -> None:
    # 당일 데이마켓 10:00 에 regular 선택 → 당일 정규장(22:30) 미개장 → 전일 정규장
    chosen, active = _resolve_session(_session_windows(_CAL4), "2026-06-03T10:00:00+09:00", "regular")
    assert active == "dayMarket"  # 10:00 은 당일 데이마켓 활성
    assert chosen["start"] == "2026-06-02T22:30:00+09:00"


def test_resolve_session_explicit_missing_returns_none() -> None:
    cal = {"today": {"regularMarket": {"startTime": "2026-06-03T22:30:00+09:00", "endTime": "2026-06-04T05:00:00+09:00"}}}
    chosen, _ = _resolve_session(_session_windows(cal), "2026-06-03T10:00:00+09:00", "after")
    assert chosen is None


def test_resolve_session_regular_anchor_matches_legacy() -> None:
    # 구 _pick_session_start 대체: ref 가 전일 정규장 안 → 전일 정규장 시작
    chosen, _ = _resolve_session(_session_windows(_CAL), "2026-06-03T03:50:00+09:00", "regular")
    assert chosen["start"] == "2026-06-02T22:30:00+09:00"


def test_resolve_session_none_when_no_regular() -> None:
    cal = {"previousBusinessDay": {"regularMarket": None}, "today": {}}
    chosen, _ = _resolve_session(_session_windows(cal), "2026-06-03T03:50:00+09:00", "regular")
    assert chosen is None


def test_session_context_calendar_anchor_excludes_pre_session() -> None:
    # 정규장 시작(22:30) 이전 프리장 봉(극단 9999/0.1)은 세션 고저에서 제외되어야 한다.
    pre = [
        {
            "timestamp": f"2026-06-02T21:0{i}:00+09:00",
            "open": 1.0,
            "high": 9999.0,
            "low": 0.1,
            "close": 1.0,
            "volume": 1.0,
        }
        for i in range(2)
    ]
    reg = [
        {
            "timestamp": f"2026-06-02T22:3{i}:00+09:00",
            "open": 10.0,
            "high": 12.0 + i,
            "low": 9.0 - i,
            "close": 10.0,
            "volume": 100.0,
        }
        for i in range(3)
    ]
    ctx = session_context(pre + reg, session_start="2026-06-02T22:30:00+09:00")
    assert ctx is not None
    assert ctx["bars_in_session"] == 3
    assert ctx["session_high"] == 14.0  # 9999(프리장) 제외
    assert ctx["session_low"] == 7.0
    assert ctx["session_complete"] is True  # 데이터가 정규장 시작 이전(21:0x)까지 닿음


def test_session_context_flags_incomplete_when_window_misses_open() -> None:
    # 모든 봉이 정규장 시작 이후 → 시작 이전 데이터 없음 → 조용한 잘림 대신 미완 플래그.
    reg = [
        {
            "timestamp": f"2026-06-02T23:0{i}:00+09:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 100.0,
        }
        for i in range(3)
    ]
    ctx = session_context(reg, session_start="2026-06-02T22:30:00+09:00")
    assert ctx is not None
    assert ctx["session_complete"] is False


def test_select_session_end_bound_excludes_after_session() -> None:
    # 결함 A: 정규장 [22:30,05:00) 윈도가 종료(05:00) 이후 애프터마켓 봉을 제외
    reg = [
        {"timestamp": f"2026-06-02T23:0{i}:00+09:00", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0}
        for i in range(2)
    ]
    after = [{"timestamp": "2026-06-03T05:30:00+09:00", "open": 10.0, "high": 99.0, "low": 1.0, "close": 10.0, "volume": 100.0}]
    ctx = session_context(reg + after, session_start="2026-06-02T22:30:00+09:00", session_end="2026-06-03T05:00:00+09:00")
    assert ctx is not None
    assert ctx["bars_in_session"] == 2  # 애프터 봉 제외
    assert ctx["session_high"] == 11.0  # 99(애프터) 아님


def test_select_session_blend_fix_excludes_prev_regular() -> None:
    # 결함 B: 데이마켓 [09:00,17:00) 선택 시 전일 정규장 봉 미포함
    prev_reg = [{"timestamp": "2026-06-02T23:00:00+09:00", "open": 10.0, "high": 88.0, "low": 1.0, "close": 10.0, "volume": 100.0}]
    day = [
        {"timestamp": f"2026-06-03T10:0{i}:00+09:00", "open": 10.0, "high": 12.0 + i, "low": 9.0, "close": 10.0, "volume": 100.0}
        for i in range(2)
    ]
    ctx = session_context(prev_reg + day, session_start="2026-06-03T09:00:00+09:00", session_end="2026-06-03T17:00:00+09:00")
    assert ctx is not None
    assert ctx["bars_in_session"] == 2
    assert ctx["session_high"] == 13.0  # 88(전일 정규장) 아님


def test_select_session_end_none_keeps_legacy_behavior() -> None:
    # session_end 미지정 → 현행(상한 없음) 동작 유지(후방호환)
    reg = [
        {"timestamp": f"2026-06-02T23:0{i}:00+09:00", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0}
        for i in range(2)
    ]
    after = [{"timestamp": "2026-06-03T05:30:00+09:00", "open": 10.0, "high": 99.0, "low": 1.0, "close": 10.0, "volume": 100.0}]
    ctx = session_context(reg + after, session_start="2026-06-02T22:30:00+09:00")
    assert ctx is not None
    assert ctx["bars_in_session"] == 3  # 상한 없음 → 애프터 포함(현행)


def test_compute_vwap_calendar_anchor_uses_regular_only() -> None:
    pre = [
        {
            "timestamp": "2026-06-02T21:00:00+09:00",
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1000.0,
        }
    ]
    reg = [
        {
            "timestamp": f"2026-06-02T22:3{i}:00+09:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0 + i,
            "volume": 100.0,
        }
        for i in range(2)
    ]
    res = compute_vwap(pre + reg, session_start="2026-06-02T22:30:00+09:00")
    assert res is not None
    assert res["bars_in_session"] == 2  # 프리장(vol 1000) 제외
    # tp0=(11+9+10)/3=10, tp1=(11+9+11)/3=10.333; vwap=(10*100+10.333*100)/200
    assert abs(res["vwap"] - 10.1667) <= 1e-2


# --- CandleCache ---
def _bar(ts: str, high: float = 1.0, low: float = 1.0) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "open": 1.0,
        "high": high,
        "low": low,
        "close": 1.0,
        "volume": 1.0,
    }


def test_candle_cache_extend_dedup_and_sort() -> None:
    c = CandleCache()
    c.extend_candles(
        "AAPL",
        "1m",
        [_bar("2026-06-02T10:01:00+09:00"), _bar("2026-06-02T10:00:00+09:00")],
    )
    c.extend_candles(
        "AAPL",
        "1m",
        [_bar("2026-06-02T10:01:00+09:00"), _bar("2026-06-02T10:02:00+09:00")],
    )
    got = c.get_candles("AAPL", "1m")
    assert [b["timestamp"] for b in got] == [
        "2026-06-02T10:00:00+09:00",
        "2026-06-02T10:01:00+09:00",
        "2026-06-02T10:02:00+09:00",
    ]


def test_candle_cache_caps_oldest() -> None:
    c = CandleCache(max_bars=3)
    c.extend_candles(
        "AAPL", "1m", [_bar(f"2026-06-02T10:0{i}:00+09:00") for i in range(5)]
    )
    got = c.get_candles("AAPL", "1m")
    assert len(got) == 3
    assert got[0]["timestamp"] == "2026-06-02T10:02:00+09:00"  # 오래된 것 폐기


def test_candle_cache_empty_key_returns_list() -> None:
    assert CandleCache().get_candles("NONE", "1m") == []


def test_candle_cache_evicts_oldest_series() -> None:
    c = CandleCache(max_series=2)
    c.extend_candles("A", "1m", [_bar("2026-06-02T10:00:00+09:00")])
    c.extend_candles("B", "1m", [_bar("2026-06-02T10:00:00+09:00")])
    c.extend_candles(
        "C", "1m", [_bar("2026-06-02T10:00:00+09:00")]
    )  # 새 시리즈 → 가장 오래된 A 폐기
    assert c.get_candles("A", "1m") == []
    assert c.get_candles("B", "1m") != []
    assert c.get_candles("C", "1m") != []


def test_candle_cache_calendar_ttl() -> None:
    c = CandleCache(calendar_ttl=100.0)
    c.set_calendar("US", {"today": {}}, now=1000.0)
    assert c.get_calendar("US", now=1050.0) == {"today": {}}  # TTL 내
    assert c.get_calendar("US", now=1101.0) is None  # 만료
    assert c.get_calendar("KR", now=1050.0) is None  # 미설정


# --- _merge_for_lookback (순수) ---
def _page(ts_list: list[str]) -> list[dict[str, Any]]:
    return [_bar(t) for t in ts_list]


def test_merge_enough_from_cache_no_need_before() -> None:
    cached = _page(
        [f"2026-06-02T10:0{i}:00+09:00" for i in range(5)]
    )  # 10:00..10:04 닫힘
    fresh = _page(
        ["2026-06-02T10:05:00+09:00", "2026-06-02T10:06:00+09:00"]
    )  # 끝=라이브
    result, to_add, need_before = _merge_for_lookback(cached, fresh, lookback=4)
    assert need_before is None
    assert result[-1]["timestamp"] == "2026-06-02T10:06:00+09:00"  # 라이브가 마지막
    assert len(result) == 4
    assert to_add == [_bar("2026-06-02T10:05:00+09:00")]  # fresh 의 닫힌 봉만 신규


def test_merge_live_bar_not_in_to_add() -> None:
    _, to_add, _ = _merge_for_lookback(
        [], _page(["2026-06-02T10:00:00+09:00", "2026-06-02T10:01:00+09:00"]), 10
    )
    assert [b["timestamp"] for b in to_add] == [
        "2026-06-02T10:00:00+09:00"
    ]  # 라이브(10:01) 제외


def test_merge_insufficient_returns_oldest_need_before() -> None:
    fresh = _page(["2026-06-02T10:05:00+09:00", "2026-06-02T10:06:00+09:00"])
    result, _, need_before = _merge_for_lookback([], fresh, lookback=10)
    assert (
        need_before == "2026-06-02T10:05:00+09:00"
    )  # 가장 오래된 보유 봉 기준 역fetch
    assert len(result) == 2


def test_merge_dedup_overlap() -> None:
    cached = _page(["2026-06-02T10:00:00+09:00", "2026-06-02T10:01:00+09:00"])
    fresh = _page(
        ["2026-06-02T10:01:00+09:00", "2026-06-02T10:02:00+09:00"]
    )  # 10:01 중복, 끝=라이브
    result, to_add, _ = _merge_for_lookback(cached, fresh, lookback=10)
    ts = [b["timestamp"] for b in result]
    assert ts == sorted(set(ts))  # 중복 없음·정렬
    assert ts == [
        "2026-06-02T10:00:00+09:00",
        "2026-06-02T10:01:00+09:00",
        "2026-06-02T10:02:00+09:00",
    ]
    assert to_add == []  # 10:01 은 캐시에 이미 있고, 10:02 는 라이브라 비캐시


def test_merge_both_empty() -> None:
    result, to_add, need_before = _merge_for_lookback([], [], lookback=5)
    assert result == [] and to_add == [] and need_before is None


def test_merge_empty_fresh_serves_cache() -> None:
    cached = _page(["2026-06-02T10:00:00+09:00"])
    result, to_add, need_before = _merge_for_lookback(cached, [], lookback=1)
    assert [b["timestamp"] for b in result] == ["2026-06-02T10:00:00+09:00"]
    assert to_add == []
    assert need_before is None


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


# --- _fetch_candles_cached (통합, MockTransport) ---
async def test_fetch_candles_cached_reuses_history() -> None:
    from tossinvest_mcp.analytics import _fetch_candles_cached

    # 페이지 A(before 없음): 10:00..10:09(끝=라이브). 페이지 B(before=10:00): 09:50..09:59.
    page_a = [
        {
            "timestamp": f"2026-06-02T10:0{i}:00+09:00",
            "openPrice": "1",
            "highPrice": "1",
            "lowPrice": "1",
            "closePrice": "1",
            "volume": "1",
        }
        for i in range(10)
    ]
    page_b = [
        {
            "timestamp": f"2026-06-02T09:5{i}:00+09:00",
            "openPrice": "1",
            "highPrice": "1",
            "lowPrice": "1",
            "closePrice": "1",
            "volume": "1",
        }
        for i in range(10)
    ]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        before = req.url.params.get("before")
        if before:  # 역방향: 과거 페이지 B, 더 이상 없음
            return httpx.Response(
                200, json={"result": {"candles": page_b, "nextBefore": None}}
            )
        return httpx.Response(  # 최신 페이지 A + 과거 커서
            200,
            json={
                "result": {
                    "candles": page_a,
                    "nextBefore": "2026-06-02T10:00:00+09:00",
                }
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    cache = CandleCache()
    try:
        r1 = await _fetch_candles_cached(client, "AAPL", "1m", 15, cache)
        first = calls["n"]
        r2 = await _fetch_candles_cached(client, "AAPL", "1m", 15, cache)
        second = calls["n"] - first
    finally:
        await client.aclose()

    assert first == 2  # 최신 1 + 역방향 1
    assert second == 1  # 2번째는 최신 페이지만(과거는 캐시)
    assert len(r1) == 15 and len(r2) == 15
    assert r1[-1]["timestamp"] == "2026-06-02T10:09:00+09:00"  # 라이브 신선
    assert [b["timestamp"] for b in r1] == [b["timestamp"] for b in r2]  # 동일 결과


async def test_fetch_candles_cached_refreshes_live_bar() -> None:
    from tossinvest_mcp.analytics import _fetch_candles_cached

    state = {"close": "100"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("before"):
            return httpx.Response(
                200, json={"result": {"candles": [], "nextBefore": None}}
            )
        candles = [
            {
                "timestamp": "2026-06-02T10:00:00+09:00",
                "openPrice": "1",
                "highPrice": "1",
                "lowPrice": "1",
                "closePrice": "1",
                "volume": "1",
            },
            {
                "timestamp": "2026-06-02T10:01:00+09:00",
                "openPrice": "1",
                "highPrice": "1",
                "lowPrice": "1",
                "closePrice": state["close"],
                "volume": "1",
            },  # 라이브 봉
        ]
        return httpx.Response(
            200, json={"result": {"candles": candles, "nextBefore": None}}
        )

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    cache = CandleCache()
    try:
        r1 = await _fetch_candles_cached(client, "AAPL", "1m", 2, cache)
        state["close"] = "200"  # 라이브 봉 가격 변동
        r2 = await _fetch_candles_cached(client, "AAPL", "1m", 2, cache)
    finally:
        await client.aclose()

    assert r1[-1]["close"] == 100.0
    assert r2[-1]["close"] == 200.0  # 라이브 봉은 매번 신선 — 캐시 stale 아님


async def test_fetch_candles_cached_grows_lookback_across_calls() -> None:
    from tossinvest_mcp.analytics import _fetch_candles_cached

    def day(d: int) -> str:
        return f"2026-06-{d:02d}T00:00:00+09:00"

    def row(d: int) -> dict[str, Any]:
        return {
            "timestamp": day(d),
            "openPrice": "1",
            "highPrice": "1",
            "lowPrice": "1",
            "closePrice": "1",
            "volume": "1",
        }

    page_a = [row(d) for d in range(10, 15)]  # 06-10..06-14 (최신, 끝=라이브)
    page_b = [row(d) for d in range(5, 10)]  # 06-05..06-09 (과거)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if req.url.params.get("before"):
            return httpx.Response(
                200, json={"result": {"candles": page_b, "nextBefore": None}}
            )
        return httpx.Response(
            200, json={"result": {"candles": page_a, "nextBefore": day(10)}}
        )

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    cache = CandleCache()
    try:
        small = await _fetch_candles_cached(client, "AAPL", "1d", 3, cache)
        n_small = calls["n"]
        large = await _fetch_candles_cached(client, "AAPL", "1d", 8, cache)
        n_large = calls["n"] - n_small
    finally:
        await client.aclose()

    assert len(small) == 3 and n_small == 1  # 최신 페이지로 충분
    assert len(large) == 8 and n_large == 2  # 최신 1 + 역방향 1(과거 보충)


async def test_regular_session_start_caches_calendar() -> None:
    from tossinvest_mcp.analytics import _regular_session_start

    cal_calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        cal_calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "result": {
                    "previousBusinessDay": {
                        "regularMarket": {
                            "startTime": "2026-06-02T22:30:00+09:00",
                            "endTime": "2026-06-03T05:00:00+09:00",
                        }
                    },
                    "today": {
                        "regularMarket": {
                            "startTime": "2026-06-03T22:30:00+09:00",
                            "endTime": "2026-06-04T05:00:00+09:00",
                        }
                    },
                    "nextBusinessDay": {"regularMarket": None},
                }
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openapi.tossinvest.com",
        transport=httpx.MockTransport(handler),
    )
    cache = CandleCache()
    candles = [_bar("2026-06-03T03:50:00+09:00")]
    try:
        s1 = await _regular_session_start(client, "AAPL", candles, cache)
        s2 = await _regular_session_start(client, "AAPL", candles, cache)
    finally:
        await client.aclose()

    assert s1 == s2 == "2026-06-02T22:30:00+09:00"
    assert cal_calls["n"] == 1  # 2번째는 캐시 사용


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
