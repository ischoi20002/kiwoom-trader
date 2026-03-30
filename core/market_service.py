import threading
import time
from datetime import datetime

class MarketDataService:
    """
    [v4.5.3] 시장 지수 모니터링 및 트렌드 분석 전문 서비스.
    Triple-Lock (MSI, ADX, VIX) 기반 정밀 장세 판정과 실시간 티커 포맷팅을 담당합니다.
    """
    def __init__(self, data_manager, gui_callback=None, log_func=None):
        self.data_manager = data_manager
        self.gui_callback = gui_callback # (indices, trend, display_text) -> void
        self.log = log_func or print
        
        self.market_indices = {} # {'kospi': {...}, 'kosdaq': {...}}
        self.market_status = {}  # k_trader_gui.market_status 호환
        self.market_trend = "NEUTRAL"
        self._last_m_trend = None
        self.is_running = False
        self.lock = threading.Lock()

    def start(self):
        """배경 루프 스레드를 시작합니다."""
        if self.is_running: return
        self.is_running = True
        
        # 1. 빠른 지수 가격 업데이트 루프 (10초)
        threading.Thread(target=self._fast_index_price_loop, daemon=True).start()
        # 2. 무거운 트렌드 정밀 분석 루프 (120초)
        threading.Thread(target=self._heavy_index_trend_loop, daemon=True).start()
        
        self.log("📡 [Market] 실시간 지수 모니터링 및 Triple-Lock 분석 엔진 가동 시작")

    def stop(self):
        self.is_running = False

    def _fast_index_price_loop(self):
        """10초 주기로 초실시간 지수 가격 업데이트 (네이버 폴링)"""
        while self.is_running:
            try:
                res = self.data_manager.get_realtime_indices()
                if res:
                    with self.lock:
                        for name, data in res.items():
                            if name in self.market_status:
                                self.market_status[name].update(data)
                            else:
                                self.market_status[name] = data
                    self._broadcast_update()
            except Exception as e:
                self.log(f"⚠️ [Market] 실시간 가격 업데이트 오류: {e}")
            time.sleep(10)

    def _heavy_index_trend_loop(self):
        """2분 주기로 MSI/ADX/VIX 포함 정밀 추세 분석 수행"""
        while self.is_running:
            try:
                res = self.data_manager.get_market_indices()
                if res:
                    with self.lock:
                        for name, data in res.items():
                            self.market_status[name] = data
                        
                        # Triple-Lock 기반 통합 추세 판단 (KOSPI 중심)
                        kospi_data = self.market_status.get("KOSPI", {})
                        self.market_trend = kospi_data.get("trend", "NEUTRAL")
                        
                        m_display = {"UP": "🟢순풍", "NEUTRAL": "⚪중립", "DOWN": "⚠️역풍"}[self.market_trend]
                        if self._last_m_trend != m_display:
                            self._last_m_trend = m_display
                            self.log(f"📊 [Market] 시장 추세 감지: {m_display}")
                    
                    self._broadcast_update()
            except Exception as e:
                self.log(f"⚠️ [Market] 정밀 트렌드 분석 오류: {e}")
            time.sleep(120)

    def _broadcast_update(self):
        """업데이트된 정보를 GUI로 전파"""
        if not self.gui_callback: return
        
        with self.lock:
            indices = self.market_status.copy()
            trend = self.market_trend
            display_text = self._format_ticker_text(indices, trend)
            
        self.gui_callback(indices=indices, trend=trend, display_text=display_text)

    def _format_ticker_text(self, indices, trend):
        """상단 지수 티커용 포맷팅 문자열 생성"""
        results = []
        vix = 15.0
        for name, data in indices.items():
            price = data.get('val', 0)
            ratio = data.get('ratio', 0)
            msi = data.get('msi', 0)
            adx = data.get('adx', 0)
            vix = data.get('vix', vix)
            
            sign = "▲" if ratio > 0 else "▼" if ratio < 0 else ""
            results.append(f"{name}: {price:,.2f}({sign}{abs(ratio):.2f}%, MSI:{msi:+.2f}, ADX:{adx:.1f})")
        
        m_tag = {"UP": "[순풍]", "NEUTRAL": "[중립]", "DOWN": "[역풍]"}.get(trend, "[확인중]")
        status_text = " | ".join(results)
        return f"시장 지수: {m_tag} {status_text} | VIX: {vix:.1f}"

    def get_status(self):
        with self.lock:
            return {
                "indices": self.market_status,
                "trend": self.market_trend
            }
