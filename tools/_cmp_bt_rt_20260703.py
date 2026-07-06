# -*- coding: utf-8 -*-
"""동적 백테스트 리포트 vs 실시간 세션 로그 매매 대조(임시 진단용).

사용법: python _cmp_bt_rt_20260703.py [YYYY-MM-DD]
날짜 미지정 시 2026-07-03. RT 로그는 해당 날짜 폴더에서 읽고, BT 리포트는
프로젝트 루트 dynamic_trade_report.html(가장 최근 실행분)을 사용한다.
"""
import re
import sys
import json
from pathlib import Path

_DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-07-03"
REPORT = Path(r"c:/vibe/dynamic_trade_report.html")
RT = Path(rf"c:/vibe/realtime_session_logs/{_DAY}/session_events.jsonl")


def parse_backtest() -> dict:
    html = REPORT.read_text(encoding="utf-8")
    groups = re.split(r'<div class="stock-group" id="stock-(\d+)">', html)
    out = {}
    for i in range(1, len(groups), 2):
        code = groups[i]
        chunk = groups[i + 1]
        mname = re.search(r"^([^<]+?)\s*<span", chunk)
        name = mname.group(1).strip() if mname else ""
        rows = re.findall(
            r'<tr><td>([^<]+)</td><td><span class="action-(\w+)">(\w+)</span>', chunk
        )
        reasons = re.findall(r'reason-text">([^<]+)</span>', chunk)
        acts = []
        for idx, (tm, cls, lab) in enumerate(rows):
            rsn = reasons[idx].strip() if idx < len(reasons) else ""
            acts.append((tm.strip(), lab, rsn))
        out[code] = {"name": name, "acts": acts}
    return out


def parse_realtime() -> dict:
    out = {}
    for line in RT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        k = ev.get("kind")
        d = ev.get("data", {})
        code = str(d.get("code", "")).zfill(6) if d.get("code") else ""
        if k not in ("fill", "order_buy", "order_sell", "strategy_action", "signal_buy_accepted"):
            continue
        if not code:
            continue
        out.setdefault(code, {"name": d.get("name", ""), "ev": []})
        out[code]["ev"].append((ev.get("ts", ""), k, d))
    return out


def main() -> None:
    bt = parse_backtest()
    rt = parse_realtime()
    all_codes = sorted(set(bt) | set(rt))
    print("=" * 70)
    print("종목별 백테스트(BT) vs 실시간(RT) 대조 — 2026-07-03")
    print("=" * 70)
    for c in all_codes:
        b = bt.get(c)
        r = rt.get(c)
        name = (b or {}).get("name") or (r or {}).get("name") or ""
        print(f"\n[{c}] {name}")
        print("  --- BT ---")
        if b:
            for tm, lab, rsn in b["acts"]:
                print(f"    {tm} {lab}  {rsn}")
        else:
            print("    (없음)")
        print("  --- RT ---")
        if r:
            for ts, k, d in r["ev"]:
                extra = ""
                if k == "fill":
                    extra = f"{d.get('side')} @{d.get('price')} x{d.get('qty')} ({d.get('reason')})"
                elif k in ("order_buy", "order_sell"):
                    extra = f"@{d.get('price')} x{d.get('qty')} ({d.get('reason')})"
                elif k == "strategy_action":
                    extra = f"{d.get('action')} {d.get('reason')} pr={d.get('profit_ratio'):.2f}"
                elif k == "signal_buy_accepted":
                    extra = f"{d.get('sig_type')} @{d.get('price')}"
                print(f"    {ts} {k}  {extra}")
        else:
            print("    (없음)")


if __name__ == "__main__":
    main()
