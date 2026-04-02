# -*- coding: utf-8 -*-
"""
S1 공통 15분봉(및 경량) 지표 중 설정 UI에서 제외·고정된 파라미터.
값 변경 시 indicators / stream_processor / backtest_engine 경량 MACD 경로를 함께 수정할 것.
"""

MACD_ENTRY_FAST: int = 12
MACD_ENTRY_SLOW: int = 26
MACD_ENTRY_SIGNAL: int = 9

# Wilder RSI 본선 주기 / RSI_Sig = RSI의 단순이동평균(봉 수)
RSI_MAIN_PERIOD: int = 14
RSI_SIG_LOOKBACK: int = 9
