# -*- coding: utf-8 -*-
from datetime import datetime
from typing import Any, Dict

import pandas as pd
from backtest_engine import TradingStrategy
from strategy.indicator_constants import (
    MACD_ENTRY_FAST,
    MACD_ENTRY_SLOW,
    MACD_ENTRY_SIGNAL,
    RSI_MAIN_PERIOD,
    RSI_SIG_LOOKBACK,
)

class StreamProcessor:
    """실시간 시세 데이터 처리 및 지표 계산 전용 클래스"""
    def __init__(
        self,
        settings,
        logger_callback=None,
        on_fifteen_min_bar_closed=None,
        bars_data_lock=None,
    ):
        self.settings = settings
        self.logger = logger_callback or print
        # (code, bar_index_ts, open, high, low, close, volume) — 슬롯이 바뀔 때 직전 봉 확정 시 1회
        self._on_bar_closed = on_fifteen_min_bar_closed
        self._bars_data_lock = bars_data_lock

    def log(self, message):
        if self.logger:
            self.logger(message)

    @staticmethod
    def _scalar_cell(df, idx, col: str):
        try:
            v = df.at[idx, col]
            if hasattr(v, "iloc"):
                return v.iloc[0]
            return v
        except Exception:
            return None

    def update_realtime_bar(
        self, code, price, volume, bars_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """틱 정보를 DataFrame 기반 봉에 반영"""
        lk = self._bars_data_lock
        if lk is not None:
            with lk:
                return self._update_realtime_bar_impl(code, price, volume, bars_data)
        return self._update_realtime_bar_impl(code, price, volume, bars_data)

    def _update_realtime_bar_impl(
        self, code, price, volume, bars_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        now = datetime.now()
        # [v3.5] 15분 단위 슬롯으로 원복 (지표 안정성 확보)
        target_minute = ((now.minute // 15) + 1) * 15
        target_hour = now.hour
        if target_minute == 60:
            target_minute, target_hour = 0, target_hour + 1
        slot_time = f"{target_hour:02d}{target_minute:02d}"

        # [v35.7] 슬롯 시간을 현재 날짜와 결합하여 전체 타임스탬프 생성
        now_dt = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        dt_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        df = bars_data.get(code)
        if df is None or df.empty:
            # 첫 진입 시 Volume은 0으로 시작 (기준점)
            new_row = {'datetime': dt_str, 'time': slot_time, 'Open': price, 'High': price, 'Low': price, 'Close': price, 'Volume': 0, 'cum_v_start': volume}
            new_df = pd.DataFrame([new_row])
            new_df['datetime'] = pd.to_datetime(new_df['datetime'])
            new_df.set_index('datetime', inplace=True)
            bars_data[code] = new_df
            return bars_data
        
        last_idx = df.index[-1]
        # [v4.5.9] Series Ambiguity 방어: 중복 인덱스 발생 시에도 첫 번째 값만 참조
        last_time_v = df.at[last_idx, 'time']
        last_time = last_time_v.iloc[0] if hasattr(last_time_v, 'iloc') else last_time_v

        if last_time == slot_time:
            # 현재 봉 업데이트
            df.at[last_idx, 'Close'] = price
            if price > float(df.at[last_idx, 'High'].iloc[0] if hasattr(df.at[last_idx, 'High'], 'iloc') else df.at[last_idx, 'High']): 
                df.at[last_idx, 'High'] = price
            if price < float(df.at[last_idx, 'Low'].iloc[0] if hasattr(df.at[last_idx, 'Low'], 'iloc') else df.at[last_idx, 'Low']): 
                df.at[last_idx, 'Low'] = price
            # 봉 내 거래량 = 현재 누계 - 봉 시작 누계
            start_v_v = df.at[last_idx, 'cum_v_start'] if 'cum_v_start' in df.columns else volume
            start_v = start_v_v.iloc[0] if hasattr(start_v_v, 'iloc') else start_v_v
            df.at[last_idx, 'Volume'] = max(0, volume - float(start_v))
        else:
            # 직전 슬롯 15분봉 확정 → 실시간 전용 DB 저장 (부하: 종목당 15분당 1회)
            if self._on_bar_closed is not None:
                try:
                    o = float(self._scalar_cell(df, last_idx, "Open"))
                    h = float(self._scalar_cell(df, last_idx, "High"))
                    l = float(self._scalar_cell(df, last_idx, "Low"))
                    c = float(self._scalar_cell(df, last_idx, "Close"))
                    v = float(self._scalar_cell(df, last_idx, "Volume"))
                    self._on_bar_closed(code, last_idx, o, h, l, c, v)
                except Exception as e:
                    if self.logger:
                        self.logger(f"⚠️ [Stream] 확정 15분봉 저장 콜백 실패: {e}")
            # 새 봉 추가 및 300개 유지 (충분한 지표 계산용)
            new_row_data = {'datetime': dt_str, 'time': slot_time, 'Open': price, 'High': price, 'Low': price, 'Close': price, 'Volume': 0, 'cum_v_start': volume}
            new_row_df = pd.DataFrame([new_row_data])
            new_row_df['datetime'] = pd.to_datetime(new_row_df['datetime'])
            new_row_df.set_index('datetime', inplace=True)
            
            df = pd.concat([df, new_row_df]).tail(300)
            
            # MA 계산 (15분봉 기준)
            df['ma5'] = df['Close'].rolling(window=5).mean()
            df['ma20'] = df['Close'].rolling(window=20).mean()
            df['ma60'] = df['Close'].rolling(window=60).mean()
            
            bars_data[code] = df
        return bars_data

    def calculate_indicators(self, df, period=None):
        """RSI 및 MACD 지표 계산 (RSI·MACD 파라미터는 indicator_constants 고정)"""
        p = int(RSI_MAIN_PERIOD if period is None else period)
        if df is None or len(df) <= p:
            return 0.0, 0.0, 0.0, 0.0  # RSI, RSI_Sig, MACD, OSC

        try:
            # 1. RSI
            delta = df['Close'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            avg_up = up.ewm(com=p - 1, adjust=False).mean()
            avg_down = down.ewm(com=p - 1, adjust=False).mean()
            rs = avg_up / (avg_down + 1e-10)
            rsi = 100 - (100 / (1 + rs))

            rsi_sig_p = max(1, int(RSI_SIG_LOOKBACK))
            rsi_sig = rsi.rolling(rsi_sig_p).mean().iloc[-1]
            rsi_val = float(rsi.iloc[-1])

            # 2. MACD
            mf, ms, msg = int(MACD_ENTRY_FAST), int(MACD_ENTRY_SLOW), int(MACD_ENTRY_SIGNAL)
            ema_f = df['Close'].ewm(span=mf, adjust=False).mean()
            ema_s = df['Close'].ewm(span=ms, adjust=False).mean()
            macd_line = ema_f - ema_s
            macd_signal = macd_line.ewm(span=msg, adjust=False).mean()
            osc = macd_line - macd_signal

            return rsi_val, float(rsi_sig), float(macd_line.iloc[-1]), float(osc.iloc[-1])
        except Exception:
            return 0.0, 0.0, 0.0, 0.0
