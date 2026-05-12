# -*- coding: utf-8 -*-
"""관심목록 편집 (텔레그램 전용, 감시 화면·일일 로그와 분리)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Optional


def open_watchlist_dialog(parent: tk.Misc, app: Any) -> None:
    """기존 목록을 보여 주며 추가·삭제 시 파일에 자동 반영하는 모달 창."""
    top = tk.Toplevel(parent)
    top.title("⭐ 관심목록 (텔레그램 알림 전용)")
    top.transient(parent)
    top.grab_set()
    top.minsize(520, 520)
    top.geometry("580x560")
    try:
        top.configure(bg=getattr(app, "colors", {}).get("bg", "#2E3440"))
    except Exception:
        pass

    fg = getattr(app, "colors", {}).get("fg", "#ECEFF4")
    primary = getattr(app, "colors", {}).get("primary", "#88C0D0")

    outer = ttk.Frame(top, padding=10)
    outer.pack(fill=tk.BOTH, expand=True)

    def _refresh() -> None:
        try:
            listbox.delete(0, tk.END)
            for it in app.watchlist_store.get_items():
                ap = it.get("avg_price")
                if ap is not None and float(ap) > 0:
                    mode = f"매도 감시 (평단 {float(ap):,.0f})"
                else:
                    mode = "매수 신호만"
                listbox.insert(
                    tk.END,
                    f"{it['code']}  |  {it.get('name', '')}  |  {mode}",
                )
        except Exception as e:
            messagebox.showerror("오류", f"목록 로드 실패: {e}", parent=top)

    def _parse_avg(raw: str) -> Optional[float]:
        s = (raw or "").strip().replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
            if v <= 0:
                return None
            return v
        except ValueError:
            raise ValueError("평단은 숫자로 입력하세요.")

    def _on_add() -> None:
        try:
            raw = (var_code.get() or "").strip()
            if not raw.isdigit() or len(raw) > 6:
                messagebox.showwarning("입력", "6자리 숫자 종목코드를 입력하세요.", parent=top)
                return
            c = raw.zfill(6)
            nm = (var_name.get() or "").strip()
            try:
                ap = _parse_avg(var_avg.get() or "")
            except ValueError as ve:
                messagebox.showwarning("입력", str(ve), parent=top)
                return
            if not app.watchlist_store.add(c, nm, ap):
                messagebox.showwarning("입력", "코드를 확인하세요.", parent=top)
                return
            var_code.set("")
            var_name.set("")
            var_avg.set("")
            _refresh()
            _persist_after_change(silent=True)
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=top)

    def _on_delete() -> None:
        try:
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("안내", "삭제할 항목을 선택하세요.", parent=top)
                return
            line = listbox.get(sel[0])
            code = line.split("|", 1)[0].strip()
            if messagebox.askyesno("삭제", f"{code} 를 관심목록에서 제거할까요?", parent=top):
                app.watchlist_store.remove(code)
                _refresh()
                _persist_after_change(silent=True)
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=top)

    def _persist_after_change(*, silent: bool) -> None:
        """
        메모리 변경 직후 디스크에 반영한다.
        silent=True 이면 성공 시 팝업 없음(추가·삭제 흐름). 실패 시에만 알림.
        """
        try:
            if app.watchlist_store.save():
                try:
                    app.log("⭐ [관심목록] watchlist_favorites.json 반영(자동 저장)")
                except Exception:
                    pass
                if not silent:
                    messagebox.showinfo(
                        "저장 완료",
                        "watchlist_favorites.json 에 반영했습니다.\n"
                        "실시간 감시는 다시 시작해야 워밍업·틱 구독 수에 반영됩니다.",
                        parent=top,
                    )
            else:
                messagebox.showerror(
                    "저장 실패",
                    "파일에 쓰지 못했습니다. 권한·경로를 확인한 뒤 "
                    "[지금 목록을 파일에 쓰기]를 다시 눌러 주세요.",
                    parent=top,
                )
        except Exception as e:
            messagebox.showerror("저장 오류", str(e), parent=top)

    def _on_save_manual() -> None:
        """자동 저장이 실패했거나, 디스크 동기화를 다시 확인할 때."""
        _persist_after_change(silent=False)

    def _on_close() -> None:
        """닫기 전에 한 번 더 디스크에 맞춤(이미 자동 저장됐어도 무해)."""
        try:
            if not app.watchlist_store.save():
                if not messagebox.askyesno(
                    "저장 실패",
                    "창을 닫기 전 파일 저장에 실패했습니다. 그래도 닫을까요?",
                    parent=top,
                ):
                    return
        except Exception as ex:
            if not messagebox.askyesno(
                "오류",
                f"저장 중 오류: {ex}\n그래도 닫을까요?",
                parent=top,
            ):
                return
        try:
            top.destroy()
        except Exception:
            pass

    try:
        top.protocol("WM_DELETE_WINDOW", _on_close)
    except Exception:
        pass

    hint = ttk.Label(
        outer,
        text=(
            "감시목록과 별도 · 화면에는 숨김 · 텔레그램만. "
            "평단을 비우면 TYPE B 매수 신호만 알림. 평단을 넣으면 그 가격 기준 S2 매도 신호만 알림. "
            "종목명을 비우면 저장 시 시장명으로 채움. "
            "추가·삭제 시 watchlist_favorites.json 에 자동 저장됩니다. "
            "실시간 감시는 다시 시작해야 워밍업·구독 종목 수에 반영됩니다."
        ),
        wraplength=540,
    )

    lb_frame = ttk.Frame(outer)
    scroll = ttk.Scrollbar(lb_frame)
    listbox = tk.Listbox(
        lb_frame,
        yscrollcommand=scroll.set,
        height=12,
        font=("Malgun Gothic", 10),
        selectmode=tk.BROWSE,
        bg="#3B4252",
        fg=fg,
        selectbackground=primary,
        selectforeground="#2E3440",
    )
    scroll.config(command=listbox.yview)

    add_f = ttk.Frame(outer)
    ttk.Label(add_f, text="코드").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
    var_code = tk.StringVar()
    ent_code = ttk.Entry(add_f, textvariable=var_code, width=10)
    ent_code.grid(row=0, column=1, padx=2, sticky=tk.W)

    ttk.Label(add_f, text="종목명(선택)").grid(row=0, column=2, sticky=tk.W, padx=(8, 4))
    var_name = tk.StringVar()
    ent_name = ttk.Entry(add_f, textvariable=var_name, width=22)
    ent_name.grid(row=0, column=3, padx=2, sticky=tk.EW)

    ttk.Label(add_f, text="평단·기준가(선택)").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
    var_avg = tk.StringVar()
    ent_avg = ttk.Entry(add_f, textvariable=var_avg, width=14)
    ent_avg.grid(row=1, column=1, padx=2, pady=(6, 0), sticky=tk.W)
    ttk.Label(
        add_f,
        text="(원, 비우면 매수만 / 입력 시 매도만)",
        font=("Malgun Gothic", 8),
    ).grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=(4, 0), pady=(6, 0))

    add_f.columnconfigure(3, weight=1)
    ttk.Button(add_f, text="추가", width=8, command=_on_add).grid(
        row=0, column=4, rowspan=2, padx=(12, 0), sticky=tk.N
    )

    btn_row = ttk.Frame(outer)
    ttk.Button(btn_row, text="삭제", width=10, command=_on_delete).pack(side=tk.LEFT, padx=2)
    ttk.Button(
        btn_row,
        text="지금 목록을 파일에 쓰기",
        width=22,
        command=_on_save_manual,
    ).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_row, text="닫기", width=8, command=_on_close).pack(side=tk.RIGHT, padx=2)

    btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
    add_f.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 4))
    hint.pack(side=tk.TOP, anchor=tk.W, pady=(0, 6))
    lb_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    try:
        ent_code.bind("<Return>", lambda _e: _on_add())
        ent_name.bind("<Return>", lambda _e: _on_add())
        ent_avg.bind("<Return>", lambda _e: _on_add())
    except Exception:
        pass

    _refresh()
    try:
        ent_code.focus_set()
    except Exception:
        pass
