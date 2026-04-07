import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping, Optional

from strategy.market_regime import regime_display_emoji, regime_display_tag


class MarketDataService:
    """
    [v4.5.3] 시장 지수 모니터링 및 트렌드 분석 전문 서비스.
    Triple-Lock (MSI, ADX, VIX) 기반 정밀 장세 판정과 실시간 티커 포맷팅을 담당합니다.
    """
    def __init__(
        self,
        data_manager,
        gui_callback=None,
        log_func=None,
        settings_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
    ):
        self.data_manager = data_manager
        self.gui_callback = gui_callback # (indices, trend, display_text) -> void
        self.log = log_func or print
        self.settings_provider = settings_provider
        
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
                try:
                    _s = self.settings_provider() if callable(self.settings_provider) else None
                except Exception:
                    _s = None
                res = self.data_manager.get_market_indices(settings=_s if isinstance(_s, dict) else None)
                if res:
                    with self.lock:
                        for name, data in res.items():
                            self.market_status[name] = data
                        
                        # 4단계 레짐 (KOSPI 기준 → 앱 market_trend·신호 엔진과 동기)
                        kospi_data = self.market_status.get("KOSPI", {})
                        self.market_trend = kospi_data.get("trend", kospi_data.get("regime", "NEUTRAL"))
                        
                        m_display = regime_display_emoji(self.market_trend)
                        if self._last_m_trend != m_display:
                            self._last_m_trend = m_display
                            self.log(f"📊 [Market] 시장 레짐 감지: {m_display}")
                    
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
            per_reg = str(data.get("regime") or data.get("trend") or "").strip().upper()
            rtag = regime_display_tag(per_reg or trend)
            results.append(
                f"{name}: {price:,.2f}({sign}{abs(ratio):.2f}%, MSI:{msi:+.2f}, ADX:{adx:.1f}) {rtag}"
            )
        
        m_tag = regime_display_tag(trend)
        status_text = " | ".join(results)
        # 선행 태그는 KOSPI 기준 4단계 레짐(백테·다이나믹과 동일). 지수별 꼬리는 각 행의 [UP]/[강세] 등.
        return f"시장 지수 · KOSPI 레짐 {m_tag} {status_text} | VIX: {vix:.1f}"

    def get_status(self):
        with self.lock:
            return {
                "indices": self.market_status,
                "trend": self.market_trend
            }
