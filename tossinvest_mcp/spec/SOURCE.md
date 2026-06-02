# 스펙 출처 및 버전 고정(pin)

이 디렉터리의 파일은 토스증권이 공식 배포하는 **권위 있는 원본**을 그대로 vendor 한 것이다.
직접 생성/가공하지 않으며, `openapi.json` 한 개가 **문서이자 MCP 툴 생성 엔진**이다.

## 고정 버전

- **토스증권 Open API `v1.0.3`** (OpenAPI `3.1.0`)
- 20 paths / 21 operations / 53 schemas, server `https://openapi.tossinvest.com`

## 파일과 원본 URL 매핑

| 파일 | 원본 URL | 용도 |
|------|----------|------|
| `openapi.json` | https://openapi.tossinvest.com/openapi-docs/latest/openapi.json | canonical 스펙. `FastMCP.from_openapi()` 입력 + 레퍼런스 |
| `overview.md` | https://openapi.tossinvest.com/openapi-docs/overview.md | 인증 흐름 / rate-limit 티어 / 에러 카탈로그 (JSON이 안 담는 산문) |

사람이 읽는 상세 레퍼런스(자동 생성본)는 vendor 하지 않고 링크로 대체한다:
- API/모델 레퍼런스 인덱스: https://openapi.tossinvest.com/openapi-docs/latest/api-reference/README.md
- 인터랙티브 문서(SPA): https://developers.tossinvest.com/docs

## 갱신 방법

토스가 스펙을 올리면 아래로 재취득하고, **diff 를 리뷰한 뒤 커밋**한다(런타임 자동 fetch 금지):

```bash
uv run python scripts/refresh_spec.py
```

버전이 바뀌면 이 문서의 "고정 버전" 항목도 함께 갱신할 것.
