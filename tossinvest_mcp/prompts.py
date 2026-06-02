"""재사용 워크플로 프롬프트.

모두 읽기 전용(정보 수집·분석)이라 거래 활성화와 무관하게 항상 노출한다.
프롬프트 텍스트가 유일한 콘텐츠이므로 유지보수 부담이 거의 없다.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """서버에 워크플로 프롬프트를 등록한다."""

    @mcp.prompt(
        name="포트폴리오_분석", description="보유 종목·평가손익·비중을 요약 분석"
    )
    def portfolio_review() -> str:
        return (
            "내 계좌를 분석해줘:\n"
            "1) getAccounts 로 계좌 확인\n"
            "2) getHoldings 로 보유 종목·수량·평가손익\n"
            "3) getBuyingPower 로 매수가능금액\n"
            "4) 해외(외화) 보유 종목이 있으면 getExchangeRate 로 환율을 확인해 원화로 환산\n"
            "종목별 비중과 손익을 표로 정리하고, 집중도/리스크를 한국어로 코멘트해줘."
        )

    @mcp.prompt(name="오늘_장_열리나", description="한국/미국 장 개장 여부와 시간")
    def market_open_today() -> str:
        return (
            "오늘 시장 상태를 알려줘:\n"
            "getKrMarketCalendar 와 getUsMarketCalendar 로 한국·미국 개장 여부와 "
            "개장/폐장 시각을 확인해 한국어로 요약."
        )

    @mcp.prompt(
        name="매수_전_체크리스트", description="종목 매수 전 시세·유의·자금 점검"
    )
    def pre_buy_checklist(symbol: str) -> str:
        return (
            f"{symbol} 매수 전 점검:\n"
            f"getPrices·getOrderbook(현재가/호가), getPriceLimit(상·하한가), "
            f"getStockWarnings(매수 유의), getBuyingPower(매수가능금액), "
            f"getCommissions(수수료)를 조회해 한 화면 체크리스트로 정리해줘.\n"
            f"해외(미국 등 비원화) 종목이면 getExchangeRate 로 환율도 반드시 확인해 "
            f"원화 환산 매수금액을 함께 계산할 것. "
            f"주문은 실행하지 말 것."
        )

    @mcp.prompt(
        name="종합_시세_브리핑",
        description="한 종목의 시세·지표·신호를 종합한 트레이딩 브리핑",
    )
    def market_briefing(symbol: str) -> str:
        return (
            f"{symbol} 종합 브리핑을 작성해줘. 아래를 빠짐없이 활용:\n"
            "1) 종목/시장: getStocks(종목명·시장·통화·상장상태)\n"
            "2) 시세: getPrices(현재가)·getOrderbook(호가)·getTrades(최근 체결)·getPriceLimit(상·하한가)\n"
            "3) 기술적 지표: analyze_indicators (추세/모멘텀/변동성/거래량 대시보드)\n"
            "4) 장중이면 intraday_vwap (VWAP 괴리율)\n"
            "5) 위험·신호: get_market_signals (경고·거래정지·상하한 근접·수급)\n"
            "해외(미국 등) 종목이면 getExchangeRate 로 환율도 확인. "
            "추세·모멘텀·수급·위험 관점으로 한국어 요약하고, 단정적 매매 권유는 피하며 주문은 실행하지 말 것."
        )

    @mcp.prompt(name="차트_분석", description="기술적 지표 대시보드 기반 차트 분석")
    def chart_analysis(symbol: str, interval: Literal["1m", "1d"] = "1d") -> str:
        return (
            f"{symbol}({interval}) 차트를 분석해줘:\n"
            "analyze_indicators(symbol, interval) 로 EMA/MACD/ADX/SuperTrend/RSI/Stochastic/"
            "Bollinger/ATR/OBV/MFI 와 기간 고저·거래량 급증을 받아, 추세 방향·강도, 모멘텀(과매수/과매도), "
            "변동성, 지지·저항, 거래량 신호를 해석해. interval='1m' 이면 intraday_vwap 도 함께 보고 "
            "VWAP 대비 위치를 평가해. 수치를 근거로 한국어로 정리하되 단정적 매매 권유는 피할 것."
        )

    @mcp.prompt(
        name="호재악재_신호_점검",
        description="데이터 파생 신호를 호재/주의/악재로 분류",
    )
    def signal_check(symbol: str) -> str:
        return (
            f"{symbol} 의 호재/악재 신호를 점검해줘:\n"
            "get_market_signals(symbol) 의 사실(투자경고/위험·과열·정리매매, 거래정지, 상·하한가 근접, "
            "호가 불균형, 체결 흐름, 거래량 급증)을 호재 / 주의 / 악재로 분류하고 근거를 달아줘.\n"
            "이는 뉴스가 아니라 시장 데이터에서 파생한 신호임을 명시할 것. 체결 흐름은 표본이 작아 약한 "
            "근거로만 다루고, 주문은 실행하지 말 것."
        )
