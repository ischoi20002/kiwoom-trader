# -*- coding: utf-8 -*-
"""관심목록 편집 (텔레그램 전용, 감시 화면·일일 로그와 분리)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any


def open_watchlist_dialog(parent: tk.Misc, app: Any) -> None:
    """기존 목록을 보여 주며 추가·삭제 후 저장할 수 있는 모달 창."""
    top = tk.Toplevel(parent)
    top.title("⭐ 관심목록 (텔레그램 알림 전용)")
    top.transient(parent)
    top.grab_set()
    top.minsize(460, 480)
    top.geometry("520x520")
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
                listbox.insert(tk.END, f"{it['code']}  |  {it.get('name', '')}")
        except Exception as e:
            messagebox.showerror("오류", f"목록 로드 실패: {e}", parent=top)

    def _on_add() -> None:
        try:
            raw = (var_code.get() or "").strip()
            if not raw.isdigit() or len(raw) > 6:
                messagebox.showwarning("입력", "6자리 숫자 종목코드를 입력하세요.", parent=top)
                return
            c = raw.zfill(6)
            nm = (var_name.get() or "").strip()
            if not app.watchlist_store.add(c, nm):
                messagebox.showwarning("입력", "코드를 확인하세요.", parent=top)
                return
            var_code.set("")
            var_name.set("")
            _refresh()
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
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=top)

    def _on_save() -> None:
        try:
            if app.watchlist_store.save():
                messagebox.showinfo("저장", "watchlist_favorites.json 에 저장했습니다.", parent=top)
                try:
                    app.log("⭐ [관심목록] 저장 완료 (감시 재시작 시 스트림 반영)")
                except Exception:
                    pass
            else:
                messagebox.showerror("오류", "저장에 실패했습니다.", parent=top)
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=top)

    # 위젯 생성 (pack 순서는 아래에서 처리)
    hint = ttk.Label(
        outer,
        text=(
            "감시목록과 별도 · 화면에는 숨김 · 텔레그램만. "
            "코드 입력 후 [추가], 저장 시 [파일에 저장]. 감시 재시작 시 스트림 반영."
        ),
        wraplength=480,
    )

    lb_frame = ttk.Frame(outer)
    scroll = ttk.Scrollbar(lb_frame)
    listbox = tk.Listbox(
        lb_frame,
        yscrollcommand=scroll.set,
        height=10,
        font=("Malgun Gothic", 10),
        selectmode=tk.BROWSE,
        bg="#3B4252",
        fg=fg,
        selectbackground=primary,
        selectforeground="#2E3440",
    )
    scroll.config(command=listbox.yview)

    add_f = ttk.Frame(outer)
    ttk.Label(add_f, text="코드").pack(side=tk.LEFT, padx=(0, 4))
    var_code = tk.StringVar()
    ent_code = ttk.Entry(add_f, textvariable=var_code, width=12)
    ent_code.pack(side=tk.LEFT, padx=2)
    ttk.Button(add_f, text="추가", width=8, command=_on_add).pack(side=tk.LEFT, padx=(4, 8))
    ttk.Label(add_f, text="종목명(선택)").pack(side=tk.LEFT, padx=(0, 4))
    var_name = tk.StringVar()
    ent_name = ttk.Entry(add_f, textvariable=var_name, width=22)
    ent_name.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)

    btn_row = ttk.Frame(outer)
    ttk.Button(btn_row, text="삭제", width=10, command=_on_delete).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_row, text="파일에 저장", width=12, command=_on_save).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_row, text="닫기", width=8, command=top.destroy).pack(side=tk.RIGHT, padx=2)

    # pack: BOTTOM은 먼저 넣은 쪽이 창 바닥에 붙음 → btn_row → add_f → 위는 리스트가 expand
    btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
    add_f.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 4))
    hint.pack(side=tk.TOP, anchor=tk.W, pady=(0, 6))
    lb_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    try:
        ent_code.bind("<Return>", lambda _e: _on_add())
        ent_name.bind("<Return>", lambda _e: _on_add())
    except Exception:
        pass

    _refresh()
    try:
        ent_code.focus_set()
    except Exception:
        pass
