# 토스증권 MCP

토스증권 Open API를 [MCP](https://modelcontextprotocol.io) 도구로 노출하는 서버입니다.
Claude 같은 AI 에이전트가 토스증권의 시세 조회·보유 종목·주문 기능을 도구로 사용할 수 있습니다.

> **실거래 경고**: 주문 집행 툴은 실제 주문·실자금을 발생시킵니다.
> 기본값은 **읽기 전용**이며, `TOSS_ENABLE_TRADING=true`일 때만 주문 집행 툴이 켜집니다.

## 할 수 있는 일

- **시세** — 현재가, 호가, 체결 내역, 캔들 차트, 상/하한가
- **종목·시장** — 종목 정보, 매수 유의사항, 환율, 국내·해외 장 운영시간
- **계좌·조회** — 계좌 목록, 보유 종목, 주문 내역, 매수 가능 금액, 매도 가능 수량, 수수료
- **주문 집행** *(거래 활성화 시)* — 주문 생성·정정·취소

## 빠른 시작

1. [토스증권 OpenAPI 콘솔](https://developers.tossinvest.com/docs)에서 `client_id`·`client_secret` 발급
2. 설정 후 실행:

```bash
cp .env.example .env   # 발급받은 키 입력
uv sync
uv run tossinvest-mcp
```

설정할 수 있는 모든 값은 [`.env.example`](.env.example)에 정리되어 있습니다.

## Claude Code에 등록

```bash
claude mcp add tossinvest -- uv --directory /path/to/tossinvest-mcp run tossinvest-mcp
```

## 거래 활성화

기본은 안전하게 **읽기 전용**입니다. 주문 집행까지 쓰려면 `.env`에 추가하세요:

```bash
TOSS_ENABLE_TRADING=true
```

AI가 직접 실주문을 낼 수 있으니 신뢰할 수 있는 환경에서만 켜세요.

## 테스트

```bash
uv run pytest -m "not e2e"
```
