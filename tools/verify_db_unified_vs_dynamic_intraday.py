# -*- coding: utf-8 -*-
"""
틱 없이 DB(15m·1m)만 사용해 **하루 동안의 매수·매도 체결**을 두 번 계산하고 나란히 비교한다.

- **표준 백테스트 결과**: ``DynamicBacktester.run(..., high_precision=True)`` 가 적는
  ``trades_history`` 의 BUY1 / BUY2 / SELL (그게 곧 동적 백테스트의 매수·매도 체결).
- **비교용 결과**: 실시간 감시 미러가 쓰는 것과 같은 함수
  ``evaluate_bt_hp_intrabar_unified`` 로만 인트라바 체결을 적용한 뒤의 BUY1/BUY2/SELL.
  (같은 날짜, 같은 DB, 같은 설정·감시 종목 순서.)

두 목록을 줄 단위로 출력하고, 서로 다른 줄만 집어서 보여 준다.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Tuple

import pandas as pd

# 프로젝트 루트를 path 에 추가
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest_engine import settings_for_backtest  # noqa: E402
from core.bt_hp_intrabar_unified import (  # noqa: E402
    evaluate_bt_hp_intrabar_unified,
    m1_buy_price_conservative,
)
from core.bt_hp_signal_bridge import compute_intrabar_indicator_step  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402


def _load_settings(path: Optional[Path]) -> dict[str, Any]:
    p = path or (_ROOT / "settings.json")
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise SystemExit(f"설정 파일을 읽을 수 없습니다: {p} ({e})") from e
    except json.JSONDecodeError as e:
        raise SystemExit(f"settings.json 파싱 실패: {e}") from e
    return settings_for_backtest(raw)


def _indicator_at_m1_idx(
    settings: dict[str, Any],
    full_hist: pd.DataFrame,
    curr_15m: Any,
    m1_chunk: pd.DataFrame,
    m1_idx: int,
) -> Any:
    holder: dict[str, Any] = {
        "initialized": False,
        "last_15m_dict": None,
        "prices_tail": None,
    }
    try:
        return compute_intrabar_indicator_step(
            settings, full_hist, curr_15m, m1_chunk, m1_idx, holder
        )
    except Exception:
        return curr_15m


def _make_unified_intrabar_method() -> Callable[..., None]:
    """``DynamicBacktester._process_intra_bar_dynamic`` 대체용(인스턴스 메서드)."""

    def _process_intra_bar_dynamic_unified(
        self: Any,
        code: str,
        m1_chunk: Any,
        day_df_15m: Any,
        today_sells: Any,
        curr_15m_time: Any,
    ) -> None:
        try:
            if code in today_sells:
                return
            if m1_chunk is None or getattr(m1_chunk, "empty", True):
                return
            if day_df_15m is None or curr_15m_time not in day_df_15m.index:
                return

            curr_15m = day_df_15m.loc[curr_15m_time]
            full = self.cached_15m_data.get(code)
            if full is None or getattr(full, "empty", True):
                return
            try:
                full_slice = full.loc[:curr_15m_time].copy()
            except (TypeError, KeyError):
                return

            h = self.holdings.get(code)
            pos = copy.deepcopy(h) if isinstance(h, dict) else None
            ct = str(getattr(self, "_current_track_map", {}).get(code, "AB"))
            if ct not in ("A", "B", "AB"):
                ct = "AB"

            out = evaluate_bt_hp_intrabar_unified(
                self.settings, str(code).strip(), full_slice, m1_chunk, pos, ct
            )
            if not isinstance(out, dict):
                return

            ex = out.get("exit_first")
            if isinstance(ex, dict) and h is not None:
                try:
                    m1_idx = int(ex.get("m1_idx", 0))
                except (TypeError, ValueError):
                    return
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_r = m1_chunk.iloc[m1_idx]
                m1_time = m1_chunk.index[m1_idx]
                px = float(ex.get("eval_price", 0) or 0)
                if px <= 0:
                    return
                ind = _indicator_at_m1_idx(
                    self.settings, full, curr_15m, m1_chunk, m1_idx
                )
                rs = str(ex.get("reason", "EXIT"))
                reason = f"1M_{rs}+{m1_idx}m"
                self._execute_sell(code, px, m1_time, reason, indicator_row=ind)
                try:
                    today_sells.add(code)
                except Exception:
                    pass
                return

            s2 = out.get("stage2_addon")
            if isinstance(s2, dict) and h is not None:
                try:
                    m1_idx = int(s2.get("m1_idx", 0))
                except (TypeError, ValueError):
                    return
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_r = m1_chunk.iloc[m1_idx]
                m1_time = m1_chunk.index[m1_idx]
                fill = float(s2.get("fill_price", 0) or 0)
                if fill <= 0:
                    return
                ind = _indicator_at_m1_idx(
                    self.settings, full, curr_15m, m1_chunk, m1_idx
                )
                sig_type = str(h.get("sig_type", "TYPE_A"))
                reason = str(s2.get("reason", "1M_ADDON(S2)"))
                self._handle_buy_execution(
                    code,
                    {"reason": reason, "Sig_Type": sig_type},
                    m1_time,
                    m1_r,
                    indicator_row=ind,
                    fill_price=fill,
                )
                return

            if str(out.get("reason") or "") == "s1_executed_intrabar" and h is None:
                se = out.get("s1_eval")
                if not isinstance(se, dict):
                    return
                reason_s = str(se.get("reason", ""))
                m = re.search(r"\+(\d+)m$", reason_s)
                try:
                    m1_idx = int(m.group(1)) if m else max(0, len(m1_chunk) - 1)
                except (TypeError, ValueError, AttributeError):
                    m1_idx = max(0, len(m1_chunk) - 1)
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_r = m1_chunk.iloc[m1_idx]
                m1_time = m1_chunk.index[m1_idx]
                ind = se.get("indicator_to_use")
                if not isinstance(ind, dict):
                    ind = _indicator_at_m1_idx(
                        self.settings, full, curr_15m, m1_chunk, m1_idx
                    )
                sig_type = str(se.get("sig_type", "TYPE_A"))
                fill_px = float(m1_buy_price_conservative(m1_r))
                self._handle_buy_execution(
                    code,
                    {"reason": reason_s or "1M_SIG", "Sig_Type": sig_type},
                    m1_time,
                    m1_r,
                    indicator_row=ind,
                    fill_price=fill_px,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            try:
                print(
                    f"[verify_db_unified] _process_intra_bar_dynamic_unified 실패 "
                    f"({code} @ {curr_15m_time}): {e!r}",
                    flush=True,
                )
            except Exception:
                pass

    return _process_intra_bar_dynamic_unified


_ORIG_PROCESS: Optional[Callable[..., None]] = None


def _patch_dynamic_intrabar_to_unified() -> None:
    global _ORIG_PROCESS
    if _ORIG_PROCESS is None:
        _ORIG_PROCESS = DynamicBacktester._process_intra_bar_dynamic
    DynamicBacktester._process_intra_bar_dynamic = _make_unified_intrabar_method()


def _unpatch_dynamic_intrabar() -> None:
    global _ORIG_PROCESS
    if _ORIG_PROCESS is not None:
        DynamicBacktester._process_intra_bar_dynamic = _ORIG_PROCESS


def _trade_key(row: dict[str, Any]) -> Tuple[str, str, str]:
    """비교용 키: 종목, 분 단위 시각, 액션."""
    try:
        ts = pd.Timestamp(row.get("Date"))
        t_str = ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        t_str = str(row.get("Date", ""))
    code = str(row.get("Code", "")).zfill(6)
    act = str(row.get("Action", ""))
    return (code, t_str, act)


def _filter_exec_rows(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            a = str(r.get("Action", ""))
        except Exception:
            continue
        if a in ("BUY1", "BUY2", "SELL"):
            out.append(r)
    return out


def _fmt_trade_line(r: dict[str, Any]) -> str:
    """한 줄 요약(터미널용)."""
    try:
        ts = pd.Timestamp(r.get("Date")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        ts = str(r.get("Date", ""))
    code = str(r.get("Code", "")).zfill(6)
    act = str(r.get("Action", ""))
    try:
        p = float(r.get("Price", 0) or 0)
        q = int(r.get("Qty", 0) or 0)
    except (TypeError, ValueError):
        p, q = 0.0, 0
    rs = str(r.get("Reason", ""))[:60]
    return f"  {ts}  {code}  {act:5s}  가격={p:,.0f}  수량={q}  | {rs}"


def _print_trade_block(title: str, rows: List[dict[str, Any]]) -> None:
    print(f"\n{title}", flush=True)
    print("-" * 72, flush=True)
    if not rows:
        print("  (체결 없음)", flush=True)
        return
    for r in rows:
        print(_fmt_trade_line(r), flush=True)


def _diff_trades(
    baseline: List[dict[str, Any]], unified_run: List[dict[str, Any]]
) -> Tuple[List[str], List[str], List[str]]:
    kb = {_trade_key(r): r for r in baseline}
    ku = {_trade_key(r): r for r in unified_run}
    only_b = sorted(set(kb) - set(ku))
    only_u = sorted(set(ku) - set(kb))
    common = sorted(set(kb) & set(ku))
    return only_b, only_u, common


def main() -> None:
    ap = argparse.ArgumentParser(
        description="하루치: 백테스트 매수·매도 vs 실시간과 같은 판정식으로 DB만 돌린 매수·매도 비교"
    )
    ap.add_argument(
        "--day",
        type=str,
        default=pd.Timestamp.today().strftime("%Y-%m-%d"),
        help="검증할 거래일 (YYYY-MM-DD)",
    )
    ap.add_argument(
        "--settings",
        type=str,
        default="",
        help="settings.json 경로(미지정 시 프로젝트 루트)",
    )
    args = ap.parse_args()
    day = pd.to_datetime(args.day).strftime("%Y-%m-%d")
    s_path = Path(args.settings) if str(args.settings).strip() else None
    settings = _load_settings(s_path)

    print(f"\n{'=' * 72}", flush=True)
    print(f"  일자: {day}  |  DB만 사용(틱 없음)  |  매수·매도 체결 비교", flush=True)
    print(f"{'=' * 72}", flush=True)
    print(
        "\n[무엇을 비교하나요]\n"
        "  (1) 백테스트 = 동적 백테스트 프로그램이 적어 주는 매수(BUY1·BUY2)·매도(SELL) 기록.\n"
        "  (2) 비교줄   = 같은 날·같은 DB로, 실시간 화면과 같은 판정식으로 다시 적은 매수·매도 기록.\n"
        "  둘이 같으면 ‘DB 기준으로는 백테와 실시간 미러 판정이 같다’고 보면 됩니다.\n",
        flush=True,
    )

    # --- 표준 동적 백테 ---
    try:
        bt_std = DynamicBacktester(settings)
        bt_std.run(day, day, high_precision=True, callback=None)
    except Exception as e:
        raise SystemExit(f"표준 동적 백테 실행 실패: {e!r}") from e
    trades_std = _filter_exec_rows(bt_std.trades_history or [])

    # --- unified 치환 ---
    _patch_dynamic_intrabar_to_unified()
    try:
        bt_uni = DynamicBacktester(settings)
        bt_uni.run(day, day, high_precision=True, callback=None)
    finally:
        _unpatch_dynamic_intrabar()

    trades_uni = _filter_exec_rows(bt_uni.trades_history or [])

    only_b, only_u, common = _diff_trades(trades_std, trades_uni)

    _print_trade_block(
        "[1] 백테스트 매수·매도 결과 (동적 백테스트, 고정밀, 표준 인트라바)",
        trades_std,
    )
    _print_trade_block(
        "[2] 비교줄 - 실시간과 같은 판정식(evaluate_bt_hp_intrabar_unified)으로 "
        "같은 DB를 돌린 매수·매도",
        trades_uni,
    )

    print(f"\n[요약]  백테 체결 {len(trades_std)}건  |  비교줄 체결 {len(trades_uni)}건", flush=True)
    print(
        f"        완전히 같은 줄(종목+분+매수/매도구분): {len(common)}건  |  "
        f"백테에만 있음: {len(only_b)}건  |  비교줄에만 있음: {len(only_u)}건",
        flush=True,
    )

    def _print_sample(label: str, keys: List[Tuple[str, str, str]], kmap: dict) -> None:
        if not keys:
            return
        print(f"\n--- {label} (최대 12건) ---", flush=True)
        for k in keys[:12]:
            r = kmap.get(k, {})
            print(_fmt_trade_line(r), flush=True)

    kb = {_trade_key(r): r for r in trades_std}
    ku = {_trade_key(r): r for r in trades_uni}
    _print_sample("백테스트에만 있는 체결", only_b, kb)
    _print_sample("비교줄에만 있는 체결", only_u, ku)

    if not only_b and not only_u:
        print(
            "\n[결론]  매수·매도 줄이 전부 일치합니다. (종목·분·BUY/SELL 기준)",
            flush=True,
        )
    else:
        print(
            "\n[결론]  위 ‘백테에만’ / ‘비교줄에만’에 적힌 줄이 서로 다른 매수·매도입니다.\n"
            "        대개 몇 분·가격 차이 한두 건 수준이면, 백테 쪽과 실시간 미러 쪽에서\n"
            "        쓰는 세부 플래그(예: is_realtime) 차이 때문인 경우가 많습니다.",
            flush=True,
        )


if __name__ == "__main__":
    main()
