import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from typing import Any, Optional
import threading

import config

class DashboardTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.lbl_mode_badge: Optional[tk.Label] = None
        self.btn_sync: Optional[ttk.Button] = None
        self.btn_restart: Optional[ttk.Button] = None
        self.btn_check_update: Optional[ttk.Button] = None
        self.btn_server_switch: Optional[ttk.Button] = None
        self.btn_select: Optional[ttk.Button] = None
        self.btn_monitor_toggle: Optional[ttk.Button] = None
        self.lbl_mobile_api: Optional[ttk.Label] = None
        self.lbl_mobile_warn: Optional[ttk.Label] = None
        self.ent_mobile_public_url: Optional[ttk.Entry] = None
        self._setup_ui()

    def _call_virtual_tab(self, method_name: str) -> None:
        """포지션 관리 탭에 구현된 동작을 그대로 호출합니다."""
        try:
            vt = getattr(self.app, "virtual_tab", None)
            if vt is None:
                messagebox.showerror(
                    "오류",
                    "포지션 관리 탭이 아직 초기화되지 않았습니다.",
                    parent=self.parent,
                )
                return
            fn = getattr(vt, method_name, None)
            if not callable(fn):
                messagebox.showerror("오류", f"기능을 찾을 수 없습니다: {method_name}")
                return
            fn()
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _setup_ui(self):
        """대시보드: 운영 메뉴 + 로그 (시세·가상 리포트는 전략검증 탭)"""
        colors = getattr(self.app, "colors", {}) or {}
        secondary = colors.get("secondary", "#81A1C1")
        primary = colors.get("primary", "#88C0D0")
        btn_gap = 6
        btn_w = 12

        db_main_frame = ttk.Frame(self.parent, padding=(20, 16))
        db_main_frame.pack(fill=tk.BOTH, expand=True)

        # ── 운영 · 계좌 ───────────────────────────────────────────
        ops = ttk.LabelFrame(
            db_main_frame,
            text=" 운영 · 계좌 ",
            padding=(16, 14),
        )
        ops.pack(fill=tk.X, pady=(0, 12))

        # 1행: 모드 배지(왼쪽) + 감시·선정·서버전환(오른쪽)
        hdr = ttk.Frame(ops)
        hdr.pack(fill=tk.X, pady=(0, 8))

        use_mock = bool(getattr(config, "USE_MOCK", False))
        mode_text = " 모의투자 " if use_mock else " 실전 "
        mode_bg = "#5E81AC" if use_mock else "#BF616A"
        self.lbl_mode_badge = tk.Label(
            hdr,
            text=mode_text,
            font=("Malgun Gothic", 9, "bold"),
            foreground="white",
            background=mode_bg,
            padx=10,
            pady=3,
        )
        self.lbl_mode_badge.pack(side=tk.LEFT)
        ttk.Label(
            hdr,
            text="계좌 · 감시 · 시스템",
            font=("Malgun Gothic", 9),
            foreground=secondary,
        ).pack(side=tk.LEFT, padx=(12, 0))

        hdr_right = ttk.Frame(hdr)
        hdr_right.pack(side=tk.RIGHT)
        self.btn_monitor_toggle = ttk.Button(
            hdr_right,
            text="감시 시작",
            width=btn_w,
            command=self.app.toggle_monitoring,
            style="Accent.TButton",
        )
        self.btn_monitor_toggle.pack(side=tk.LEFT, padx=(0, btn_gap))
        self.btn_select = ttk.Button(
            hdr_right,
            text="목록 선정",
            width=btn_w,
            style="Action.TButton",
            command=self._start_daily_selection,
            state=tk.DISABLED,
        )
        self.btn_select.pack(side=tk.LEFT, padx=(0, btn_gap))
        self.btn_server_switch = ttk.Button(
            hdr_right,
            text="서버 전환",
            width=btn_w,
            command=lambda: self._call_virtual_tab("_toggle_environment"),
        )
        self.btn_server_switch.pack(side=tk.LEFT)

        # 2행: 동기화 · 재시작 · 업데이트 확인 (오른쪽 정렬)
        row_sys = ttk.Frame(ops)
        row_sys.pack(fill=tk.X)
        sys_right = ttk.Frame(row_sys)
        sys_right.pack(side=tk.RIGHT)

        self.btn_sync = ttk.Button(
            sys_right,
            text="동기화",
            width=btn_w,
            command=self.app._sync_account_deposit,
        )
        self.btn_sync.pack(side=tk.LEFT, padx=(0, btn_gap))
        self.btn_restart = ttk.Button(
            sys_right,
            text="재시작",
            width=btn_w,
            command=self._on_restart_clicked,
        )
        self.btn_restart.pack(side=tk.LEFT, padx=(0, btn_gap))
        self.btn_check_update = ttk.Button(
            sys_right,
            text="업데이트 확인",
            width=btn_w,
            command=self._on_check_update_clicked,
        )
        self.btn_check_update.pack(side=tk.LEFT)
        self._update_check_busy = False

        # ── 모바일 현황 ───────────────────────────────────────────
        mobile = ttk.LabelFrame(
            db_main_frame,
            text=" 모바일 현황 ",
            padding=(16, 12),
        )
        mobile.pack(fill=tk.X, pady=(0, 12))

        mob_hdr = ttk.Frame(mobile)
        mob_hdr.pack(fill=tk.X, pady=(0, 6))
        self.lbl_mobile_api = ttk.Label(
            mob_hdr,
            text="API: 꺼짐",
            font=("Malgun Gothic", 9),
            foreground=secondary,
        )
        self.lbl_mobile_api.pack(side=tk.LEFT)
        self.lbl_mobile_warn = ttk.Label(
            mob_hdr,
            text="",
            font=("Malgun Gothic", 8),
            foreground="#D08770",
        )
        self.lbl_mobile_warn.pack(side=tk.LEFT, padx=(12, 0))

        mob_row = ttk.Frame(mobile)
        mob_row.pack(fill=tk.X)
        ttk.Label(
            mob_row,
            text="공개 URL",
            font=("Malgun Gothic", 8),
            foreground=secondary,
            width=8,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.ent_mobile_public_url = ttk.Entry(mob_row)
        try:
            self.ent_mobile_public_url.insert(
                0, str((self.app.settings or {}).get("mobile_status_public_url") or "")
            )
        except Exception:
            pass
        self.ent_mobile_public_url.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, btn_gap)
        )
        ttk.Button(
            mob_row,
            text="URL 복사",
            width=10,
            command=self._copy_mobile_public_url,
        ).pack(side=tk.LEFT, padx=(0, btn_gap))
        ttk.Button(
            mob_row,
            text="설정 링크",
            width=10,
            command=self._copy_mobile_setup_link,
        ).pack(side=tk.LEFT, padx=(0, btn_gap))
        ttk.Button(
            mob_row,
            text="상태 갱신",
            width=10,
            command=self.refresh_mobile_api_status,
        ).pack(side=tk.LEFT)
        ttk.Label(
            mobile,
            text="PIN 8자리 · 설정 링크 = URL+#t=PIN (폰에 붙여넣기)",
            font=("Malgun Gothic", 8),
            foreground=secondary,
        ).pack(anchor=tk.W, pady=(8, 0))

        # 상태 요약
        status_info_frame = ttk.Frame(db_main_frame)
        status_info_frame.pack(fill=tk.X, pady=(0, 10))
        self.app.status_label = ttk.Label(
            status_info_frame,
            text="시스템 엔진 가동 중…",
            font=("Malgun Gothic", 10),
            foreground=primary,
        )
        self.app.status_label.pack(side=tk.LEFT)

        # 첫 기동: REST 인증 대기 동안 인디터미네이트 바 (완료 후 자동 숨김)
        self.boot_progress_frame = ttk.Frame(db_main_frame)
        self.boot_progress_label: Optional[ttk.Label] = None
        self.boot_progress: Optional[ttk.Progressbar] = None
        if not getattr(self.app, "_boot_progress_finished", False):
            self.boot_progress_frame.pack(fill=tk.X, pady=(0, 10))
            self.boot_progress_label = ttk.Label(
                self.boot_progress_frame,
                text="기동 준비 중(인증·동기화 대기)…",
                font=("Malgun Gothic", 9),
                foreground=self.app.colors.get("secondary", "#787c99"),
            )
            self.boot_progress_label.pack(anchor=tk.W, pady=(0, 4))
            self.boot_progress = ttk.Progressbar(
                self.boot_progress_frame,
                mode="indeterminate",
                maximum=100,
            )
            self.boot_progress.pack(fill=tk.X)
            try:
                self.boot_progress.start(12)
            except (tk.TclError, Exception):
                pass

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

        try:
            self.refresh_mobile_api_status()
        except Exception:
            pass

    def refresh_mobile_api_status(self) -> None:
        """모바일 API 가동 상태를 대시보드에 반영합니다."""
        try:
            api = getattr(self.app, "status_api_server", None)
            if api is None:
                if self.lbl_mobile_api is not None:
                    self.lbl_mobile_api.config(text="API: 미초기화")
                return
            info = api.status_summary()
            scheme = "https" if info.get("tls") else "http"
            host = info.get("host") or "0.0.0.0"
            port = info.get("port") or 8765
            if info.get("running"):
                text = f"API: 가동 중 · {scheme}://{host}:{port}"
                color = "#A3BE8C"
            elif info.get("enabled"):
                err = str(info.get("last_error") or "").strip()
                text = f"API: 설정 ON · 미가동" + (f" ({err})" if err else "")
                color = "#D08770"
            else:
                text = "API: 꺼짐 (전략 설정에서 활성화)"
                color = self.app.colors.get("secondary", "#787c99")
            if self.lbl_mobile_api is not None:
                self.lbl_mobile_api.config(text=text, foreground=color)
            warn = ""
            if info.get("running") and not info.get("tls"):
                warn = "경고: TLS 없음(HTTP). 포트포워딩 시 토큰이 평문으로 노출될 수 있습니다."
            elif info.get("enabled") and not info.get("token_set"):
                warn = "토큰이 비어 있습니다. 전략 설정에서 토큰을 생성하세요."
            if self.lbl_mobile_warn is not None:
                self.lbl_mobile_warn.config(text=warn)
            try:
                pub = str((self.app.settings or {}).get("mobile_status_public_url") or "")
                cur = self.ent_mobile_public_url.get() if self.ent_mobile_public_url else ""
                if self.ent_mobile_public_url is not None and pub and not str(cur).strip():
                    self.ent_mobile_public_url.delete(0, tk.END)
                    self.ent_mobile_public_url.insert(0, pub)
            except Exception:
                pass
        except Exception:
            pass

    def _start_daily_selection(self) -> None:
        """일일 감시목록 선정 — monitor_tab 선정 로직을 호출합니다."""
        try:
            mt = getattr(self.app, "monitor_tab", None)
            if mt is None or not hasattr(mt, "start_selection_thread"):
                messagebox.showerror(
                    "오류",
                    "실시간 감시 탭이 아직 초기화되지 않았습니다.",
                    parent=self.parent,
                )
                return
            mt.start_selection_thread()
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _on_restart_clicked(self) -> None:
        """대시보드 재시작 버튼 — 감시 상태를 유지한 채 GUI만 재기동."""
        try:
            if hasattr(self.app, "restart_gui"):
                self.app.restart_gui()
                return
        except Exception as e:
            try:
                messagebox.showerror("재시작", str(e), parent=self.parent)
            except Exception:
                pass

    def _on_check_update_clicked(self) -> None:
        """GitHub(origin/main) 대비 업데이트 확인 → 선택 시 운영 배포·재시작."""
        if bool(getattr(self, "_update_check_busy", False)):
            return
        self._update_check_busy = True
        try:
            if self.btn_check_update is not None:
                self.btn_check_update.configure(state=tk.DISABLED, text="확인 중…")
        except Exception:
            pass

        def _work() -> None:
            result = None
            err_msg = ""
            try:
                from core.app_updater import check_for_update, format_check_summary

                log_fn = getattr(self.app, "log", None)
                result = check_for_update(
                    log=log_fn if callable(log_fn) else None,
                )
            except Exception as e:
                err_msg = repr(e)

            def _done() -> None:
                self._update_check_busy = False
                try:
                    if self.btn_check_update is not None:
                        self.btn_check_update.configure(
                            state=tk.NORMAL, text="업데이트 확인"
                        )
                except Exception:
                    pass
                if err_msg:
                    try:
                        messagebox.showerror(
                            "업데이트 확인",
                            f"확인 중 오류:\n{err_msg}",
                            parent=self.parent,
                        )
                    except Exception:
                        pass
                    return
                self._show_update_check_result(result)

            try:
                self.app.root.after(0, _done)
            except Exception:
                _done()

        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception as e:
            self._update_check_busy = False
            try:
                if self.btn_check_update is not None:
                    self.btn_check_update.configure(
                        state=tk.NORMAL, text="업데이트 확인"
                    )
            except Exception:
                pass
            try:
                messagebox.showerror("업데이트 확인", str(e), parent=self.parent)
            except Exception:
                pass

    def _show_update_check_result(self, result: Any) -> None:
        """확인 결과 다이얼로그 — 새 버전이면 적용 여부 확인."""
        try:
            from core.app_updater import format_check_summary, start_update_process
            from utils.gui_restart import is_monitoring_active
        except Exception as e:
            try:
                messagebox.showerror("업데이트 확인", repr(e), parent=self.parent)
            except Exception:
                pass
            return

        try:
            summary = format_check_summary(result)
        except Exception:
            summary = str(result)

        if result is None or not getattr(result, "ok", False):
            try:
                messagebox.showerror(
                    "업데이트 확인",
                    summary or "확인 실패",
                    parent=self.parent,
                )
            except Exception:
                pass
            return

        if not bool(getattr(result, "update_available", False)):
            try:
                messagebox.showinfo("업데이트 확인", summary, parent=self.parent)
            except Exception:
                pass
            return

        monitoring = False
        try:
            monitoring = bool(is_monitoring_active(self.app))
        except Exception:
            monitoring = False

        warn = ""
        if monitoring:
            warn = (
                "\n\n⚠️ 현재 실시간 감시 중입니다.\n"
                "업데이트하면 감시가 중단되고 프로그램이 재시작됩니다."
            )

        apply = False
        try:
            apply = bool(
                messagebox.askyesno(
                    "업데이트 적용",
                    f"{summary}{warn}\n\n지금 적용하고 재시작할까요?",
                    parent=self.parent,
                )
            )
        except Exception:
            apply = False
        if not apply:
            return

        ok, msg = False, ""
        log_fn = getattr(self.app, "log", None)
        try:
            ok, msg = start_update_process(
                log=log_fn if callable(log_fn) else None,
            )
        except Exception as e:
            ok, msg = False, repr(e)

        if not ok:
            try:
                messagebox.showerror(
                    "업데이트 적용",
                    f"시작 실패:\n{msg}",
                    parent=self.parent,
                )
            except Exception:
                pass
            return

        try:
            if callable(log_fn):
                log_fn("📦 [Update] 원격 업데이트 시작 — 잠시 후 종료합니다.")
        except Exception:
            pass

        # 배치가 pull·배포·재기동하는 동안 현재 GUI 종료
        try:
            self.app.root.after(800, self.app.on_close)
        except Exception:
            try:
                self.app.on_close()
            except Exception:
                pass

    def _copy_mobile_public_url(self) -> None:
        """공개 URL을 클립보드에 복사하고 settings에 저장합니다."""
        try:
            url = ""
            if self.ent_mobile_public_url is not None:
                url = str(self.ent_mobile_public_url.get() or "").strip()
            if not url:
                url = str((self.app.settings or {}).get("mobile_status_public_url") or "").strip()
            if not url:
                messagebox.showinfo(
                    "안내",
                    "공개 URL이 비어 있습니다.\n예: http://내DDNS:28765",
                    parent=self.parent,
                )
                return
            try:
                self.app.settings["mobile_status_public_url"] = url
                if hasattr(self.app, "config_handler"):
                    self.app.config_handler.save_settings(self.app.settings)
            except Exception:
                pass
            try:
                self.parent.clipboard_clear()
                self.parent.clipboard_append(url)
            except Exception:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(url)
            messagebox.showinfo("복사됨", f"공개 URL을 복사했습니다.\n{url}", parent=self.parent)
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _copy_mobile_setup_link(self) -> None:
        """공개 URL + #t=PIN 설정 링크를 복사 (폰에서 입력 없이 로그인)."""
        try:
            from core.status_api import normalize_status_api_token

            url = ""
            if self.ent_mobile_public_url is not None:
                url = str(self.ent_mobile_public_url.get() or "").strip()
            if not url:
                url = str(
                    (self.app.settings or {}).get("mobile_status_public_url") or ""
                ).strip()
            pin = normalize_status_api_token(
                (self.app.settings or {}).get("mobile_status_api_token")
            )
            if not url:
                messagebox.showinfo(
                    "안내",
                    "공개 URL을 먼저 입력하세요.",
                    parent=self.parent,
                )
                return
            if not pin:
                messagebox.showinfo(
                    "안내",
                    "접속 PIN이 없습니다.\n전략 설정에서 PIN을 생성·저장하세요.",
                    parent=self.parent,
                )
                return
            base = url.rstrip("/")
            # 기존 해시 제거 후 붙임
            if "#" in base:
                base = base.split("#", 1)[0]
            link = f"{base}/#t={pin}"
            try:
                self.parent.clipboard_clear()
                self.parent.clipboard_append(link)
            except Exception:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(link)
            messagebox.showinfo(
                "복사됨",
                "설정 링크를 복사했습니다.\n메신저로 폰에 보내 열면 PIN 입력 없이 접속됩니다.\n\n"
                f"{link}",
                parent=self.parent,
            )
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass
