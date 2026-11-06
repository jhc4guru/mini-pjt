"""개발용 지표 요약(T7-1, D-60): `data/trace.sqlite`(로컬 트레이스)를 읽어 호출·토큰, 폴백 비율, 쓰로틀링, 가드 차단 비율, 수정 시도, 계획 결과를 출력한다.

    .venv\\Scripts\\python.exe eval\\trace_summary.py                 # 전체
    .venv\\Scripts\\python.exe eval\\trace_summary.py --session <id>  # 세션 하나
    .venv\\Scripts\\python.exe eval\\trace_summary.py --hours 24      # 최근 24시간

모델은 부르지 않는다. 트레이스에는 요청·프롬프트·결과 본문이 없고 이름·코드·토큰·지연만 있다.
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

from app import config  # noqa: E402
from app.observability import LocalTracer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--hours", type=float)
    ap.add_argument("--db", default=str(config.DATA_DIR / "trace.sqlite"))
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"트레이스 파일이 없습니다: {a.db} (서버를 한 번 실행해 요청을 처리하면 생깁니다)")
        return 1
    t = LocalTracer(a.db)
    s = t.summary(since=(time.time() - a.hours * 3600) if a.hours else None, session_id=a.session)
    print(f"모델 호출 {s['llm_calls']}회(실패 {s['llm_failures']}, 쓰로틀링 {s['throttles']}) | 입력 {s['input_tokens']:,} 출력 {s['output_tokens']:,} 토큰 | 캐시 읽기 {s['cache_read_tokens']:,} 쓰기 {s['cache_write_tokens']:,} | 평균 지연 {s['avg_latency_ms']}ms")
    for role, d in sorted(s["by_role"].items()):
        print(f"  {role}: {d['calls']}회, 입력 {d['input_tokens']:,} 출력 {d['output_tokens']:,}")
    print(f"폴백(요청 역할 ≠ 실제 역할): {s['fallback_calls']}회 ({s['fallback_ratio'] * 100:.0f}%)")
    print(f"읽기 도구 {s['tool_calls']}회 | 오류 {s['tool_errors']}회 | 가드 차단 {s['guard_blocks']}회 ({s['guard_block_ratio'] * 100:.0f}%)")
    print(f"계획 생성 결과: {s['pipelines'] or '없음'} | 수정 시도 합계 {s['repairs']}회 | 누출 차단된 트레이스 이벤트 {s['blocked_events']}건")
    t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
