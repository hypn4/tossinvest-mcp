"""시세·종목 데이터 기반 기술적 지표·신호 분석 툴.

talipp(순수 파이썬, 의존성 0)로 표준 기술적 지표를 계산하고, talipp 에 없는 MFI 하나만
자체 구현한다. 계산(순수 함수)과 fetch(httpx) 를 분리해 순수 함수는 네트워크 없이 단위 테스트한다.
모든 툴은 읽기 전용(시세 조회 + 계산)이라 거래 활성화와 무관하게 항상 노출한다.

설계: 계산툴은 정확한 지표 값 **대시보드**만 반환하고, 호재/악재 같은 종합 판단은 LLM(프롬프트)이
한다. 다수 지표를 하드코딩 가중합한 종합 점수는 가짜 정밀이라 두지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from talipp.indicators import (
    ADX,
    ATR,
    BB,
    EMA,
    MACD,
    OBV,
    RSI,
    SMA,
    Stoch,
    SuperTrend,
    VWAP,
)
from talipp.ohlcv import OHLCVFactory

logger = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# 생성 툴(getPrices/getCandles 등)과 동일한 심볼 안내를 재사용해 LLM 에 일관된 가이드를 준다.
_SYMBOL = Annotated[
    str,
    Field(
        description="종목 심볼. KRX: 6자리 숫자 (예: 005930), US: 영문 티커 (예: AAPL). "
        "영문 대/소문자, 숫자, '.', '-' 만 허용한다."
    ),
]

_CANDLE_KEYS = ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")

# 1분봉 분석이 한 거래 세션 사이클 전체를 덮도록 가져올 봉 수. analyze_indicators(1m)·
# intraday_vwap 가 공유한다. 1500봉(≈25h)이면 US 한 사이클(데이 09:00→애프터 08:50≈1430분)을
# 덮어, 네 세션 중 무엇을 골라도 직전 인스턴스를 온전히 잡는다(닫힌 봉은 캐시로 상쇄).
_INTRADAY_BARS = 1500
_US_SESSIONS = ("dayMarket", "preMarket", "regularMarket", "afterMarket")
_SESSION_ALIAS = {
    "day": "dayMarket",
    "pre": "preMarket",
    "regular": "regularMarket",
    "after": "afterMarket",
}


# --- 작은 유틸 (순수) ---------------------------------------------------------
def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _r(x: Any, n: int = 4) -> float | None:
    return round(x, n) if isinstance(x, (int, float)) else None


def _last(seq: list[Any]) -> Any | None:
    """talipp 출력의 마지막 유효(non-None) 값. warmup 구간은 None 이므로 건너뛴다."""
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _last_if(cond: bool, build: Any) -> Any | None:
    return _last(list(build())) if cond else None


# --- MFI: talipp 에 없는 유일한 지표(자체 구현) -------------------------------
def money_flow_index(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    period: int = 14,
) -> list[float] | None:
    """Money Flow Index(거래량 가중 RSI). 봉이 period 이하이면 None."""
    if len(closes) <= period:
        return None
    tp = [(h + low + c) / 3 for h, low, c in zip(highs, lows, closes)]
    pos = [tp[i] * volumes[i] if tp[i] > tp[i - 1] else 0.0 for i in range(1, len(tp))]
    neg = [tp[i] * volumes[i] if tp[i] < tp[i - 1] else 0.0 for i in range(1, len(tp))]
    out: list[float] = []
    for i in range(period - 1, len(pos)):
        p = sum(pos[i - period + 1 : i + 1])
        n = sum(neg[i - period + 1 : i + 1])
        out.append(100.0 if n == 0 else 0.0 if p == 0 else 100 - 100 / (1 + p / n))
    return out


# --- 캔들 파싱/정렬 (순수) ----------------------------------------------------
def parse_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """문자열 OHLCV -> float, 시각 오름차순 정렬. 결측 봉은 제외."""
    parsed: list[dict[str, Any]] = []
    for row in rows:
        o, h, low, c, v = (_f(row.get(k)) for k in _CANDLE_KEYS)
        if None in (o, h, low, c, v):
            continue
        parsed.append(
            {
                "timestamp": row.get("timestamp"),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
            }
        )
    parsed.sort(key=lambda x: x["timestamp"] or "")
    return parsed


def _ohlcv(candles: list[dict[str, Any]]) -> Any:
    return OHLCVFactory.from_matrix(
        [[c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles]
    )


def _volume_context(candles: list[dict[str, Any]]) -> dict[str, Any]:
    vols = [c["volume"] for c in candles]
    if not vols:
        return {"latest": None, "avg": None, "spike_ratio": None}
    latest = vols[-1]
    prev = vols[:-1] or vols
    avg = sum(prev) / len(prev)
    return {
        "latest": _r(latest, 2),
        "avg": _r(avg, 2),
        "spike_ratio": _r(latest / avg, 2) if avg else None,
    }


# --- 지표 대시보드 (순수) -----------------------------------------------------
def compute_indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """파싱된 캔들에서 표준 지표 + 서술 플래그 대시보드를 만든다(점수화 없음).

    봉 부족/warmup 구간 지표는 null. 장기 지표 신뢰를 위해 최장 기간은 60 으로 제한한다.
    """
    n = len(candles)
    if n == 0:
        return {"bars": 0, "note": "데이터 없음"}
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    ohlcv = _ohlcv(candles)

    ema12 = _last_if(n >= 12, lambda: EMA(period=12, input_values=closes))
    ema26 = _last_if(n >= 26, lambda: EMA(period=26, input_values=closes))
    ema50 = _last_if(n >= 50, lambda: EMA(period=50, input_values=closes))
    sma20 = _last_if(n >= 20, lambda: SMA(period=20, input_values=closes))
    sma60 = _last_if(n >= 60, lambda: SMA(period=60, input_values=closes))
    adx_v = _last_if(
        n > 28, lambda: ADX(di_period=14, adx_period=14, input_values=ohlcv)
    )
    st_v = _last_if(
        n > 10, lambda: SuperTrend(atr_period=10, mult=3, input_values=ohlcv)
    )
    rsi = _last_if(n > 14, lambda: RSI(period=14, input_values=closes))
    stoch_v = _last_if(
        n > 17, lambda: Stoch(period=14, smoothing_period=3, input_values=ohlcv)
    )
    macd_v = _last_if(
        n > 35,
        lambda: MACD(
            fast_period=12, slow_period=26, signal_period=9, input_values=closes
        ),
    )
    bb_v = _last_if(
        n >= 20, lambda: BB(period=20, std_dev_mult=2.0, input_values=closes)
    )
    atr = _last_if(n > 14, lambda: ATR(period=14, input_values=ohlcv))
    obv = _last_if(n >= 2, lambda: OBV(input_values=ohlcv))
    mfi_series = money_flow_index(
        highs, lows, closes, [c["volume"] for c in candles], 14
    )
    mfi = mfi_series[-1] if mfi_series else None

    close = closes[-1]
    pct_b = None
    price_vs_bb = None
    if bb_v is not None:
        span = bb_v.ub - bb_v.lb
        pct_b = _r((close - bb_v.lb) / span) if span else None
        price_vs_bb = (
            "상단 돌파"
            if close > bb_v.ub
            else "하단 이탈"
            if close < bb_v.lb
            else "밴드 내"
        )

    trend_state = None
    if sma20 is not None and sma60 is not None:
        trend_state = (
            "상승"
            if close > sma20 > sma60
            else "하락"
            if close < sma20 < sma60
            else "혼조"
        )

    return {
        "bars": n,
        "latest": {"close": _r(close), "timestamp": candles[-1]["timestamp"]},
        "change": {
            "pct_1bar": _r((closes[-1] / closes[-2] - 1) * 100, 2) if n >= 2 else None,
            "pct_period": _r((closes[-1] / closes[0] - 1) * 100, 2) if n >= 2 else None,
        },
        "trend": {
            "ema12": _r(ema12),
            "ema26": _r(ema26),
            "ema50": _r(ema50),
            "sma20": _r(sma20),
            "sma60": _r(sma60),
            "adx": (
                {
                    "adx": _r(adx_v.adx, 2),
                    "plus_di": _r(adx_v.plus_di, 2),
                    "minus_di": _r(adx_v.minus_di, 2),
                }
                if adx_v
                else None
            ),
            "adx_state": _adx_state(adx_v.adx if adx_v else None),
            "supertrend": (
                {"value": _r(st_v.value), "trend": st_v.trend.name} if st_v else None
            ),
            "macd": (
                {
                    "macd": _r(macd_v.macd),
                    "signal": _r(macd_v.signal),
                    "histogram": _r(macd_v.histogram),
                    "state": "강세" if macd_v.histogram > 0 else "약세",
                }
                if macd_v
                else None
            ),
            "state": trend_state,
        },
        "momentum": {
            "rsi": _r(rsi, 2),
            "rsi_state": _rsi_state(rsi),
            "stoch": (
                {"k": _r(stoch_v.k, 2), "d": _r(stoch_v.d, 2)} if stoch_v else None
            ),
            "stoch_state": _stoch_state(stoch_v.k if stoch_v else None),
        },
        "volatility": {
            "bollinger": (
                {
                    "lower": _r(bb_v.lb),
                    "mid": _r(bb_v.cb),
                    "upper": _r(bb_v.ub),
                    "pct_b": pct_b,
                }
                if bb_v
                else None
            ),
            "price_vs_bb": price_vs_bb,
            "atr": _r(atr),
        },
        "volume": {
            **_volume_context(candles),
            "obv": _r(obv, 2),
            "mfi": _r(mfi, 2),
            "mfi_state": _rsi_state(mfi),  # MFI 도 0~100, 동일 임계 해석
        },
        "range": {
            "period_high": _r(max(highs)),
            "period_low": _r(min(lows)),
        },
        "note": "지표 값 대시보드. 추세/매매 판단(호재·악재 포함)은 해석 단계(LLM)에서. 부족 지표는 null.",
    }


def _rsi_state(v: float | None) -> str | None:
    if v is None:
        return None
    return "과매수" if v >= 70 else "과매도" if v <= 30 else "중립"


def _stoch_state(v: float | None) -> str | None:
    if v is None:
        return None
    return "과매수" if v >= 80 else "과매도" if v <= 20 else "중립"


def _adx_state(v: float | None) -> str | None:
    if v is None:
        return None
    return "추세 강함" if v >= 25 else "횡보" if v < 20 else "중간"


# --- 세션 앵커/VWAP (순수) ----------------------------------------------------
def _parse_ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _session_bars(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최근 세션 봉만 반환. 야간 갭(통상 step 의 5배 또는 30분 초과)을 세션 경계로 본다.

    KR 등 진짜 야간 갭이 있는 시장에서 동작한다. 거의 24시간 연속 거래되는 US 는 갭이
    없어 경계를 못 찾으므로(전체 반환), 이때는 _resolve_session 으로 정규장 시작을
    명시 앵커해야 한다(_select_session 참조).
    """
    if len(candles) < 2:
        return candles
    times = [_parse_ts(c["timestamp"]) for c in candles]
    deltas = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    step = sorted(deltas)[len(deltas) // 2]  # 중앙값 = 통상 1분 간격
    boundary = 0
    for i in range(1, len(times)):
        if (times[i] - times[i - 1]).total_seconds() > max(step * 5, 1800):
            boundary = i
    return candles[boundary:]


def _select_session(
    candles: list[dict[str, Any]],
    session_start: str | None = None,
    session_end: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """(세션 봉, 세션 시작 관측 여부)를 반환한다.

    session_start(ISO) 가 주어지면 [session_start, session_end) 구간 봉으로 앵커(US 세션 캘린더
    기준; session_end=None 이면 상한 없음). 없으면 _session_bars 갭 휴리스틱(KR 등). complete=False 면
    윈도가 세션 시작까지 못 닿아 고저가 불완전함을 뜻한다(조용한 잘림 방지 플래그).
    """
    if session_start is not None:
        start = _parse_ts(session_start)
        end = _parse_ts(session_end) if session_end is not None else None
        session = [
            c
            for c in candles
            if _parse_ts(c["timestamp"]) >= start
            and (end is None or _parse_ts(c["timestamp"]) < end)
        ]
        complete = bool(candles) and _parse_ts(candles[0]["timestamp"]) <= start
        return session, complete
    session = _session_bars(candles)
    return session, len(session) < len(candles)


def _session_windows(calendar: Any) -> list[dict[str, str]]:
    """캘린더 → 세션 인스턴스 [{name,start,end}] (start 오름차순). 결측·null 세션 제외.

    US 4 세션(dayMarket/preMarket/regularMarket/afterMarket)을 previous/today/next 영업일에서
    모아 정렬한다. 순수 함수(네트워크 없음).
    """
    if not isinstance(calendar, dict):
        return []
    out: list[dict[str, str]] = []
    for day in ("previousBusinessDay", "today", "nextBusinessDay"):
        info = calendar.get(day)
        if not isinstance(info, dict):
            continue
        for name in _US_SESSIONS:
            sess = info.get(name)
            if isinstance(sess, dict) and sess.get("startTime") and sess.get("endTime"):
                out.append(
                    {"name": name, "start": sess["startTime"], "end": sess["endTime"]}
                )
    out.sort(key=lambda w: _parse_ts(w["start"]))
    return out


def _resolve_session(
    windows: list[dict[str, str]], ref: Any, requested: str
) -> tuple[dict[str, str] | None, str | None]:
    """(선택 세션 윈도, 현재 활성 세션명)을 고른다. 순수 함수.

    active = start ≤ ref < end 인 인스턴스(최대 1개). requested='auto' 는 active, 없으면 ref
    이전 시작한 가장 최근 세션. 명시(day/pre/regular/after)는 해당 name 중 ref 이전 시작한 가장
    최근 인스턴스(없으면 None). active_name 은 chosen 과 무관하게 현재 열린 세션명(or None).
    """
    r = _parse_ts(ref)
    active = next(
        (w for w in windows if _parse_ts(w["start"]) <= r < _parse_ts(w["end"])), None
    )
    active_name = active["name"] if active else None
    if requested == "auto":
        if active is not None:
            return active, active_name
        started = [w for w in windows if _parse_ts(w["start"]) <= r]
        chosen = max(started, key=lambda w: _parse_ts(w["start"])) if started else None
        return chosen, active_name
    target = _SESSION_ALIAS.get(requested)
    cands = [w for w in windows if w["name"] == target and _parse_ts(w["start"]) <= r]
    chosen = max(cands, key=lambda w: _parse_ts(w["start"])) if cands else None
    return chosen, active_name


def compute_vwap(
    candles: list[dict[str, Any]],
    session_start: str | None = None,
    session_end: str | None = None,
) -> dict[str, Any] | None:
    if not candles:
        return None
    session, complete = _select_session(candles, session_start, session_end)
    if not session:
        return None
    vwap = _last(list(VWAP(input_values=_ohlcv(session))))
    last_close = session[-1]["close"]
    return {
        "vwap": _r(vwap),
        "last_price": _r(last_close),
        "deviation_pct": _r((last_close / vwap - 1) * 100, 2) if vwap else None,
        "bars_in_session": len(session),
        "session_start": session[0]["timestamp"],
        "session_complete": complete,
    }


def session_context(
    candles: list[dict[str, Any]],
    session_start: str | None = None,
    session_end: str | None = None,
) -> dict[str, Any] | None:
    """현재 세션 봉만으로 장중 고저·세션 시작을 요약한다(1분봉 지표 대시보드 보강).

    session_start 가 주어지면 그 이후(US 정규장 캘린더 앵커), 없으면 갭 휴리스틱(KR).
    session_end(ISO) 가 주어지면 [session_start, session_end) 구간으로 앵커(상한 없으면 현행).
    롤링 윈도 기준 range.period_high/low 와 달리 여기 고저는 당일 세션 실제 고저라,
    윈도가 세션 일부만 덮어도 장중 극단값을 놓치지 않는다. session_complete=False 면
    윈도가 세션 시작까지 못 닿아 고저가 불완전하다는 뜻.
    """
    if not candles:
        return None
    session, complete = _select_session(candles, session_start, session_end)
    if not session:
        return None
    return {
        "session_start": session[0]["timestamp"],
        "bars_in_session": len(session),
        "session_high": _r(max(c["high"] for c in session)),
        "session_low": _r(min(c["low"] for c in session)),
        "session_complete": complete,
    }


# --- 마이크로구조/위험 사실 대시보드 (순수, 점수 없음) ------------------------
def _trade_flow(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for t in trades or []:
        p, v = _f(t.get("price")), _f(t.get("volume"))
        if p is None or v is None:
            continue
        rows.append((t.get("timestamp") or "", p, v))
    rows.sort(key=lambda x: x[0])
    up = down = 0.0
    for i in range(1, len(rows)):
        if rows[i][1] > rows[i - 1][1]:
            up += rows[i][2]
        elif rows[i][1] < rows[i - 1][1]:
            down += rows[i][2]
    total = up + down
    return {
        "up_volume": _r(up, 2),
        "down_volume": _r(down, 2),
        "up_ratio": _r(up / total, 3) if total else None,
        "sample": len(rows),
        "note": "최근 ≤50건 표본 — 약한 프록시",
    }


def summarize_microstructure(
    *,
    warnings: list[dict[str, Any]] | None,
    kr_detail: dict[str, Any] | None,
    last_price: Any,
    limits: dict[str, Any],
    orderbook: dict[str, Any],
    trades: list[dict[str, Any]],
    volume_ctx: dict[str, Any],
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "warnings": [
            {
                "type": w.get("warningType"),
                "start": w.get("startDate"),
                "end": w.get("endDate"),
            }
            for w in (warnings or [])
        ]
    }
    if kr_detail:
        facts["kr_flags"] = {
            "liquidation_trading": kr_detail.get("liquidationTrading"),
            "krx_trading_suspended": kr_detail.get("krxTradingSuspended"),
        }

    up, lo, lp = (
        _f(limits.get("upperLimitPrice")),
        _f(limits.get("lowerLimitPrice")),
        _f(last_price),
    )
    limit: dict[str, Any] = {}
    if up and lp:
        limit["pct_to_upper"] = _r((up / lp - 1) * 100, 2)
        limit["near_upper"] = (lp / up) >= 0.97
    if lo and lp:
        limit["pct_to_lower"] = _r((lp / lo - 1) * 100, 2)
        limit["near_lower"] = (lp / lo) <= 1.03
    facts["limit"] = limit or {"note": "가격제한 없음(해외 등)"}

    asks = sum(_f(a.get("volume")) or 0 for a in orderbook.get("asks", []))
    bids = sum(_f(b.get("volume")) or 0 for b in orderbook.get("bids", []))
    facts["orderbook"] = {
        "bid_volume": _r(bids, 2),
        "ask_volume": _r(asks, 2),
        "bid_ask_ratio": _r(bids / asks, 3) if asks else None,
    }
    facts["trade_flow"] = _trade_flow(trades)
    facts["volume_surge"] = volume_ctx
    facts["note"] = (
        "데이터 파생 신호이며 뉴스가 아님. 호재/악재 분류는 해석 단계(LLM)에서."
    )
    return facts


# --- 캔들·캘린더 캐시 (rate-limit 완화; 닫힌 봉·캘린더만, 라이브는 항상 신선) ------
# 기본은 인메모리 dict(휘발성). db_path 설정 시 SQLite 파일에 영속(opt-in, 아래 CandleCache).
_MAX_PAGES = 10  # 캐시 역방향 페이지네이션 가드(200*10=2000봉)
_CANDLE_RETENTION = 7 * 86400.0  # 디스크 보존창(초). 이보다 오래 미재조회된 봉은 prune.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT, interval TEXT, timestamp TEXT,
  open REAL, high REAL, low REAL, close REAL, volume REAL, fetched_at REAL,
  PRIMARY KEY (symbol, interval, timestamp)
);
CREATE TABLE IF NOT EXISTS calendar(market TEXT PRIMARY KEY, payload TEXT, fetched_at REAL);
"""


class _SqliteStore:
    """닫힌 봉·캘린더의 SQLite 영속 저장소(stdlib sqlite3, 의존성 0).

    CandleCache 의 파일 모드에서만 쓰인다. 동기 호출·단일 장수 연결이며 모든 SQL/직렬화/prune
    를 여기 가둔다(파일 모드 신규 실패 모드 격리). 손상 파일이면 __init__ 에서 예외 → 호출자 폴백.
    """

    def __init__(
        self, db_path: str, retention_seconds: float = _CANDLE_RETENTION
    ) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")  # 다중 프로세스(여러 세션) 공존
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)  # 손상 파일이면 여기서 예외
        self._retention = retention_seconds

    def load_candles(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT timestamp, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND interval=? ORDER BY timestamp",
            (symbol, interval),
        )
        return [
            {
                "timestamp": r[0],
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5],
            }
            for r in cur.fetchall()
        ]

    def upsert_candles(
        self, symbol: str, interval: str, bars: list[dict[str, Any]], now: float
    ) -> None:
        self._conn.executemany(
            "INSERT INTO candles(symbol,interval,timestamp,open,high,low,close,volume,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,interval,timestamp) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at",
            [
                (
                    symbol,
                    interval,
                    b["timestamp"],
                    b["open"],
                    b["high"],
                    b["low"],
                    b["close"],
                    b["volume"],
                    now,
                )
                for b in bars
            ],
        )
        # 절대 기준 prune(상대 축출 금지 — 다중 프로세스 경합 회피)
        self._conn.execute(
            "DELETE FROM candles WHERE fetched_at < ?", (now - self._retention,)
        )
        self._conn.commit()

    def delete_series(self, symbol: str, interval: str) -> None:
        self._conn.execute(
            "DELETE FROM candles WHERE symbol=? AND interval=?", (symbol, interval)
        )
        self._conn.commit()

    def load_calendar(self, market: str) -> tuple[Any, float] | None:
        cur = self._conn.execute(
            "SELECT payload, fetched_at FROM calendar WHERE market=?", (market,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0]), row[1]

    def save_calendar(self, market: str, payload: Any, now: float) -> None:
        self._conn.execute(
            "INSERT INTO calendar(market,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(market) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (market, json.dumps(payload), now),
        )
        self._conn.commit()


class CandleCache:
    """확정(닫힌) 캔들과 시장 캘린더의 캐시(서버 수명 1개).

    닫힌 봉은 불변이라 누적 보관하고, 라이브 봉은 캐시하지 않는다(호출자가 매번 신선
    fetch). 캘린더는 market 별 TTL. 단일 사용자 세션 기준이라 동시성 보호는 두지 않는다.

    기본은 인메모리 dict(휘발성, 현행). db_path 가 주어지면(opt-in) 닫힌 봉·캘린더를 SQLite
    파일에 write-through 하고 최초 접근 시 로드해 재시작 후에도 재사용한다. dict 가 인프로세스
    단일 진실원(핫 패스)이고 SQLite 는 영속 사이드카다. 연결/스키마 실패·손상 시 로그 후 인메모리로
    폴백한다(분석은 절대 막지 않음). 캘린더 TTL 은 wall-clock(`now` 주입) 기준이라 재시작 후에도 유효.
    """

    def __init__(
        self,
        max_bars: int = 2000,
        max_series: int = 64,
        calendar_ttl: float = 1800.0,
        db_path: str | None = None,
        retention_seconds: float = _CANDLE_RETENTION,
    ) -> None:
        self._candles: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._calendar: dict[str, tuple[Any, float]] = {}
        self._max_bars = max_bars
        self._max_series = max_series
        self._calendar_ttl = calendar_ttl
        self._store: _SqliteStore | None = None
        if db_path is not None:
            try:
                self._store = _SqliteStore(db_path, retention_seconds)
            except Exception as exc:  # 손상·권한 등 → 인메모리 폴백
                logger.warning("캔들 캐시 SQLite 비활성(인메모리 폴백): %s", exc)
                self._store = None

    def get_candles(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        key = (symbol, interval)
        if key not in self._candles and self._store is not None:
            try:
                loaded = self._store.load_candles(symbol, interval)
            except Exception as exc:
                logger.warning("캔들 캐시 로드 실패(%s/%s): %s", symbol, interval, exc)
                loaded = []
            if loaded:
                self._candles[key] = loaded[-self._max_bars :]
        return self._candles.get(key, [])

    def extend_candles(
        self, symbol: str, interval: str, bars: list[dict[str, Any]]
    ) -> None:
        key = (symbol, interval)
        merged = {c["timestamp"]: c for c in self._candles.get(key, [])}
        for c in bars:
            merged[c["timestamp"]] = c
        out = sorted(merged.values(), key=lambda c: c["timestamp"])
        if len(out) > self._max_bars:
            out = out[-self._max_bars :]
        if key not in self._candles and len(self._candles) >= self._max_series:
            self._candles.pop(next(iter(self._candles)))  # 가장 먼저 들어온 시리즈 폐기
        self._candles[key] = out
        if self._store is not None and bars:
            try:
                self._store.upsert_candles(symbol, interval, bars, time.time())
            except Exception as exc:  # 영속 실패는 비치명(인메모리는 이미 갱신됨)
                logger.warning("캔들 캐시 영속 실패(%s/%s): %s", symbol, interval, exc)

    def invalidate(self, symbol: str, interval: str) -> None:
        """해당 시리즈를 인메모리·디스크에서 폐기(조정가격 재조정 감지 시)."""
        self._candles.pop((symbol, interval), None)
        if self._store is not None:
            try:
                self._store.delete_series(symbol, interval)
            except Exception as exc:
                logger.warning(
                    "캔들 캐시 무효화 실패(%s/%s): %s", symbol, interval, exc
                )

    def get_calendar(self, market: str, now: float) -> Any | None:
        hit = self._calendar.get(market)
        if hit is None and self._store is not None:
            try:
                hit = self._store.load_calendar(market)
            except Exception as exc:
                logger.warning("캘린더 캐시 로드 실패(%s): %s", market, exc)
                hit = None
            if hit is not None:
                self._calendar[market] = hit
        if hit is not None and now - hit[1] < self._calendar_ttl:
            return hit[0]
        return None

    def set_calendar(self, market: str, payload: Any, now: float) -> None:
        self._calendar[market] = (payload, now)
        if self._store is not None:
            try:
                self._store.save_calendar(market, payload, now)
            except Exception as exc:
                logger.warning("캘린더 캐시 영속 실패(%s): %s", market, exc)


def _merge_for_lookback(
    cached_closed: list[dict[str, Any]],
    fresh_page: list[dict[str, Any]],
    lookback: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """캐시된 닫힌 봉 + 방금 받은 최신 페이지로 (반환봉, 신규 캐시봉, 역fetch 기준시각) 계산.

    fresh_page 의 마지막 원소는 라이브(진행 중) 봉으로 캐시하지 않는다. 반환봉 수가
    lookback 에 못 미치면 need_before(가장 오래된 보유 봉 timestamp)로 역방향 추가 fetch
    를 알린다. 순수 함수(네트워크 없음).
    """
    closed_fresh = fresh_page[:-1]
    live = fresh_page[-1:]  # 0 또는 1개
    seen = {c["timestamp"] for c in cached_closed}
    closed_to_add = [c for c in closed_fresh if c["timestamp"] not in seen]
    merged = {c["timestamp"]: c for c in cached_closed}
    for c in closed_fresh:
        merged[c["timestamp"]] = c
    closed_sorted = sorted(merged.values(), key=lambda c: c["timestamp"])
    available = closed_sorted + list(live)
    result = available[-lookback:]
    need_before = (
        available[0]["timestamp"] if available and len(available) < lookback else None
    )
    return result, closed_to_add, need_before


def _overlap_consistent(
    cached_closed: list[dict[str, Any]], fresh_page: list[dict[str, Any]]
) -> bool | None:
    """캐시된 닫힌 봉과 방금 받은 최신 페이지에서 '겹치는 가장 최근 봉'의 OHLC 일치 여부.

    조정가격(split/dividend) 소급 재조정 감지용. True=겹침+일치(조정 기준 동일 → 캐시 신뢰),
    False=겹침+불일치(재조정 → 캐시 폐기), None=겹침 없음(검증 불가, 보통 교차일 갭 → 캐시 폐기).
    순수 함수. 영속 캐시가 조정 전 옛 봉을 새 조정 최신 페이지와 병합해 지표를 오염시키는 것을 막는다.
    OHLC 를 프록시로 쓴다(가격은 그대로고 거래량만 재산정되는 드문 경우는 못 잡음 — VWAP 만 영향).
    인메모리·파일 모드 모두에 적용돼, 겹침 없는 드문 경우의 비연속 윈도도 함께 정리한다(부수 효과).
    """
    if not cached_closed or not fresh_page:
        return None
    fresh_by_ts = {
        c["timestamp"]: c for c in fresh_page[:-1]
    }  # 라이브(마지막) 봉 제외 — 닫힌 봉만 비교
    for c in reversed(cached_closed):
        f = fresh_by_ts.get(c["timestamp"])
        if f is not None:
            return (
                c["open"] == f["open"]
                and c["high"] == f["high"]
                and c["low"] == f["low"]
                and c["close"] == f["close"]
            )
    return None


# --- fetch (httpx, 순수 계산과 분리) ------------------------------------------
def _error_detail(resp: httpx.Response) -> str:
    """토스 에러 엔벨로프(ApiError{code,message,requestId})를 LLM 친화 메시지로.

    생성 툴(OpenAPITool)이 에러 바디를 노출하는 것과 일관되게, 분석 툴도 원인을 드러낸다.
    """
    try:
        err = resp.json().get("error")
    except Exception:
        err = None
    if isinstance(err, dict):
        return (
            f"토스 API {resp.status_code} [{err.get('code')}] "
            f"{err.get('message')} (requestId={err.get('requestId')})"
        )
    return f"토스 API {resp.status_code}: {resp.text[:200]}"


async def _result(
    client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
) -> Any:
    resp = await client.get(path, params=params)
    if resp.is_error:
        raise ToolError(_error_detail(resp))
    data = resp.json()
    return data.get("result", data) if isinstance(data, dict) else data


async def _fetch_candles(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    lookback: int,
    cache: CandleCache | None = None,
) -> list[dict[str, Any]]:
    if cache is not None:
        return await _fetch_candles_cached(client, symbol, interval, lookback, cache)
    rows: list[dict[str, Any]] = []
    before: str | None = None
    while len(rows) < lookback:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": 200,
            "adjusted": True,
        }
        if before:
            params["before"] = before
        res = await _result(client, "/api/v1/candles", params)
        batch = res.get("candles", []) if isinstance(res, dict) else []
        rows.extend(batch)
        before = res.get("nextBefore") if isinstance(res, dict) else None
        if not before or not batch:
            break
    parsed = parse_candles(rows)
    return parsed[-lookback:] if lookback else parsed


async def _fetch_candles_cached(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    lookback: int,
    cache: CandleCache,
) -> list[dict[str, Any]]:
    """닫힌 봉은 캐시에서, 라이브 봉은 최신 페이지로 항상 신선하게. 부족분만 역방향 fetch.

    역방향 페이지네이션은 무캐시 경로와 동일하게 API 의 nextBefore 커서를 사용한다.
    정상 응답에서는 무캐시 경로와 동일한 닫힌 봉을 돌려준다. 단 API 가 결측 OHLCV(파싱 탈락) 봉을
    섞어 줄 때만, 무캐시(원시 행 수 기준)와 캐시(파싱 유효 봉 수 기준)의 정지 조건이 달라질 수 있다.
    """

    async def page(before: str | None) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": 200,
            "adjusted": True,
        }
        if before:
            params["before"] = before
        res = await _result(client, "/api/v1/candles", params)
        if not isinstance(res, dict):
            return [], None
        return parse_candles(res.get("candles", [])), res.get("nextBefore")

    fresh, cursor = await page(None)  # 최신 페이지 — 항상 신선
    cached = cache.get_candles(symbol, interval)
    # fresh 가 비면(일시적 빈 응답) 검증·병합 대상이 없으므로 캐시를 폐기하지 않고 그대로 서빙한다.
    if cached and fresh and _overlap_consistent(cached, fresh) is not True:
        # 조정가격 재조정(가격 불연속) 또는 검증 불가(겹침 없음) → 캐시 폐기 후 fresh 기준 재구성
        cache.invalidate(symbol, interval)
        cached = []
    result, to_add, need_before = _merge_for_lookback(cached, fresh, lookback)
    if to_add:
        cache.extend_candles(symbol, interval, to_add)
    guard = 0
    while need_before is not None and cursor and guard < _MAX_PAGES:
        guard += 1
        older, cursor = await page(cursor)  # API nextBefore 커서로 과거 페이지
        if not older:
            break
        cache.extend_candles(symbol, interval, older)  # 과거 = 닫힘
        result, _, need_before = _merge_for_lookback(
            cache.get_candles(symbol, interval), fresh, lookback
        )
    return result


async def _session_anchor(
    client: httpx.AsyncClient,
    symbol: str,
    candles: list[dict[str, Any]],
    requested: str,
    cache: CandleCache | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """선택 세션 윈도를 시장 캘린더로 해석한다 → (start, end, name, active_name).

    US 종목만 캘린더 앵커. KR(6자리 숫자)·결측·호출 실패 시 (None,None,None,None) → 호출자가 갭
    휴리스틱으로 폴백한다. cache 주입 시 market 별 TTL 로 캘린더를 재사용한다.
    """
    if not candles or symbol.isdigit():  # KR 6자리 숫자는 갭 휴리스틱
        return None, None, None, None
    now = (
        time.time()
    )  # wall-clock(캘린더 TTL은 재시작 후에도 유효해야 하므로 monotonic 아님)
    cal = cache.get_calendar("US", now) if cache is not None else None
    if cal is None:
        try:
            cal = await _result(client, "/api/v1/market-calendar/US", {})
        except Exception:
            return None, None, None, None
        if cache is not None:
            cache.set_calendar("US", cal, now)
    chosen, active = _resolve_session(
        _session_windows(cal), candles[-1]["timestamp"], requested
    )
    if chosen is None:
        return None, None, None, active
    return chosen["start"], chosen["end"], chosen["name"], active


# --- 등록 --------------------------------------------------------------------
def register_analytics(
    mcp: FastMCP, client: httpx.AsyncClient, db_path: str | None = None
) -> None:
    """계산형 분석 툴을 서버에 등록한다(모두 읽기 전용, 항상 노출).

    db_path 가 주어지면 캔들·캘린더 캐시를 SQLite 파일에 영속한다(opt-in, rate-limit 완화).
    """
    cache = CandleCache(db_path=db_path)

    @mcp.tool(annotations=_READ_ONLY)
    async def analyze_indicators(
        symbol: _SYMBOL,
        interval: Annotated[
            Literal["1m", "1d"],
            Field(
                description="봉 단위: 1d=일봉(중기 추세), "
                "1m=분봉(장중, 세션 앵커 — 당일 세션 전체를 덮어 분석)."
            ),
        ] = "1d",
        lookback: Annotated[
            int,
            Field(
                ge=30,
                le=252,
                description="가져올 봉 수(30~252). 52주 고저 근사는 1d+252. "
                "interval=1d 에만 적용된다. 1m 은 lookback 을 무시하고 "
                "당일 세션 전체(약 1,500봉)를 덮는다.",
            ),
        ] = 120,
        session: Annotated[
            Literal["auto", "day", "pre", "regular", "after"],
            Field(
                description="1m 세션 선택: auto=현재 활성(없으면 직전) 세션, "
                "day/pre/regular/after=US 데이/프리/정규/애프터마켓. interval=1m 에만 적용. "
                "지표 대시보드는 롤링 윈도라 session 과 무관하게 동일하고, session.* 와 VWAP 만 선택 세션을 반영한다."
            ),
        ] = "auto",
    ) -> dict[str, Any]:
        """기술적 지표 대시보드(EMA·MACD·ADX·SuperTrend·RSI·Stochastic·Bollinger·ATR·OBV·MFI 등).

        가격/거래량을 정확히 계산해 값으로 반환하며, 매매 판단은 하지 않는다(해석은 호출자).

        interval=1m 은 당일 세션 전체를 가져와 분석하고 `session`(session_start·
        bars_in_session·session_high·session_low·session_complete)을 함께 반환한다.
        US 는 거의 24h 연속 거래라 갭으로 세션을 못 가르므로 시장 캘린더의 정규장 시작을
        앵커하고(KR 은 야간 갭 휴리스틱), session_complete=False 면 윈도가 정규장 시작까지
        못 닿아 고저가 불완전함을 뜻한다. 표준 관례대로 지표 자체는 세션마다 리셋하지 않는
        롤링 윈도로 계산하므로(차트 플랫폼과 동일), 장중 실제 고저는 롤링 윈도의 range 가
        아니라 session_high/low 를 참조해야 한다.
        """
        if interval == "1m":
            candles = await _fetch_candles(client, symbol, "1m", _INTRADAY_BARS, cache)
            start, end, name, active = await _session_anchor(
                client, symbol, candles, session, cache
            )
            sess = session_context(candles, start, end)
            if sess is not None:
                sess = {
                    "name": name,
                    **sess,
                    "in_progress": (name == active) if name is not None else None,
                }
            else:
                sess = {
                    "name": name,
                    "session_start": None,
                    "bars_in_session": 0,
                    "session_high": None,
                    "session_low": None,
                    "session_complete": False,
                    "in_progress": (name == active) if name is not None else None,
                }
            return {
                "symbol": symbol,
                "interval": interval,
                "requested_session": session,
                "active_session": active,
                **compute_indicators(candles),
                "session": sess,
            }
        candles = await _fetch_candles(client, symbol, interval, lookback, cache)
        return {"symbol": symbol, "interval": interval, **compute_indicators(candles)}

    @mcp.tool(annotations=_READ_ONLY)
    async def intraday_vwap(
        symbol: _SYMBOL,
        session: Annotated[
            Literal["auto", "day", "pre", "regular", "after"],
            Field(
                description="세션 선택: auto=현재 활성(없으면 직전) 세션, "
                "day/pre/regular/after=US 데이/프리/정규/애프터마켓. VWAP 를 선택 세션 시작에 앵커한다."
            ),
        ] = "auto",
    ) -> dict[str, Any]:
        """장중 세션 기준 VWAP(거래량가중평균가)과 현재가 괴리율.

        US 는 시장 캘린더로 선택 세션(`session`)의 시작에 앵커하고, KR 은 야간 갭을 세션 경계로
        앵커한다. session_complete=False 면 윈도가 세션 시작까지 못 닿아 VWAP 가 세션 일부만
        반영함을 뜻한다. active_session 은 현재 열린 세션(KR/폴백 시 null).
        """
        candles = await _fetch_candles(client, symbol, "1m", _INTRADAY_BARS, cache)
        start, end, name, active = await _session_anchor(
            client, symbol, candles, session, cache
        )
        res = compute_vwap(candles, start, end)
        out: dict[str, Any] = {
            "symbol": symbol,
            "requested_session": session,
            "active_session": active,
            "name": name,
        }
        if res is not None:
            out.update(res)
            out["in_progress"] = (name == active) if name is not None else None
        else:
            # candles 는 있으나 선택 세션 윈도에 봉이 0개(직전 인스턴스가 fetch 윈도 밖)면 name 이 잡힌다.
            # candles 자체가 없으면 name=None. 두 경우 모두 키 집합을 동일하게 유지한다.
            out.update(
                {
                    "vwap": None,
                    "last_price": None,
                    "deviation_pct": None,
                    "bars_in_session": 0,
                    "session_start": None,
                    "session_complete": False,
                    "in_progress": (name == active) if name is not None else None,
                    "note": "선택 세션에 봉 없음(윈도 밖)"
                    if name is not None
                    else "데이터 없음",
                }
            )
        return out

    @mcp.tool(annotations=_READ_ONLY)
    async def get_market_signals(symbol: _SYMBOL) -> dict[str, Any]:
        """호재/악재 판단에 쓰는 **데이터 파생 신호 사실 대시보드**(점수화하지 않음).

        경고(투자위험/경고/과열/정리매매)·KR 거래정지·상하한가 근접·호가 불균형·체결 흐름·거래량 급증을
        구조화해 반환한다. 호재/악재 분류는 호출자(LLM)가 한다. 뉴스가 아니다.

        부분 실패 내성: 7개 엔드포인트를 병렬 호출하고, 일부가 실패해도 성공한 신호만 모아 반환하며
        실패 항목은 `unavailable` 에 표기한다(US/KR 엔드포인트 거동 차이·일시 오류에 강건).
        """
        results = await asyncio.gather(
            _result(client, f"/api/v1/stocks/{symbol}/warnings"),
            _result(client, "/api/v1/stocks", {"symbols": symbol}),
            _result(client, "/api/v1/price-limits", {"symbol": symbol}),
            _result(client, "/api/v1/prices", {"symbols": symbol}),
            _result(client, "/api/v1/orderbook", {"symbol": symbol}),
            _result(client, "/api/v1/trades", {"symbol": symbol, "count": 50}),
            _fetch_candles(client, symbol, "1d", 20, cache),
            return_exceptions=True,
        )
        unavailable: list[str] = []

        def _ok(value: Any, name: str, default: Any) -> Any:
            if isinstance(value, Exception):
                unavailable.append(name)
                return default
            return value

        warnings = _ok(results[0], "warnings", [])
        stocks = _ok(results[1], "stocks", [])
        limits = _ok(results[2], "price_limits", {})
        prices = _ok(results[3], "prices", [])
        orderbook = _ok(results[4], "orderbook", {})
        trades = _ok(results[5], "trades", [])
        ctx = _ok(results[6], "candles", [])

        kr_detail = stocks[0].get("koreanMarketDetail") if stocks else None
        last_price = prices[0].get("lastPrice") if prices else None
        facts = summarize_microstructure(
            warnings=warnings,
            kr_detail=kr_detail,
            last_price=last_price,
            limits=limits,
            orderbook=orderbook,
            trades=trades,
            volume_ctx=_volume_context(ctx),
        )
        if unavailable:
            facts["unavailable"] = unavailable
        return {"symbol": symbol, **facts}
