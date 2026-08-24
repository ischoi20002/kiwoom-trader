# -*- coding: utf-8 -*-

import threading
from datetime import datetime
from typing import Any

import pandas as pd

import config

class StrategyEngine:
    """
    [v4.5.3] 보유 중인 포지션의 상태를 분석하고 매매 결정을 처리하는 전략 집행 엔진.
    GUI 프레임워크에서 비즈니스 로직(익절, 손절, 추매 판단)을 완전히 분리합니다.
    """
    def __init__(self, app):
        self.app = app # KiwoomApp 인스턴스 (설정 및 데이터 참조용)
        self.log = app.log
        
    def _get_val(self, data, key, default=0):
        """[v4.5.9] 헬퍼: 데이터(dict/Series)에서 안전하게 단일 스칼라 값 추출"""
        if data is None: return default
        if hasattr(data, 'get'): return data.get(key, default)
        if hasattr(data, 'iloc'): 
            try: return data.iloc[0].get(key, default)
            except: return data.get(key, default)
        return default

    def evaluate_position(self, code, now_price, **kwargs):
        """
        보유 종목의 현재 시세/상태 분석 및 액션 결정.
        결과 형태: {'action': 'EXIT'/'STAGE2'/'HOLD', 'reason': '...', 'data': {...}}
        """
        return self.evaluate_position_scoped(code, now_price, scope="full", **kwargs)

    def evaluate_position_scoped(
        self,
        code: str,
        now_price: float,
        *,
        scope: str = "full",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        보유 평가 범위 분리.

        - ``full``: 청산(``TradingStrategy.evaluate_position``) 후 S2 검토(기존과 동일).
        - ``exit_only``: 청산만.
        - ``s2_only``: S2만(청산 생략 — M1 주기 S2용).
        """
        if now_price <= 0:
            return {"action": "HOLD"}
        if code not in self.app.virtual_positions:
            return {"action": "HOLD"}

        pos = self.app.virtual_positions[code]
        if pos.get("exit_pending", False):
            return {"action": "HOLD"}

        sc = str(scope or "full").strip().lower()
        if sc not in ("full", "exit_only", "s2_only"):
            sc = "full"

        if sc in ("full", "exit_only"):
            from backtest_engine import TradingStrategy

            merged_kw = dict(kwargs)
            try:
                _bd = (self.app.bars_data or {}).get(code)
                if _bd is not None and not getattr(_bd, "empty", True):
                    merged_kw["bars_df"] = _bd
            except Exception:
                pass
            res = TradingStrategy.evaluate_position(
                code,
                now_price,
                pos,
                self.app.settings,
                indicator_row=kwargs.get("ind"),
                current_time=datetime.now(),
                is_realtime=True,
                kwargs=merged_kw,
            )
            # Flash/ATR 고점 DB 보존(재시작·다일 보유)
            try:
                pf = getattr(self.app, "portfolio", None)
                if pf is not None and hasattr(pf, "persist_holding_peaks"):
                    pf.persist_holding_peaks(code)
            except Exception:
                pass

            if res["action"] == "EXIT":
                return {
                    "action": "EXIT",
                    "reason": res["reason"],
                    "profit_ratio": res.get("profit_ratio", 0),
                }
            if sc == "exit_only":
                return {"action": "HOLD"}

        # --- S2 (full 또는 s2_only) ---
        from backtest_engine import TradingStrategy

        page_avg_p = pos.get("avg_price", now_price)
        profit_ratio = (now_price / page_avg_p - 1) * 100
        pos["current_price"] = now_price

        s = self.app.settings
        ind = kwargs.get("ind")
        vol_power = kwargs.get("vol_power", 100.0)
        ask_bid_ratio = kwargs.get("ask_bid_ratio", 1.0)

        try:
            stage = int(pos.get("entry_n", pos.get("stage", 1)) or 1)
            pf = getattr(self.app, "portfolio", None)
            if pf is not None and hasattr(pf, "position_effective_stage"):
                stage = int(pf.position_effective_stage(pos))
        except Exception:
            stage = 1
        if stage == 1:
            if bool(s.get("transition_runtime_block_s2", False)):
                return {"action": "HOLD"}
            if pos.get("s2_limit_order_pending", False):
                return {"action": "HOLD"}
            pos_s2 = dict(pos)
            try:
                et = pos.get("entry_time") or pos.get("entry_date")
                pos_s2["entry_date"] = pd.to_datetime(et) if et is not None else datetime.now()
            except Exception:
                pos_s2["entry_date"] = datetime.now()
            pos_s2["avg_p"] = pos.get("avg_price", now_price)
            pos_s2["sig_type"] = pos.get("sig_type", "TYPE_A")

            try:
                if not TradingStrategy._check_s2_approval(
                    pos_s2,
                    None,
                    s,
                    indicator_row=ind,
                    market_trend="UP",
                    curr_dt=datetime.now(),
                ):
                    return {"action": "HOLD"}
            except Exception:
                return {"action": "HOLD"}

            _is_b = TradingStrategy._holding_sig_is_type_b(pos)
            if not _is_b:
                try:
                    _is_b = "TYPE_B" in str(pos.get("reason", "")).upper()
                except Exception:
                    _is_b = False
            use_sb_s2 = (
                s.get("use_smart_buy_s2_b", True) if _is_b else s.get("use_smart_buy_s2", True)
            )
            if use_sb_s2:
                if vol_power < float(s.get("smart_buy_vp", 120.0)) or ask_bid_ratio < float(
                    s.get("smart_buy_ba_ratio", 1.5)
                ):
                    return {"action": "HOLD"}

            return {
                "action": "STAGE2",
                "reason": "PROFIT_TRIGGER",
                "profit_ratio": profit_ratio,
            }

        return {"action": "HOLD"}

    def calculate_confluence_status(self, code, now_price, ind, vol_power, ask_bid_ratio):
        """[v22.0] 실시간 감시창의 '상태' 컬럼에 표시할 종합 지수 산출"""
        if code not in self.app.virtual_positions: return "-", 0.0
        pos = self.app.virtual_positions[code]
        if pos.get('stage', 1) == 2:
            profit = (now_price / pos['avg_price'] - 1) * 100
            return f"보유 ({profit:+.1f}%)", 1.0
            
        s = self.app.settings
        total, met = 0, 0
        _is_b = pos.get("sig_type") == "TYPE_B"

        def _s2x(a_key: str, b_key: str, default):
            """S2 표시용: Type A는 s2_* 만, Type B는 s2_b_* 만."""
            return s.get(b_key, default) if _is_b else s.get(a_key, default)

        # 1. 시간 지연
        if _s2x('use_s2_delay', 'use_s2_b_delay', True):
            total += 1
            min_h = float(_s2x('s2_delay_hours', 's2_b_delay_hours', 24.0))
            if pos.get('entry_time'):
                try:
                    if (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600 >= min_h:
                        met += 1
                except Exception:
                    pass
        
        # 2. 수익권 트리거
        if _s2x('use_s2_profit', 'use_s2_b_profit', True):
            total += 1
            profit = (now_price / pos['avg_price'] - 1) * 100
            if profit >= float(_s2x('s2_profit_limit', 's2_b_profit_limit', 1.0)):
                met += 1
        
        # 3. 기술적 지표(MA20, MA5, RSI, MACD)
        if ind is not None:
            if _s2x('use_s2_ma20', 'use_s2_b_ma20', True):
                total += 1
                ma20 = float(self._get_val(ind, 'MA20', 0))
                if now_price >= ma20 * (1 + float(_s2x('s2_ma20_gap', 's2_b_ma20_gap', 0.0))/100.0):
                    met += 1
            if _s2x('use_s2_ma5', 'use_s2_b_ma5', True):
                total += 1
                ma5 = float(self._get_val(ind, 'MA5', 0))
                if now_price >= ma5 * (1 + float(_s2x('s2_ma5_gap', 's2_b_ma5_gap', -0.5))/100.0):
                    met += 1
            if _s2x('use_s2_rsi_sig', 'use_s2_b_rsi_sig', True):
                total += 1
                rsi = float(self._get_val(ind, 'RSI', 0))
                rsi_sig = float(self._get_val(ind, 'RSI_Sig', 50))
                prev_rsi = float(self._get_val(ind, 'Prev_RSI', 0))
                gap = float(_s2x('s2_rsi_gap', 's2_b_rsi_gap', 0.0))
                if rsi >= rsi_sig * (1 + gap / 100.0) and rsi > prev_rsi:
                    met += 1
            if _s2x('use_s2_macd_gc', 'use_s2_b_macd_gc', True):
                total += 1
                macd = float(self._get_val(ind, 'MACD', 0))
                macd_sig = float(self._get_val(ind, 'MACD_Sig', 0))
                osc = float(self._get_val(ind, 'OSC', 0))
                prev_osc = float(self._get_val(ind, 'Prev_OSC', 0))
                mgap = float(_s2x('s2_macd_gap', 's2_b_macd_gap', 0.0))
                if macd >= macd_sig + mgap and osc > 0 and osc > prev_osc:
                    met += 1

        # 4. 수급 지표 (추매 Smart Buy)
        use_s2_sb = s.get("use_smart_buy_s2_b", True) if _is_b else s.get("use_smart_buy_s2", True)
        if use_s2_sb:
            total += 1
            if vol_power >= float(s.get('smart_buy_vp', 120.0)) and ask_bid_ratio >= float(s.get('smart_buy_ba_ratio', 1.5)):
                met += 1
                
        ratio = met / total if total > 0 else 0
        return f"추매 ({met}/{total})", ratio

    def recalculate_indicators(self, code, bars_data, settings):
        """[v4.5.3] 특정 종목의 모든 기술적 지표를 최신 설정으로 재계산합니다."""
        df = bars_data.get(code)
        if df is None or df.empty: return None
        try:
            from backtest_engine import TradingStrategy
            # [v31.0] 전체 지표 통합 계산 (ATR 포함)
            updated_df = TradingStrategy.calculate_indicators(df, settings)
            bars_data[code] = updated_df
            return updated_df
        except Exception as e:
            print(f"[DEBUG] Indicator Recalc Error ({code}): {e}")
            return None
