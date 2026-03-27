import threading
import time
from datetime import datetime

class MarketDataService:
    """
    [v3.8] 시장 지수 모니터링 및 트렌드 분석을 전담하는 모듈.
    """
    def __init__(self, data_manager, gui_callback=None, log_func=None):
        self.data_manager = data_manager
        self.gui_callback = gui_callback # UI 갱신 (ticker update) 콜백
        self.log = log_func or print
        
        self.market_indices = {} # {'kospi': {'p':0, 'c':0, 'pct':0}, ...}
        self.market_trend = "NEUTRAL"
        self.is_running = False
        
        self.lock = threading.Lock()

    def start(self):
        """배경 루프 스레드를 시작합니다."""
        if self.is_running: return
        self.is_running = True
        
        # 1. 빠른 지수 가격 업데이트 루프 (10초)
        threading.Thread(target=self._fast_index_price_loop, daemon=True).start()
        # 2. 무거운 트렌드 분석 루프 (60초)
        threading.Thread(target=self._heavy_index_trend_loop, daemon=True).start()
        
        self.log("📡 [Market] 지수 모니터링 및 트렌드 분석 엔진 가동 시작")

    def stop(self):
        self.is_running = False

    def _fast_index_price_loop(self):
        """10초마다 지수 현재가만 빠르게 업데이트"""
        while self.is_running:
            try:
                indices = self.data_manager.get_market_indices()
                if indices:
                    with self.lock:
                        self.market_indices = indices
                    if self.gui_callback: self.gui_callback(indices=indices)
            except Exception as e:
                self.log(f"⚠️ [Market] 지수 업데이트 오류: {e}")
            time.sleep(10)

    def _heavy_index_trend_loop(self):
        """1분마다 무거운 트렌드 정밀 분석 수행"""
        while self.is_running:
            try:
                # DataManagerV2의 정밀 분석 로직 호출 (예: _heavy_trend_analysis)
                # 현재는 단순화하여 data_manager에서 트렌드 판단을 가져온다고 가정
                trend = self.data_manager.get_market_trend_label()
                with self.lock:
                    self.market_trend = trend
                if self.gui_callback: self.gui_callback(trend=trend)
            except Exception as e:
                self.log(f"⚠️ [Market] 트렌드 분석 오류: {e}")
            time.sleep(60)

    def get_status(self):
        """현재 시장 상태를 반환합니다."""
        with self.lock:
            return {
                "indices": self.market_indices,
                "trend": self.market_trend
            }
