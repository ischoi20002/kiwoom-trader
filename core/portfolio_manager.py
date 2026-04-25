import threading
import copy
import json
import os
import re
from datetime import datetime, timedelta
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
        # record_orphan_sell_close 동일 주문번호 중복 기록 방지
        self._orphan_sell_order_ids: Set[str] = set()
        # 매도 주문 접수 직후 포지션 전체 스냅샷(주문번호 키) — 동기화로 로컬 보유가 먼저 사라져도 체결 시 종료 확정
        self._pending_exit_snapshots: Dict[str, Dict[str, Any]] = {}

    def _virtual_db_path(self) -> str:
        """종료내역 JSON — config.USE_MOCK 에 따라 virtual_db_mock.json / virtual_db_live.json."""
        try:
            return virtual_db_path()
        except Exception:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            return os.path.join(root, "virtual_db_mock.json")

    def _schedule_gui_callback(self) -> None:
        """
        gui_callback(포지션 관리 탭 전체 갱신 등)은 체결·WS·백그라운드 스레드에서 트리거될 수 있습니다.
        Tkinter 위젯은 메인 스레드에서만 안전하므로 root.after(0)으로 넘깁니다.
        """
        if not self.gui_callback:
            return
        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(0, self._run_gui_callback_on_main)
            else:
                self.gui_callback()
        except Exception:
            try:
                self.gui_callback()
            except Exception as e:
                self.log(f"⚠️ [Portfolio] GUI 콜백 실패: {e}")

    def _run_gui_callback_on_main(self) -> None:
        try:
            if self.gui_callback:
                self.gui_callback()
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
                            self.virtual_positions[code] = {
                                "name": row[1], "avg_price": float(row[2]), "qty": int(row[3]),
                                "stage": int(row[4]), "invested": float(row[5]),
                                "entry_time": row[6], "reason": row[7],
                                "sig_type": _sig,
                                "events": [], "max_now": float(row[2])
                            }
                    else:
                        self.virtual_positions = active
                    self.log(f"[Portfolio] DB에서 {len(self.virtual_positions)}개의 활성 포지션 복구")
                    self._dedupe_all_vp_keys()
            except Exception as e:
                self.log(f"[Error] [Portfolio] DB 포지션 복구 실패: {e}")

    def save_db(self):
        """현재 상태를 파일에 저장합니다 (히스토리 위주)."""
        try:
            try:
                self._prune_stale_pending_exit_snapshots()
            except Exception:
                pass
            with open(self._virtual_db_path(), 'w', encoding='utf-8') as f:
                json.dump(
                    {
                        "history": self.virtual_history,
                        "pending_exit_snapshots": self._pending_exit_snapshots,
                    },
                    f,
                    ensure_ascii=False,
                    indent=4,
                )
        except Exception as e:
            self.log(f"[Error] [Portfolio] DB 저장 실패: {e}")

    def sync_with_actual_account(
        self,
        silent: bool = False,
        force: bool = False,
        *,
        log_boot_summary: bool = False,
        refresh_cash_deposit: bool = True,
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
                refresh_cash_deposit=refresh_cash_deposit
            )
        except Exception as gh_ex:
            self.log(f"[Error] [Sync] 계좌 조회 호출 실패: {gh_ex}")
            return
        if account_data is None: 
            self.log("[Error] [Sync] 계좌 정보를 실시간으로 가져올 수 없습니다. (혹은 API 429 제한)")
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
                    if self._recent_sell_exit_exists_for_code(
                        code, within_hours=nh
                    ):
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

            # 보유 API가 0건인데 로컬만 있으면: 응답 누락/지연 가능성이 커서 자동 제거하지 않음
            if len(holdings) == 0 and len(local_codes) > 0:
                if not silent:
                    self.log(
                        "[Sync] 보유 API 응답 0건 — 로컬 포지션 자동 제거 생략 "
                        "(전량 매도 후에는 수동 동기화 또는 다음 성공 응답을 확인하세요)"
                    )
            else:
                try:
                    confirm_n = int(
                        float(self.settings.get("reconcile_remove_confirm_syncs", 2))
                    )
                except (TypeError, ValueError):
                    confirm_n = 2
                confirm_n = max(1, min(confirm_n, 10))

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
                    # 보유 0건 분기에서는 위에서 제거 루프를 돌지 않음 — 여기서도 스킵
                    if len(holdings) == 0 and len(local_codes) > 0:
                        continue
                    nc = self._normalize_stock_code(code)
                    try:
                        confirm_n = int(
                            float(self.settings.get("reconcile_remove_confirm_syncs", 2))
                        )
                    except (TypeError, ValueError):
                        confirm_n = 2
                    confirm_n = max(1, min(confirm_n, 10))
                    if (
                        int(self._reconcile_server_missing_streak.get(nc, 0))
                        < confirm_n
                    ):
                        if not silent:
                            try:
                                self.log(
                                    f"[Sync] {code} 서버 목록 미포함 "
                                    f"({self._reconcile_server_missing_streak.get(nc, 0)}/{confirm_n}회) "
                                    f"— 연속 확인 후에만 제거"
                                )
                            except Exception:
                                pass
                        continue

                    pos = self.virtual_positions[code]
                    if self._should_skip_reconcile_remove(pos):
                        if not silent:
                            try:
                                self.log(
                                    f"[Sync] {pos.get('name', code)}({code}) "
                                    f"잔고 API 미반영 가능 — 유예 중(재동기화 대기)"
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
        self._schedule_gui_callback()

        # 증권사 체결내역 REST(kt00007) → 주문 로그 DB 보강 (백그라운드 — API 다건·연속조회 지연 방지)
        try:
            app_ref = getattr(self, "app", None)
            if app_ref is not None:
                om = getattr(app_ref, "order_manager", None)
                dm = getattr(app_ref, "data_manager", None)
                if om is not None and dm is not None:
                    st = self.settings or {}

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
                            root = getattr(app_ref, "root", None)
                            if root is not None and hasattr(
                                app_ref, "refresh_virtual_tab_orders_only"
                            ):
                                root.after(0, app_ref.refresh_virtual_tab_orders_only)
                        except Exception:
                            pass

                    threading.Thread(target=_bg_exchange_fills, daemon=True).start()
        except Exception as e:
            if not silent:
                try:
                    self.log(f"⚠️ [동기화] 체결내역 스케줄 실패: {e}")
                except Exception:
                    pass

        # 『계좌 동기화』요약 로그는 get_holdings(ka10085) 안에서 먼저 찍힌 뒤, 여기까지 오면
        # 로컬 정합·DB·GUI·체결내역 백그라운드 보강이 이어진다. 기동 후 첫 동기화에서만 요약.
        try:
            if log_boot_summary and not getattr(
                self, "_boot_sync_summary_logged", False
            ):
                self._boot_sync_summary_logged = True
                _auto = ""
                try:
                    if getattr(self.app, "auto_start", False):
                        _auto = "\n  · [Auto] 약 8초 후 실시간 감시 시작 예약"
                except Exception:
                    pass
                self.log(
                    "[기동 요약·동기화 직후] ka85 응답 로그 다음에 이어지는 처리:\n"
                    "  · 로컬 포지션 정합·가상매매 JSON 저장(save_db)\n"
                    "  · 포지션 탭 전체 갱신 예약(gui_callback, 메인 스레드) — 하단에 "
                    "「처리 중: 보유·주문 화면…」표시·모래시계 커서로 반응 지연 안내\n"
                    "  · 증권 체결내역(kt00007)으로 주문 로그 DB 보강 — 백그라운드 "
                    "(기간·건수에 따라 수 초 이상)\n"
                    "  · 보강 끝나면 주문 영역만 추가 리프레시\n"
                    "  · (병행) 시장 지수·원격 폴링·KOSPI200 로드 등은 이미 다른 스레드에서 동작 중"
                    + _auto
                )
        except Exception:
            pass

        # 총자산·예수·보유 상세는 OrderManager 요약 1회에 포함됨. 자동 동기화(silent) 시 로그 중복 방지.
        if not silent:
            self.log(
                f"[Portfolio] 로컬 포지션 반영: 갱신 {updated_count}, 신규 {new_count}, 제거 {removed_count}"
            )

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

    def _max_buy_principal_cash_d2_limit(self) -> int:
        """
        현금매수 전용 상한(원): D+2 예수금 범위에서만 매수(신용·주문가능 별도 한도 미사용).
        체결금액 P·수수료율 r일 때 총지출 = P*(1+r) ≤ D+2 → P ≤ D+2/(1+r).
        D+2 ≤ 0 이면 추가 매수 불가(0).
        """
        try:
            d2 = float(getattr(self, "d2_deposit", 0) or 0)
            if d2 <= 0:
                return 0
            buy_r, _ = fee_decimal_rates_live_trading(self.settings)
            den = 1.0 + float(buy_r)
            if den <= 1e-12:
                return 0
            return int(max(0.0, d2 / den))
        except Exception:
            return 0

    def calculate_order_quantity(self, stage=1, is_simulation=False):
        """[v2.2] 설정창의 비중 설정(원/%)에 따른 실제 투자금액 산출 (실전/시뮬레이션 로직 분리)"""
        s = self.settings
        unit = s.get(f's{stage}_sizing_unit', '원')
        
        if unit == '원':
            invest_amt = int(float(str(s.get(f'stage{stage}_amt', 1000000)).replace(',', '')))
        else:
            pct = float(s.get(f's{stage}_equity_pct', 10.0))
            # [v18.0] Separation of Logic: Real-time order vs Simulation
            if is_simulation:
                # 백테스팅/가상매매는 설정창의 기반 자산을 사용
                total_cap = float(str(s.get('backtest_initial_capital', 10000000)).replace(',', ''))
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
                        d2v = float(getattr(self, "d2_deposit", 0) or 0)
                        if invest_amt > 0:
                            self.log(
                                "❌ [Risk] D+2 예수금 한도 내 현금 매수 불가 "
                                f"(D+2={d2v:,.0f}원). 계좌 동기화·입금을 확인하세요."
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
                    self.log(
                        f"ℹ️ [Risk] 투자금액을 D+2 현금 한도로 축소합니다: "
                        f"{before_amt:,} → {invest_amt:,}원 (매수대금+매수수수료 ≤ D+2)"
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
        if self.telegram_callback:
            msg = (
                f"<b>🚨 [매도완료/{exit_reason}]</b>\n종목: {pos['name']}({code})\n"
                f"최종수익: {pnl_pct:+.1f}%\n수익금: {int(pnl_amt):,}원\n"
                f"시간: {pos.get('exit_datetime', _ex.strftime('%Y-%m-%d %H:%M:%S'))}"
            )
            threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()

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
                    self.log(f"[Portfolio] {reg_name}({code}) 신규 진입 등록 ({price:,}원)")
                    if self.telegram_callback:
                        msg = f"<b>✅ [가상체결/1차]</b>\n종목: {reg_name}({code})\n가: {price:,}원 / 수: {qty}주\n등급: {reg_reason}\n시간: {now_str}"
                        threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()
                else:
                    # 추가 매수 (Stage 2)
                    pos = self.virtual_positions[code]
                    old_qty = self._to_i(pos.get('qty', 0))
                    old_avg = self._to_f(pos.get('avg_price', 0.0))
                    new_total_qty = old_qty + self._to_i(qty)
                    new_avg_price = ((old_avg * old_qty) + (self._to_f(price) * self._to_i(qty))) / new_total_qty if new_total_qty > 0 else self._to_f(price)
                    
                    pos['qty'] = new_total_qty
                    pos['avg_price'] = new_avg_price
                    pos['stage'] = 2
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
                    self.log(f"[Portfolio] {pos['name']}({code}) 2차 수량 업데이트 (평단: {int(new_avg_price):,}원)")
                    if self.telegram_callback:
                        msg = f"<b>🔥 [추매체결/2차]</b>\n종목: {pos['name']}({code})\n추가: {price:,}원 / {qty}주\n평단: {int(new_avg_price):,}원\n시간: {now_str}"
                        threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()
            
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
        self._schedule_gui_callback()

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
        self._schedule_gui_callback()

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
                        order_type="00",
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
