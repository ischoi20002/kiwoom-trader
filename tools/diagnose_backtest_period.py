# -*- coding: utf-8 -*-
"""
포트폴리오 백테스트 기간 중 15분봉 Signal∧Ready(시장필터 전·후) 일별 집계.
미체결이 '데이터 부재'인지 '전략/시장필터상 신호 없음'인지 구분할 때 사용.

사용 예 (프로젝트 루트에서):
  python tools/diagnose_backtest_period.py 2025-04-01 2025-04-29
  python tools/diagnose_backtest_period.py   # settings.json 의 bt_start_date ~ bt_end_date
"""
from __future__ import annotations

import json
import os
import sys

# 프로젝트 루트를 path 에 추가
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    settings_path = os.path.join(_ROOT, "settings.json")
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)
    if len(sys.argv) >= 3:
        start_d, end_d = sys.argv[1], sys.argv[2]
    else:
        start_d = settings.get("bt_start_date", "2025-04-01")
        end_d = settings.get("bt_end_date", "2025-04-29")
        print(f"(settings.json 기간) {start_d} ~ {end_d}")

    from dynamic_backtest import DynamicBacktester

    tester = DynamicBacktester(settings)
    df = tester.diagnose_intraday_entry_potential(start_date=start_d, end_date=end_d)
    if df.empty:
        print("진단 실패 또는 데이터 없음.")
        return
    print(df.to_string(index=False))
    tot_pre = int(df["n_signal_ready_pre_mkt"].sum())
    tot_post = int(df["n_signal_ready_post_mkt"].sum())
    print("---")
    print(f"기간 합계: 시장필터 전 Signal∧Ready = {tot_pre}, 후 = {tot_post}")
    print("※ 고정밀(1m) 재판단·Smart Buy·잔고/슬롯은 미반영. 체결 0이어도 위 수치가 있으면 차단은 이후 단계일 수 있음.")


if __name__ == "__main__":
    main()
