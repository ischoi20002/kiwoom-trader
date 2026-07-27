# -*- coding: utf-8 -*-
"""B 추세 레짐 — 최근 영업일 타임라인 다이얼로그."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Dict, List, Optional

_REGIME_COLORS: Dict[str, str] = {
    "OFF": "#A3BE8C",
    "TRANSITION": "#EBCB8B",
    "HARD": "#BF616A",
    "UNKNOWN": "#81A1C1",
}

_REGIME_LABELS: Dict[str, str] = {
    "OFF": "유지",
    "TRANSITION": "약화",
    "HARD": "붕괴(chop)",
}


def open_regime_history_dialog(
    parent: tk.Misc,
    app: Any,
    rows: List[Dict[str, Any]],
    *,
    gate_enabled: bool = False,
) -> None:
    """일별 레짐 막대 타임라인 + 표."""
    if not rows:
        try:
            messagebox.showinfo(
                "레짐 이력",
                "표시할 레짐 데이터가 없습니다.\n네트워크 연결 후 FDR 일봉 조회를 다시 시도하세요.",
                parent=parent,
            )
        except Exception:
            pass
        return

    colors = getattr(app, "colors", {}) or {}
    bg = colors.get("bg", "#2E3440")
    fg = colors.get("fg", "#ECEFF4")
    card = colors.get("card", "#3B4252")
    primary = colors.get("primary", "#88C0D0")

    top = tk.Toplevel(parent)
    top.title("B 추세 레짐 — 최근 영업일")
    top.transient(parent)
    top.geometry("860x480")
    top.minsize(720, 400)
    try:
        top.configure(bg=bg)
    except Exception:
        pass

    outer = ttk.Frame(top, padding=12)
    outer.pack(fill=tk.BOTH, expand=True)

    gate_note = (
        "실전 게이트: ON"
        if gate_enabled
        else "실전 게이트: OFF (아래는 현재 파라미터 기준 참고용)"
    )
    ttk.Label(
        outer,
        text=f"최근 {len(rows)}영업일 · {gate_note}",
        font=("Malgun Gothic", 10, "bold"),
        foreground=primary,
    ).pack(anchor="w", pady=(0, 8))

    # 범례
    legend_f = ttk.Frame(outer)
    legend_f.pack(anchor="w", pady=(0, 10))
    for key, label in (
        ("OFF", "유지"),
        ("TRANSITION", "약화"),
        ("HARD", "붕괴(chop)"),
    ):
        sw = tk.Frame(legend_f, bg=_REGIME_COLORS[key], width=14, height=14)
        sw.pack(side=tk.LEFT, padx=(0, 4))
        sw.pack_propagate(False)
        ttk.Label(legend_f, text=label).pack(side=tk.LEFT, padx=(0, 14))

    # 캔버스 타임라인
    canvas_wrap = ttk.Frame(outer)
    canvas_wrap.pack(fill=tk.X, pady=(0, 10))

    cv_w, cv_h = 820, 140
    canvas = tk.Canvas(
        canvas_wrap,
        width=cv_w,
        height=cv_h,
        bg=card,
        highlightthickness=1,
        highlightbackground=colors.get("border", "#4C566A"),
    )
    canvas.pack(fill=tk.X)

    n = len(rows)
    pad_x, pad_top, pad_bot = 36, 18, 28
    bar_area_w = cv_w - pad_x * 2
    bar_w = max(6, min(28, bar_area_w // max(n, 1) - 2))
    gap = max(1, (bar_area_w - bar_w * n) // max(n - 1, 1)) if n > 1 else 0
    x0 = pad_x + max(0, (bar_area_w - (bar_w * n + gap * max(n - 1, 0))) // 2)

    bar_h_max = cv_h - pad_top - pad_bot
    for i, row in enumerate(rows):
        reg = str(row.get("regime", "OFF")).upper()
        col = _REGIME_COLORS.get(reg, _REGIME_COLORS["UNKNOWN"])
        x = x0 + i * (bar_w + gap)
        y1 = pad_top
        y2 = pad_top + bar_h_max
        d = str(row.get("date", ""))
        tag = f"bar_{i}"
        tip = f"{d}  {_REGIME_LABELS.get(reg, reg)}"

        canvas.create_rectangle(x, y1, x + bar_w, y2, fill=col, outline=col, tags=(tag,))
        if d:
            short = d[5:] if len(d) >= 10 else d
            if i == 0 or i == n - 1 or (i % 5 == 0):
                canvas.create_text(
                    x + bar_w / 2,
                    cv_h - 10,
                    text=short,
                    fill=fg,
                    font=("Malgun Gothic", 8),
                    anchor="n",
                )

        def _bind_tip(_e: Any, t: str = tip) -> None:
            try:
                top.title(f"B 추세 레짐 — {t}")
            except Exception:
                pass

        def _reset_title(_e: Any) -> None:
            try:
                top.title("B 추세 레짐 — 최근 영업일")
            except Exception:
                pass

        canvas.tag_bind(tag, "<Enter>", _bind_tip)
        canvas.tag_bind(tag, "<Leave>", _reset_title)

    # 표
    table_f = ttk.Frame(outer)
    table_f.pack(fill=tk.BOTH, expand=True)

    cols = ("date", "regime", "label")
    tree = ttk.Treeview(table_f, columns=cols, show="headings", height=min(12, max(6, n)))
    tree.heading("date", text="일자")
    tree.heading("regime", text="코드")
    tree.heading("label", text="상태")
    tree.column("date", width=110, anchor="center")
    tree.column("regime", width=90, anchor="center")
    tree.column("label", width=120, anchor="center")
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    sb = ttk.Scrollbar(table_f, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    for row in reversed(rows):
        tree.insert(
            "",
            tk.END,
            values=(
                row.get("date", ""),
                row.get("regime", ""),
                row.get("label", ""),
            ),
        )

    btn_f = ttk.Frame(outer)
    btn_f.pack(fill=tk.X, pady=(10, 0))
    ttk.Button(btn_f, text="닫기", command=top.destroy).pack(side=tk.RIGHT)
