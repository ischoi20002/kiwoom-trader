"""전략설정 탭의 Track C 전용 섹션 회귀 테스트."""

from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from typing import Any

from core.defaults import DEFAULT_SETTINGS
from ui.tabs.settings_tab import SettingsTab


class _ConfigStub:
    """설정 저장 호출을 받는 최소 스텁."""

    def save_settings(self, _settings: dict[str, Any]) -> bool:
        return True


class _AppStub:
    """SettingsTab 생성에 필요한 앱 속성만 제공합니다."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = deepcopy(DEFAULT_SETTINGS)
        self.colors = {
            "bg": "#2E3440",
            "fg": "#ECEFF4",
            "primary": "#88C0D0",
            "secondary": "#81A1C1",
        }
        self.opt_params_vars: dict[str, tk.BooleanVar] = {}
        self.opt_param_labels: dict[str, str] = {}
        self.config_handler = _ConfigStub()

    def log(self, _message: str) -> None:
        return


def test_track_c_settings_section_and_integer_collection() -> None:
    """C 설정 45개가 별도 섹션에 노출되고 정수 키가 정수로 저장되는지 확인합니다."""
    root = tk.Tk()
    root.withdraw()
    try:
        parent = ttk.Frame(root)
        parent.pack(fill=tk.BOTH, expand=True)
        app = _AppStub(root)
        tab = SettingsTab(parent, app, DEFAULT_SETTINGS)
        root.update_idletasks()

        assert "track_c" in tab._settings_section_keys
        assert "use_sel_track_c" in tab.conf_vars
        assert "use_type_c" in tab.conf_vars
        assert "use_type_c_prev_high_break" in tab.conf_vars
        assert "use_type_c_gap_down_exit" in tab.conf_vars

        numeric_keys = {
            "max_slots_c",
            "sel_track_min_score_c",
            *(
                key
                for key in DEFAULT_SETTINGS
                if key.startswith("box_sel_") or key.startswith("type_c_")
            ),
        }
        assert numeric_keys.issubset(tab.params_entries)

        tab.params_entries["max_slots_c"].delete(0, tk.END)
        tab.params_entries["max_slots_c"].insert(0, "7")
        tab.params_entries["type_c_touch_lookback_bars"].delete(0, tk.END)
        tab.params_entries["type_c_touch_lookback_bars"].insert(0, "2")
        tab.collect_from_ui()

        assert app.settings["max_slots_c"] == 7
        assert isinstance(app.settings["max_slots_c"], int)
        assert app.settings["type_c_touch_lookback_bars"] == 2
        assert isinstance(app.settings["type_c_touch_lookback_bars"], int)
    finally:
        root.destroy()
