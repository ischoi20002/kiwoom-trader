# -*- coding: utf-8 -*-
"""선정 신선도 확인 후 실시간 감시 시작 서비스."""
from __future__ import annotations

from typing import Any, Optional


class SelectionMonitorStartService:
    """ensure_fresh_selection_then_start 로직 (GUI에서 이전)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def ensure_fresh_selection_then_start(
        self,
        force_selection: bool = False,
        on_done: Optional[Any] = None,
    ) -> bool:
        """selected_targets.csv 신선하면 즉시 감시 시작, 아니면 일일 선정 후 시작."""
        app = self.app
        done_cb = on_done if callable(on_done) else None

        def _notify(ok: bool) -> None:
            if done_cb is None:
                return
            try:
                done_cb(bool(ok))
            except Exception:
                pass

        tc = app.trading_controller
        try:
            if bool(getattr(tc, "is_monitoring", False)):
                _notify(True)
                return False
            if bool(getattr(tc, "_run_start_sequence_active", False)):
                try:
                    app.log(
                        "⚠️ [감시] 시작 절차가 이미 진행 중입니다. 잠시 후 다시 시도하세요."
                    )
                except Exception:
                    pass
                _notify(False)
                return False
            if bool(getattr(app, "_ensure_sel_start_active", False)):
                # 선정 대기 중 재클릭: 플래그 해제 후 보유만 즉시(옛 CSV 사용 금지)
                try:
                    app._ensure_sel_start_active = False
                except Exception:
                    pass
                held_codes = []
                try:
                    held_codes = list(tc.held_position_codes())
                except Exception:
                    held_codes = []
                if held_codes:
                    try:
                        app.log(
                            f"ℹ️ [감시] 선정 대기 취소 — "
                            f"보유 {len(held_codes)}종만 즉시 감시 "
                            f"(선정 완료 전 옛 CSV는 사용하지 않음)"
                        )
                    except Exception:
                        pass
                    started = bool(tc._start_held_positions_only(held_codes))
                    _notify(started)
                    return started
                try:
                    app.log(
                        "⚠️ [감시] 일일 선정이 진행 중이었습니다. "
                        "보유가 없으면 선정 완료 후 다시 눌러 주세요."
                    )
                except Exception:
                    pass
                _notify(False)
                return False
        except Exception:
            pass

        need_select = bool(force_selection)
        try:
            from core.selection_freshness import (
                is_selected_targets_fresh,
                selection_freshness_status,
            )

            st = getattr(app, "settings", None) or {}
            path = "selected_targets.csv"
            if not need_select and is_selected_targets_fresh(path, settings=st):
                try:
                    from core.selection_freshness import count_selected_targets

                    n_sel = int(count_selected_targets(path))
                except Exception:
                    n_sel = -1
                if n_sel == 0:
                    held_codes = []
                    try:
                        held_codes = list(tc.held_position_codes())
                    except Exception:
                        held_codes = []
                    if held_codes:
                        try:
                            app.log(
                                f"ℹ️ [감시] 선정 CSV 0건·보유 {len(held_codes)}종 — "
                                f"보유만 감시합니다."
                            )
                        except Exception:
                            pass
                        started = bool(tc._start_held_positions_only(held_codes))
                        _notify(started)
                        return started
                    try:
                        app.log(
                            "⚠️ [감시] 선정 결과가 0건입니다 — 감시 시작을 건너뜁니다."
                        )
                    except Exception:
                        pass
                    try:
                        scb = getattr(tc, "status_cb", None)
                        if callable(scb):
                            scb(
                                "SHOW_WARNING",
                                "감시 선정 결과가 0건이라 감시를 시작하지 않습니다.",
                            )
                    except Exception:
                        pass
                    _notify(False)
                    return False
                started = bool(tc.start(skip_selection_fresh_check=True))
                _notify(started)
                return started
            if need_select:
                try:
                    app.log(
                        "🔄 [감시] 강제 재선정(장마감 실패 복구 또는 요청) — "
                        "선정을 수행한 뒤 감시를 시작합니다."
                    )
                except Exception:
                    pass
            else:
                _ok, detail = selection_freshness_status(path, settings=st)
                try:
                    app.log(
                        f"🔄 [감시] 일일 감시목록 갱신 필요 — {detail}. "
                        "선정을 수행한 뒤 감시를 시작합니다."
                    )
                except Exception:
                    pass
        except Exception as e:
            if force_selection:
                try:
                    app.log(
                        f"⚠️ [감시] 신선도 검사 실패({e}) — 강제 재선정을 계속합니다."
                    )
                except Exception:
                    pass
            else:
                try:
                    app.log(f"⚠️ [감시] 신선도 검사 실패({e}) — 기존 CSV로 시작 시도")
                except Exception:
                    pass
                started = bool(tc.start(skip_selection_fresh_check=True))
                _notify(started)
                return started

        mt = getattr(app, "monitor_tab", None)
        if mt is None or not hasattr(mt, "select_monitoring_targets"):
            held_n = 0
            try:
                held_n = len(tc.held_position_codes())
            except Exception:
                held_n = 0
            if held_n > 0:
                try:
                    app.log(
                        f"ℹ️ [감시] 선정 UI 없음·보유 {held_n}종 — "
                        f"보유 청산 감시로 시작합니다."
                    )
                except Exception:
                    pass
                started = bool(tc.start(skip_selection_fresh_check=True))
                _notify(started)
                return started
            try:
                app.log("⚠️ [감시] monitor_tab 없음 — 선정 불가")
            except Exception:
                pass
            try:
                scb = getattr(tc, "status_cb", None)
                if callable(scb):
                    scb(
                        "SHOW_WARNING",
                        "일일 감시목록 선정이 필요합니다.",
                    )
            except Exception:
                pass
            _notify(False)
            return False

        app._ensure_sel_start_active = True

        def _on_complete(ok: bool) -> None:
            def _job() -> None:
                try:
                    app._ensure_sel_start_active = False
                except Exception:
                    pass
                if not ok:
                    held_codes: list = []
                    try:
                        held_codes = list(tc.held_position_codes())
                    except Exception:
                        held_codes = []
                    if held_codes:
                        try:
                            app.log(
                                f"ℹ️ [감시] 오늘 선정 0건·보유 {len(held_codes)}종 — "
                                f"이전 일자 CSV는 쓰지 않고 보유만 감시합니다."
                            )
                        except Exception:
                            pass
                        started = False
                        try:
                            started = bool(
                                tc._start_held_positions_only(held_codes)
                            )
                        except Exception as ex:
                            try:
                                app.log(f"❌ [감시] 보유-only 시작 실패: {ex}")
                            except Exception:
                                pass
                        _notify(started)
                        return
                    try:
                        app.log(
                            "⚠️ [감시] 오늘 선정 0건·보유 없음 — "
                            "이전 일자 감시목록으로 시작하지 않습니다."
                        )
                    except Exception:
                        pass
                    try:
                        scb = getattr(tc, "status_cb", None)
                        if callable(scb):
                            scb(
                                "SHOW_WARNING",
                                "오늘 선정 종목이 0건입니다. 보유가 있으면 보유만 감시합니다.",
                            )
                    except Exception:
                        pass
                    _notify(False)
                    return
                try:
                    from core.selection_freshness import count_selected_targets

                    n_sel = int(count_selected_targets("selected_targets.csv"))
                except Exception:
                    n_sel = -1
                if n_sel == 0:
                    held_codes = []
                    try:
                        held_codes = list(tc.held_position_codes())
                    except Exception:
                        held_codes = []
                    if held_codes:
                        try:
                            app.log(
                                f"ℹ️ [감시] 선정 CSV 0건·보유 {len(held_codes)}종 — "
                                f"보유만 감시합니다."
                            )
                        except Exception:
                            pass
                        started = False
                        try:
                            started = bool(
                                tc._start_held_positions_only(held_codes)
                            )
                        except Exception as ex:
                            try:
                                app.log(f"❌ [감시] 보유-only 시작 실패: {ex}")
                            except Exception:
                                pass
                        _notify(started)
                        return
                    try:
                        app.log(
                            "⚠️ [감시] 선정 결과가 0건입니다 — 감시 시작을 건너뜁니다."
                        )
                    except Exception:
                        pass
                    try:
                        scb = getattr(tc, "status_cb", None)
                        if callable(scb):
                            scb(
                                "SHOW_WARNING",
                                "감시 선정 결과가 0건이라 감시를 시작하지 않습니다.",
                            )
                    except Exception:
                        pass
                    _notify(False)
                    return
                try:
                    app.log(
                        f"✅ [감시] 일일 선정 완료({n_sel}종목) — 감시를 시작합니다."
                    )
                except Exception:
                    pass
                started = False
                try:
                    started = bool(tc.start(skip_selection_fresh_check=True))
                except Exception as ex:
                    try:
                        app.log(f"❌ [감시] 선정 후 시작 실패: {ex}")
                    except Exception:
                        pass
                    started = False
                _notify(started)

            try:
                app.root.after(0, _job)
            except Exception:
                _job()

        try:
            import threading

            threading.Thread(
                target=lambda: mt.select_monitoring_targets(
                    auto_confirm=True,
                    on_complete=_on_complete,
                ),
                daemon=True,
                name="EnsureSelThenStart",
            ).start()
            # 비동기 진행 중 — 최종 결과는 on_done 으로 통지
            return True
        except Exception as e:
            try:
                app._ensure_sel_start_active = False
            except Exception:
                pass
            try:
                app.log(f"❌ [감시] 선정 스레드 기동 실패: {e}")
            except Exception:
                pass
            _notify(False)
            return False
