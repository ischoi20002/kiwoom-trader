# -*- coding: utf-8 -*-

import html
import threading
import time

import pandas as pd
import strategy_selector

class SelectionService:
    """
    [v4.5.3] 전 종목 스캔 및 종목 선정 프로세스 전용 서비스.
    비즈니스 로직과 텔레그램 보고 기능을 GUI에서 완전히 격리합니다.
    """
    def __init__(self, app, logger_callback=None):
        self.app = app
        self.log = logger_callback or print
        self.is_running = False

    def run(self, on_complete_callback=None):
        """종목 선정을 별도 스레드에서 실행합니다."""
        if self.is_running: return False
        
        self.is_running = True
        def worker():
            try:
                self.log("🔍 [Selection] 시장 전 종목 스캔 및 전략 필터링 시작...")
                kw_tok = None
                try:
                    kw_tok = self.app.auth_manager.get_token()
                except Exception:
                    kw_tok = None
                df_result = strategy_selector.select_monitoring_targets(
                    settings=self.app.settings,
                    logger_callback=self.log,
                    data_manager=getattr(self.app, "data_manager", None),
                    selection_ohlcv_source="api_only",
                    kiwoom_access_token=kw_tok,
                )
                
                if df_result is not None and not df_result.empty:
                    self._brief_results(df_result)
                    if on_complete_callback:
                        on_complete_callback(df_result)
                else:
                    self.log("⚠️ [Selection] 조건에 부합하는 종목이 발견되지 않았습니다.")
                    if on_complete_callback:
                        on_complete_callback(None)
                        
            except strategy_selector.SelectionDbStaleError:
                if on_complete_callback:
                    on_complete_callback(None)
            except Exception as e:
                self.log(f"❌ [Error] 종목 선정 시스템 오류: {e}")
                if on_complete_callback:
                    on_complete_callback(None)
            finally:
                self.is_running = False
                
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _brief_results(self, df):
        """[Restore] 선정 결과를 로그 테이블로 출력하고 텔레그램 브리핑 발송"""
        if df.empty: return
        
        # 1. 로컬 랭킹 로그 출력 (가독성 강화)
        self.log("\n" + "="*120)
        self.log(f"{'No':<3} | {'가점':>5} | {'코드':<6} | {'종목명':<16} | {'현재가':>8} | {'등락률':>7} | {'대금':>4}억 | {'RSI':>5}")
        self.log("-" * 120)
        
        criteria_text = self._get_criteria_summary(format_type="telegram")
        msg = f"🚀 <b>오늘의 스윙 매수 랭킹 (v4.5.3)</b>\n\n<b>[선정 기준]</b>\n{criteria_text}\n\n"
        
        for i, (_, row) in enumerate(df.iterrows(), 1):
            c = str(row.get('Code', '')).zfill(6)
            name = row.get('Name', 'Unknown')
            price = int(row.get('Price', 0))
            ratio = row.get('Ratio', 0)
            trde_val = row.get('AvgTrdeVal', 0)
            rsi = row.get('RSI', 0)
            score = row.get('Score', 0.0)
            
            # 로그 출력
            self.log(f"{i:3d} | {score:5.1f} | {c:6s} | {name:<16s} | {price:8,d} | {ratio:>6.1f}% | {trde_val:>4d}억 | {rsi:>5.1f}")
            
            # 텔레그램: 전체 행 요약(이전에는 상위 10개만 포함)
            name_esc = html.escape(str(name))
            msg += (
                f"<b>{i}. {name_esc}</b> ({c}) - <b>{score}점</b>\n"
                f"📊 현재가: {price:,}원 ({ratio:+.2f}%)\n"
                f"📈 RSI: {rsi:.1f} | 💰 대금: {trde_val}억\n\n"
            )

        self.log("=" * 120 + "\n")

        threading.Thread(target=self._send_telegram_chunks_selection, args=(msg,), daemon=True).start()

    def _send_telegram_chunks_selection(self, msg: str) -> None:
        """HTML 본문을 4000자 단위로 나눠 순차 전송."""
        try:
            parts: list = []
            buf = ""
            for line in msg.split("\n"):
                add = line + "\n"
                if len(buf) + len(add) > 4000:
                    parts.append(buf)
                    buf = add
                else:
                    buf += add
            if buf.strip():
                parts.append(buf)
            for i, p in enumerate(parts):
                self.app.data_manager.send_telegram_message(p, parse_mode="HTML")
                if i + 1 < len(parts):
                    time.sleep(0.4)
        except Exception as e:
            self.log(f"[Selection] 텔레그램 분할 전송 실패: {e}")

    def _get_criteria_summary(self, format_type="ui"):
        """현재 적용 중인 주요 필터 구성을 문자열로 요약"""
        s = self.app.settings
        summary = []
        if s.get('use_daily_macd', True): summary.append("D-MACD GC")
        if s.get('use_daily_rsi', True): summary.append(f"D-RSI > {s.get('daily_rsi_limit', 50)}")
        if s.get('use_ma_alignment', True): summary.append("이평 정배열")
        if s.get('use_volume_filter', True): summary.append(f"평균대금 > {s.get('min_avg_trde_val', 50)}억")
        
        if format_type == "telegram":
            return " • " + "\n • ".join(summary)
        return ", ".join(summary)
