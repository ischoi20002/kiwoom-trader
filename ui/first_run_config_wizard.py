# -*- coding: utf-8 -*-
"""
첫 실행·API 계정 설정 마법사 (config.py 수동 편집 대체).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from core.config_bootstrap import (
    ConfigFormValues,
    is_config_ready,
    load_config_form_values,
    project_root,
    validate_form_values,
    write_config_py,
)


_COLORS = {
    "bg": "#2E3440",
    "card": "#3B4252",
    "fg": "#ECEFF4",
    "primary": "#88C0D0",
    "secondary": "#81A1C1",
    "danger": "#BF616A",
    "border": "#4C566A",
}


class FirstRunConfigWizard(tk.Toplevel):
    """API·계정 설정 대화상자."""

    def __init__(
        self,
        master: Optional[tk.Misc] = None,
        *,
        root_dir: Optional[Path] = None,
        force: bool = False,
        title_prefix: str = "K-Trader 초기 설정",
    ) -> None:
        owns_root = master is None
        if owns_root:
            master = tk.Tk()
            master.withdraw()
        super().__init__(master)
        self._owns_root = owns_root
        self._root_dir = Path(root_dir) if root_dir is not None else project_root()
        self._force = force
        self.result_saved = False
        self.title(title_prefix)
        self.configure(bg=_COLORS["bg"])
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        self._vals = load_config_form_values(self._root_dir)
        self._build()
        self._load_into_form()
        self._on_mode_change()
        try:
            self.update_idletasks()
            w, h = 560, 620
            x = max(0, (self.winfo_screenwidth() - w) // 2)
            y = max(0, (self.winfo_screenheight() - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self) -> None:
        pad = {"padx": 16, "pady": 6}
        hdr = tk.Frame(self, bg=_COLORS["bg"])
        hdr.pack(fill=tk.X, **pad)
        tk.Label(
            hdr,
            text="키움 REST API · 계좌 설정",
            font=("Malgun Gothic", 14, "bold"),
            fg=_COLORS["primary"],
            bg=_COLORS["bg"],
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text="config.py 를 직접 편집하지 않아도 됩니다. 저장 후 프로그램이 다시 시작됩니다.",
            font=("Malgun Gothic", 9),
            fg=_COLORS["secondary"],
            bg=_COLORS["bg"],
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self, bg=_COLORS["card"], highlightbackground=_COLORS["border"], highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        # 모드
        mode_fr = tk.Frame(body, bg=_COLORS["card"])
        mode_fr.pack(fill=tk.X, padx=12, pady=(12, 4))
        tk.Label(mode_fr, text="거래 모드", fg=_COLORS["fg"], bg=_COLORS["card"], font=("Malgun Gothic", 9, "bold")).pack(
            side=tk.LEFT
        )
        self._mode_var = tk.StringVar(value="mock" if self._vals.use_mock else "live")
        ttk.Radiobutton(
            mode_fr, text="모의투자", value="mock", variable=self._mode_var, command=self._on_mode_change
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Radiobutton(
            mode_fr, text="실전", value="live", variable=self._mode_var, command=self._on_mode_change
        ).pack(side=tk.LEFT, padx=(8, 0))

        self._entries: dict[str, tk.Entry] = {}

        def row(parent: tk.Misc, key: str, label: str, *, show: str = "") -> None:
            fr = tk.Frame(parent, bg=_COLORS["card"])
            fr.pack(fill=tk.X, padx=12, pady=3)
            tk.Label(
                fr,
                text=label,
                width=18,
                anchor="w",
                fg=_COLORS["fg"],
                bg=_COLORS["card"],
                font=("Malgun Gothic", 9),
            ).pack(side=tk.LEFT)
            ent = tk.Entry(
                fr,
                show=show,
                bg="#242933",
                fg=_COLORS["fg"],
                insertbackground=_COLORS["fg"],
                relief=tk.FLAT,
                font=("Consolas", 10),
            )
            ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            self._entries[key] = ent

        self._mock_box = tk.LabelFrame(
            body,
            text=" 모의투자 키 ",
            fg=_COLORS["primary"],
            bg=_COLORS["card"],
            font=("Malgun Gothic", 9, "bold"),
        )
        self._mock_box.pack(fill=tk.X, padx=8, pady=4)
        row(self._mock_box, "mock_app_key", "MOCK_APP_KEY")
        row(self._mock_box, "mock_app_secret", "MOCK_APP_SECRET", show="*")

        self._live_box = tk.LabelFrame(
            body,
            text=" 실전 키 · 계좌 ",
            fg=_COLORS["primary"],
            bg=_COLORS["card"],
            font=("Malgun Gothic", 9, "bold"),
        )
        self._live_box.pack(fill=tk.X, padx=8, pady=4)
        row(self._live_box, "app_key", "APP_KEY")
        row(self._live_box, "app_secret", "APP_SECRET", show="*")
        row(self._live_box, "live_account_no", "LIVE_ACCOUNT_NO")

        lic_fr = tk.Frame(self._live_box, bg=_COLORS["card"])
        lic_fr.pack(fill=tk.X, padx=12, pady=3)
        tk.Label(
            lic_fr,
            text="license.key",
            width=18,
            anchor="w",
            fg=_COLORS["fg"],
            bg=_COLORS["card"],
            font=("Malgun Gothic", 9),
        ).pack(side=tk.LEFT)
        self._license_var = tk.StringVar(value="")
        tk.Entry(
            lic_fr,
            textvariable=self._license_var,
            bg="#242933",
            fg=_COLORS["fg"],
            insertbackground=_COLORS["fg"],
            relief=tk.FLAT,
            font=("Consolas", 9),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        ttk.Button(lic_fr, text="찾기…", width=8, command=self._browse_license).pack(side=tk.LEFT, padx=(6, 0))

        self._allow_real_var = tk.BooleanVar(value=bool(self._vals.allow_real_order))
        tk.Checkbutton(
            self._live_box,
            text="실전 주문 허용 (ALLOW_REAL_ORDER) — 잘못 켜면 실제 체결됩니다",
            variable=self._allow_real_var,
            fg=_COLORS["danger"],
            bg=_COLORS["card"],
            activebackground=_COLORS["card"],
            activeforeground=_COLORS["danger"],
            selectcolor=_COLORS["bg"],
            font=("Malgun Gothic", 9),
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(2, 8))

        opt = tk.LabelFrame(
            body,
            text=" 선택 항목 ",
            fg=_COLORS["secondary"],
            bg=_COLORS["card"],
            font=("Malgun Gothic", 9, "bold"),
        )
        opt.pack(fill=tk.X, padx=8, pady=4)
        row(opt, "telegram_bot_token", "텔레그램 토큰", show="*")
        row(opt, "telegram_chat_id", "텔레그램 Chat ID")
        row(opt, "krx_id", "KRX ID")
        row(opt, "krx_pw", "KRX PW", show="*")

        btns = tk.Frame(self, bg=_COLORS["bg"])
        btns.pack(fill=tk.X, padx=16, pady=(4, 14))
        ttk.Button(btns, text="취소", command=self._on_cancel).pack(side=tk.RIGHT)
        ttk.Button(btns, text="저장하고 계속", style="Accent.TButton", command=self._on_save).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

    def _load_into_form(self) -> None:
        mapping = {
            "mock_app_key": self._vals.mock_app_key,
            "mock_app_secret": self._vals.mock_app_secret,
            "app_key": self._vals.app_key,
            "app_secret": self._vals.app_secret,
            "live_account_no": self._vals.live_account_no,
            "telegram_bot_token": self._vals.telegram_bot_token,
            "telegram_chat_id": self._vals.telegram_chat_id,
            "krx_id": self._vals.krx_id,
            "krx_pw": self._vals.krx_pw,
        }
        for k, v in mapping.items():
            try:
                self._entries[k].delete(0, tk.END)
                self._entries[k].insert(0, v or "")
            except Exception:
                pass
        lic = self._root_dir / "license.key"
        if lic.is_file():
            self._license_var.set(str(lic))

    def _on_mode_change(self) -> None:
        mock = self._mode_var.get() == "mock"
        try:
            if mock:
                self._mock_box.pack(fill=tk.X, padx=8, pady=4, before=self._live_box)
            # 실전 박스는 항상 보이되, 모의일 때는 접히게 하지 않고 안내만
            state_live = tk.NORMAL
            for child in self._live_box.winfo_children():
                try:
                    if isinstance(child, (tk.Entry, ttk.Button, tk.Checkbutton)):
                        child.configure(state=state_live)
                except Exception:
                    pass
        except Exception:
            pass

    def _browse_license(self) -> None:
        try:
            path = filedialog.askopenfilename(
                parent=self,
                title="license.key 선택",
                filetypes=[("License key", "*.key"), ("All", "*.*")],
            )
            if path:
                self._license_var.set(path)
        except Exception as ex:
            messagebox.showerror("오류", f"파일 선택 실패: {ex}", parent=self)

    def _collect(self) -> ConfigFormValues:
        g = lambda k: (self._entries.get(k).get() if k in self._entries else "") or ""
        return ConfigFormValues(
            use_mock=self._mode_var.get() == "mock",
            app_key=g("app_key").strip(),
            app_secret=g("app_secret").strip(),
            mock_app_key=g("mock_app_key").strip(),
            mock_app_secret=g("mock_app_secret").strip(),
            live_account_no=g("live_account_no").strip(),
            allow_real_order=bool(self._allow_real_var.get()),
            allow_mock_order=True,
            telegram_bot_token=g("telegram_bot_token").strip(),
            telegram_chat_id=g("telegram_chat_id").strip(),
            krx_id=g("krx_id").strip(),
            krx_pw=g("krx_pw").strip(),
            license_src=(self._license_var.get() or "").strip(),
        )

    def _on_save(self) -> None:
        vals = self._collect()
        err = validate_form_values(vals, self._root_dir)
        if err:
            messagebox.showwarning("입력 확인", err, parent=self)
            return
        if not vals.use_mock and vals.allow_real_order:
            if not messagebox.askyesno(
                "실전 주문 경고",
                "ALLOW_REAL_ORDER 가 켜져 있습니다.\n실제 주문이 나갈 수 있습니다. 계속할까요?",
                parent=self,
                icon="warning",
            ):
                return
        try:
            write_config_py(vals, self._root_dir)
        except Exception as ex:
            messagebox.showerror("저장 실패", str(ex), parent=self)
            return
        if not is_config_ready(self._root_dir):
            messagebox.showwarning(
                "저장 확인",
                "파일이 저장되었지만 필수 항목이 아직 부족합니다.",
                parent=self,
            )
            return
        self.result_saved = True
        messagebox.showinfo("완료", "설정을 저장했습니다. 프로그램을 다시 시작합니다.", parent=self)
        self.destroy()
        if self._owns_root and self.master is not None:
            try:
                self.master.destroy()
            except Exception:
                pass

    def _on_cancel(self) -> None:
        self.result_saved = False
        self.destroy()
        if self._owns_root and self.master is not None:
            try:
                self.master.destroy()
            except Exception:
                pass


def run_first_run_wizard(
    *,
    root_dir: Optional[Path] = None,
    force: bool = False,
) -> bool:
    """
    설정 마법사를 모달로 실행한다.

    Returns
    -------
    bool
        저장 성공 여부
    """
    root_p = Path(root_dir) if root_dir is not None else project_root()
    if not force and is_config_ready(root_p):
        return True
    try:
        from ui.styles import NordicStyle
    except Exception:
        NordicStyle = None  # type: ignore

    owner = tk.Tk()
    try:
        owner.withdraw()
        if NordicStyle is not None:
            try:
                NordicStyle(owner)
            except Exception:
                pass
        wiz = FirstRunConfigWizard(owner, root_dir=root_p, force=force)
        owner.wait_window(wiz)
        return bool(getattr(wiz, "result_saved", False))
    except Exception as ex:
        try:
            messagebox.showerror("설정 마법사", f"실행 실패: {ex}")
        except Exception:
            print(f"[설정 마법사] 실패: {ex}", flush=True)
        return False
    finally:
        try:
            owner.destroy()
        except Exception:
            pass


def open_config_wizard_from_app(app: object) -> None:
    """실행 중 GUI에서 설정 마법사를 열고, 저장 시 재시작을 권유한다."""
    root_widget = getattr(app, "root", None)
    root_p = project_root()
    try:
        wiz = FirstRunConfigWizard(
            root_widget,
            root_dir=root_p,
            force=True,
            title_prefix="API·계정 설정",
        )
        if root_widget is not None:
            root_widget.wait_window(wiz)
        else:
            wiz.wait_window()
    except Exception as ex:
        try:
            messagebox.showerror("API·계정 설정", f"실행 실패: {ex}", parent=root_widget)
        except Exception:
            pass
        return
    if not getattr(wiz, "result_saved", False):
        return
    restart = False
    try:
        restart = bool(
            messagebox.askyesno(
                "재시작",
                "변경된 API 설정을 적용하려면 재시작이 필요합니다.\n지금 재시작할까요?",
                parent=root_widget,
            )
        )
    except Exception:
        restart = True
    if not restart:
        return
    try:
        from utils.gui_restart import spawn_gui_restart, is_monitoring_active

        resume = is_monitoring_active(app)
        if spawn_gui_restart(resume_monitoring=resume):
            try:
                if root_widget is not None:
                    root_widget.after(200, root_widget.destroy)
            except Exception:
                pass
            return
    except Exception:
        pass
    # 폴백: 현재 인터프리터로 run_entry 재실행
    try:
        cwd = str(root_p)
        entry = root_p / "run_entry.py"
        target = str(entry) if entry.is_file() else "k_trader_gui.py"
        subprocess.Popen([sys.executable, "-u", target], cwd=cwd)
        if root_widget is not None:
            root_widget.destroy()
    except Exception as ex:
        try:
            messagebox.showerror("재시작 실패", str(ex), parent=root_widget)
        except Exception:
            pass


def maybe_bootstrap_and_restart() -> bool:
    """
    설정이 미완이면 마법사 후 현재 프로세스를 재시작한다.

    Returns
    -------
    bool
        True 이면 호출측이 즉시 종료해야 함 (재시작 예약됨 또는 사용자가 취소).
        False 이면 설정 완료 — 본 GUI 계속.
    """
    root_p = project_root()
    try:
        from core.config_bootstrap import ensure_config_file_exists

        ensure_config_file_exists(root_p)
    except Exception as ex:
        print(f"[부트스트랩] config 준비 실패: {ex}", flush=True)
        return True
    if is_config_ready(root_p):
        return False
    saved = run_first_run_wizard(root_dir=root_p, force=False)
    if not saved:
        print("[부트스트랩] 설정이 완료되지 않아 종료합니다.", flush=True)
        return True
    # 저장됨 → 같은 인자로 재실행
    try:
        cwd = str(root_p)
        entry = root_p / "run_entry.py"
        script = str(entry) if entry.is_file() else os.path.abspath(sys.argv[0])
        args = [sys.executable, "-u", script, *sys.argv[1:]]
        subprocess.Popen(args, cwd=cwd)
    except Exception as ex:
        print(f"[부트스트랩] 재시작 실패: {ex}", flush=True)
    return True
