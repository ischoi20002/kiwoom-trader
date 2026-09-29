# -*- coding: utf-8 -*-
"""B 추세 레짐 — 적용일(다음 장) 기준 타임라인 다이얼로그."""
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

# G1/R5 발동 마커 색상 (헤더 라벨과 동일)
_G1_COLOR: str = "#D08770"
_R5_COLOR: str = "#EBCB8B"
_MARKER_OFF_COLOR: str = "#434C5E"


def _g1_cell_text(row: Dict[str, Any]) -> str:
    """표 G1 열 문구."""
    return "A신규차단" if bool(row.get("g1", False)) else "—"


def _r5_cell_text(row: Dict[str, Any]) -> str:
    """표 R5 열 문구 (RS 5일 상관값 병기)."""
    rs5 = row.get("rs5")
    rs_txt = ""
    try:
        if rs5 is not None:
            rs_txt = f" ({float(rs5):.2f})"
    except (TypeError, ValueError):
        rs_txt = ""
    if bool(row.get("r5", False)):
        return f"청산타이트{rs_txt}"
    return f"정상{rs_txt}" if rs_txt else "—"


def open_regime_history_dialog(
    parent: tk.Misc,
    app: Any,
    rows: List[Dict[str, Any]],
    *,
    gate_enabled: bool = False,
    g1_enabled: bool = False,
    r5_enabled: bool = False,
) -> None:
    """일별 레짐 막대 타임라인 + 표 (G1/R5 발동 마커 포함)."""
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
    top.title("B 추세 레짐 — 적용일(다음 장) 기준")
    top.transient(parent)
    top.geometry("880x540")
    top.minsize(740, 440)
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
    gate_note += f" · G1 {'ON' if g1_enabled else 'OFF(참고)'}"
    gate_note += f" · R5 {'ON' if r5_enabled else 'OFF(참고)'}"
    ttk.Label(
        outer,
        text=f"최근 {len(rows)}영업일 · 적용일 표시 · {gate_note}",
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
    for col, label in (
        (_G1_COLOR, "G1 A신규차단"),
        (_R5_COLOR, "R5 청산타이트"),
    ):
        sw = tk.Frame(legend_f, bg=col, width=14, height=8)
        sw.pack(side=tk.LEFT, padx=(0, 4))
        sw.pack_propagate(False)
        ttk.Label(legend_f, text=label).pack(side=tk.LEFT, padx=(0, 14))

    # 캔버스 타임라인
    canvas_wrap = ttk.Frame(outer)
    canvas_wrap.pack(fill=tk.X, pady=(0, 10))

    cv_w, cv_h = 840, 170
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
    # 상단 2줄(G1·R5 마커) + 레짐 막대
    pad_x, pad_top, pad_bot = 36, 40, 28
    g1_y1, g1_y2 = 6, 16
    r5_y1, r5_y2 = 21, 31
    bar_area_w = cv_w - pad_x * 2
    bar_w = max(6, min(28, bar_area_w // max(n, 1) - 2))
    gap = max(1, (bar_area_w - bar_w * n) // max(n - 1, 1)) if n > 1 else 0
    x0 = pad_x + max(0, (bar_area_w - (bar_w * n + gap * max(n - 1, 0))) // 2)

    for txt, y_mid in (("G1", (g1_y1 + g1_y2) / 2), ("R5", (r5_y1 + r5_y2) / 2)):
        canvas.create_text(
            x0 - 6, y_mid, text=txt, fill=fg, font=("Malgun Gothic", 8, "bold"), anchor="e"
        )

    bar_h_max = cv_h - pad_top - pad_bot
    for i, row in enumerate(rows):
        reg = str(row.get("regime", "OFF")).upper()
        col = _REGIME_COLORS.get(reg, _REGIME_COLORS["UNKNOWN"])
        x = x0 + i * (bar_w + gap)
        y1 = pad_top
        y2 = pad_top + bar_h_max
        d = str(row.get("date", "") or row.get("apply_on", ""))
        data_asof = str(row.get("data_as_of", "") or "")
        tag = f"bar_{i}"
        tip = f"적용 {d}"
        if data_asof and data_asof != d:
            tip += f" (데이터 {data_asof})"
        tip += f"  {_REGIME_LABELS.get(reg, reg)}"
        g1_hit = bool(row.get("g1", False))
        r5_hit = bool(row.get("r5", False))
        tip += f" · G1 {_g1_cell_text(row)} · R5 {_r5_cell_text(row)}"

        g1_col = _G1_COLOR if g1_hit else _MARKER_OFF_COLOR
        r5_col = _R5_COLOR if r5_hit else _MARKER_OFF_COLOR
        canvas.create_rectangle(
            x, g1_y1, x + bar_w, g1_y2, fill=g1_col, outline=g1_col, tags=(tag,)
        )
        canvas.create_rectangle(
            x, r5_y1, x + bar_w, r5_y2, fill=r5_col, outline=r5_col, tags=(tag,)
        )
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
                top.title("B 추세 레짐 — 적용일(다음 장) 기준")
            except Exception:
                pass

        canvas.tag_bind(tag, "<Enter>", _bind_tip)
        canvas.tag_bind(tag, "<Leave>", _reset_title)

    # 표
    # 전역 Treeview 는 감시 탭 태그 색상 우선을 위해 foreground="" 이라
    # 태그 없는 기본 행은 글자가 안 보임 → 이력 전용 스타일 사용
    table_f = ttk.Frame(outer)
    table_f.pack(fill=tk.BOTH, expand=True)

    try:
        style = ttk.Style()
        style.configure(
            "RegimeHistory.Treeview",
            background="#242933",
            foreground=fg,
            fieldbackground="#242933",
            rowheight=26,
            borderwidth=0,
        )
        style.configure(
            "RegimeHistory.Treeview.Heading",
            background="#434C5E",
            foreground=primary,
            font=("Malgun Gothic", 9, "bold"),
            borderwidth=1,
            relief=tk.FLAT,
        )
        style.map(
            "RegimeHistory.Treeview",
            background=[("selected", primary)],
            foreground=[("selected", bg)],
        )
    except Exception:
        pass

    cols = ("date", "regime", "label", "g1", "r5")
    tree = ttk.Treeview(
        table_f,
        columns=cols,
        show="headings",
        height=min(12, max(6, n)),
        style="RegimeHistory.Treeview",
    )
    tree.heading("date", text="적용일")
    tree.heading("regime", text="코드")
    tree.heading("label", text="상태")
    tree.column("date", width=110, anchor="center")
    tree.column("regime", width=90, anchor="center")
    tree.column("label", width=120, anchor="center")
    tree.heading("g1", text="G1 (A신규)" if g1_enabled else "G1 (참고)")
    tree.heading("r5", text="R5 (청산·RS5)" if r5_enabled else "R5 (참고)")
    tree.column("g1", width=110, anchor="center")
    tree.column("r5", width=140, anchor="center")
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    sb = ttk.Scrollbar(table_f, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    for key, col in _REGIME_COLORS.items():
        try:
            tree.tag_configure(key, foreground=col)
        except Exception:
            pass

    for row in reversed(rows):
        reg = str(row.get("regime", "") or "").strip().upper() or "UNKNOWN"
        try:
            tree.insert(
                "",
                tk.END,
                values=(
                    row.get("date", "") or row.get("apply_on", ""),
                    row.get("regime", ""),
                    row.get("label", ""),
                    _g1_cell_text(row),
                    _r5_cell_text(row),
                ),
                tags=(reg if reg in _REGIME_COLORS else "UNKNOWN",),
            )
        except Exception:
            continue

    btn_f = ttk.Frame(outer)
    btn_f.pack(fill=tk.X, pady=(10, 0))
    ttk.Button(btn_f, text="닫기", command=top.destroy).pack(side=tk.RIGHT)
