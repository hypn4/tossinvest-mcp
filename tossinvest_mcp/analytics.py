"""시세·종목 데이터 기반 기술적 지표·신호 분석 툴.

talipp(순수 파이썬, 의존성 0)로 표준 기술적 지표를 계산하고, talipp 에 없는 MFI 하나만
자체 구현한다. 계산(순수 함수)과 fetch(httpx) 를 분리해 순수 함수는 네트워크 없이 단위 테스트한다.
모든 툴은 읽기 전용(시세 조회 + 계산)이라 거래 활성화와 무관하게 항상 노출한다.

설계: 계산툴은 정확한 지표 값 **대시보드**만 반환하고, 호재/악재 같은 종합 판단은 LLM(프롬프트)이
한다. 다수 지표를 하드코딩 가중합한 종합 점수는 가짜 정밀이라 두지 않는다.
"""

from __future__ import annotations

import asyncio
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


# --- VWAP (세션 앵커, 순수) ---------------------------------------------------
def _session_bars(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최근 세션 봉만 반환. 야간 갭(통상 step 의 5배 또는 30분 초과)을 세션 경계로 본다."""
    if len(candles) < 2:
        return candles

    def ts(c: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(c["timestamp"]).replace("Z", "+00:00"))

    times = [ts(c) for c in candles]
    deltas = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    step = sorted(deltas)[len(deltas) // 2]  # 중앙값 = 통상 1분 간격
    boundary = 0
    for i in range(1, len(times)):
        if (times[i] - times[i - 1]).total_seconds() > max(step * 5, 1800):
            boundary = i
    return candles[boundary:]


def compute_vwap(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candles:
        return None
    session = _session_bars(candles)
    vwap = _last(list(VWAP(input_values=_ohlcv(session))))
    last_close = session[-1]["close"]
    return {
        "vwap": _r(vwap),
        "last_price": _r(last_close),
        "deviation_pct": _r((last_close / vwap - 1) * 100, 2) if vwap else None,
        "bars_in_session": len(session),
        "session_start": session[0]["timestamp"],
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
    client: httpx.AsyncClient, symbol: str, interval: str, lookback: int
) -> list[dict[str, Any]]:
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


# --- 등록 --------------------------------------------------------------------
def register_analytics(mcp: FastMCP, client: httpx.AsyncClient) -> None:
    """계산형 분석 툴을 서버에 등록한다(모두 읽기 전용, 항상 노출)."""

    @mcp.tool(annotations=_READ_ONLY)
    async def analyze_indicators(
        symbol: _SYMBOL,
        interval: Annotated[
            Literal["1m", "1d"],
            Field(description="봉 단위: 1d=일봉(중기 추세), 1m=분봉(장중)."),
        ] = "1d",
        lookback: Annotated[
            int,
            Field(
                ge=30,
                le=252,
                description="가져올 봉 수(30~252). 52주 고저 근사는 1d+252.",
            ),
        ] = 120,
    ) -> dict[str, Any]:
        """기술적 지표 대시보드(EMA·MACD·ADX·SuperTrend·RSI·Stochastic·Bollinger·ATR·OBV·MFI 등).

        가격/거래량을 정확히 계산해 값으로 반환하며, 매매 판단은 하지 않는다(해석은 호출자).
        """
        candles = await _fetch_candles(client, symbol, interval, lookback)
        return {"symbol": symbol, "interval": interval, **compute_indicators(candles)}

    @mcp.tool(annotations=_READ_ONLY)
    async def intraday_vwap(symbol: _SYMBOL) -> dict[str, Any]:
        """장중 세션 기준 VWAP(거래량가중평균가)과 현재가 괴리율. 1분봉을 세션 시작부터 누적."""
        candles = await _fetch_candles(client, symbol, "1m", 400)
        res = compute_vwap(candles)
        return {"symbol": symbol, **(res or {"vwap": None, "note": "데이터 없음"})}

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
            _fetch_candles(client, symbol, "1d", 20),
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
