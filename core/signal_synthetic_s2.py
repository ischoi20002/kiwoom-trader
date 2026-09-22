# -*- coding: utf-8 -*-
"""워치리스트 합성 S2 청산·실시간 RSI 믹스인."""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd


class SignalSyntheticS2Mixin:
    def _tick_watchlist_synthetic_s2_exit(
        self,
        code: str,
        now_price: float,
        last_row: dict,
        info: dict,
        *,
        bid_ask_ratio: float,
        ask_bid_ratio: float,
        vol_power: float,
        is_power_exhausted: bool,
        ref_avg: Optional[float] = None,
    ) -> None:
        """
        보유 여부와 무관하게 Type B · Stage2 청산 규칙만 적용한 합성 포지션 평가.
        ref_avg 가 있으면 사용자 등록 평단을 기준으로, 없으면 당일 시가(info['open'])를 평단으로 둠.
        EXIT 시 텔레그램만 발송.
        """
        try:
            st_root = getattr(self.app, "watchlist_synth_state", None)
            if not isinstance(st_root, dict):
                return
            use_ref = ref_avg is not None and float(ref_avg) > 0
            day = datetime.now().date().isoformat()
            s = st_root.get(code)
            if not isinstance(s, dict):
                s = {}
                st_root[code] = s
            if use_ref:
                base_avg = float(ref_avg)
            else:
                open_p = float(self._to_scalar(info.get("open", 0), 0.0))
                if open_p <= 0:
                    open_p = float(now_price)
                base_avg = open_p
            if s.get("session") != day:
                s["session"] = day
                s["avg_p"] = base_avg
                s["max_p"] = max(base_avg, float(now_price))
            else:
                if use_ref:
                    s["avg_p"] = base_avg
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            avg_p = float(s.get("avg_p", base_avg))
            pos = {
                "stage": 2,
                "entry_n": 2,
                "avg_p": avg_p,
                "avg_price": avg_p,
                "max_p": float(s["max_p"]),
                "max_price": float(s["max_p"]),
                "sig_type": "TYPE_B",
                "reason": "WATCHLIST_SYNTH_S2B",
            }
            res = TradingStrategy.evaluate_position(
                code,
                float(now_price),
                pos,
                self.app.settings,
                indicator_row=last_row,
                current_time=datetime.now(),
                is_realtime=True,
                kwargs={
                    "is_power_exhausted": is_power_exhausted,
                    "bid_ask_ratio": bid_ask_ratio,
                    "ask_bid_ratio": ask_bid_ratio,
                    "vol_power": vol_power,
                },
            )
            try:
                s["max_p"] = float(pos.get("max_p", s["max_p"]))
            except Exception:
                pass
            if res.get("action") != "EXIT":
                return
            t0 = self._wl_sell_last_tm.get(code)
            if t0 is not None and (datetime.now() - t0).total_seconds() < 180:
                return
            self._wl_sell_last_tm[code] = datetime.now()
            try:
                nm = self.app.watchlist_store.get_display_name(code)
            except Exception:
                nm = str(info.get("name", code))
            pr = float(res.get("profit_ratio", 0) or 0)
            er = str(res.get("reason", ""))
            anchor = (
                f"등록 평단 {avg_p:,.0f}원 기준"
                if use_ref
                else "당일 시가 앵커"
            )
            msg = (
                "<b>⭐ [텔레그램·관심목록 전용]</b>\n"
                "<i>감시 자동매매/주문과 별도 · 합성 S2-B 평가(보유 무관)</i>\n\n"
                f"<b>TYPE B S2 매도 신호 — {er}</b>\n"
                f"종목: {nm} ({code})\n"
                f"합성 수익률: {pr:+.1f}% ({anchor})\n"
                f"시간: {datetime.now().strftime('%H:%M:%S')}"
            )
            threading.Thread(
                target=self._send_watchlist_telegram,
                args=(msg,),
                daemon=True,
            ).start()
            if use_ref:
                s["avg_p"] = float(ref_avg)
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            else:
                s["avg_p"] = float(now_price)
                s["max_p"] = float(now_price)
        except Exception:
            pass


    def _calculate_realtime_rsi(self, code: str) -> float:
        """
        [v15.0] 실시간 봉 데이터를 기반으로 즉시 RSI 산출.

        틱마다 rolling 을 돌리면 메인 스레드가 과점유되므로 최소 재계산 간격을 둔다.
        """
        try:
            now_ts = time.time()
            last_ts = float(self._rsi_calc_last_ts.get(code, 0.0) or 0.0)
            if now_ts - last_ts < self._realtime_rsi_calc_min_interval_sec:
                return float(self._rsi_calc_last_val.get(code, 0.0) or 0.0)
        except Exception:
            pass

        try:
            _, df = self._bars_key_and_frame(code)
            if df is None or getattr(df, "empty", True) or len(df) < 15:
                return 0.0

            delta = df["Close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()

            rs = gain / (loss + 1e-10)
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 0.0
            out = float(val) if val is not None else 0.0
            try:
                self._rsi_calc_last_ts[code] = time.time()
                self._rsi_calc_last_val[code] = out
            except Exception:
                pass
            return out
        except Exception:
            try:
                return float(self._rsi_calc_last_val.get(code, 0.0) or 0.0)
            except Exception:
                return 0.0

