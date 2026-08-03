import threading
import copy
import json
import os
import re
import time
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Set, Tuple

import config
from utils.fee_rates import fee_decimal_rates_live_trading
from utils.server_mode_paths import migrate_legacy_virtual_db_files, virtual_db_path


class PortfolioManager:
    """
    [v3.8] 포지션 관리(모의/실전별 저장), 계좌 동기화(Reconciliation), DB 연동을 전담하는 모듈.
    """
    def __init__(self, app, data_manager, order_manager, settings=None, log_func=None, gui_callback=None, telegram_callback=None):
        self.app = app
        self.data_manager = data_manager
        self.order_manager = order_manager
        self.settings = settings or {}
        self.log = log_func or print
        self.gui_callback = gui_callback
        self.telegram_callback = telegram_callback

        # 수수료: 모의/실전은 on_fill 시점 settings + config.USE_MOCK 로 조회 (fee_decimal_rates_live_trading)

        self.virtual_positions = {}
        self.virtual_history = []

        # [v4.5 Asset Harden] 실시간 자산 현황 및 스로틀링(Throttling) 트래킹
        self.total_equity = 0
        self.d1_deposit = 0
        self.d2_deposit = 0
        self.actual_deposit = 0
        # ka10085 보유 평가 합산(잔고 API). 포지션 관리 탭 주식평가금·총자산과 동기화.
        self.holdings_stocks_mv_api: float = 0.0
        # ka10085 tot_evlu_amt 원본 — 총자산 표시 최우선
        self.last_tot_evlu_amt: float = 0.0
        self.last_sync_time = 0.0
        self.min_sync_interval = 30.0 # [v4.5.3] 30초 간격으로 완화
        # 서버 잔고 목록에 종목이 없을 때 연속 몇 번인지 (API 일시 누락·파싱 실패로 인한 UI 깜빡임 방지)
        self._reconcile_server_missing_streak: Dict[str, int] = {}

        # on_fill 등에서 보조 메서드가 같은 스레드로 lock 재진입할 수 있음
        self.lock = threading.RLock()
        # [버그픽스] 계좌 동기화 동시 실행(여러 경로의 직접 호출) 시 reconcile·save_db·
        # streak 카운터가 interleave 되는 것을 막는 비차단 재진입 가드.
        self._sync_in_progress_lock = threading.Lock()
        # record_orphan_sell_close 동일 주문번호 중복 기록 방지
        self._orphan_sell_order_ids: Set[str] = set()
        # 매도 주문 접수 직후 포지션 전체 스냅샷(주문번호 키) — 동기화로 로컬 보유가 먼저 사라져도 체결 시 종료 확정
        self._pending_exit_snapshots: Dict[str, Dict[str, Any]] = {}
        # D+2 축소 안내 로그 스로틀(틱마다 동일 메시지 반복 방지)
        self._d2_shrink_log_ts: float = 0.0
        self._d2_shrink_log_sig: Optional[Tuple[int, int]] = None
        # 미체결 S1/S2 매수 주문이 점유하는 D+2 현금(로컬 예약 — 동기화 전 중복 매수 방지)
        self._d2_pending_buy_reserve: Dict[str, float] = {}
        # 체결 직후 on_fill 연속 호출 시 포지션 탭 전체 리빌드를 한 번으로 묶음
        self._gui_cb_after_id: Any = None
        self._gui_cb_last_flush_ts: float = 0.0
        self._gui_cb_dirty: bool = False
        self._gui_cb_in_progress: bool = False

    def _virtual_db_path(self) -> str:
        """종료내역 JSON — config.USE_MOCK 에 따라 virtual_db_mock.json / virtual_db_live.json."""
        try:
            return virtual_db_path()
        except Exception:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            return os.path.join(root, "virtual_db_mock.json")

    def _portfolio_gui_callback_debounce_ms(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            ms = int(float(st.get("portfolio_gui_callback_debounce_ms", 1200)))
            return max(200, min(ms, 5000))
        except Exception:
            return 1200

    def _schedule_portfolio_ui_refresh(self, *, light: bool = False) -> None:
        """
        체결·동기화 후 UI 갱신.
        light=True: 보유 행·요약·주문 로그만(전체 리빌드·금일손익 REST 재계산 생략).
        """
        if light:
            try:
                app = self.app
                fn = getattr(app, "request_virtual_tab_sync_light_refresh", None)
                if callable(fn):
                    fn()
                    return
            except Exception:
                pass
        self._schedule_gui_callback()

    def is_redundant_late_exchange_sell_fill(
        self, code: str, order_no: Optional[str]
    ) -> bool:
        """
        계좌 동기화로 보유가 이미 비워진 뒤 kt00007 매도 체결이 늦게 오면
        종료내역 보강·전체 GUI 리빌드를 생략합니다.
        """
        try:
            ncode = self._normalize_stock_code(code)
        except Exception:
            try:
                ncode = str(code).split("_")[0].zfill(6)
            except Exception:
                ncode = ""
        if not ncode:
            return False
        oid = str(order_no or "").strip()
        try:
            with self.lock:
                if ncode in self.virtual_positions:
                    return False
                if oid and oid in self._orphan_sell_order_ids:
                    return True
        except Exception:
            pass
        if oid:
            try:
                row = self.data_manager.get_order_history_row_by_order_no(oid)
            except Exception:
                row = None
            if isinstance(row, dict):
                try:
                    side = str(row.get("side", "") or "").strip().lower()
                    fq = int(row.get("filled_qty", 0) or 0)
                    res = str(row.get("result", "") or "").strip().upper()
                    if side == "sell" and fq > 0:
                        if res in ("FILLED", "SUCCESS") or fq > 0:
                            return True
                except Exception:
                    pass
        return False

    def _schedule_gui_callback(self) -> None:
        """
        gui_callback(포지션 관리 탭 전체 갱신 등)은 체결·WS·백그라운드 스레드에서 트리거될 수 있습니다.
        연속 호출은 trailing debounce 로 한 번만 메인 스레드에서 실행합니다.
        """
        if not self.gui_callback:
            return
        try:
            self._gui_cb_dirty = True
        except Exception:
            pass
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                self.gui_callback()
                return
            delay = int(self._portfolio_gui_callback_debounce_ms())
            try:
                if self._gui_cb_after_id is not None:
                    root.after_cancel(self._gui_cb_after_id)
            except Exception:
                pass
            self._gui_cb_after_id = root.after(delay, self._flush_gui_callback_on_main)
        except Exception:
            try:
                self.gui_callback()
            except Exception as e:
                self.log(f"⚠️ [Portfolio] GUI 콜백 실패: {e}")

    def _flush_gui_callback_on_main(self) -> None:
        try:
            self._gui_cb_after_id = None
            if not self._gui_cb_dirty:
                return
            if not self.gui_callback:
                return
            if bool(getattr(self, "_gui_cb_in_progress", False)):
                try:
                    root = getattr(self.app, "root", None)
                    if root is not None:
                        delay = int(self._portfolio_gui_callback_debounce_ms())
                        self._gui_cb_after_id = root.after(
                            max(120, delay), self._flush_gui_callback_on_main
                        )
                except Exception:
                    pass
                return
            self._gui_cb_dirty = False
            self._gui_cb_in_progress = True
            try:
                self._gui_cb_last_flush_ts = time.time()
                self.gui_callback()
            finally:
                self._gui_cb_in_progress = False
            if bool(getattr(self, "_gui_cb_dirty", False)):
                try:
                    root = getattr(self.app, "root", None)
                    if root is not None:
                        delay = int(self._portfolio_gui_callback_debounce_ms())
                        self._gui_cb_after_id = root.after(
                            delay, self._flush_gui_callback_on_main
                        )
                except Exception:
                    pass
        except Exception as e:
            self.log(f"⚠️ [Portfolio] GUI 콜백 예외: {e}")

    def _normalize_stock_code(self, raw_code: Any) -> str:
        """키움 REST·내부 저장 간 종목코드 형식(6자리) 통일."""
        try:
            if raw_code is None:
                return ""
            s = str(raw_code).strip()
            if not s:
                return ""
            if "_" in s:
                s = s.split("_", 1)[0]
            return s.zfill(6)
        except Exception:
            return ""

    def _parse_ka10085_holding_row(self, item: Dict[str, Any]) -> Optional[Tuple[str, str, int, float]]:
        """
        ka10085 응답 행 파싱 (공식: stk_cd, stk_nm, rmnd_qty, pur_pric).
        구 HTS/다른 TR 필드(pdno, hldg_qty 등)도 보조로 인식합니다.
        """
        try:
            if not isinstance(item, dict):
                return None
            raw_code = item.get("stk_cd") or item.get("pdno") or item.get("code")
            code = self._normalize_stock_code(raw_code)
            if not code:
                return None
            name = (
                item.get("stk_nm")
                or item.get("prdt_name")
                or item.get("name")
                or "Unknown"
            )
            qty = self._to_i(
                item.get("rmnd_qty")
                or item.get("hldg_qty")
                or item.get("qty")
                or 0
            )
            avg_price = self._to_f(
                item.get("pur_pric")
                or item.get("pchs_avg_pric")
                or item.get("avg_price")
                or 0.0
            )
            if qty <= 0:
                return None
            return (code, str(name), int(qty), float(avg_price))
        except Exception:
            return None

    def _parse_ka10085_row_pnl(self, item: Dict[str, Any]) -> Dict[str, float]:
        """
        ka10085 `acnt_prft_rt` 한 행에서 손익·평가·현재가 등 표시용 수치를 추출합니다.
        REST/모의 응답에서 필드명이 조금씩 달라도 동작하도록 복수 키를 시도합니다.
        """
        out: Dict[str, float] = {}
        try:
            if not isinstance(item, dict):
                return out

            def _nf(*keys: str) -> float:
                for k in keys:
                    v = item.get(k)
                    if v is None or v == "":
                        continue
                    try:
                        s = str(v).replace(",", "").replace("+", "").strip()
                        if s in ("", "-", "—"):
                            continue
                        return float(s)
                    except (TypeError, ValueError):
                        continue
                return 0.0

            cur = _nf("cur_prc", "prpr", "stck_prpr", "now_prc")
            qty_row = _nf("rmnd_qty", "hldg_qty", "qty")
            evlu = _nf(
                "evlu_amt",
                "stk_evlu_amt",
                "stk_evlu_amt_smtl",
                "evlu_amt_smtl",
            )
            pur_amt = _nf("pur_amt", "pchs_amt", "buy_amt", "byng_amt")
            pur_pric = _nf("pur_pric", "pchs_avg_pric", "avg_price", "bpsp_avg_pric")
            # 일부 REST 응답은 평가금액 필드만 비고 현재가·수량은 오는 경우가 있음 → 근사 복원
            if evlu <= 0 and cur > 0 and qty_row > 0:
                evlu = cur * qty_row
            if pur_amt <= 0 and pur_pric > 0 and qty_row > 0:
                pur_amt = pur_pric * qty_row
            pfls = _nf(
                "evlu_pfls_amt",
                "evlu_pfls",
                "stck_evlu_pfls_amt",
                "pfls_amt",
                "evlu_pl_amt",
                "pl_amt",
            )
            if abs(pfls) < 1e-9 and evlu > 0 and pur_amt > 0:
                pfls = evlu - pur_amt
            rt_api = _nf("prft_rt", "pl_rt", "evlu_pfls_rt", "evlu_erng_rt")
            if pur_amt > 1e-6:
                rt = (pfls / pur_amt) * 100.0
            else:
                rt = rt_api
            out["ka_cur_prc"] = float(cur)
            out["ka_evlu_amt"] = float(evlu)
            out["ka_evlu_pfls_amt"] = float(pfls)
            out["ka_prft_rt"] = float(rt)
            out["ka_pur_amt"] = float(pur_amt)
        except Exception:
            return {}
        return out

    @staticmethod
    def _sanitize_for_virtual_db_json(value: Any) -> Any:
        """
        ``virtual_db_*.json`` 저장용 — ``json.dump`` 이 처리하지 못하는
        pandas.Timestamp·numpy 스칼라·datetime 등을 Python 기본형·문자열로 바꿉니다.
        """
        try:
            import numpy as np
        except Exception:
            np = None  # type: ignore[assignment]
        try:
            import pandas as pd
        except Exception:
            pd = None  # type: ignore[assignment]

        if isinstance(value, dict):
            return {
                str(k): PortfolioManager._sanitize_for_virtual_db_json(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [PortfolioManager._sanitize_for_virtual_db_json(v) for v in value]
        if isinstance(value, set):
            return [
                PortfolioManager._sanitize_for_virtual_db_json(v)
                for v in list(value)
            ]
        if isinstance(value, (datetime, date, timedelta)):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        if pd is not None:
            try:
                if isinstance(value, pd.Timestamp):
                    try:
                        if bool(pd.isna(value)):
                            return None
                    except Exception:
                        pass
                    try:
                        return value.isoformat()
                    except Exception:
                        return str(value)
            except Exception:
                pass
        if np is not None:
            try:
                if isinstance(value, np.datetime64):
                    return str(value)
            except Exception:
                pass
            try:
                if isinstance(value, np.ndarray):
                    return PortfolioManager._sanitize_for_virtual_db_json(value.tolist())
            except Exception:
                pass
            try:
                if isinstance(value, (np.integer, np.floating, np.bool_)):
                    return value.item()
            except Exception:
                pass
            try:
                if isinstance(value, np.generic):
                    return value.item()
            except Exception:
                pass
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="replace")
            except Exception:
                return str(value)
        try:
            if hasattr(value, "isoformat") and callable(getattr(value, "isoformat")):
                if type(value).__module__.startswith("pandas"):
                    return str(value)
        except Exception:
            pass
        try:
            return str(value)
        except Exception:
            return None

    def _find_vp_key_for_code(self, norm_code: str) -> Optional[str]:
        """virtual_positions 키가 6자리 미일치일 때 동일 종목 키를 찾습니다."""
        try:
            for k in self.virtual_positions:
                if self._normalize_stock_code(k) == norm_code:
                    return str(k)
        except Exception:
            pass
        return None

    def _merge_vp_alias_to_canonical(self, canonical: str) -> None:
        """
        체결·주문 경로에서 종목코드가 3490 vs 003490 처럼 섞이면 dict 키가 둘로 나뉩니다.
        6자리 canonical 키 하나로 병합합니다 (대한항공 등 앞자리 0 종목에서 보유 UI 깜빡임 방지).

        주의: 예전 구현은 dict 순서상 \"003490\" 키가 먼저 오면 `k == nk` 에서 바로 return 해
        나머지 별칭(3490 등)을 영원히 병합하지 못하는 버그가 있었습니다.
        """
        try:
            nk = self._normalize_stock_code(canonical)
            if not nk:
                return
            keys: List[Any] = [
                k
                for k in list(self.virtual_positions.keys())
                if self._normalize_stock_code(k) == nk
            ]
            if len(keys) == 0:
                return
            if len(keys) == 1:
                k = keys[0]
                if k == nk:
                    return
                pos = self.virtual_positions.pop(k)
                self.virtual_positions[nk] = pos
                try:
                    self.data_manager.delete_active_position(k)
                except Exception:
                    pass
                try:
                    self.data_manager.save_active_position(
                        nk,
                        pos.get("name", ""),
                        float(pos.get("avg_price", 0)),
                        int(pos.get("qty", 0)),
                        int(pos.get("stage", 1)),
                        float(pos.get("invested", 0)),
                        str(pos.get("entry_time", "")),
                        str(pos.get("reason", "")),
                    )
                except Exception:
                    pass
                self.log(f"[Portfolio] 포지션 키 정규화: {k} → {nk}")
                return

            best_pos: Optional[Dict[str, Any]] = None
            for k in keys:
                pos = self.virtual_positions.get(k)
                if pos is None:
                    continue
                if best_pos is None:
                    best_pos = pos
                    continue
                if int(pos.get("qty", 0) or 0) > int(best_pos.get("qty", 0) or 0):
                    best_pos = pos
            if best_pos is None:
                return
            for k in keys:
                self.virtual_positions.pop(k, None)
                try:
                    self.data_manager.delete_active_position(k)
                except Exception:
                    pass
            self.virtual_positions[nk] = best_pos
            try:
                self.data_manager.save_active_position(
                    nk,
                    best_pos.get("name", ""),
                    float(best_pos.get("avg_price", 0)),
                    int(best_pos.get("qty", 0)),
                    int(best_pos.get("stage", 1)),
                    float(best_pos.get("invested", 0)),
                    str(best_pos.get("entry_time", "")),
                    str(best_pos.get("reason", "")),
                )
            except Exception:
                pass
            self.log(f"[Portfolio] 포지션 키 정규화(중복 병합): {keys} → {nk}")
        except Exception as e:
            self.log(f"⚠️ [Portfolio] 포지션 키 병합 실패: {e}")

    def _dedupe_all_vp_keys(self) -> None:
        """로드·동기화 직후: 동일 종목이 서로 다른 키로 남아 있으면 6자리 키로 일괄 병합합니다."""
        try:
            nks = set()
            for k in list(self.virtual_positions.keys()):
                nc = self._normalize_stock_code(k)
                if nc:
                    nks.add(nc)
            for nk in nks:
                self._merge_vp_alias_to_canonical(nk)
        except Exception as e:
            self.log(f"⚠️ [Portfolio] VP 키 일괄 정규화 실패: {e}")

    def _has_confirmed_buy_events(self, pos: Dict[str, Any]) -> bool:
        """포지션에 확정된 매수(BUY_S1/S2) 이벤트가 있는지."""
        try:
            for ev in pos.get("events") or []:
                if isinstance(ev, dict) and str(ev.get("action", "")).startswith("BUY_"):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_s2_fill_reason(reason: Optional[str]) -> bool:
        """체결·주문 reason 문자열이 Stage 2 추매인지 판별."""
        try:
            r = str(reason or "").upper()
        except Exception:
            return False
        return any(
            tok in r
            for tok in (
                "STAGE 2",
                "STAGE2",
                "BUY_2ND",
                "ADDON(S2)",
                "SCALING",
                "S2_",
            )
        )

    def position_effective_stage(self, pos: Dict[str, Any]) -> int:
        """entry_n·stage·BUY_S2 이벤트를 종합한 실효 단계(1 또는 2+)."""
        try:
            st = max(
                int(pos.get("entry_n", 0) or 0),
                int(pos.get("stage", 1) or 1),
            )
            if st >= 2:
                return st
            for ev in pos.get("events") or []:
                if isinstance(ev, dict) and str(ev.get("action", "")).upper() == "BUY_S2":
                    return 2
        except Exception:
            pass
        return 1

    def _resolve_fill_is_s2(
        self,
        code: str,
        reason: Optional[str],
        order_no: Optional[str],
    ) -> bool:
        """on_fill 매수가 S2 추매 체결인지 reason·주문메타·주문번호로 판별."""
        if self._is_s2_fill_reason(reason):
            return True
        try:
            meta = self.data_manager.get_latest_order_meta(code)
            if meta and self._is_s2_fill_reason(meta[1] if len(meta) > 1 else None):
                return True
        except Exception:
            pass
        oid = str(order_no or "").strip()
        if oid:
            try:
                row = self.data_manager.get_order_history_row_by_order_no(oid)
                if isinstance(row, dict) and self._is_s2_fill_reason(
                    row.get("reason")
                ):
                    return True
            except Exception:
                pass
        return False

    def _position_has_buy_s2_event(self, pos: Dict[str, Any]) -> bool:
        try:
            for ev in pos.get("events") or []:
                if isinstance(ev, dict) and str(ev.get("action", "")).upper() == "BUY_S2":
                    return True
        except Exception:
            pass
        return False

    def _ensure_s1_event_before_s2_fill(
        self,
        pos: Dict[str, Any],
        *,
        fill_qty: int,
        now_str: str,
        code: str,
        snapshot: Any,
        reg_reason: str,
    ) -> None:
        """S2 체결인데 BUY_S1 이벤트가 없으면(동기화·DB 유실) 기존 보유로 S1 이벤트를 복원."""
        if self._has_confirmed_buy_events(pos):
            return
        try:
            cur_q = self._to_i(pos.get("qty", 0))
            fq = max(0, self._to_i(fill_qty))
            s1_q = cur_q - fq if cur_q > fq else cur_q
            if s1_q <= 0:
                return
            s1_p = self._to_f(pos.get("avg_price", 0))
            if s1_p <= 0:
                return
            _snap = self._ensure_fill_snapshot(code, s1_p, snapshot, str(reg_reason))
            pos.setdefault("events", []).append(
                {
                    "time": now_str,
                    "action": "BUY_S1",
                    "price": s1_p,
                    "qty": s1_q,
                    "snapshot": _snap,
                }
            )
        except Exception:
            pass

    def _finalize_s2_fill_state(
        self, pos: Dict[str, Any], *, fill_price: float, fill_qty: int, now_str: str
    ) -> None:
        """S2 체결 후 stage·entry_n·pending 플래그를 확정."""
        pos["stage"] = 2
        pos["entry_n"] = 2
        pos.pop("s2_limit_order_pending", None)
        pos.pop("s2_pending_ord_no", None)
        if not self._position_has_buy_s2_event(pos):
            pos.setdefault("events", []).append(
                {
                    "time": now_str,
                    "action": "BUY_S2",
                    "price": float(fill_price),
                    "qty": int(fill_qty),
                    "snapshot": {},
                }
            )

    def _is_synced_placeholder_position(self, pos: Dict[str, Any]) -> bool:
        """ka10085 선반영(SYNCED) — on_fill 전 임시 포지션."""
        try:
            if str(pos.get("reason", "")).upper() != "SYNCED":
                return False
            return not self._has_confirmed_buy_events(pos)
        except Exception:
            return False

    def _should_skip_reconcile_remove(self, pos: Dict[str, Any]) -> bool:
        """
        매수 직후 ka10085 잔고에 아직 안 잡히는 API 지연으로,
        로컬만 보유로 오판해 포지션을 지웠다가(보유 UI 깜빡임) 다시 붙는 현상을 완화합니다.
        SYNCED(서버에서만 유입된) 포지션은 유예 없이 정상 비교합니다.
        """
        try:
            if str(pos.get("reason", "")).upper() == "SYNCED":
                return False
            et = pos.get("entry_time") or ""
            if not et:
                return False
            try:
                s = str(et).strip().replace("Z", "")
                m = re.match(
                    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                    s,
                )
                if not m:
                    return False
                dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            except (TypeError, ValueError):
                return False
            try:
                grace = float(self.settings.get("reconcile_remove_grace_seconds", 180))
            except (TypeError, ValueError):
                grace = 180.0
            grace = max(30.0, min(grace, 3600.0))
            if (datetime.now() - dt).total_seconds() < grace:
                return True
        except Exception:
            pass
        return False

    def _parse_history_exit_datetime(self, pos: Dict[str, Any]) -> Optional[datetime]:
        """종료 이력 dict에서 종료 시각을 datetime으로 복원합니다."""
        try:
            ex = str(pos.get("exit_datetime") or "").strip()
            if ex and " " in ex:
                return datetime.strptime(ex[:19], "%Y-%m-%d %H:%M:%S")
            ed = str(pos.get("exit_date") or "").strip()
            et = str(pos.get("exit_time") or "").strip()
            if ed and et:
                etp = et if ":" in et else f"{et}:00"
                return datetime.strptime(f"{ed} {etp}"[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
        return None

    def _recent_sell_exit_exists_for_code(
        self, code: str, *, within_hours: Optional[float] = None
    ) -> bool:
        """
        최근(설정 시간 이내) 동일 종목에 SELL 이벤트가 있는 종료 이력이 있으면 True.

        - within_hours가 **지정된 경우**(예: SYNCED 자동등록 생략): 해당 시간(시) 윈도우.
        - within_hours가 **None**(SYNCED 유령 포지션만 로컬 제거): **분 단위** 윈도우.
          과거 기본 `sync_ghost_suppress_hours`(12h)는 동일 종목 당일 재매도 후에도 True가 되어
          유령 제거만 하고 실제 체결 `on_fill`은 '보유 없음'으로 스킵 → 매매종료내역 누락이
          발생할 수 있어, 분 설정(`sync_ghost_suppress_minutes`)을 쓰고 시간 키는 상한 변환만 허용합니다.
        """
        try:
            nc = self._normalize_stock_code(code)
            if not nc:
                return False
            st = self.settings or {}
            if within_hours is None:
                try:
                    raw_m = st.get("sync_ghost_suppress_minutes")
                    if raw_m is not None and str(raw_m).strip() != "":
                        mins = float(raw_m)
                    else:
                        try:
                            hrs_legacy = float(st.get("sync_ghost_suppress_hours", 12.0))
                        except (TypeError, ValueError):
                            hrs_legacy = 12.0
                        # 레거시(시간) 값은 분으로 바꾸되, 유령 억제에 쓰기엔 과도하게 길지 않게 상한
                        mins = min(max(hrs_legacy * 60.0, 3.0), 45.0)
                except (TypeError, ValueError):
                    mins = 15.0
                mins = max(2.0, min(mins, 180.0))
                cutoff = datetime.now() - timedelta(minutes=mins)
            else:
                try:
                    hrs = float(within_hours)
                except (TypeError, ValueError):
                    hrs = 12.0
                hrs = max(0.25, min(hrs, 168.0))
                cutoff = datetime.now() - timedelta(hours=hrs)
            for pos in reversed(self.virtual_history):
                try:
                    pc = self._normalize_stock_code(str(pos.get("code", "")))
                except Exception:
                    continue
                if pc != nc:
                    continue
                evs = pos.get("events") or []
                has_sell = any(
                    str(e.get("action", "")).upper() == "SELL" for e in evs
                )
                if not has_sell:
                    continue
                ex_dt = self._parse_history_exit_datetime(pos)
                if ex_dt is None:
                    continue
                if ex_dt >= cutoff:
                    return True
            return False
        except Exception:
            return False

    def _latest_sell_exit_datetime_for_code(
        self, code: str, *, within_hours: Optional[float] = None
    ) -> Optional[datetime]:
        """
        최근(설정 시간 이내) 해당 종목에 대해 종료(SELL) 이력이 있으면
        가장 최근 종료 시각(datetime)을 반환합니다.

        SYNCED 자동등록 생략이 과도하게 걸린 경우(SELL 직후 재진입 등)에
        'SELL 이후 새 BUY가 발생했는지' 판단을 돕기 위한 보조 함수입니다.
        """
        try:
            nc = self._normalize_stock_code(code)
            if not nc:
                return None

            st = self.settings or {}
            if within_hours is None:
                try:
                    raw_m = st.get("sync_ghost_suppress_minutes")
                    if raw_m is not None and str(raw_m).strip() != "":
                        mins = float(raw_m)
                    else:
                        hrs_legacy = float(st.get("sync_ghost_suppress_hours", 12.0))
                        mins = hrs_legacy * 60.0
                except Exception:
                    mins = 15.0
                mins = min(max(mins, 2.0), 180.0)
                cutoff = datetime.now() - timedelta(minutes=mins)
            else:
                try:
                    hrs = float(within_hours)
                except (TypeError, ValueError):
                    hrs = 12.0
                hrs = max(0.25, min(hrs, 168.0))
                cutoff = datetime.now() - timedelta(hours=hrs)

            latest_exit: Optional[datetime] = None
            for pos in reversed(self.virtual_history):
                try:
                    pc = self._normalize_stock_code(str(pos.get("code", "")))
                except Exception:
                    continue
                if pc != nc:
                    continue
                evs = pos.get("events") or []
                has_sell = any(
                    str(e.get("action", "")).upper() == "SELL"
                    for e in evs
                    if isinstance(e, dict)
                )
                if not has_sell:
                    continue
                ex_dt = self._parse_history_exit_datetime(pos)
                if ex_dt is None:
                    continue
                if ex_dt < cutoff:
                    # reversed 순회이므로 최신 종료가 cutoff보다 작으면 종료
                    break
                latest_exit = ex_dt
                break
            return latest_exit
        except Exception:
            return None

    def _cost_basis_from_buy_events(
        self, pos: Dict[str, Any]
    ) -> Optional[Tuple[float, float, int]]:
        """
        events의 BUY_S1/BUY_S2 체결만으로 가중 평균 체결단가·매입원(매수수수료 포함)·총 매수 주수를 계산합니다.
        실계좌 동기화(ka10085)의 pur_pric은 체결 호가와 다를 수 있어, 로컬 체결 이력이 있으면 이 값을 우선합니다.
        """
        try:
            evs = pos.get("events") or []
            if not isinstance(evs, list):
                return None
            legs: List[Tuple[float, int]] = []
            for e in evs:
                if not isinstance(e, dict):
                    continue
                act = str(e.get("action", "") or "").upper()
                if act not in ("BUY_S1", "BUY_S2"):
                    continue
                p = self._to_f(e.get("price", 0))
                q = self._to_i(e.get("qty", 0))
                if p > 0.0 and q > 0:
                    legs.append((p, q))
            if not legs:
                return None
            total_q = sum(x[1] for x in legs)
            if total_q <= 0:
                return None
            gross = sum(x[0] * float(x[1]) for x in legs)
            w_avg = gross / float(total_q)
            try:
                buy_r, _ = fee_decimal_rates_live_trading(self.settings)
            except Exception:
                try:
                    buy_r, _ = fee_decimal_rates_live_trading({})
                except Exception:
                    buy_r = 0.0035
            invested = gross + gross * float(buy_r)
            return (float(w_avg), float(invested), int(total_q))
        except Exception:
            return None

    def _gross_to_invested_with_buy_fee(self, gross: float) -> float:
        """체결금액(수수료 제외)에 매수 수수료를 더한 매입원가 — 동기화·폴백 시 on_fill(S1)과 동일."""
        try:
            g = max(0.0, float(gross))
        except (TypeError, ValueError):
            return 0.0
        try:
            buy_r, _ = fee_decimal_rates_live_trading(self.settings)
        except Exception:
            try:
                buy_r, _ = fee_decimal_rates_live_trading({})
            except Exception:
                buy_r = 0.0035
        try:
            return float(g + g * float(buy_r))
        except (TypeError, ValueError):
            return float(g)

    def _try_apply_local_buy_events_cost(
        self, pos: Dict[str, Any], broker_qty: int
    ) -> bool:
        """체결 이벤트 합산 수량이 서버 잔고와 같으면 평단·invested를 체결 기준으로 유지합니다."""
        try:
            tup = self._cost_basis_from_buy_events(pos)
            if tup is None:
                return False
            w_avg, invested, ev_q = tup
            if ev_q != int(broker_qty):
                return False
            pos["avg_price"] = w_avg
            pos["invested"] = invested
            return True
        except Exception:
            return False

    def _infer_sig_type_from_entry_reason(self, code: str, reason: Any) -> str:
        """
        active_positions.reason 등만 있을 때 TYPE_A / TYPE_B 를 복원합니다.
        SYNCED·API 문자열 등에는 감시 sel_track·최근 매수 주문 reason 으로 보강합니다.
        """
        try:
            nk = str(code or "").split("_")[0].zfill(6)
            r = str(reason or "").upper()
            if "TYPE_B" in r:
                return "TYPE_B"
            if "TYPE_A" in r:
                return "TYPE_A"
            if "SYNCED" in r or r.strip() == "SYNCED":
                try:
                    mi = getattr(self.app, "monitor_info", None) or {}
                    for k in (nk, str(code or "").strip()):
                        inf = mi.get(k)
                        if isinstance(inf, dict):
                            st = str(
                                inf.get("sel_track")
                                or inf.get("SelTrack")
                                or ""
                            ).strip().upper()
                            if st == "B":
                                return "TYPE_B"
                            if st == "A":
                                return "TYPE_A"
                            break
                except Exception:
                    pass
                try:
                    rs_hint = self.data_manager.get_latest_buy_reason_with_sig_type(
                        nk
                    )
                    if rs_hint:
                        br = str(rs_hint).upper()
                        if "TYPE_B" in br:
                            return "TYPE_B"
                        if "TYPE_A" in br:
                            return "TYPE_A"
                except Exception:
                    pass
                return "TYPE_A"
        except Exception:
            pass
        try:
            rs_hint = self.data_manager.get_latest_buy_reason_with_sig_type(
                str(code or "").split("_")[0].zfill(6)
            )
            if rs_hint:
                br = str(rs_hint).upper()
                if "TYPE_B" in br:
                    return "TYPE_B"
                if "TYPE_A" in br:
                    return "TYPE_A"
        except Exception:
            pass
        return "TYPE_A"

    def infer_sig_type_for_ui(self, code: str, pos: Optional[Dict[str, Any]]) -> str:
        """
        감시 트리「전략유형」용: 보유 dict 의 sig_type·reason 우선, 없으면 주문·선정 맥락으로 단일 A/B.
        """
        try:
            if isinstance(pos, dict):
                st = str(pos.get("sig_type") or "").strip().upper()
                if st == "TYPE_B" or "TYPE_B" in st:
                    return "TYPE_B"
                if st == "TYPE_A" or (st and "TYPE_A" in st):
                    return "TYPE_A"
                rsn = str(pos.get("reason") or "").upper()
                if "TYPE_B" in rsn:
                    return "TYPE_B"
                if "TYPE_A" in rsn and "TYPE_B" not in rsn:
                    return "TYPE_A"
                return self._infer_sig_type_from_entry_reason(
                    str(code or "").split("_")[0].zfill(6),
                    pos.get("reason"),
                )
        except Exception:
            pass
        return self._infer_sig_type_from_entry_reason(
            str(code or "").split("_")[0].zfill(6), None
        )

    def load_all(self):
        """DB 및 파일에서 포지션 데이터를 로드합니다."""
        with self.lock:
            try:
                migrate_legacy_virtual_db_files()
            except Exception:
                pass
            vpath = self._virtual_db_path()
            # 1. 파일에서 히스토리 로드
            if os.path.exists(vpath):
                try:
                    with open(vpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.virtual_history = data.get('history', [])
                        try:
                            pend = data.get("pending_exit_snapshots") or {}
                            if isinstance(pend, dict):
                                self._pending_exit_snapshots = {
                                    str(k).strip(): v
                                    for k, v in pend.items()
                                    if str(k).strip() and isinstance(v, dict)
                                }
                                self._prune_stale_pending_exit_snapshots()
                        except Exception:
                            pass
                        # 구버전 이력: exit_date+exit_time 만 있으면 exit_datetime 보강(종료일시 UI 일관)
                        for h in self.virtual_history:
                            try:
                                if not isinstance(h, dict) or h.get("exit_datetime"):
                                    continue
                                ed = str(h.get("exit_date") or "").strip()
                                et = str(h.get("exit_time") or "").strip()
                                if ed and et and " " not in et:
                                    h["exit_datetime"] = f"{ed} {et}"
                            except Exception:
                                pass
                except Exception as e:
                    self.log(f"[Error] [Portfolio] 히스토리 로드 실패: {e}")

            # 2. DB에서 활성 포지션 로드 (DataManagerV2 기준)
            try:
                # [v3.0.4] DataManagerV2의 get_active_positions() 활용
                active = self.data_manager.get_active_positions() # 딕셔너리 리턴 가정
                if active:
                    # active가 리스트라면 딕셔너리로 변환
                    if isinstance(active, list):
                        # [버그픽스] 재인증 등으로 load_all 이 재호출되면 DB active 에는
                        # exit_pending/pending_*·max_p·events 같은 휘발 상태가 없어 그대로
                        # 덮으면 '매도 진행 중' 플래그가 사라져 이중 매도가 날 수 있다.
                        # 기존 메모리 포지션의 휘발 필드를 베이스코드 기준으로 보존한다.
                        prev_by_base: Dict[str, Dict[str, Any]] = {}
                        try:
                            for _pk, _pv in (self.virtual_positions or {}).items():
                                if isinstance(_pv, dict):
                                    _bk = str(_pk).split("_")[0].zfill(6)
                                    if _bk not in prev_by_base:
                                        prev_by_base[_bk] = _pv
                        except Exception:
                            prev_by_base = {}
                        for row in active:
                            # row: (code, name, avg_p, qty, stage, invested, time, reason)
                            code = row[0]
                            try:
                                _nk = str(code or "").split("_")[0].zfill(6)
                                _sig = self._infer_sig_type_from_entry_reason(
                                    _nk, row[7] if len(row) > 7 else None
                                )
                            except Exception:
                                _sig = "TYPE_A"
                            new_pos: Dict[str, Any] = {
                                "name": row[1], "avg_price": float(row[2]), "qty": int(row[3]),
                                "stage": int(row[4]), "invested": float(row[5]),
                                "entry_time": row[6], "reason": row[7],
                                "sig_type": _sig,
                                "events": [], "max_now": float(row[2])
                            }
                            try:
                                if int(new_pos.get("stage", 1) or 1) >= 2:
                                    new_pos["entry_n"] = 2
                            except (TypeError, ValueError):
                                pass
                            try:
                                _old = prev_by_base.get(_nk)
                                if isinstance(_old, dict):
                                    for _vf in (
                                        "exit_pending",
                                        "pending_exit_ord_no",
                                        "s1_limit_order_pending",
                                        "s2_limit_order_pending",
                                        "s2_limit_order_pending_ord_no",
                                    ):
                                        if _vf in _old:
                                            new_pos[_vf] = _old[_vf]
                                    try:
                                        _om = max(
                                            float(_old.get("max_p", 0) or 0),
                                            float(_old.get("max_now", 0) or 0),
                                            float(_old.get("max_price", 0) or 0),
                                        )
                                        if _om > float(new_pos.get("max_now", 0) or 0):
                                            new_pos["max_now"] = _om
                                            new_pos["max_p"] = _om
                                            new_pos["max_price"] = _om
                                    except (TypeError, ValueError):
                                        pass
                                    try:
                                        if (
                                            isinstance(_old.get("events"), list)
                                            and _old.get("events")
                                        ):
                                            new_pos["events"] = _old["events"]
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            self.virtual_positions[code] = new_pos
                    else:
                        self.virtual_positions = active
                    self.log(f"[Portfolio] DB에서 {len(self.virtual_positions)}개의 활성 포지션 복구")
                    self._dedupe_all_vp_keys()
            except Exception as e:
                self.log(f"[Error] [Portfolio] DB 포지션 복구 실패: {e}")

    def save_db(self):
        """현재 상태를 파일에 저장합니다 (히스토리 위주).

        [버그픽스] 직렬화 스냅샷을 `self.lock` 안에서 만들어 다른 스레드(on_fill/sync/
        register_pending_exit_snapshot)의 동시 변경으로 인한 history-pending 불일치를 막고,
        임시파일에 기록 후 os.replace 로 원자적 교체하여 쓰기 도중 종료 시 파일 손상을 방지한다.
        """
        try:
            try:
                with self.lock:
                    self._prune_stale_pending_exit_snapshots()
            except Exception:
                pass
            # 락 안에서 직렬화까지 완료하여 일관된 스냅샷을 확보(부분 변경 차단)
            try:
                with self.lock:
                    payload = {
                        "history": self._sanitize_for_virtual_db_json(
                            self.virtual_history
                        ),
                        "pending_exit_snapshots": self._sanitize_for_virtual_db_json(
                            self._pending_exit_snapshots
                        ),
                    }
                    data_str = json.dumps(
                        payload, ensure_ascii=False, indent=4
                    )
            except Exception as e:
                self.log(f"[Error] [Portfolio] DB 직렬화 실패: {e}")
                return
            path = self._virtual_db_path()
            tmp_path = f"{path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(data_str)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(tmp_path, path)
            except Exception as e:
                self.log(f"[Error] [Portfolio] DB 저장(원자적 교체) 실패: {e}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"[Error] [Portfolio] DB 저장 실패: {e}")

    def sync_with_actual_account(
        self,
        silent: bool = False,
        force: bool = False,
        *,
        refresh_cash_deposit: bool = True,
        schedule_gui_refresh: bool = True,
        schedule_orders_refresh: bool = True,
        sync_context: str = "",
    ) -> None:
        """[버그픽스] 동시 실행 가드 래퍼 — 이미 동기화가 진행 중이면 중복 호출을 건너뛴다.

        여러 경로(체결 확인·미체결 타임아웃·EOD·수동 버튼)에서 직접 호출되어도
        reconcile/save_db/streak 카운터가 interleave 되지 않도록 직렬화한다.
        """
        if not self._sync_in_progress_lock.acquire(blocking=False):
            if not silent:
                try:
                    self.log("ℹ️ [Sync] 이미 계좌 동기화가 진행 중 — 중복 호출 건너뜀")
                except Exception:
                    pass
            return
        try:
            self._sync_with_actual_account_impl(
                silent=silent,
                force=force,
                refresh_cash_deposit=refresh_cash_deposit,
                schedule_gui_refresh=schedule_gui_refresh,
                schedule_orders_refresh=schedule_orders_refresh,
                sync_context=sync_context,
            )
        finally:
            try:
                self._sync_in_progress_lock.release()
            except Exception:
                pass

    def _sync_with_actual_account_impl(
        self,
        silent: bool = False,
        force: bool = False,
        *,
        refresh_cash_deposit: bool = True,
        schedule_gui_refresh: bool = True,
        schedule_orders_refresh: bool = True,
        sync_context: str = "",
    ) -> None:
        """[v3.7.8] 실제 키움 계좌의 보유 종목과 로컬 포지션을 동기화 (Reconciliation)"""
        # [v4.5.1 API Throttle] 자동 동기화만 쿨다운; 동기화 버튼(force=True)은 즉시 실행
        import time
        now = time.time()
        if not force and (now - self.last_sync_time < self.min_sync_interval):
            wait_s = int(self.min_sync_interval - (now - self.last_sync_time))
            if not silent:
                self.log(f"⏳ [Throttle] 계좌 조회가 너무 잦습니다. {wait_s}초 후에 업데이트가 가능합니다.")
            return

        # [v4.5 Asset Harden] get_holdings()가 딕셔너리를 반환함에 따라 호출부 수정
        try:
            account_data = self.order_manager.get_holdings(
                refresh_cash_deposit=refresh_cash_deposit,
                log_summary=not silent,
                sync_context=str(sync_context or "").strip(),
            )
        except Exception as gh_ex:
            self.log(f"[Error] [Sync] 계좌 조회 호출 실패: {gh_ex}")
            return
        if account_data is None:
            auth_busy = False
            try:
                ras = getattr(self.app, "rest_auth_retry_service", None)
                auth_busy = bool(ras is not None and ras.is_running())
            except Exception:
                auth_busy = False
            if auth_busy:
                if not silent:
                    self.log(
                        "ℹ️ [Sync] 인증 진행 중 — 계좌 동기화는 인증 완료 후 자동 재시도합니다."
                    )
                return
            try:
                am = getattr(self.app, "auth_manager", None)
                if am is not None and not am.has_cached_token():
                    if not silent:
                        self.log(
                            "ℹ️ [Sync] REST 토큰 없음 — 키움 API 인증 후 동기화해 주세요."
                        )
                    return
            except Exception:
                pass
            self.log(
                "[Error] [Sync] 계좌 정보를 실시간으로 가져올 수 없습니다. "
                "(API 429·토큰 만료 가능 — 1~2분 후 재시도)"
            )
            return
        
        self.last_sync_time = now # 성공 시점 업데이트
        
        holdings = account_data.get('holdings', [])
        te_resolved = float(account_data.get('total_equity', 0) or 0)
        
        # refresh_cash_deposit=False: 주기 정합 — 보유(ka10085)만 반영하고
        # 예수금·D+1/D+2·총자산·API 평가액 캐시는 유지(매매 체결 후 전체 동기화에서만 REST 반영).
        if refresh_cash_deposit:
            with self.lock:
                self.d1_deposit = account_data.get('d1_deposit', 0)
                self.d2_deposit = account_data.get('d2_deposit', 0)
                try:
                    self.holdings_stocks_mv_api = float(
                        account_data.get("stocks_market_value", 0) or 0
                    )
                except (TypeError, ValueError):
                    self.holdings_stocks_mv_api = 0.0
                try:
                    self.last_tot_evlu_amt = float(
                        account_data.get("tot_evlu_amt", 0) or 0
                    )
                except (TypeError, ValueError):
                    self.last_tot_evlu_amt = 0.0
                # 총자산: D+2(예수금상세 반영) + 보유평가 합이 기본. tot_evlu_amt는 이전 예수 기준이거나 음수 D+2와 불일치할 수 있음.
                try:
                    te_parts = float(self.d2_deposit or 0) + float(self.holdings_stocks_mv_api or 0)
                except (TypeError, ValueError):
                    te_parts = 0.0
                raw_tot = float(self.last_tot_evlu_amt or 0)
                if abs(te_parts) > 1e-6 or self.holdings_stocks_mv_api > 0 or self.d2_deposit != 0:
                    if raw_tot > 0:
                        den = max(abs(raw_tot), abs(te_parts), 1.0)
                        if abs(raw_tot - te_parts) / den <= 0.025:
                            self.total_equity = raw_tot
                        else:
                            self.total_equity = float(te_parts)
                    else:
                        self.total_equity = float(te_parts)
                elif raw_tot > 0:
                    self.total_equity = raw_tot
                else:
                    self.total_equity = float(te_resolved)
                # 포지션 탭 '예수금' = 출금가능금액(pymn_alow). 주문가능(ord_alow)과 별개.
                # D+2 추정(d2_deposit)은 결제 추정이라 음수 가능 — 예수금 칸에 쓰지 않음.
                try:
                    wd = float(
                        account_data.get("cash_withdrawable")
                        or account_data.get("cash_available")
                        or 0
                    )
                    self.actual_deposit = max(0.0, wd)
                except (TypeError, ValueError):
                    self.actual_deposit = 0.0
        
        server_codes = set()
        updated_count = 0
        new_count = 0
        local_codes: List[str] = []

        with self.lock:
            # 1. 서버 데이터 기준으로 로컬 데이터 업데이트
            for item in holdings:
                parsed = self._parse_ka10085_holding_row(item)
                if not parsed:
                    continue
                code, name, qty, avg_price = parsed
                server_codes.add(code)

                vp_key = self._find_vp_key_for_code(code)
                if vp_key is not None and vp_key != code:
                    try:
                        self.virtual_positions[code] = self.virtual_positions.pop(vp_key)
                    except Exception as e:
                        self.log(f"[Sync] 종목코드 키 정규화 실패 ({vp_key}→{code}): {e}")
                        continue
                    try:
                        self.data_manager.delete_active_position(vp_key)
                    except Exception:
                        pass

                if code in self.virtual_positions:
                    pos_ref = self.virtual_positions[code]
                    pos_ref["qty"] = qty
                    pos_ref["name"] = name
                    # ka10085 매입평균(pur_pric)은 체결 호가·내부 반올림과 다를 수 있음 — BUY 이벤트가 있으면 체결가 기준 유지
                    if not self._try_apply_local_buy_events_cost(pos_ref, qty):
                        pos_ref["avg_price"] = avg_price
                        pos_ref["invested"] = self._gross_to_invested_with_buy_fee(
                            float(qty) * float(avg_price)
                        )
                    updated_count += 1
                else:
                    try:
                        nh = float(
                            self.settings.get("sync_ghost_new_synced_hours", 1.0)
                        )
                    except (TypeError, ValueError):
                        nh = 1.0
                    nh = max(0.1, min(nh, 48.0))
                    # 최근 실매도 종료(ghost 방지) 스킵은 "서버 잔고가 0인데 로컬만 남은" 경우에만
                    # 의미가 있습니다. 서버 보유가 qty>0인데도 SYNCED 등록을 스킵하면
                    # "체결은 됐는데 VP로 안 올라오는" 현상이 생깁니다.
                    qty_i = 0
                    try:
                        qty_i = int(qty or 0)
                    except Exception:
                        qty_i = 0

                    if qty_i <= 0 and self._recent_sell_exit_exists_for_code(
                        code, within_hours=nh
                    ):
                        # SELL 직후 재진입(새 BUY)인데도 SYNCED 등록이 스킵되면
                        # "체결은 됐는데 보유종목으로 안 올라오는" 현상이 생깁니다.
                        # 따라서 SELL 이후 새 BUY SUCCESS/filled가 들어왔는지 보고 허용합니다.
                        allow_register = False
                        try:
                            last_sell_dt = self._latest_sell_exit_datetime_for_code(
                                code, within_hours=nh
                            )
                        except Exception:
                            last_sell_dt = None

                        try:
                            lb = self.data_manager.get_last_successful_buy_for_code(
                                self._normalize_stock_code(code)
                            )
                        except Exception:
                            lb = None

                        if last_sell_dt is not None and lb is not None:
                            try:
                                buy_ts = str(lb.get("timestamp") or "").strip()
                                buy_dt = datetime.strptime(
                                    buy_ts[:19], "%Y-%m-%d %H:%M:%S"
                                )
                                allow_register = buy_dt > last_sell_dt
                            except Exception:
                                allow_register = False

                        if not allow_register:
                            if not silent:
                                try:
                                    self.log(
                                        f"[Sync] {code}({name}) SYNCED 자동등록 생략 — "
                                        f"최근 {nh:.1f}h 이내 실매도 종료 이력 있음(잔고 유령 방지)"
                                    )
                                except Exception:
                                    pass
                            continue
                    _sync_sig = "TYPE_A"
                    try:
                        mi = getattr(self.app, "monitor_info", None) or {}
                        for _k in (code, str(code).zfill(6)):
                            _inf = mi.get(_k)
                            if isinstance(_inf, dict):
                                _st = str(
                                    _inf.get("sel_track")
                                    or _inf.get("SelTrack")
                                    or ""
                                ).strip().upper()
                                if _st == "B":
                                    _sync_sig = "TYPE_B"
                                break
                    except Exception:
                        pass
                    _inv_sync = self._gross_to_invested_with_buy_fee(
                        float(qty) * float(avg_price)
                    )
                    self.virtual_positions[code] = {
                        "name": name, "avg_price": avg_price, "qty": qty,
                        "stage": 1, "invested": _inv_sync,
                        "entry_time": datetime.now().isoformat(),
                        "reason": "SYNCED", "sig_type": _sync_sig, "events": [], "max_now": avg_price
                    }
                    self.data_manager.save_active_position(
                        code, name, avg_price, qty, 1, _inv_sync, datetime.now().isoformat(), "SYNCED"
                    )
                    new_count += 1
                try:
                    pnl_snap = self._parse_ka10085_row_pnl(item)
                    if pnl_snap:
                        self.virtual_positions[code].update(pnl_snap)
                    self.virtual_positions[code]["ka_sync_iso"] = datetime.now().isoformat()
                except Exception as e:
                    try:
                        self.log(f"⚠️ [Sync] ka10085 손익 스냅샷 저장 실패 ({code}): {e}")
                    except Exception:
                        pass
                try:
                    # 동기화 후 VP에 반영된 평단으로 주문 이력 보정(체결 우선 시 실제 체결가 유지)
                    _patch_p = float(
                        self.virtual_positions[code].get("avg_price", avg_price)
                        or avg_price
                    )
                    self.data_manager.patch_buy_filled_price_after_reconcile(
                        str(code), _patch_p, int(qty)
                    )
                except Exception:
                    pass
                    
            # 2. 로컬에만 있는 종목 제거 (매매 누락 보정)
            local_codes = list(self.virtual_positions.keys())
            removed_count = 0
            # qty>0 파싱 결과 — ka10085 성공 후 비어 있으면 증권사 잔고 0종목 확정
            confirmed_zero_holdings = len(server_codes) == 0

            try:
                confirm_n = int(
                    float(self.settings.get("reconcile_remove_confirm_syncs", 2))
                )
            except (TypeError, ValueError):
                confirm_n = 2
            confirm_n = max(1, min(confirm_n, 10))

            # 대시보드「동기화」(force=True): 잔고 0 확인 시 연속 확인·매수 유예 없이 즉시 정합
            force_reconcile_zero = bool(force and confirmed_zero_holdings)

            if confirmed_zero_holdings and len(local_codes) > 0 and not silent:
                try:
                    if force_reconcile_zero:
                        self.log(
                            "[Sync] 증권사 보유 0종목 확인 — "
                            "로컬 포지션을 정합합니다(수동 동기화)"
                        )
                    else:
                        self.log(
                            f"[Sync] 증권사 보유 0종목 — 로컬만 남은 종목은 "
                            f"{confirm_n}회 연속 확인 후 제거합니다"
                        )
                except Exception:
                    pass

            if len(local_codes) > 0:
                for code in local_codes:
                    nc = self._normalize_stock_code(code)
                    if nc in server_codes:
                        self._reconcile_server_missing_streak[nc] = 0
                    else:
                        self._reconcile_server_missing_streak[nc] = (
                            int(self._reconcile_server_missing_streak.get(nc, 0)) + 1
                        )

            for code in local_codes:
                if self._normalize_stock_code(code) not in server_codes:
                    nc = self._normalize_stock_code(code)
                    streak = int(self._reconcile_server_missing_streak.get(nc, 0) or 0)
                    if not force_reconcile_zero and streak < confirm_n:
                        if not silent:
                            try:
                                self.log(
                                    f"[Sync] {code} 서버 목록 미포함 "
                                    f"({streak}/{confirm_n}회) "
                                    f"— 연속 확인 후에만 제거"
                                )
                            except Exception:
                                pass
                        continue

                    pos = self.virtual_positions[code]
                    if not force_reconcile_zero and self._should_skip_reconcile_remove(pos):
                        if not silent:
                            try:
                                self.log(
                                    f"[Sync] {pos.get('name', code)}({code}) "
                                    f"잔고 API 미반영 가능 — 유예 중(재동기화 대기)"
                                )
                            except Exception:
                                pass
                        continue
                    # [버그픽스] 매도 주문이 실제 진행 중(exit_pending + pending 스냅샷 존재)인데
                    # 잔고에서 잠시 사라진 경우, 여기서 독립적으로 SYNC_EXIT 이력을 만들면
                    # 이후 체결 확인 경로(_take_pending_exit_for_order → 실 SELL 기록)와 겹쳐
                    # 동일 라운드 종료내역이 이중 기록되고 손익이 왜곡된다.
                    # 따라서 진행 중 매도는 reconcile 제거를 보류하고 체결 경로가 처리하도록 둔다.
                    # (단, 대기 상한 초과 시 체결 누락 유령 방지를 위해 보류 해제)
                    if (
                        not force_reconcile_zero
                        and self._should_defer_reconcile_for_exit_pending(code, pos)
                    ):
                        if not silent:
                            try:
                                self.log(
                                    f"[Sync] {pos.get('name', code)}({code}) "
                                    f"매도 진행 중(미체결 대기) — reconcile 제거 보류"
                                )
                            except Exception:
                                pass
                        continue
                    # SYNCED 유령: 직전 실매도로 이미 종료됐는데 잔고에 잠깐 남아 생긴 포지션 — 종료 이력 미추가·로컬만 제거
                    try:
                        if (
                            str(pos.get("reason", "")).upper() == "SYNCED"
                            and self._recent_sell_exit_exists_for_code(code)
                        ):
                            if not silent:
                                try:
                                    self.log(
                                        f"[Sync] {pos.get('name', code)}({code}) "
                                        f"SYNCED 유령 포지션 제거 — 최근 실매도 종료 이력 있음, "
                                        f"매매종료내역(SYNC_EXIT) 미추가"
                                    )
                                except Exception:
                                    pass
                            self.virtual_positions.pop(code, None)
                            self.data_manager.delete_active_position(code)
                            try:
                                self._reconcile_server_missing_streak.pop(nc, None)
                            except Exception:
                                pass
                            removed_count += 1
                            continue
                    except Exception:
                        pass
                    curr_p = self._to_f(pos.get('current_price', pos.get('avg_price', 0)))
                    avg_p = self._to_f(pos.get('avg_price', 0))
                    
                    pnl_pct = (curr_p / avg_p - 1) * 100 if avg_p != 0 else 0
                    pnl_amt = (curr_p - avg_p) * self._to_i(pos.get('qty', 0))
                    
                    _sync_ex = datetime.now()
                    pos['events'].append({
                        "time": _sync_ex.strftime("%Y-%m-%d %H:%M:%S"),
                        "action": "SYNC_EXIT", "price": curr_p, "qty": pos.get('qty', 0),
                        "pnl": f"{pnl_pct:+.2f}%", "reason": "RECONCILIATION"
                    })
                    pos['final_pnl_pct'] = pnl_pct
                    pos['realized_pnl_amt'] = pnl_amt
                    pos['exit_price'] = curr_p
                    _ex = _sync_ex
                    pos["exit_date"] = _ex.strftime("%Y-%m-%d")
                    pos["exit_time"] = _ex.strftime("%H:%M:%S")
                    pos["exit_datetime"] = _ex.strftime("%Y-%m-%d %H:%M:%S")
                    pos['code'] = code
                    self.virtual_history.append(pos)
                    
                    self.virtual_positions.pop(code, None)
                    self.data_manager.delete_active_position(code)
                    try:
                        self._reconcile_server_missing_streak.pop(nc, None)
                    except Exception:
                        pass
                    removed_count += 1
                    self.log(f"[Sync] {pos['name']}({code}) 종목이 서버에서 사라져 이력으로 이동 (Reconciled)")

            self._dedupe_all_vp_keys()

        self.save_db()
        if schedule_gui_refresh:
            self._schedule_portfolio_ui_refresh(light=bool(silent))

        ambiguous_zh = False
        try:
            _patched_sell_rows: List[Dict[str, Any]] = []
            n_sell_fix = int(
                self.data_manager.patch_market_sell_success_when_not_in_holdings(
                    set(server_codes),
                    ambiguous_zero_holdings=ambiguous_zh,
                    patched_rows_out=_patched_sell_rows,
                )
            )
            self._apply_patched_sell_rows_to_positions(
                _patched_sell_rows, silent=silent
            )
            if n_sell_fix > 0:
                if not silent:
                    try:
                        self.log(
                            f"ℹ️ [Sync] 매도 접수(SUCCESS) {n_sell_fix}건을 체결(FILLED)로 보정 "
                            f"(잔고에 해당 종목 없음·시장가/지정가 공통)"
                        )
                    except Exception:
                        pass
                if schedule_orders_refresh:
                    try:
                        app_ref2 = getattr(self, "app", None)
                        if app_ref2 is not None and hasattr(
                            app_ref2, "request_virtual_tab_orders_refresh"
                        ):
                            app_ref2.request_virtual_tab_orders_refresh()
                    except Exception:
                        pass
        except Exception as e:
            try:
                self.log(f"⚠️ [Sync] 매도 접수→FILLED 보정 경로 실패: {e}")
            except Exception:
                pass

        # 증권사 체결내역 REST(kt00007) → 주문 로그 DB 보강 (백그라운드 — API 다건·연속조회 지연 방지)
        # 체결 직후 silent 동기화에서는 이미 kt00007 병합이 돌았을 수 있어 기본 생략(UI·REST 폭주 방지).
        try:
            app_ref = getattr(self, "app", None)
            run_bg_fills = True
            if silent:
                try:
                    st = self.settings or {}
                    run_bg_fills = bool(st.get("sync_run_kt00007_on_silent_sync", False))
                except Exception:
                    run_bg_fills = False
            if app_ref is not None and run_bg_fills:
                om = getattr(app_ref, "order_manager", None)
                dm = getattr(app_ref, "data_manager", None)
                if om is not None and dm is not None:
                    st = self.settings or {}
                    _cap_sc = set(server_codes)
                    try:
                        _cap_amb = bool(ambiguous_zh)
                    except Exception:
                        _cap_amb = False

                    def _bg_exchange_fills() -> None:
                        try:
                            try:
                                lb = int(
                                    float(st.get("exchange_fill_sync_lookback_days", 7))
                                )
                            except (TypeError, ValueError):
                                lb = 7
                            lb = max(1, min(lb, 60))
                            n = om.sync_exchange_fills_into_database(
                                dm,
                                lookback_days=lb,
                                silent=silent,
                                logger=self.log,
                            )
                            if n and not silent:
                                self.log(
                                    f"ℹ️ [동기화] 증권사 체결 {n}건을 주문 로그에 반영했습니다."
                                )
                        except Exception as e:
                            if not silent:
                                try:
                                    self.log(f"⚠️ [동기화] 체결내역 API 보강 실패: {e}")
                                except Exception:
                                    pass
                        try:
                            _rows2: List[Dict[str, Any]] = []
                            n2 = int(
                                dm.patch_market_sell_success_when_not_in_holdings(
                                    _cap_sc,
                                    ambiguous_zero_holdings=_cap_amb,
                                    patched_rows_out=_rows2,
                                )
                            )
                            self._apply_patched_sell_rows_to_positions(
                                _rows2, silent=silent
                            )
                            if n2 and not silent:
                                self.log(
                                    f"ℹ️ [동기화] 매도 접수(SUCCESS) {n2}건을 체결(FILLED)로 보정 "
                                    f"(kt00007 이후 재확인)"
                                )
                        except Exception:
                            pass
                        if schedule_orders_refresh:
                            try:
                                if hasattr(
                                    app_ref, "request_virtual_tab_orders_refresh"
                                ):
                                    app_ref.request_virtual_tab_orders_refresh()
                            except Exception:
                                pass

                    threading.Thread(target=_bg_exchange_fills, daemon=True).start()
        except Exception as e:
            if not silent:
                try:
                    self.log(f"⚠️ [동기화] 체결내역 스케줄 실패: {e}")
                except Exception:
                    pass

        # 총자산·예수·보유 상세는 OrderManager 요약 1회에 포함됨. 자동 동기화(silent) 시 로그 중복 방지.
        if not silent:
            self.log(
                f"[Portfolio] 로컬 포지션 반영: 갱신 {updated_count}, 신규 {new_count}, 제거 {removed_count}"
            )

    def _apply_patched_sell_rows_to_positions(
        self,
        patched_rows: List[Dict[str, Any]],
        *,
        silent: bool,
    ) -> None:
        """
        잔고에 없는데 DB만 FILLED 로 승격된 매도 행에 대해, 로컬 보유가 남아 있으면
        ``on_fill`` 로 청산·종료내역을 맞춥니다.
        """
        if not patched_rows:
            return
        for _pr in patched_rows:
            try:
                nk = str((_pr or {}).get("code") or "").strip().zfill(6)
                if not nk or nk == "000000":
                    continue
                if nk not in self.virtual_positions:
                    continue
                _pq = int((_pr or {}).get("qty") or 0)
                _pp = float((_pr or {}).get("price") or 0)
                _oid = str((_pr or {}).get("order_no") or "").strip()
                if _pq <= 0:
                    continue
                _px = _pp if _pp > 0 else float(
                    self.virtual_positions[nk].get("avg_price") or 0
                )
                self.on_fill(
                    nk,
                    "sell",
                    _px,
                    _pq,
                    name=str((_pr or {}).get("name") or ""),
                    reason=str((_pr or {}).get("reason") or ""),
                    fill_channel="exchange",
                    order_no=_oid or None,
                )
            except Exception as _pf_e:
                if not silent:
                    try:
                        self.log(
                            f"⚠️ [Sync] 잔고없음 매도 FILLED 보정 후 on_fill 실패: {_pf_e}"
                        )
                    except Exception:
                        pass

    def sync_deposit(self):
        """[v4.5.3] 예수금상세에서 출금가능금액만 조회해 actual_deposit 갱신(주문가능 아님)."""
        try:
            d = self.order_manager.get_withdrawable_deposit()
        except Exception:
            d = None
        if d is not None:
            self.actual_deposit = float(d)
            self._schedule_gui_callback()
            return d
        return None

    def _effective_sizing_capital(self) -> float:
        """
        % 비중 계산에 쓰이는 기준 자산.
        ka10085 tot_evlu_amt(증권사 총평가) → total_equity → 주문가능 순.
        """
        try:
            raw = float(getattr(self, "last_tot_evlu_amt", 0) or 0)
            if raw > 0:
                return raw
            te = float(self.total_equity or 0)
            if te > 0:
                return te
            dep = float(self.actual_deposit or 0)
            if dep > 0:
                return dep
        except Exception:
            pass
        return 0.0

    def _buy_fee_rate_decimal(self) -> float:
        try:
            buy_r, _ = fee_decimal_rates_live_trading(self.settings)
            return float(buy_r)
        except Exception:
            return 0.0

    def estimate_buy_cash_outlay(self, qty: int, unit_price: float) -> float:
        """매수 체결 시 D+2 에서 빠질 추정 금액(매수대금+매수수수료)."""
        try:
            q = int(qty)
            p = float(unit_price)
            if q <= 0 or p <= 0:
                return 0.0
            return p * float(q) * (1.0 + self._buy_fee_rate_decimal())
        except Exception:
            return 0.0

    def _d2_pending_buy_reserve_total(self) -> float:
        try:
            with self.lock:
                return float(
                    sum(float(v or 0) for v in self._d2_pending_buy_reserve.values())
                )
        except Exception:
            return 0.0

    def _effective_d2_cash_for_buy(self) -> float:
        """미체결 매수 예약을 차감한 D+2 가용 현금."""
        try:
            d2 = float(getattr(self, "d2_deposit", 0) or 0)
            reserved = self._d2_pending_buy_reserve_total()
            return max(0.0, d2 - reserved)
        except Exception:
            return 0.0

    def reserve_d2_for_buy_order(self, order_no: str, cash_outlay: float) -> None:
        """매수 주문 접수 직후 D+2 가용 한도에서 예약(중복 매수 방지)."""
        try:
            oid = str(order_no or "").strip()
            amt = max(0.0, float(cash_outlay))
            if not oid or amt <= 0:
                return
            with self.lock:
                self._d2_pending_buy_reserve[oid] = amt
        except Exception:
            pass

    def release_d2_buy_reserve(self, order_no: str) -> None:
        """체결·취소·타임아웃 시 예약 해제."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return
            with self.lock:
                self._d2_pending_buy_reserve.pop(oid, None)
        except Exception:
            pass

    def max_buy_qty_for_d2(self, unit_price: float) -> int:
        """D+2 가용(미체결 예약 차감) 기준 최대 매수 가능 주수."""
        try:
            p = float(unit_price)
            if p <= 0:
                return 0
            eff = self._effective_d2_cash_for_buy()
            if eff <= 0:
                return 0
            den = 1.0 + self._buy_fee_rate_decimal()
            if den <= 1e-12:
                return 0
            max_principal = eff / den
            return int(max_principal // p)
        except Exception:
            return 0

    def _max_buy_principal_cash_d2_limit(self) -> int:
        """
        현금매수 전용 상한(원): D+2 예수금 범위에서만 매수(신용·주문가능 별도 한도 미사용).
        체결금액 P·수수료율 r일 때 총지출 = P*(1+r) ≤ D+2 → P ≤ D+2/(1+r).
        D+2 ≤ 0 이면 추가 매수 불가(0).
        미체결 매수 주문 예약액은 가용 D+2 에서 선차감한다.
        """
        try:
            eff = self._effective_d2_cash_for_buy()
            if eff <= 0:
                return 0
            den = 1.0 + self._buy_fee_rate_decimal()
            if den <= 1e-12:
                return 0
            return int(max(0.0, eff / den))
        except Exception:
            return 0

    def calculate_order_quantity(self, stage=1, is_simulation=False):
        """설정창의 총자산 대비 % 비중에 따른 투자금액 산출 (실전/시뮬레이션 로직 분리)."""
        s = self.settings
        try:
            pct = float(s.get(f's{stage}_equity_pct', 10.0))
        except (TypeError, ValueError):
            pct = 10.0
        # [v18.0] Separation of Logic: Real-time order vs Simulation
        if is_simulation:
            # 백테스팅/가상매매는 설정창의 기반 자산을 사용
            try:
                total_cap = float(
                    str(s.get('backtest_initial_capital', 10000000)).replace(',', '')
                )
            except (TypeError, ValueError):
                total_cap = 10_000_000.0
        else:
            # 실전/모의: 잔고 API(총평가) 기준으로 동기화된 self.total_equity / last_tot_evlu_amt
            total_cap = self._effective_sizing_capital()
            if total_cap <= 0:
                self.log(
                    "❌ [Risk] 실제 계좌 자산 정보를 가져오지 못해 주문을 발송할 수 없습니다. "
                    "(동기화·계좌 조회를 확인하세요)"
                )
                return 0
        invest_amt = int(total_cap * pct / 100.0)
        # 실전/모의 실주문: D+2 예수 한도 내에서만 매수액+매수수수료 지불(증권사 주문가능·신용 한도는 사용하지 않음)
        if not is_simulation:
            try:
                cap = self._max_buy_principal_cash_d2_limit()
                if cap <= 0:
                    try:
                        d2v = self._effective_d2_cash_for_buy()
                        reserved = self._d2_pending_buy_reserve_total()
                        if invest_amt > 0:
                            extra = (
                                f", 미체결예약 {reserved:,.0f}원"
                                if reserved > 0
                                else ""
                            )
                            self.log(
                                "❌ [Risk] D+2 예수금 한도 내 현금 매수 불가 "
                                f"(가용 D+2={d2v:,.0f}원{extra}). "
                                "계좌 동기화·입금을 확인하세요."
                            )
                    except Exception:
                        if invest_amt > 0:
                            self.log(
                                "❌ [Risk] D+2 예수금 한도 내 현금 매수 불가. 계좌 동기화를 확인하세요."
                            )
                    return 0
                before_amt = int(invest_amt)
                invest_amt = min(int(invest_amt), cap)
                if before_amt > invest_amt > 0:
                    sig: Tuple[int, int] = (before_amt, int(invest_amt))
                    now = time.time()
                    if (
                        sig != getattr(self, "_d2_shrink_log_sig", None)
                        or now - float(getattr(self, "_d2_shrink_log_ts", 0.0) or 0.0)
                        >= 180.0
                    ):
                        self._d2_shrink_log_sig = sig
                        self._d2_shrink_log_ts = now
                        self.log(
                            f"ℹ️ [Risk] 투자금액을 D+2 가용 한도로 축소합니다: "
                            f"{before_amt:,} → {invest_amt:,}원 "
                            f"(매수대금+매수수수료 ≤ 가용 D+2)"
                        )
            except Exception:
                pass
        return invest_amt

    def capture_snapshot(self, code, price, reason="N/A"):
        """[v18.0] 현재 시점의 시장지수 및 보조지표 상황 정밀 캡처 (지식백과 반영)"""
        if not hasattr(self, 'app') or not self.app: return {}
        
        info = self.app.monitor_info.get(code, {})
        snapshot = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price": price,
            "market_trend": "N/A",
            "vol_power": info.get('vol_power', 100.0),
            "reason": reason,
            "rsi": 0.0, "macd": 0.0, "osc": 0.0, "adx": 0.0,
            "ma_alignment": "N/A", "bid_ask_ratio": 0.0
        }
        
        # 호가 잔량
        total_ask = info.get('total_ask', 0)
        total_bid = info.get('total_bid', 1)
        if total_ask > 0: snapshot["bid_ask_ratio"] = round(total_bid / total_ask, 2)

        # 지표 데이터
        if code in self.app.bars_data and not self.app.bars_data[code].empty:
            ind = self.app.bars_data[code].iloc[-1]
            snapshot["rsi"] = round(float(ind.get('RSI', 0)), 1)
            snapshot["macd"] = round(float(ind.get('MACD', 0)), 2)
            snapshot["osc"] = round(float(ind.get('OSC', 0)), 2)
            snapshot["adx"] = round(float(ind.get('ADX', 0)), 1)
            
            ma5, ma20, ma60 = ind.get('MA5', 0), ind.get('MA20', 0), ind.get('MA60', 0)
            if ma5 > ma20 > ma60: snapshot["ma_alignment"] = "정배열(Bull)"
            elif ma5 < ma20 < ma60: snapshot["ma_alignment"] = "역배열(Bear)"
            else: snapshot["ma_alignment"] = "혼조(Neutral)"
            
        return snapshot

    def _ensure_fill_snapshot(
        self,
        code: str,
        price: float,
        snapshot: Optional[Dict[str, Any]],
        reason_fallback: str,
    ) -> Dict[str, Any]:
        """체결 이벤트에 항상 스냅샷 dict를 붙입니다. 없으면 즉시 캡처(실패 시 빈 dict)."""
        try:
            if isinstance(snapshot, dict) and snapshot:
                return snapshot
        except Exception:
            pass
        try:
            sn = self.capture_snapshot(code, float(price), str(reason_fallback or "CHE"))
            return sn if isinstance(sn, dict) else {}
        except Exception:
            return {}

    def _to_f(self, v: Any) -> float:
        try:
            if isinstance(v, list):
                return 0.0
            if isinstance(v, str):
                v = v.replace(",", "").replace("+", "").strip()
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    def _to_i(self, v: Any) -> int:
        try:
            if isinstance(v, list):
                return 0
            if isinstance(v, str):
                v = v.replace(",", "").replace("+", "").strip()
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    def _prune_stale_pending_exit_snapshots(self) -> None:
        """오래된 매도 대기 스냅샷 제거(재시작 후 쌓임 방지)."""
        try:
            cutoff = datetime.now() - timedelta(days=14)
            to_del: List[str] = []
            for k, v in list(self._pending_exit_snapshots.items()):
                try:
                    ra = str((v or {}).get("registered_at") or "")
                    if not ra:
                        continue
                    dt = datetime.strptime(ra[:19], "%Y-%m-%dT%H:%M:%S")
                    if dt < cutoff:
                        to_del.append(k)
                except Exception:
                    continue
            for k in to_del:
                self._pending_exit_snapshots.pop(k, None)
            if len(self._pending_exit_snapshots) > 80:
                items = sorted(
                    self._pending_exit_snapshots.items(),
                    key=lambda x: str((x[1] or {}).get("registered_at") or ""),
                )
                for k, _ in items[: max(0, len(items) - 60)]:
                    self._pending_exit_snapshots.pop(k, None)
        except Exception:
            pass

    def register_pending_exit_snapshot(
        self,
        order_no: str,
        code: str,
        pos: Dict[str, Any],
        exit_reason: str,
    ) -> None:
        """
        실매도 주문 접수 성공 직후 호출: 포지션 전체를 주문번호에 묶어 저장합니다.
        이후 계좌 동기화로 로컬 보유가 지워져도 체결 시 종료 내역을 복원할 수 있습니다.
        """
        try:
            oid = str(order_no or "").strip()
            if not oid or not isinstance(pos, dict):
                return
            nc = self._normalize_stock_code(code)
            if not nc:
                return
            with self.lock:
                self._pending_exit_snapshots[oid] = {
                    "code": nc,
                    "exit_reason": str(exit_reason or ""),
                    "registered_at": datetime.now().isoformat(),
                    "pos": copy.deepcopy(pos),
                }
                self._prune_stale_pending_exit_snapshots()
            self.save_db()
        except Exception as e:
            try:
                self.log(f"⚠️ [Portfolio] 매도 대기 스냅샷 저장 실패: {e}")
            except Exception:
                pass

    def _take_pending_exit_for_order(self, order_no: str) -> Optional[Dict[str, Any]]:
        """주문번호에 해당하는 매도 스냅샷을 꺼내고 저장소에서 제거합니다."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self.lock:
                rec = self._pending_exit_snapshots.pop(oid, None)
            if isinstance(rec, dict):
                self.save_db()
                return rec
        except Exception:
            pass
        return None

    def _pending_exit_newest_age_seconds_for_code(self, code: str) -> Optional[float]:
        """해당 종목 pending exit 스냅샷 중 가장 최근 registered_at 기준 경과 초."""
        try:
            nc = self._normalize_stock_code(code)
            if not nc:
                return None
            now = datetime.now()
            ages: List[float] = []
            with self.lock:
                for rec in self._pending_exit_snapshots.values():
                    if not isinstance(rec, dict):
                        continue
                    if self._normalize_stock_code(rec.get("code")) != nc:
                        continue
                    raw = rec.get("registered_at")
                    if not raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(raw))
                    except Exception:
                        continue
                    ages.append(max(0.0, (now - ts).total_seconds()))
            if not ages:
                return None
            # 가장 최근 스냅샷 경과(초) — 전부 한도 초과일 때만 보류 해제
            return float(min(ages))
        except Exception:
            return None

    def _should_defer_reconcile_for_exit_pending(
        self, code: str, pos: Dict[str, Any]
    ) -> bool:
        """
        True: 매도 진행 중이라 reconcile 제거를 보류.
        최신 스냅샷 등록 후 N초(기본 120) 초과면 False — 체결 확인 누락 유령 방지.
        """
        try:
            if not bool(pos.get("exit_pending")):
                return False
            if not self._has_pending_exit_snapshot_for_code(code):
                return False
            max_s = 120
            try:
                st = getattr(self.app, "settings", None) or {}
                max_s = int(float(st.get("reconcile_exit_pending_max_seconds", 120)))
                max_s = max(30, min(max_s, 900))
            except Exception:
                max_s = 120
            age = self._pending_exit_newest_age_seconds_for_code(code)
            if age is not None and age >= float(max_s):
                try:
                    self.log(
                        f"[Sync] {pos.get('name', code)}({code}) "
                        f"매도 대기 {int(age)}초 초과(한도 {max_s}s) — "
                        f"reconcile 제거 진행"
                    )
                except Exception:
                    pass
                return False
            return True
        except Exception:
            return bool(pos.get("exit_pending")) and self._has_pending_exit_snapshot_for_code(
                code
            )

    def _has_pending_exit_snapshot_for_code(self, code: str) -> bool:
        """해당 종목코드로 진행 중(접수 후 미체결) 매도 스냅샷이 있는지 확인합니다."""
        try:
            nc = self._normalize_stock_code(code)
            if not nc:
                return False
            with self.lock:
                for rec in self._pending_exit_snapshots.values():
                    if not isinstance(rec, dict):
                        continue
                    if self._normalize_stock_code(rec.get("code")) == nc:
                        return True
        except Exception:
            pass
        return False

    def _discard_pending_exit_snapshot(self, order_no: str) -> None:
        """정상 청산 완료 등으로 더 이상 필요 없는 대기 스냅샷을 제거합니다."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return
            with self.lock:
                self._pending_exit_snapshots.pop(oid, None)
            self.save_db()
        except Exception:
            pass

    def _finalize_sell_close_to_history(
        self,
        pos: Dict[str, Any],
        code: str,
        p_val: float,
        exit_reason: str,
        snapshot: Optional[Dict[str, Any]],
        sell_r: float,
        now_str: str,
        *,
        remove_from_vp: bool,
    ) -> None:
        """매도 체결 1건을 종료 이력으로 확정합니다 (보유 dict 또는 접수 스냅샷 공통)."""
        if not isinstance(pos.get("events"), list):
            pos["events"] = []
        pos["exit_reason"] = exit_reason
        avg_p = self._to_f(pos.get("avg_price", 0.0))
        qty_val = self._to_i(pos.get("qty", 0))
        sell_fee = p_val * qty_val * sell_r
        net_proceeds = p_val * qty_val - sell_fee
        cost_basis = pos.get("invested", avg_p * qty_val)
        pnl_amt = net_proceeds - cost_basis
        pnl_pct = (pnl_amt / cost_basis) * 100 if cost_basis != 0 else 0
        pos["final_pnl_pct"] = pnl_pct
        pos["realized_pnl_amt"] = pnl_amt
        pos["exit_price"] = p_val
        _ex = datetime.now()
        pos["exit_date"] = _ex.strftime("%Y-%m-%d")
        pos["exit_time"] = _ex.strftime("%H:%M:%S")
        pos["exit_datetime"] = _ex.strftime("%Y-%m-%d %H:%M:%S")
        pos["code"] = code
        _snap_ex = self._ensure_fill_snapshot(
            code, p_val, snapshot, str(exit_reason)
        )
        try:
            rsnap = str((_snap_ex or {}).get("reason", "") or "")
            if rsnap.upper().startswith("REST_") or rsnap.upper().startswith(
                "EXEC_"
            ):
                _snap_ex["reason"] = str(exit_reason)
        except Exception:
            pass
        pos["events"].append(
            {
                "time": now_str,
                "action": "SELL",
                "price": p_val,
                "qty": qty_val,
                "snapshot": _snap_ex,
            }
        )
        self.virtual_history.append(pos)
        if remove_from_vp:
            self.virtual_positions.pop(code, None)
        try:
            self.data_manager.delete_active_position(code)
        except Exception:
            pass
        self.log(f"[Portfolio] {pos['name']}({code}) 최종 청산 완료 ({pnl_pct:+.2f}%)")
        if remove_from_vp:
            try:
                self._notify_holdings_ui_cleared(code)
            except Exception:
                pass
        # 텔레그램은 FillReconciliationService에서 '포지션관리탭 주문·체결 내역 row' 기준으로
        # 매수/매도 1회 전송 정책에 맞춰 발송됩니다.

    def _notify_holdings_ui_cleared(self, code: str) -> None:
        """전량 매도 후 모니터 보유 디바운스·유령 행을 즉시 정리합니다."""
        try:
            nc = self._normalize_stock_code(code)
        except Exception:
            try:
                nc = str(code).split("_")[0].zfill(6)
            except Exception:
                nc = ""
        if not nc:
            return

        def _job() -> None:
            try:
                mt = getattr(self.app, "monitor_tab", None)
                if mt is not None and hasattr(mt, "clear_holdings_ui_debounce"):
                    mt.clear_holdings_ui_debounce(nc)
            except Exception:
                pass
            try:
                fn = getattr(self.app, "request_virtual_tab_sync_light_refresh", None)
                if callable(fn):
                    fn()
            except Exception:
                pass

        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(0, _job)
            else:
                _job()
        except Exception:
            try:
                _job()
            except Exception:
                pass

    def on_fill(
        self,
        code,
        side,
        price,
        qty,
        name=None,
        reason=None,
        snapshot=None,
        fill_channel: str = "virtual",
        order_no: Optional[str] = None,
    ) -> bool:
        """[v18.0] 체결(CHE) 데이터를 처리하여 포지션을 업데이트하며, 시점 스냅샷을 함께 기록합니다.
        fill_channel: \"virtual\" | \"exchange\" — 일별 세션 로그 구분용.

        Returns
        -------
        bool
            True: 포지션·히스토리 반영 후 save_db·GUI 스케줄까지 진행.
            False: 매도인데 로컬 보유가 없어 스킵(이중 체결·동기화 선행 등) — 호출부에서 주문번호 중복 마킹 생략.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            buy_r, sell_r = fee_decimal_rates_live_trading(self.settings)
        except Exception:
            try:
                buy_r, sell_r = fee_decimal_rates_live_trading({})
            except Exception:
                buy_r, sell_r = 0.0035, 0.005
        try:
            code = self._normalize_stock_code(code)
        except Exception:
            code = str(code).split("_")[0].zfill(6)
        # 매도 체결이 REST(선체결)·웹소켓 CHE에서 각각 오거나 주문번호가 비어 중복 방지에 실패한 경우,
        # 두 번째 매도는 이미 포지션이 없어 매매종료내역에 동일 라운드가 2번 찍힐 수 있음 → 스킵.
        skipped_sell_no_position: bool = False
        with self.lock:
            self._merge_vp_alias_to_canonical(code)
            if side == "buy":
                # 진입 혹은 추매
                if code not in self.virtual_positions:
                    # 신규 진입 (Stage 1)
                    meta = self.data_manager.get_latest_order_meta(code)
                    reg_name = name or (meta[0] if meta else "Unknown")
                    reg_reason = reason or (meta[1] if meta else "SIGNAL")
                    # Type B 판별: evaluate_position·S2·균형게이트가 sig_type 키를 우선 참조
                    try:
                        _sig_type = (
                            "TYPE_B" if "TYPE_B" in str(reg_reason).upper() else "TYPE_A"
                        )
                    except Exception:
                        _sig_type = "TYPE_A"
                    
                    fp = self._to_f(price)
                    iq = self._to_i(qty)
                    
                    fee_amt = fp * iq * buy_r
                    total_invested = fp * iq + fee_amt
                    
                    self.virtual_positions[code] = {
                        "name": reg_name, "avg_price": fp, "qty": iq,
                        "stage": 1, "invested": total_invested,
                        "entry_time": datetime.now().isoformat(),
                        "reason": reg_reason,
                        "sig_type": _sig_type,
                        "events": [], "max_now": fp
                    }
                    _snap_s1 = self._ensure_fill_snapshot(code, fp, snapshot, str(reg_reason))
                    self.virtual_positions[code]["events"].append(
                        {
                            "time": now_str,
                            "action": "BUY_S1",
                            "price": fp,
                            "qty": iq,
                            "snapshot": _snap_s1,
                        }
                    )
                    self.data_manager.save_active_position(code, reg_name, fp, iq, 1, total_invested, datetime.now().isoformat(), reg_reason)
                    _ord_sfx = f" ord={order_no}" if order_no else ""
                    self.log(
                        f"[Portfolio] {reg_name}({code}) S1 진입 · {iq}주 @ {int(fp):,}원{_ord_sfx}"
                    )
                    # 텔레그램 발송은 체결 DB(row) 확정 시점으로 중앙화되어 있습니다.
                else:
                    pos = self.virtual_positions[code]
                    synced_ph = self._is_synced_placeholder_position(pos)
                    has_buy_ev = self._has_confirmed_buy_events(pos)
                    is_s2_fill = self._resolve_fill_is_s2(code, reason, order_no)
                    eff_stage = self.position_effective_stage(pos)

                    # 이미 추매 완료(stage>=2)인데 S2 체결·주문이 또 오면 수량 중복 반영 방지
                    if is_s2_fill and eff_stage >= 2:
                        _ord_sfx = f" ord={order_no}" if order_no else ""
                        self.log(
                            f"⚠️ [Portfolio] {pos.get('name', code)}({code}) "
                            f"S2 중복 체결 생략 — 이미 추매 완료(stage={eff_stage}){_ord_sfx}"
                        )
                    elif is_s2_fill:
                        # S2 추매 — events 없음(DB 복구)·잔고 선반영이어도 S1 리셋하지 않음
                        s2_handled = False
                        self._ensure_s1_event_before_s2_fill(
                            pos,
                            fill_qty=self._to_i(qty),
                            now_str=now_str,
                            code=code,
                            snapshot=snapshot,
                            reg_reason=str(
                                reason or pos.get("reason") or "Stage 2 Scaling"
                            ),
                        )
                        cur_q = self._to_i(pos.get("qty", 0))
                        fill_q = self._to_i(qty)
                        # ka10085 선반영으로 수량만 먼저 늘었으면 stage·이벤트만 보정
                        if (
                            cur_q >= fill_q
                            and not self._position_has_buy_s2_event(pos)
                            and self._has_confirmed_buy_events(pos)
                        ):
                            try:
                                _ev_q = self._cost_basis_from_buy_events(pos)
                                _leg_q = int(_ev_q[2]) if _ev_q is not None else 0
                            except Exception:
                                _leg_q = 0
                            if _leg_q > 0 and cur_q >= _leg_q + fill_q:
                                _fp = self._to_f(price)
                                self._finalize_s2_fill_state(
                                    pos,
                                    fill_price=_fp,
                                    fill_qty=fill_q,
                                    now_str=now_str,
                                )
                                self.data_manager.save_active_position(
                                    code,
                                    pos["name"],
                                    float(pos.get("avg_price", 0)),
                                    cur_q,
                                    2,
                                    float(pos.get("invested", 0) or 0),
                                    pos.get("entry_time"),
                                    pos.get("reason"),
                                )
                                _ord_sfx = f" ord={order_no}" if order_no else ""
                                self.log(
                                    f"[Portfolio] {pos['name']}({code}) S2 추매 확정 · "
                                    f"잔고 선반영 {cur_q}주 (stage→2){_ord_sfx}"
                                )
                                s2_handled = True

                        if not s2_handled:
                            old_qty = self._to_i(pos.get("qty", 0))
                            old_avg = self._to_f(pos.get("avg_price", 0.0))
                            new_total_qty = old_qty + self._to_i(qty)
                            new_avg_price = (
                                ((old_avg * old_qty) + (self._to_f(price) * self._to_i(qty)))
                                / new_total_qty
                                if new_total_qty > 0
                                else self._to_f(price)
                            )

                            pos["qty"] = new_total_qty
                            pos["avg_price"] = new_avg_price
                            _p2 = self._to_f(price)
                            _q2 = self._to_i(qty)
                            _snap_s2 = self._ensure_fill_snapshot(
                                code,
                                _p2,
                                snapshot,
                                str(reason or pos.get("reason") or "S2"),
                            )
                            pos.setdefault("events", []).append(
                                {
                                    "time": now_str,
                                    "action": "BUY_S2",
                                    "price": _p2,
                                    "qty": _q2,
                                    "snapshot": _snap_s2,
                                }
                            )
                            self._finalize_s2_fill_state(
                                pos,
                                fill_price=_p2,
                                fill_qty=_q2,
                                now_str=now_str,
                            )
                            _tup_ev = self._cost_basis_from_buy_events(pos)
                            if _tup_ev is not None:
                                _w_avg_ev, _inv_ev, _ev_q = _tup_ev
                                if _ev_q == new_total_qty:
                                    pos["avg_price"] = float(_w_avg_ev)
                                    pos["invested"] = float(_inv_ev)
                                else:
                                    _fee2 = _p2 * float(_q2) * buy_r
                                    pos["invested"] = float(
                                        pos.get("invested", 0) or 0
                                    ) + _p2 * float(_q2) + _fee2
                            else:
                                _fee2 = _p2 * float(_q2) * buy_r
                                pos["invested"] = float(
                                    pos.get("invested", 0) or 0
                                ) + _p2 * float(_q2) + _fee2

                            self.data_manager.save_active_position(
                                code,
                                pos["name"],
                                pos["avg_price"],
                                new_total_qty,
                                2,
                                pos["invested"],
                                pos["entry_time"],
                                pos["reason"],
                            )
                            _ord_sfx = f" ord={order_no}" if order_no else ""
                            self.log(
                                f"[Portfolio] {pos['name']}({code}) S2 추매 · "
                                f"+{_q2}주 (합계 {new_total_qty}주, 평단 {int(new_avg_price):,}원){_ord_sfx}"
                            )

                    # S1 첫 체결: SYNCED 선반영·이벤트 없음 — 수량 합산하지 않음
                    elif synced_ph or not has_buy_ev:
                        meta = self.data_manager.get_latest_order_meta(code)
                        reg_name = name or str(pos.get("name") or "") or (
                            meta[0] if meta else "Unknown"
                        )
                        reg_reason = reason or str(pos.get("reason") or "") or (
                            meta[1] if meta else "SIGNAL"
                        )
                        if str(reg_reason).upper() == "SYNCED":
                            reg_reason = str(
                                reason or (meta[1] if meta else "SIGNAL")
                            )
                        try:
                            _sig_type = (
                                "TYPE_B"
                                if "TYPE_B" in str(reg_reason).upper()
                                else "TYPE_A"
                            )
                        except Exception:
                            _sig_type = str(pos.get("sig_type") or "TYPE_A")
                        fp = self._to_f(price)
                        iq = self._to_i(qty)
                        fee_amt = fp * iq * buy_r
                        total_invested = fp * iq + fee_amt
                        _fill_et = datetime.now().isoformat()
                        pos["name"] = reg_name
                        pos["avg_price"] = fp
                        pos["qty"] = iq
                        pos["stage"] = 1
                        pos["invested"] = total_invested
                        pos["entry_time"] = _fill_et
                        pos["reason"] = reg_reason
                        pos["sig_type"] = _sig_type
                        try:
                            pos["max_now"] = max(
                                float(pos.get("max_now") or fp), fp
                            )
                        except (TypeError, ValueError):
                            pos["max_now"] = fp
                        _snap_s1 = self._ensure_fill_snapshot(
                            code, fp, snapshot, str(reg_reason)
                        )
                        pos["events"] = [
                            {
                                "time": now_str,
                                "action": "BUY_S1",
                                "price": fp,
                                "qty": iq,
                                "snapshot": _snap_s1,
                            }
                        ]
                        self.data_manager.save_active_position(
                            code,
                            reg_name,
                            fp,
                            iq,
                            1,
                            total_invested,
                            _fill_et,
                            reg_reason,
                        )
                        _ord_sfx = f" ord={order_no}" if order_no else ""
                        if synced_ph:
                            self.log(
                                f"[Portfolio] {reg_name}({code}) S1 체결 확정 · "
                                f"{iq}주 @ {int(fp):,}원 (잔고 선반영→주문 확정){_ord_sfx}"
                            )
                        else:
                            self.log(
                                f"[Portfolio] {reg_name}({code}) S1 진입 · "
                                f"{iq}주 @ {int(fp):,}원{_ord_sfx}"
                            )
                    else:
                        # 추가 매수 (Stage 2)
                        old_qty = self._to_i(pos.get('qty', 0))
                        old_avg = self._to_f(pos.get('avg_price', 0.0))
                        new_total_qty = old_qty + self._to_i(qty)
                        new_avg_price = ((old_avg * old_qty) + (self._to_f(price) * self._to_i(qty))) / new_total_qty if new_total_qty > 0 else self._to_f(price)

                        pos['qty'] = new_total_qty
                        pos['avg_price'] = new_avg_price
                        _p2 = self._to_f(price)
                        _q2 = self._to_i(qty)
                        _snap_s2 = self._ensure_fill_snapshot(
                            code, _p2, snapshot, str(reason or pos.get("reason") or "S2")
                        )
                        pos["events"].append(
                            {
                                "time": now_str,
                                "action": "BUY_S2",
                                "price": _p2,
                                "qty": _q2,
                                "snapshot": _snap_s2,
                            }
                        )
                        self._finalize_s2_fill_state(
                            pos,
                            fill_price=_p2,
                            fill_qty=_q2,
                            now_str=now_str,
                        )
                        # invested: S1과 동일하게 매수수수료 포함(이벤트 합산 우선, 실패 시 추매 다리만 수수료 반영)
                        _tup_ev = self._cost_basis_from_buy_events(pos)
                        if _tup_ev is not None:
                            _w_avg_ev, _inv_ev, _ev_q = _tup_ev
                            if _ev_q == new_total_qty:
                                pos["avg_price"] = float(_w_avg_ev)
                                pos["invested"] = float(_inv_ev)
                            else:
                                _fee2 = _p2 * float(_q2) * buy_r
                                pos["invested"] = float(pos.get("invested", 0) or 0) + _p2 * float(_q2) + _fee2
                        else:
                            _fee2 = _p2 * float(_q2) * buy_r
                            pos["invested"] = float(pos.get("invested", 0) or 0) + _p2 * float(_q2) + _fee2

                        # DB 동기화
                        self.data_manager.save_active_position(code, pos['name'], pos['avg_price'], new_total_qty, 2, pos['invested'], pos['entry_time'], pos['reason'])
                        _ord_sfx = f" ord={order_no}" if order_no else ""
                        self.log(
                            f"[Portfolio] {pos['name']}({code}) S2 추매 · "
                            f"+{_q2}주 (합계 {new_total_qty}주, 평단 {int(new_avg_price):,}원){_ord_sfx}"
                        )
                        # 텔레그램 발송은 체결 DB(row) 확정 시점으로 중앙화되어 있습니다.
            
            elif side == "sell":
                # 청산 — 보유 dict 우선, 없으면 매도 접수 시 저장한 스냅샷(주문번호)
                oid = ""
                try:
                    oid = str(order_no or "").strip()
                except Exception:
                    oid = ""
                pos: Optional[Dict[str, Any]] = None
                remove_from_vp = True
                pending_rec: Optional[Dict[str, Any]] = None

                if code in self.virtual_positions:
                    pos = self.virtual_positions[code]
                elif oid:
                    pending_rec = self._take_pending_exit_for_order(oid)
                    if pending_rec:
                        try:
                            pos = copy.deepcopy(pending_rec.get("pos") or {})
                        except Exception:
                            pos = pending_rec.get("pos") or {}
                        remove_from_vp = False
                        try:
                            self.log(
                                f"📎 [Portfolio] 매도 체결 — 접수 시 스냅샷으로 종료 확정 "
                                f"({code}, 주문 {oid})"
                            )
                        except Exception:
                            pass

                if pos is None:
                    skipped_sell_no_position = True
                else:
                    try:
                        er_order = str(reason or "").strip()
                    except Exception:
                        er_order = ""
                    try:
                        er_pend = str((pending_rec or {}).get("exit_reason") or "").strip()
                    except Exception:
                        er_pend = ""
                    try:
                        er_pos = str(pos.get("exit_reason") or "").strip()
                    except Exception:
                        er_pos = ""
                    exit_reason = er_order or er_pend or er_pos or "MANUAL"
                    p_val = self._to_f(price)

                    # 부분체결(부분매도) 처리:
                    # - pos['qty'](보유 잔량) <-> 체결 인자 qty(이번 체결량)가 다를 수 있음
                    # - 이 경우에는 보유를 제거(remove_from_vp)하지 말고 qty/invested만 감소시켜야 합니다.
                    sell_qty_val = 0
                    try:
                        sell_qty_val = self._to_i(qty)
                    except Exception:
                        sell_qty_val = 0
                    try:
                        cur_qty_val = self._to_i(pos.get("qty", 0))
                    except Exception:
                        cur_qty_val = 0

                    if (
                        sell_qty_val > 0
                        and cur_qty_val > 0
                        and sell_qty_val < cur_qty_val
                    ):
                        # 부분 청산: 종료내역(virtual_history) 확정/보유 제거는 하지 않고 잔량만 갱신
                        try:
                            if not isinstance(pos.get("events"), list):
                                pos["events"] = []
                        except Exception:
                            pos["events"] = []

                        avg_p = self._to_f(pos.get("avg_price", 0.0))
                        invested_total = self._to_f(
                            pos.get("invested", avg_p * cur_qty_val)
                        )
                        # invested 기반이면 비례로 비용을 차감(근사)
                        cost_basis_total = (
                            invested_total if invested_total > 0 else avg_p * cur_qty_val
                        )
                        cost_basis_sell = (
                            cost_basis_total * (sell_qty_val / cur_qty_val)
                            if cost_basis_total > 0
                            else avg_p * sell_qty_val
                        )

                        sell_fee = p_val * sell_qty_val * sell_r
                        net_proceeds = p_val * sell_qty_val - sell_fee
                        pnl_amt = net_proceeds - cost_basis_sell
                        pnl_pct = (
                            (pnl_amt / cost_basis_sell) * 100
                            if cost_basis_sell != 0
                            else 0.0
                        )

                        try:
                            _snap_ex = self._ensure_fill_snapshot(
                                code,
                                p_val,
                                snapshot,
                                str(exit_reason),
                            )
                        except Exception:
                            _snap_ex = snapshot

                        pos["events"].append(
                            {
                                "time": now_str,
                                "action": "SELL_PARTIAL",
                                "price": p_val,
                                "qty": sell_qty_val,
                                "snapshot": _snap_ex,
                                "final_pnl_pct": pnl_pct,
                                "realized_pnl_amt": pnl_amt,
                            }
                        )

                        # 잔량 업데이트
                        pos["qty"] = cur_qty_val - sell_qty_val
                        # 평균단가 유지: invested만 남은 비용으로 감소
                        pos["invested"] = max(
                            0.0, cost_basis_total - cost_basis_sell
                        )
                        pos["exit_reason"] = pos.get("exit_reason", "")

                        # DB 갱신(보유 유지)
                        try:
                            self.data_manager.save_active_position(
                                code,
                                pos.get("name", code),
                                pos.get("avg_price", avg_p),
                                pos.get("qty", cur_qty_val - sell_qty_val),
                                pos.get("stage", 1),
                                pos.get("invested", pos.get("invested", 0.0)),
                                pos.get("entry_time", datetime.now().isoformat()),
                                pos.get("reason", ""),
                            )
                        except Exception:
                            pass

                        # 부분체결이면 보유 제거/대기스냅샷 폐기는 하지 않습니다.
                        remove_from_vp = False
                    else:
                        # 완전 청산: 기존 종료 확정 로직 사용
                        self._finalize_sell_close_to_history(
                            pos,
                            code,
                            p_val,
                            exit_reason,
                            snapshot,
                            sell_r,
                            now_str,
                            remove_from_vp=remove_from_vp,
                        )
                        if remove_from_vp and oid:
                            self._discard_pending_exit_snapshot(oid)

        if skipped_sell_no_position:
            try:
                self.log(
                    f"⚠️ [Portfolio] 매도 체결 무시({code}) — 보유 포지션 없음 "
                    "(이미 청산·동기화됨, REST/CHE 이중 체결 가능성)."
                )
            except Exception:
                pass
            return False

        self.save_db()
        self._schedule_portfolio_ui_refresh(light=True)

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                _fd: Dict[str, Any] = {
                    "code": code,
                    "side": side,
                    "price": self._to_f(price),
                    "qty": self._to_i(qty),
                    "name": name or "",
                    "reason": reason or "",
                    "fill_channel": fill_channel,
                }
                try:
                    from core.supply_display import (
                        capture_supply_snapshot,
                        format_vol_power_column,
                    )

                    _sn = capture_supply_snapshot(self.app, code)
                    _fd["snap_vol_power"] = _sn.get("vol_power")
                    _fd["snap_vol_power_str"] = format_vol_power_column(
                        _sn.get("vol_power")
                    )
                    _fd["snap_bid_ask"] = _sn.get("bid_ask_text")
                except Exception:
                    _fd["snap_vol_power"] = None
                    _fd["snap_vol_power_str"] = "—"
                    _fd["snap_bid_ask"] = "— / — (—)"
                sl.log("fill", _fd)
        except Exception:
            pass

        return True

    def record_orphan_sell_close(
        self,
        code: str,
        price: float,
        qty: int,
        name: Optional[str],
        reason: Optional[str],
        order_no: str,
        snapshot: Optional[Dict[str, Any]],
        fill_channel: str = "exchange",
    ) -> bool:
        """
        로컬 보유(`virtual_positions`)가 없을 때도 거래소 매도 체결이 들어오면
        주문 로그와 매매종료내역이 어긋나지 않도록 종료 행을 보강합니다.
        (동기화 유령 제거·타 경로 매도 등으로 보유 레코드가 먼저 사라진 경우)
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        disp_name = ""
        exit_reason = "MANUAL"
        try:
            buy_r, sell_r = fee_decimal_rates_live_trading(self.settings)
        except Exception:
            try:
                buy_r, sell_r = fee_decimal_rates_live_trading({})
            except Exception:
                buy_r, sell_r = 0.0035, 0.005
        try:
            ncode = self._normalize_stock_code(code)
        except Exception:
            ncode = str(code).split("_")[0].zfill(6)
        oid = str(order_no or "").strip()
        pending_handled = False

        try:
            if self.is_redundant_late_exchange_sell_fill(ncode, oid):
                return False
        except Exception:
            pass

        with self.lock:
            self._merge_vp_alias_to_canonical(ncode)
            if not ncode:
                return False
            if ncode in self.virtual_positions:
                return False
            if oid and oid in self._orphan_sell_order_ids:
                return False

            p_val = self._to_f(price)
            qty_val = self._to_i(qty)
            if qty_val <= 0 or p_val <= 0:
                return False

            try:
                exit_reason = str(reason or "").strip()
            except Exception:
                exit_reason = ""
            if not exit_reason:
                exit_reason = "MANUAL"

            # 1) 접수 시 저장해 둔 스냅샷(주문번호) — on_fill 에서 못 쓴 경우에만 여기서 소비
            if oid:
                try:
                    pend = self._take_pending_exit_for_order(oid)
                except Exception:
                    pend = None
                if pend:
                    try:
                        pos = copy.deepcopy(pend.get("pos") or {})
                    except Exception:
                        pos = pend.get("pos") or {}
                    er = (
                        str(reason or "").strip()
                        or str(pend.get("exit_reason") or "").strip()
                        or "MANUAL"
                    )
                    self._finalize_sell_close_to_history(
                        pos,
                        ncode,
                        p_val,
                        er,
                        snapshot,
                        sell_r,
                        now_str,
                        remove_from_vp=False,
                    )
                    if oid:
                        self._orphan_sell_order_ids.add(oid)
                    try:
                        disp_name = str(pos.get("name") or "Unknown")
                    except Exception:
                        disp_name = "Unknown"
                    exit_reason = er
                    pending_handled = True

            if not pending_handled:
                buy_info: Optional[Dict[str, Any]] = None
                try:
                    buy_info = self.data_manager.get_last_successful_buy_for_code(ncode)
                except Exception:
                    buy_info = None

                if buy_info:
                    buy_px = self._to_f(buy_info.get("buy_price"))
                    bq = self._to_i(buy_info.get("qty"))
                    use_q = min(qty_val, bq) if bq > 0 else qty_val
                    if use_q <= 0:
                        use_q = qty_val
                    fee_buy_leg = buy_px * float(use_q) * buy_r
                    cost_basis = buy_px * float(use_q) + fee_buy_leg
                    if bq > 0 and qty_val > bq:
                        try:
                            self.log(
                                f"⚠️ [Portfolio] 종료내역 보강: 매도수량({qty_val})이 주문로그 매수수량({bq}) 초과 "
                                f"— 손익은 부분 원가만 반영({ncode})"
                            )
                        except Exception:
                            pass
                else:
                    buy_px = p_val
                    cost_basis = p_val * float(qty_val) * (1.0 + buy_r)
                    try:
                        self.log(
                            f"⚠️ [Portfolio] 종료내역 보강: 해당 종목 매수 로그 없음 — "
                            f"손익은 매도가 기준 근사({ncode})"
                        )
                    except Exception:
                        pass

                sell_fee = p_val * float(qty_val) * sell_r
                net_proceeds = p_val * float(qty_val) - sell_fee
                pnl_amt = net_proceeds - cost_basis
                pnl_pct = (pnl_amt / cost_basis) * 100 if cost_basis != 0 else 0.0

                try:
                    dn = str(name or "").strip()
                except Exception:
                    dn = ""
                if not dn and buy_info:
                    dn = str(buy_info.get("name") or "").strip()
                disp_name = dn or "Unknown"

                pos: Dict[str, Any] = {
                    "name": disp_name,
                    "avg_price": buy_px,
                    "qty": qty_val,
                    "stage": 1,
                    "invested": cost_basis,
                    "entry_time": str(buy_info.get("timestamp") if buy_info else "")
                    or datetime.now().isoformat(),
                    "reason": str(buy_info.get("reason") if buy_info else "ORPHAN_NO_LOCAL_POSITION"),
                    "sig_type": "TYPE_A",
                    "events": [],
                    "max_now": p_val,
                }
                if buy_info:
                    pos["events"].append(
                        {
                            "time": str(buy_info.get("timestamp") or now_str),
                            "action": "BUY_S1",
                            "price": buy_px,
                            "qty": use_q,
                            "snapshot": {},
                        }
                    )

                pos["exit_reason"] = exit_reason
                pos["final_pnl_pct"] = pnl_pct
                pos["realized_pnl_amt"] = pnl_amt
                pos["exit_price"] = p_val
                _ex = datetime.now()
                pos["exit_date"] = _ex.strftime("%Y-%m-%d")
                pos["exit_time"] = _ex.strftime("%H:%M:%S")
                pos["exit_datetime"] = _ex.strftime("%Y-%m-%d %H:%M:%S")
                pos["code"] = ncode

                _snap_ex = self._ensure_fill_snapshot(
                    ncode, p_val, snapshot, str(exit_reason)
                )
                try:
                    rsnap = str((_snap_ex or {}).get("reason", "") or "")
                    if rsnap.upper().startswith("REST_") or rsnap.upper().startswith(
                        "EXEC_"
                    ):
                        _snap_ex["reason"] = str(exit_reason)
                except Exception:
                    pass
                pos["events"].append(
                    {
                        "time": now_str,
                        "action": "SELL",
                        "price": p_val,
                        "qty": qty_val,
                        "snapshot": _snap_ex,
                    }
                )

                self.virtual_history.append(pos)
                if oid:
                    self._orphan_sell_order_ids.add(oid)

                try:
                    self.log(
                        f"📌 [Portfolio] 종료내역 보강(보유 없음 매도체결): {disp_name}({ncode}) "
                        f"{qty_val}주 @ {int(p_val):,}원 | 사유={exit_reason}"
                    )
                except Exception:
                    pass

        self.save_db()
        self._schedule_portfolio_ui_refresh(light=True)

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                _fd: Dict[str, Any] = {
                    "code": ncode,
                    "side": "sell",
                    "price": self._to_f(price),
                    "qty": self._to_i(qty),
                    "name": disp_name,
                    "reason": exit_reason,
                    "fill_channel": fill_channel,
                    "orphan_close": True,
                }
                try:
                    from core.supply_display import (
                        capture_supply_snapshot,
                        format_vol_power_column,
                    )

                    _sn = capture_supply_snapshot(self.app, ncode)
                    _fd["snap_vol_power"] = _sn.get("vol_power")
                    _fd["snap_vol_power_str"] = format_vol_power_column(
                        _sn.get("vol_power")
                    )
                    _fd["snap_bid_ask"] = _sn.get("bid_ask_text")
                except Exception:
                    _fd["snap_vol_power"] = None
                    _fd["snap_vol_power_str"] = "—"
                    _fd["snap_bid_ask"] = "— / — (—)"
                sl.log("fill", _fd)
        except Exception:
            pass

        return True

    def clear_all(self):
        """모든 로컬 포지션을 초기화합니다."""
        with self.lock:
            codes = list(self.virtual_positions.keys())
            for code in codes:
                self.data_manager.delete_active_position(code)
            self.virtual_positions = {}
            self._pending_exit_snapshots = {}
        self.save_db()
        self._schedule_gui_callback()

    def sell_all(self):
        """[v3.8/v4.5.4] 비상 상황: 모든 가상/실제 포지션 즉시 시장가 매도 청산"""
        with self.lock:
            codes = list(self.virtual_positions.keys())
            if not codes: return
            
            for code in codes:
                pos = self.virtual_positions[code]
                qty = pos.get('qty', 0)
                if qty <= 0: continue
                
                # [v3.8] 공격적 체결(매도)을 위한 보정가 산출 로직 유지
                info = self.app.monitor_info.get(code, {})
                curr_p = info.get('price', pos['avg_price'])
                
                # 실제 주문 발송 (허용된 경우)
                use_mock = getattr(config, 'USE_MOCK', False)
                allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
                if allow_order:
                    ord_no_em = self.order_manager.place_order(
                        code,
                        qty,
                        curr_p,
                        order_type="03",
                        side="sell",
                        name=str(pos.get("name", "")),
                        reason="EMERGENCY",
                        skip_price_guard=True,
                    )
                    if ord_no_em:
                        try:
                            self.register_pending_exit_snapshot(
                                str(ord_no_em).strip(),
                                code,
                                pos,
                                "EMERGENCY",
                            )
                        except Exception as ex:
                            self.log(f"⚠️ [Emergency] 매도 스냅샷 등록 실패: {ex}")
                
                # 가상 상태 업데이트 (즉시 삭제 대신 '매도 대기' 상태로 전환하여 체결 대기)
                pos['exit_pending'] = True
                pos['exit_reason'] = "EMERGENCY"
                
                self.log(f"🚨 [Emergency] {pos.get('name')}({code}) 비상 청산 주문 발송")
                
        self.save_db()
        self._schedule_gui_callback()
