import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import os
import time
from typing import List

import pandas as pd
import strategy_selector

# 텔레그램 단일 메시지 최대 길이(HTML·여유)
_TELEGRAM_MAX_CHARS = 4000


def _selection_telegram_chunks(tag_stripped: str, total: int, lines: List[str], extra_note: str = "") -> List[str]:
    """종목 선정 알림 본문을 텔레그램 길이 제한에 맞게 분할(일반 텍스트, HTML 미사용)."""
    header = (
        f"🎯 종목 선정 완료 ({tag_stripped})\n"
        f"총 {total}개 감시 등록\n"
    )
    if extra_note.strip():
        header += extra_note.strip() + "\n"
    header += "\n"
    chunks: List[str] = []
    buf = header
    for line in lines:
        add = line + "\n"
        if len(buf) + len(add) > _TELEGRAM_MAX_CHARS:
            chunks.append(buf)
            buf = f"<b>(목록 계속)</b>\n{add}"
        else:
            buf += add
    if buf.strip():
        chunks.append(buf)
    return chunks


class DashboardTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self._setup_ui()

    def _setup_ui(self):
        """대시보드 탭 레이아웃 고도화 (Midnight Control Room 스타일)"""
        db_main_frame = ttk.Frame(self.parent, padding="25")
        db_main_frame.pack(fill=tk.BOTH, expand=True)

        # 중앙 핵심 컨트롤 센터 (Grid: 두 타일 동일 너비·동일 높이 셀)
        tiles_frame = ttk.Frame(db_main_frame)
        tiles_frame.pack(fill=tk.X, pady=(0, 25))
        tiles_frame.columnconfigure(0, weight=1, uniform="dashboard_tile")
        tiles_frame.columnconfigure(1, weight=1, uniform="dashboard_tile")
        tiles_frame.rowconfigure(0, minsize=140, weight=0)

        # [타일 1] 전략 종목 스캔 (Scanner)
        scan_tile = ttk.LabelFrame(tiles_frame, text=" 🎯 종목 스캐너 ", padding="15")
        scan_tile.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(
            scan_tile,
            text="KODEX200 편입종목 중 Type A·B 유니버스를 각각 선정합니다.\n"
            "일봉은 API(FDR) 기준이며 trading_data.db(백테스트용)는 사용하지 않습니다.\n"
            "결과는 로그창에 표시되며 감시 대상으로 바로 등록됩니다.",
            foreground="#EBCB8B",
        ).pack(expand=True)
        self.btn_select = ttk.Button(scan_tile, text="종목 선정 실행", style="Action.TButton", 
                                        command=self.start_selection_thread, state=tk.DISABLED)
        self.btn_select.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

        # [타일 2] 데이터 수집 (Harvest)
        harvest_tile = ttk.LabelFrame(tiles_frame, text=" 🚜 데이터 수집 ", padding="15")
        harvest_tile.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(
            harvest_tile,
            text="최신 시장 데이터를\n로컬 DB로 저장합니다.",
            foreground="#EBCB8B",
        ).pack(expand=True)
        self.btn_harvest = ttk.Button(harvest_tile, text="수집 시작", style="Action.TButton", command=self._on_btn_harvest)
        self.btn_harvest.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

        # 상태 요약 라인 (아이콘 추가)
        status_info_frame = ttk.Frame(db_main_frame)
        status_info_frame.pack(fill=tk.X, pady=(0, 12))
        self.app.status_label = ttk.Label(status_info_frame, text="⚡ 시스템 엔진 가동 중...", 
                                        font=("Malgun Gothic", 10), foreground=self.app.colors['primary'])
        self.app.status_label.pack(side=tk.LEFT)

        # [v5.0] 노르딕 스타일 로그 영역 (Soft Slate-Blue)
        self.app.log_area = scrolledtext.ScrolledText(db_main_frame, height=12, 
                                                     font=("Consolas", 10), 
                                                     bg="#242933", fg="#D8DEE9",
                                                     insertbackground="#88C0D0",
                                                     borderwidth=0, padx=10, pady=10)
        self.app.log_area.pack(fill=tk.BOTH, expand=True)
        
        # [v4.5.4] GuiUtils에 로그 위젯 연결
        self.app.gui_utils.log_area = self.app.log_area
        
        # 로그 태그 설정 (노르딕 팔레트 기반)
        self.app.log_area.tag_configure("info", foreground="#88C0D0") # Frost Blue
        self.app.log_area.tag_configure("success", foreground="#A3BE8C") # Moss Green
        self.app.log_area.tag_configure("error", foreground="#BF616A") # Aurora Red
        self.app.log_area.tag_configure("warning", foreground="#D08770") # Aurora Orange
        self.app.log_area.tag_configure("header", foreground="#B48EAD", font=("Consolas", 10, "bold")) # Purple

    def _on_btn_harvest(self):
        """데이터 하베스팅 서비스 실행 (독립 프로세스)"""
        try:
            import subprocess
            # 별도 콘솔 창에서 실행하여 GUI 종료와 무관하게 동작하도록 함
            subprocess.Popen(["python", "harvest_service.py"], creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.app.log("🚀 [하베스팅] 전 종목 수집 프로세스가 별도 창에서 시작되었습니다.")
            messagebox.showinfo("알림", "데이터 하베스팅이 별도 창에서 시작되었습니다.\n이 프로그램(GUI)을 종료해도 수집은 계속됩니다.")
        except Exception as e:
            self.app.log(f"❌ [하베스팅] 실행 실패: {e}")
            messagebox.showerror("오류", f"수집 프로세스 실행 중 오류 발생: {e}")

    def start_selection_thread(self, auto_confirm=False):
        if self.btn_select:
            self.btn_select.config(state=tk.DISABLED)
        threading.Thread(
            target=self.select_monitoring_targets, args=(auto_confirm,), daemon=True
        ).start()

    @staticmethod
    def _log_selection_results(app, df: pd.DataFrame) -> None:
        """선정 결과를 로그창에 표 형태로 출력 (팝업 대체)."""
        try:
            d = df.copy()
            d.columns = [str(c).lower() for c in d.columns]
            app.log("\n" + "=" * 102)
            app.log(f"🎯 [종목 선정 결과] {len(d)}개 → 실시간 감시 대상으로 바로 등록합니다.")
            app.log("-" * 102)
            app.log(
                f"{'#':>3} | {'코드':8} | {'종목명':18} | {'가점':>6} | {'트랙':^5} | "
                f"{'현재가':>10} | {'등락%':>7} | {'RSI':>5} | {'5일대금(억)':>10}"
            )
            app.log("-" * 102)
            for i, (_, row) in enumerate(d.iterrows(), 1):
                code = str(row.get("code", "")).zfill(6)
                name = str(row.get("name", "Unknown"))[:18]
                try:
                    sc = float(row.get("score", 0))
                except (TypeError, ValueError):
                    sc = 0.0
                st = str(row.get("seltrack", "")).strip().upper() or "-"
                try:
                    price = int(float(row.get("price", 0)))
                except (TypeError, ValueError):
                    price = 0
                try:
                    ratio = float(row.get("ratio", 0))
                except (TypeError, ValueError):
                    ratio = 0.0
                try:
                    rsi = float(row.get("rsi", 0))
                except (TypeError, ValueError):
                    rsi = 0.0
                try:
                    tv = int(float(row.get("avgtrdeval", 0)))
                except (TypeError, ValueError):
                    tv = 0
                app.log(
                    f"{i:3d} | {code} | {name:18} | {sc:6.1f} | {st:^5} | "
                    f"{price:10,d} | {ratio:7.2f} | {rsi:5.1f} | {tv:10d}"
                )
            app.log("=" * 102 + "\n")
        except Exception as e:
            app.log(f"⚠️ [선정] 결과 로그 출력 실패: {e}")

    def select_monitoring_targets(self, auto_confirm=False):
        """선정 스캔 후 로그 출력 + 감시 대상 즉시 등록 (확인 팝업 없음)."""
        df_snap = None
        aborted_stale = False
        try:
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.app.settings,
                logger_callback=self.app.log,
                data_manager=self.app.data_manager,
                selection_ohlcv_source="api_only",
            )
            if df_result is not None and not df_result.empty:
                df_snap = df_result.copy()
        except strategy_selector.SelectionDbStaleError as stale:
            msg = str(stale.args[0]) if stale.args else "일봉 DB가 최신이 아닙니다."

            def _warn_stale() -> None:
                try:
                    messagebox.showwarning(
                        "일봉 DB 미갱신",
                        "DB가 최신 장마감 일봉까지 반영되지 않았습니다.\n\n"
                        f"{msg}\n\n"
                        "데이터 수집(하베스팅)을 먼저 완료한 뒤 종목 선정을 실행하세요.",
                    )
                finally:
                    if self.btn_select:
                        self.btn_select.config(state=tk.NORMAL)

            try:
                self.app.root.after(0, _warn_stale)
            except Exception:
                _warn_stale()
            aborted_stale = True
        except Exception as e:
            import traceback

            self.app.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.app.log(f"DEBUG Traceback:\n{traceback.format_exc()}")

        if aborted_stale:
            return

        def finish_on_main() -> None:
            try:
                if df_snap is None or df_snap.empty:
                    self.app.log("⚠️ [선정] 조건에 부합하는 종목이 없습니다.")
                    return
                self._log_selection_results(self.app, df_snap)
                self.app.trading_controller.set_monitor_targets(df_snap)
                self.app.monitor_codes = self.app.trading_controller.monitor_codes
                self.app.monitor_info = self.app.trading_controller.monitor_info
                if hasattr(self.app, "monitor_tab"):
                    self.app.monitor_tab.update_watchlist(self.app.monitor_info)
                tag = "[원격] " if auto_confirm else "[GUI] "
                self.app.log(
                    f"✅ {tag}{len(self.app.monitor_codes)}개 종목을 실시간 감시 대상으로 등록했습니다."
                )
                # 텔레그램: 실제 감시 등록된 monitor_codes 기준(선정 분석 행수와 다를 수 있음). HTML 미사용으로 특수문자 종목명도 안전.
                n_reg = len(self.app.monitor_codes or [])
                n_analyzed = len(df_snap)
                extra_note = ""
                if n_analyzed != n_reg:
                    extra_note = f"※ 선정 분석 {n_analyzed}개 → 감시 상한 적용 후 {n_reg}개 등록"
                lines: List[str] = []
                for code in self.app.monitor_codes or []:
                    info = (self.app.monitor_info or {}).get(code, {})
                    nm = str(info.get("name", code)).strip()
                    tr = str(info.get("sel_track", "")).strip().upper() or "-"
                    lines.append(f"• {nm} ({code})  [{tr}]")

                parts = _selection_telegram_chunks(
                    tag.strip(), n_reg, lines, extra_note=extra_note
                )

                def _send_telegram_parts() -> None:
                    try:
                        for i, part in enumerate(parts):
                            self.app.send_telegram_message(part, parse_mode=None)
                            if i + 1 < len(parts):
                                time.sleep(0.4)
                    except Exception as te:
                        try:
                            self.app.log(f"⚠️ [Telegram] 분할 전송 중 오류: {te}")
                        except Exception:
                            pass

                threading.Thread(target=_send_telegram_parts, daemon=True).start()
            except Exception as ex:
                self.app.log(f"❌ [선정] 등록·로그 후처리 실패: {ex}")

        try:
            self.app.root.after(0, finish_on_main)
        except Exception:
            finish_on_main()

        try:
            self.app.root.after(
                0,
                lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None,
            )
        except Exception:
            if self.btn_select:
                self.btn_select.config(state=tk.NORMAL)
