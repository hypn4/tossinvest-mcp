"""핀된 스펙 갱신 스크립트.

토스증권이 스펙을 올렸을 때 `spec/openapi.json` 과 `spec/overview.md` 를
재취득한다. 런타임 자동 fetch 가 아니라, **리뷰 가능한 커밋**으로 갱신하기 위한 도구다.

사용:
    uv run python scripts/refresh_spec.py
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

BASE = "https://openapi.tossinvest.com/openapi-docs"
SPEC_DIR = Path(__file__).resolve().parent.parent / "tossinvest_mcp" / "spec"
SOURCES = {
    "openapi.json": f"{BASE}/latest/openapi.json",
    "overview.md": f"{BASE}/overview.md",
}


def main() -> None:
    SPEC_DIR.mkdir(exist_ok=True)
    with httpx.Client(timeout=30.0) as client:
        for filename, url in SOURCES.items():
            resp = client.get(url)
            resp.raise_for_status()
            (SPEC_DIR / filename).write_bytes(resp.content)
            print(f"saved {filename}  ({len(resp.content):,} bytes)")

    spec = json.loads((SPEC_DIR / "openapi.json").read_text(encoding="utf-8"))
    info = spec["info"]
    print(
        f"\npinned: {info['title']} v{info['version']} "
        f"(openapi {spec['openapi']}, paths {len(spec['paths'])}, "
        f"schemas {len(spec.get('components', {}).get('schemas', {}))})"
    )
    print(
        "git diff 로 변경을 확인하고 커밋하세요. 버전이 바뀌면 spec/SOURCE.md 도 갱신."
    )


if __name__ == "__main__":
    main()
