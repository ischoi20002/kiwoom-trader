import os
import shutil
import tkinter as tk
from tkinter import ttk, messagebox
import threading
from typing import Any, Dict, List, Optional, Set, Tuple
import asyncio
import websockets
import json
import time
import pandas as pd
from datetime import datetime, timedelta
import FinanceDataReader as fdr

# Custom modules
import config
import strategy_selector
from backtest_engine import TradingStrategy
from kiwoom_rest_api.auth.token import TokenManager

# 텔레그램 단일 메시지 최대 길이(일반 텍스트 기준 여유)
_TELEGRAM_MAX_CHARS = 4000


def _selection_telegram_chunks(
    tag_stripped: str, total: int, lines: List[str], extra_note: str = ""
) -> List[str]:
    """종목 선정 알림 본문을 텔레그램 길이 제한에 맞게 분할(일반 텍스트, HTML 미사용)."""
    header = (
        f"🎯 종목 선정 완료 ({tag_stripped})\n"
        f"총 {total}개 감시 등록\n"
    )
    if extra_note.strip():
        header += extra_note.strip() + "\n"
    header += "\n"
    chunks: List[str] = []
    buf = header
    for line in lines:
        add = line + "\n"
        if len(buf) + len(add) > _TELEGRAM_MAX_CHARS:
            chunks.append(buf)
            buf = f"(목록 계속)\n{add}"
        else:
            buf += add
    if buf.strip():
        chunks.append(buf)
    return chunks


def _monitor_fmt_vs_prev(info: Dict[str, Any]) -> str:
    """전일 종가 대비 등락폭(원). 실시간 fid 11 등 → monitor_info['diff']."""
    try:
        raw = info.get("diff")
        if raw is None:
            return "—"
        s = str(raw).strip().replace(",", "")
        if s in ("", "-", "—"):
            return "—"
        iv = int(round(float(s)))
        if iv == 0:
            return "0"
        return f"{iv:+,}"
    except Exception:
        return "—"


def _monitor_fmt_bid_ask(info: Dict[str, Any]) -> str:
    """매도잔량 / 매수잔량 및 매도÷매수 비율(소수 1자리). SignalEngine·주문 로그와 동일."""
    try:
        from core.supply_display import format_bid_ask_monitor_display

        return format_bid_ask_monitor_display(info.get("total_ask"), info.get("total_bid"))
    except Exception:
        return "— / — (—)"


# 감시 트리 컬럼 기본 폭(px). 사용자 조절값·세로 분할 비율은 monitor_column_widths.json 에 저장됩니다.
# JSON 형식: {"columns": {..}, "sash0_rel": 0.0~1.0} — 구버전 평면 dict 는 columns 만 있는 것으로 간주합니다.
_DEFAULT_MONITOR_COL_WIDTHS: Dict[str, int] = {
    "Score": 40,
    "Code": 56,
    "Name": 88,
    "StratType": 52,
    "Price": 64,
    "AtrExit": 138,
    "VsPrev": 50,
    "Ratio": 62,
    "Status": 188,
    "VolPower": 52,
    "BidAsk": 118,
    "Volume": 72,
}
# 열을 너무 좁게 못 쓰게 하는 하한(px)
_MONITOR_COL_MIN_WIDTH: Dict[str, int] = {
    "Score": 32,
    "Code": 48,
    "Name": 52,
    "StratType": 44,
    "Price": 52,
    "AtrExit": 72,
    "VsPrev": 40,
    "Ratio": 52,
    "Status": 80,
    "VolPower": 44,
    "BidAsk": 72,
    "Volume": 56,
}


def format_strategy_type_label(sel_track: object) -> str:
    """
    선정 트랙(sel_track: A / B / AB) → 실시간 감시 '전략유형' 컬럼 표시문자열.
    SignalEngine 등에서 동일 문구를 쓰려면 이 함수를 import 해 사용합니다.
    """
    try:
        tr = str(sel_track if sel_track is not None else "").strip().upper()
        if tr == "A":
            return "전략 A"
        if tr == "B":
            return "전략 B"
        if tr in ("AB", "A+B"):
            return "전략 A+B"
        return "전략 A+B"
    except Exception:
        return "전략 A+B"


def format_entry_signal_type_label(
    pos: Optional[Dict[str, Any]],
    sel_track: object,
    *,
    code: Optional[str] = None,
    app: Any = None,
) -> str:
    """
    보유 종목: 실제 매수 체결에 쓰인 신호 구분(sig_type TYPE_A/B)을 표시.
    비보유(감시만)·pos 없음: 선정 트랙(sel_track) 기존 문구(전략 A/B/A+B).

    code·app 이 있으면 PortfolioManager.infer_sig_type_for_ui 로 단일 A/B 를
    (DB 복구 후 sig_type 누락·SYNCED 등) 보강합니다.
    """
    try:
        # 보유(dict)일 때만 포트폴리오 추정 — 미보유면 과거 매수 reason 으로 오표시될 수 있음
        if app is not None and code and isinstance(pos, dict):
            pf = getattr(app, "portfolio", None)
            if pf is not None and hasattr(pf, "infer_sig_type_for_ui"):
                eff = pf.infer_sig_type_for_ui(
                    str(code).split("_")[0].zfill(6), pos
                )
                if eff == "TYPE_B":
                    return "전략 B"
                if eff == "TYPE_A":
                    return "전략 A"
        if isinstance(pos, dict):
            st = str(pos.get("sig_type") or "").strip().upper()
            if st == "TYPE_B" or "TYPE_B" in st:
                return "전략 B"
            if st == "TYPE_A" or "TYPE_A" in st:
                return "전략 A"
            rsn = str(pos.get("reason") or "").upper()
            if "TYPE_B" in rsn:
                return "전략 B"
            if "TYPE_A" in rsn and "TYPE_B" not in rsn:
                return "전략 A"
    except Exception:
        pass
    return format_strategy_type_label(sel_track)


class MonitorTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        # 보유 종목 추가 시 웹소켓 REG 재전송 요청(과도 호출 방지는 루프에서 스로틀)
        self._pending_reg_refresh: bool = False
        self._last_stream_reg_ts: float = 0.0
        # 직전 REG에 실린 종목 집합(정규화·정렬). 동일이면 재전송·대시보드 로그 생략(스팸 방지)
        self._last_stream_reg_sig: Optional[Tuple[str, ...]] = None
        # 증분 워밍업 등으로 REG는 자주 가도, 사용자 로그는 이 간격(초) 이상일 때만 1줄 출력
        self._last_stream_reg_user_log_ts: float = 0.0
        # 웹소켓 LOGIN 실패(8005 등) 로그 스팸 방지·재시도 카운트
        self._ws_login_fail_streak: int = 0
        self._last_ws_login_fail_log_ts: float = 0.0
        # 감시 트리 열 폭·세로 Paned 비율 저장(구분선 / sash 드래그 후 디바운스)
        self._monitor_col_width_save_job: Optional[Any] = None
        self._monitor_sash_sync_job: Optional[Any] = None  # 세시 드래그 직후 sashpos 지연 읽기
        self._monitor_width_source_tree: Optional[ttk.Treeview] = None
        self._monitor_paned: Optional[ttk.PanedWindow] = None
        self._monitor_saved_sash_rel: Optional[float] = None  # _load_saved_monitor_col_widths 에서 채움
        self._monitor_tree_columns: Tuple[str, ...] = tuple()
        # 실시간 틱 경로: 계좌 동기화 1프레임 등으로 VP에만 없으면 보유→감시 트리로 잘못 옮기는 깜빡임 방지
        self._vp_ui_inactive_streak: Dict[str, int] = {}
        # 실제로 VP에 들어간 적 있을 때만 비보유 2틱 디바운스 적용(미보유 종목 첫 틱이 보유 트리로 가는 버그 방지)
        self._vp_ever_in_holdings: Dict[str, bool] = {}
        # 틱이 끊긴 종목도 monitor_info 기준으로 트리에 유지 (봉 마감·무거래 구간)
        self._last_monitor_tree_reconcile_ts: float = 0.0
        self._setup_ui()

    @staticmethod
    def _subscription_codes_signature(codes: Any) -> Tuple[str, ...]:
        """REG `item` 리스트를 비교용 시그니처로 만듭니다(6자리·중복 제거·정렬)."""
        try:
            if not codes:
                return tuple()
            seen: Set[str] = set()
            out: List[str] = []
            for x in codes:
                try:
                    nk = str(x).split("_")[0].zfill(6)
                except Exception:
                    continue
                if nk not in seen:
                    seen.add(nk)
                    out.append(nk)
            return tuple(sorted(out))
        except Exception:
            return tuple()

    def _monitor_col_widths_json_path(self) -> str:
        """
        monitor_column_widths.json — **항상** k_trader_gui.py 와 같은 폴더(``_app_root_dir``).
        예전에는 cwd 등에만 있던 파일이 있으면 **한 번만** 그 내용을 루트로 복사한다.
        (여러 후보 중 '먼저 발견된 파일'에만 쓰면 사용자가 보는 경로와 달라 저장이 안 되는 것처럼 보였음)
        """
        try:
            app = self.app
            root_dir = getattr(app, "_app_root_dir", None)
            if not isinstance(root_dir, str) or not root_dir.strip():
                root_dir = os.getcwd()
            path = os.path.abspath(os.path.join(root_dir, "monitor_column_widths.json"))
            if not getattr(self, "_monitor_json_migrated", False):
                try:
                    if not os.path.isfile(path):
                        cwd_f = os.path.abspath(
                            os.path.join(os.getcwd(), "monitor_column_widths.json")
                        )
                        if cwd_f != path and os.path.isfile(cwd_f):
                            shutil.copy2(cwd_f, path)
                except Exception:
                    pass
                try:
                    self._monitor_json_migrated = True
                except Exception:
                    pass
            return path
        except Exception:
            return os.path.abspath(os.path.join(os.getcwd(), "monitor_column_widths.json"))

    def _read_monitor_ui_file(self) -> Dict[str, Any]:
        """monitor_column_widths.json 원본(dict). 없거나 오류 시 {}."""
        try:
            path = self._monitor_col_widths_json_path()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                return raw if isinstance(raw, dict) else {}
        except Exception as e:
            try:
                print(f"[MonitorTab] UI 상태 파일 읽기 실패: {e}")
            except Exception:
                pass
        return {}

    @staticmethod
    def _parse_monitor_sash_rel(raw: Dict[str, Any]) -> Optional[float]:
        """저장된 상·하단 분할 비율(첫 번째 sash 위치 / Paned 높이)."""
        try:
            if not isinstance(raw, dict):
                return None
            v = raw.get("sash0_rel")
            if v is None:
                return None
            x = float(v)
            if x != x:  # NaN
                return None
            return max(0.06, min(0.90, x))
        except (TypeError, ValueError):
            return None

    def _load_saved_monitor_col_widths(self, cols: Tuple[str, ...]) -> Dict[str, int]:
        """저장된 폭이 있으면 병합(없는 열·잘못된 값은 기본값). sash0_rel 은 _monitor_saved_sash_rel 에 반영."""
        out: Dict[str, int] = {}
        for c in cols:
            out[c] = int(_DEFAULT_MONITOR_COL_WIDTHS.get(c, 64))
        try:
            raw = self._read_monitor_ui_file()
            src: Dict[str, Any]
            if isinstance(raw.get("columns"), dict):
                src = raw["columns"]
            else:
                # 구버전: 최상위 키가 곧 컬럼명
                src = {k: v for k, v in raw.items() if k in out}
            for k, v in src.items():
                if k not in out:
                    continue
                try:
                    iw = int(v)
                    if 24 <= iw <= 4000:
                        out[k] = iw
                except (TypeError, ValueError):
                    pass
            self._monitor_saved_sash_rel = self._parse_monitor_sash_rel(raw)
        except Exception as e:
            try:
                print(f"[MonitorTab] 컬럼 폭 로드 실패: {e}")
            except Exception:
                pass
        return out

    def _on_monitor_tree_column_drag_end(self, event: tk.Event) -> None:
        """헤더 열 구분선 드래그 후 다른 트리와 폭을 맞추고 저장을 예약합니다."""
        try:
            w = event.widget
            if not isinstance(w, ttk.Treeview):
                return
            reg = str(w.identify_region(event.x, event.y)).lower()
            if reg != "separator":
                return
        except tk.TclError:
            return
        except Exception:
            return
        try:
            cols = self._monitor_tree_columns
            if not cols or not hasattr(self, "tree_monitor"):
                return
            t_act, t_wl = self.tree_active_monitor, self.tree_monitor
            src = w
            other = t_wl if src is t_act else t_act
            for c in cols:
                try:
                    tw = int(src.column(c, "width"))
                    mw = _MONITOR_COL_MIN_WIDTH.get(c, 32)
                    other.column(c, width=max(mw, tw))
                except Exception:
                    pass
            self._monitor_width_source_tree = src
        except Exception:
            pass
        try:
            r = getattr(self.app, "root", None)
            if r is None:
                return
            jid = self._monitor_col_width_save_job
            if jid is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
            self._monitor_col_width_save_job = r.after(
                350, self._flush_monitor_col_widths_to_disk
            )
        except Exception:
            pass

    def _monitor_current_sash_rel(self) -> Optional[float]:
        """Paned 첫 sash 상대 위치(0~1). 측정 불가 시 None."""
        try:
            p = self._monitor_paned
            if p is None:
                return None
            h = int(p.winfo_height())
            # 탭 비활성·레이아웃 미계산 시 winfo_height 가 0에 가깝게 나와 저장이 막히는 경우 보완
            if h < 8:
                try:
                    h = max(h, int(p.winfo_reqheight()))
                except (tk.TclError, TypeError, ValueError):
                    pass
            if h < 8:
                try:
                    par = p.master
                    if par is not None:
                        h = max(h, int(par.winfo_height()))
                except (tk.TclError, TypeError, ValueError):
                    pass
            if h < 8:
                return None
            sp = int(p.sashpos(0))
            return max(0.06, min(0.90, float(sp) / float(h)))
        except (tk.TclError, TypeError, ValueError, ZeroDivisionError):
            return None

    def _request_monitor_sash_sync(self) -> None:
        """
        세로 세시(sash) 드래그 직후에는 Tk가 sashpos 를 한 틱 늦게 반영하는 경우가 있어
        짧게 지연한 뒤 비율을 읽어 저장합니다. (<<PanedWindowMoved>> 누락 완화는 마우스 업 바인딩)
        """
        try:
            r = getattr(self.app, "root", None)
            if r is None:
                return
            jid = getattr(self, "_monitor_sash_sync_job", None)
            if jid is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
            self._monitor_sash_sync_job = r.after(20, self._apply_monitor_sash_sync)
        except Exception:
            pass

    def _apply_monitor_sash_sync(self, attempt: int = 0) -> None:
        """지연 후 메모리에 sash 비율을 반영하고 monitor_column_widths.json 저장을 예약합니다."""
        if attempt == 0:
            self._monitor_sash_sync_job = None
        try:
            rel = self._monitor_current_sash_rel()
            if rel is not None:
                self._monitor_saved_sash_rel = rel
            r = getattr(self.app, "root", None)
            if r is None:
                return
            # sashpos 가 한두 틱 더 늦게 맞는 경우 재시도(저장 None 방지)
            if rel is None and attempt < 3:
                self._monitor_sash_sync_job = r.after(
                    90, lambda: self._apply_monitor_sash_sync(attempt + 1)
                )
                return
            jid = self._monitor_col_width_save_job
            if jid is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
            self._monitor_col_width_save_job = r.after(
                250, self._flush_monitor_col_widths_to_disk
            )
        except Exception:
            pass

    def _release_is_on_monitor_paned_sash(self, event: Any) -> bool:
        """패인 자식에서 올린 이벤트도 오므로, 세시 위에서만 True."""
        try:
            p = self._monitor_paned
            if p is None:
                return False
            ex = int(event.x_root - p.winfo_rootx())
            ey = int(event.y_root - p.winfo_rooty())
            rid = p.identify(ex, ey)
            if rid is None:
                return False
            if isinstance(rid, int):
                return True
            s = str(rid).lower()
            return "sash" in s
        except (tk.TclError, TypeError, ValueError):
            return False
        except Exception:
            return False

    def _on_monitor_paned_mouse_release(self, event: Any) -> None:
        try:
            if not self._release_is_on_monitor_paned_sash(event):
                return
            self._request_monitor_sash_sync()
        except Exception:
            pass

    def _on_monitor_paned_moved(self, _event: Any = None) -> None:
        """상·하단 분할 변경 시 저장 예약."""
        self._request_monitor_sash_sync()

    def _schedule_restore_monitor_sash(self) -> None:
        """
        창 실제 높이가 잡힌 뒤 저장된 비율로 첫 sash 를 복원합니다.
        Notebook 에서 숨겨진 탭은 winfo_height 가 매우 작게 나오므로,
        포지션 탭과 같이 탭을 처음 열 때(k_trader_gui._on_tab_changed) 다시 호출하는 것이 안전합니다.
        """
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                return

            def _try(attempt: int = 0) -> None:
                try:
                    rel = getattr(self, "_monitor_saved_sash_rel", None)
                    p = self._monitor_paned
                    if p is None:
                        return
                    try:
                        p.update_idletasks()
                    except Exception:
                        pass
                    h = int(p.winfo_height())
                    # 저장 시와 동일: 미계산 높이 보완
                    if h < 8:
                        try:
                            h = max(h, int(p.winfo_reqheight()))
                        except (tk.TclError, TypeError, ValueError):
                            pass
                    if h < 8:
                        try:
                            par = p.master
                            if par is not None:
                                h = max(h, int(par.winfo_height()))
                        except (tk.TclError, TypeError, ValueError):
                            pass
                    # 레이아웃 미확정 시 대기 — 작은 h 에서 sashpos 하면 한쪽 패인이 사실상 사라진 것처럼 보임
                    if h < 160 and attempt < 24:
                        root.after(100, lambda a=attempt + 1: _try(a))
                        return
                    # 저장 없음: 상단이 접히는 환경 대비 기본 비율(약 22%)
                    if rel is None:
                        rel = 0.22
                    min_top = 96
                    min_bot = 120
                    if h < 300:
                        min_top = 72
                        min_bot = 96
                    raw = float(rel) * float(h)
                    pos = int(round(raw))
                    pos = max(min_top, min(pos, h - min_bot))
                    if pos >= h - 8:
                        pos = max(min_top, h // 2)
                    if pos < min_top:
                        pos = min_top
                    p.sashpos(0, pos)
                except Exception:
                    if attempt < 24:
                        root.after(100, lambda a=attempt + 1: _try(a))

            root.after(120, lambda: _try(0))
        except Exception:
            pass

    def _flush_monitor_col_widths_to_disk(self) -> None:
        """트리 열 폭·Paned 세로 분할을 monitor_column_widths.json 에 저장합니다."""
        self._monitor_col_width_save_job = None
        try:
            cols = self._monitor_tree_columns
            raw_existing = self._read_monitor_ui_file()
            data: Dict[str, int] = {}
            tr = getattr(self, "tree_monitor", None)
            if cols and tr is not None:
                src = self._monitor_width_source_tree
                if src is None:
                    src = tr
                for c in cols:
                    try:
                        mw = _MONITOR_COL_MIN_WIDTH.get(c, 32)
                        data[c] = max(mw, int(src.column(c, "width")))
                    except Exception:
                        data[c] = int(_DEFAULT_MONITOR_COL_WIDTHS.get(c, 64))
            else:
                # 트리 미준비 시에도 세로 분할(sash)만 갱신할 수 있게 기존 열 폭 유지
                prev_c = raw_existing.get("columns") if isinstance(raw_existing, dict) else None
                if isinstance(prev_c, dict):
                    for k, v in prev_c.items():
                        try:
                            data[str(k)] = int(v)
                        except (TypeError, ValueError):
                            pass
                if not data and cols:
                    for c in cols:
                        data[c] = int(_DEFAULT_MONITOR_COL_WIDTHS.get(c, 64))
            rel = self._monitor_current_sash_rel()
            if rel is None:
                rel = getattr(self, "_monitor_saved_sash_rel", None)
            if rel is None:
                rel = self._parse_monitor_sash_rel(raw_existing)
            path = self._monitor_col_widths_json_path()
            dname = os.path.dirname(path)
            if dname:
                os.makedirs(dname, exist_ok=True)
            payload: Dict[str, Any] = {"columns": data}
            if rel is not None:
                payload["sash0_rel"] = float(rel)
            elif isinstance(raw_existing, dict) and raw_existing.get("sash0_rel") is not None:
                # 측정·메모리 모두 실패해도 기존 파일의 세로 분할은 유지(실수로 키 삭제 방지)
                try:
                    payload["sash0_rel"] = float(raw_existing["sash0_rel"])
                except (TypeError, ValueError):
                    pass
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            try:
                os.replace(tmp_path, path)
            except OSError:
                try:
                    shutil.copyfile(tmp_path, path)
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        except Exception as e:
            try:
                log_fn = getattr(self.app, "log", None)
                if callable(log_fn):
                    log_fn(f"[MonitorTab] UI 상태 저장 실패: {e}")
                else:
                    print(f"[MonitorTab] UI 상태 저장 실패: {e}")
            except Exception:
                pass

    def flush_monitor_ui_before_quit(self) -> None:
        """
        종료 직전 동기 저장. root.destroy() 가 예약된 after(저장) 보다 먼저 실행되면
        디스크에 한 번도 안 쓰이는 문제가 생길 수 있어, 대기 중 작업을 취소하고 즉시 flush 한다.
        """
        try:
            r = getattr(self.app, "root", None)
            if r is not None:
                for jid in (
                    getattr(self, "_monitor_sash_sync_job", None),
                    getattr(self, "_monitor_col_width_save_job", None),
                ):
                    if jid is not None:
                        try:
                            r.after_cancel(jid)
                        except (tk.TclError, ValueError, AttributeError):
                            pass
                self._monitor_sash_sync_job = None
                self._monitor_col_width_save_job = None
            try:
                p = self._monitor_paned
                if p is not None:
                    p.update_idletasks()
            except (tk.TclError, AttributeError):
                pass
            if r is not None:
                try:
                    r.update()
                except (tk.TclError, RuntimeError):
                    pass
                for _ in range(20):
                    try:
                        r.update_idletasks()
                    except (tk.TclError, RuntimeError):
                        break
                    rel = self._monitor_current_sash_rel()
                    if rel is not None:
                        self._monitor_saved_sash_rel = rel
                        break
            self._flush_monitor_col_widths_to_disk()
            try:
                pth = self._monitor_col_widths_json_path()
                sr: Any = "?"
                try:
                    if os.path.isfile(pth):
                        with open(pth, "r", encoding="utf-8") as f:
                            jd = json.load(f)
                        sr = jd.get("sash0_rel", "?")
                except Exception:
                    pass
                log_fn = getattr(self.app, "log", None)
                if callable(log_fn):
                    log_fn(f"[UI] 종료 시 감시 레이아웃 저장: {pth} sash0_rel={sr}")
            except Exception:
                pass
        except Exception as e:
            try:
                log_fn = getattr(self.app, "log", None)
                if callable(log_fn):
                    log_fn(f"[MonitorTab] 종료 시 UI 저장 실패: {e}")
            except Exception:
                pass

    def _setup_ui(self):
        """실시간 감시 탭 레이아웃 설정"""
        self.mo_main_frame = ttk.Frame(self.parent, padding="15")
        self.mo_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 컨트롤 영역
        self.ctrl_frame = ttk.Frame(self.mo_main_frame)
        self.ctrl_frame.pack(fill=tk.X, pady=(0, 15))

        # [임시] 매수신호 없이 주문·체결 흐름 테스트 — 감시/보유 트리에서 행 선택 후 1주 시장가
        test_wrap = ttk.Frame(self.ctrl_frame)
        test_wrap.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(
            test_wrap,
            text="테스트 주문 (임시 · 1주 시장가)",
            font=("Malgun Gothic", 8),
            foreground="#81A1C1",
        ).pack(anchor=tk.W)
        test_btns = ttk.Frame(test_wrap)
        test_btns.pack(fill=tk.X)
        ttk.Button(
            test_btns,
            text="1주 매수",
            width=10,
            command=lambda: self._test_market_order_1share("buy"),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            test_btns,
            text="1주 매도",
            width=10,
            command=lambda: self._test_market_order_1share("sell"),
        ).pack(side=tk.LEFT, padx=0)

        # 실시간 감시 시작/중단과 동일 폭(폰트 기준 문자 단위). 한글·이모지 여유.
        _monitor_ctrl_btn_w = 20
        self.btn_select = ttk.Button(
            self.ctrl_frame,
            text="일일 감시목록 선정",
            width=_monitor_ctrl_btn_w,
            style="Action.TButton",
            command=self.start_selection_thread,
            state=tk.DISABLED,
        )
        self.btn_monitor_toggle = ttk.Button(
            self.ctrl_frame,
            text="▶️ 실시간 감시 시작",
            width=_monitor_ctrl_btn_w,
            command=self.app.toggle_monitoring,
            style="Accent.TButton",
        )
        self.lbl_monitor_status = ttk.Label(
            self.ctrl_frame,
            text="상태: 대기 중",
            font=("Malgun Gothic", 10),
            foreground=self.app.colors["primary"],
        )
        # 오른쪽 묶음: 읽기 순서(왼→오) = 상태 | 실시간 감시 시작/중단 | 일일 감시목록 선정
        # side=RIGHT 는 먼저 pack 한 위젯이 부모의 오른쪽에 붙고, 다음은 그 왼쪽에 이어 붙음.
        # 상태 라벨을 왼쪽에 두면 문구 길이가 바뀌어도 두 버튼은 오른쪽 고정으로 겹침·밀림이 줄어듦.
        self.btn_select.pack(side=tk.RIGHT, padx=(0, 4))
        self.btn_monitor_toggle.pack(side=tk.RIGHT, padx=(4, 8))
        self.lbl_monitor_status.pack(side=tk.RIGHT, padx=(4, 0))

        self.lbl_headwind_caution = ttk.Label(self.ctrl_frame, text="⚠️ 시장 역풍 - 매수 신호가 일시 정지되었습니다", 
                                                font=("Malgun Gothic", 10, "bold"), foreground="#BF616A")
        
        # 트리뷰 영역 (Split-View 도입) [v2.2]
        paned = ttk.PanedWindow(self.mo_main_frame, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        self._monitor_paned = paned
        try:
            paned.bind("<<PanedWindowMoved>>", self._on_monitor_paned_moved, add="+")
        except Exception:
            pass
        # Windows 등에서 <<PanedWindowMoved>> 누락·sashpos 지연 반영 대비(포지션 탭과 동일 패턴)
        try:
            paned.bind("<ButtonRelease-1>", self._on_monitor_paned_mouse_release, add="+")
        except Exception:
            pass

        # [상단] 보유 종목 창 (세로폭 축소)
        # weight=0 은 일부 Windows/ttk에서 상단 패널 높이가 0에 가깝게 붙어 트리가 안 보이는 사례가 있어
        # 양쪽 모두 weight>0 + 상단 minsize 로 최소 높이를 보장한다.
        active_frame = ttk.LabelFrame(paned, text=" 🔥 보유 및 대응 종목 (Active Positions) ", padding="5")
        paned.add(active_frame, weight=1)

        # [하단] 일반 감시 창 (확대)
        watchlist_frame = ttk.LabelFrame(paned, text=" 👁️ 실시간 감시 목록 (Watchlist) ", padding="5")
        paned.add(watchlist_frame, weight=3)
        try:
            paned.paneconfigure(active_frame, minsize=96)
        except (tk.TclError, AttributeError):
            pass
        
        columns = (
            "Score",
            "Code",
            "Name",
            "StratType",
            "Price",
            "AtrExit",
            "VsPrev",
            "Ratio",
            "Status",
            "VolPower",
            "BidAsk",
            "Volume",
        )
        # 워밍업 등에서 '신호상태' 열 값 패치 시 인덱스 오류 방지
        self._monitor_status_value_index = columns.index("Status")
        headings = {
            "Score": "가점",
            "Code": "코드",
            "Name": "종목명",
            "StratType": "전략유형",
            "Price": "현재가",
            "AtrExit": "ATR기준가",
            "VsPrev": "대비",
            "Ratio": "등락률",
            "Status": "신호상태",
            "VolPower": "체결강도",
            "BidAsk": "Bid/Ask",
            "Volume": "거래량",
        }
        self._monitor_tree_columns = tuple(columns)
        col_widths = self._load_saved_monitor_col_widths(self._monitor_tree_columns)

        def _pack_tree_grid_scroll(inner: ttk.Frame, tree: ttk.Treeview) -> None:
            """트리·세로·가로 스크롤을 inner에 배치(tree는 inner의 자식이어야 함)."""
            ysb = ttk.Scrollbar(inner, orient=tk.VERTICAL, command=tree.yview)
            xsb = ttk.Scrollbar(inner, orient=tk.HORIZONTAL, command=tree.xview)
            tree.configure(yscroll=ysb.set, xscroll=xsb.set)
            tree.grid(row=0, column=0, sticky="nsew")
            ysb.grid(row=0, column=1, sticky="ns")
            xsb.grid(row=1, column=0, sticky="ew")
            inner.rowconfigure(0, weight=1)
            inner.columnconfigure(0, weight=1)

        # 1. 상단 트리뷰 (Active) - 높이 축소
        active_inner = ttk.Frame(active_frame)
        active_inner.pack(fill=tk.BOTH, expand=True)
        def _monitor_col_anchor(col_id: str) -> str:
            """
            보유/감시 트리 **셀(body)** 정렬. 컬럼 헤더(heading)는 항상 가운데.
            전략유형·신호상태는 중앙, 종목명은 왼쪽, 현재가(Price)부터 오른쪽 열은 수치·호가이므로 오른쪽.
            """
            if col_id == "Name":
                return tk.W
            if col_id in ("StratType", "Status"):
                return tk.CENTER
            if col_id in ("Score", "Code"):
                return tk.CENTER
            if col_id in (
                "Price",
                "AtrExit",
                "VsPrev",
                "Ratio",
                "VolPower",
                "BidAsk",
                "Volume",
            ):
                return tk.E
            return tk.CENTER

        self.tree_active_monitor = ttk.Treeview(
            active_inner, columns=columns, show="headings", selectmode="browse", height=3
        )
        for col in columns:
            anchor = _monitor_col_anchor(col)
            self.tree_active_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            mw = _MONITOR_COL_MIN_WIDTH.get(col, 32)
            cw = max(mw, int(col_widths[col]))
            self.tree_active_monitor.column(
                col,
                width=cw,
                minwidth=mw,
                anchor=anchor,
                stretch=False,
            )
        _pack_tree_grid_scroll(active_inner, self.tree_active_monitor)

        # 2. 하단 트리뷰 (Watchlist)
        wl_inner = ttk.Frame(watchlist_frame)
        wl_inner.pack(fill=tk.BOTH, expand=True)
        self.tree_monitor = ttk.Treeview(
            wl_inner, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            anchor = _monitor_col_anchor(col)
            self.tree_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            mw = _MONITOR_COL_MIN_WIDTH.get(col, 32)
            cw = max(mw, int(col_widths[col]))
            self.tree_monitor.column(
                col,
                width=cw,
                minwidth=mw,
                anchor=anchor,
                stretch=False,
            )
        _pack_tree_grid_scroll(wl_inner, self.tree_monitor)
        self.tree_active_monitor.bind(
            "<ButtonRelease-1>", self._on_monitor_tree_column_drag_end, add="+"
        )
        self.tree_monitor.bind(
            "<ButtonRelease-1>", self._on_monitor_tree_column_drag_end, add="+"
        )
        self._schedule_restore_monitor_sash()

        # [v4.7.5] 'HD Crisp UI' 도입: 굵지 않고 날렵한 Malgun Gothic 및 차분한 프로 컬러 사용
        row_font = ("Malgun Gothic", 9, "normal")
        up_col = "#E35F5F" # 채도가 너무 높아 번져 보이는 #FF0000 대신 차분한 레드
        dn_col = "#81A1C3" # 너무 쨍한 파란색 대신 가시성 좋은 노르딕 블루
        
        for t in [self.tree_monitor, self.tree_active_monitor]:
            # 1. 상승 (레드)
            t.tag_configure("up_even", foreground=up_col, background="#3B4252", font=row_font)
            t.tag_configure("up_odd", foreground=up_col, background="#242933", font=row_font)
            
            # 2. 하락 (블루)
            t.tag_configure("down_even", foreground=dn_col, background="#3B4252", font=row_font)
            t.tag_configure("down_odd", foreground=dn_col, background="#242933", font=row_font)
            
            # 3. 보합 (기본)
            t.tag_configure("stable_even", foreground="#D8DEE9", background="#3B4252", font=row_font)
            t.tag_configure("stable_odd", foreground="#D8DEE9", background="#242933", font=row_font)
            
            # 4. 보유 종목 — 감시목록과 동일하게 등락색 + 짝/홀 줄무늬 배경
            for direction, fg in (("up", up_col), ("down", dn_col), ("stable", "#D8DEE9")):
                t.tag_configure(
                    f"active_{direction}_even",
                    foreground=fg,
                    background="#3B4252",
                    font=row_font,
                )
                t.tag_configure(
                    f"active_{direction}_odd",
                    foreground=fg,
                    background="#242933",
                    font=row_font,
                )
            
            # 5. 보조 상태 (합치도)
            t.tag_configure("status_high", foreground="#EBCB8B", font=row_font)
            t.tag_configure("status_low", foreground="#616E88", font=row_font)

            # 6. 워밍업(15분봉 API·DB 검증) 단계 구분
            t.tag_configure(
                "warmup_pending",
                foreground="#ECEFF4",
                background="#434C5E",
                font=row_font,
            )
            t.tag_configure(
                "warmup_ok_row",
                foreground="#A3BE8C",
                background="#3B4252",
                font=row_font,
            )
            t.tag_configure(
                "warmup_fail_row",
                foreground="#ECEFF4",
                background="#5c3d3d",
                font=row_font,
            )

        try:
            self._apply_default_monitor_theme()
        except Exception:
            pass

    def start_selection_thread(self, auto_confirm: bool = False) -> None:
        """일일 감시 목록 선정을 백그라운드에서 실행합니다."""
        try:
            if getattr(self, "btn_select", None) is not None:
                self.btn_select.config(state=tk.DISABLED)
        except Exception:
            pass
        threading.Thread(
            target=self.select_monitoring_targets, args=(auto_confirm,), daemon=True
        ).start()

    @staticmethod
    def _log_selection_results(app: Any, df: pd.DataFrame) -> None:
        """선정 결과를 로그창에 표 형태로 출력 (팝업 대체)."""
        try:
            d = df.copy()
            d.columns = [str(c).lower() for c in d.columns]
            app.log("\n" + "=" * 102)
            app.log(f"🎯 [종목 선정 결과] {len(d)}개 → 실시간 감시 대상으로 바로 등록합니다.")
            app.log("-" * 102)
            app.log(
                f"{'#':>3} | {'코드':8} | {'종목명':18} | {'가점':>6} | {'트랙':^5} | "
                f"{'현재가':>10} | {'등락%':>7} | {'RSI':>5} | {'5일대금(억)':>10}"
            )
            app.log("-" * 102)
            for i, (_, row) in enumerate(d.iterrows(), 1):
                code = str(row.get("code", "")).zfill(6)
                name = str(row.get("name", "Unknown"))[:18]
                try:
                    sc = float(row.get("score", 0))
                except (TypeError, ValueError):
                    sc = 0.0
                st = str(row.get("seltrack", "")).strip().upper() or "-"
                try:
                    price = int(float(row.get("price", 0)))
                except (TypeError, ValueError):
                    price = 0
                try:
                    ratio = float(row.get("ratio", 0))
                except (TypeError, ValueError):
                    ratio = 0.0
                try:
                    rsi = float(row.get("rsi", 0))
                except (TypeError, ValueError):
                    rsi = 0.0
                try:
                    tv = int(float(row.get("avgtrdeval", 0)))
                except (TypeError, ValueError):
                    tv = 0
                app.log(
                    f"{i:3d} | {code} | {name:18} | {sc:6.1f} | {st:^5} | "
                    f"{price:10,d} | {ratio:7.2f} | {rsi:5.1f} | {tv:10d}"
                )
            app.log("=" * 102 + "\n")
        except Exception as e:
            app.log(f"⚠️ [선정] 결과 로그 출력 실패: {e}")

    def select_monitoring_targets(self, auto_confirm: bool = False) -> None:
        """선정 스캔 후 로그 출력 + 감시 대상 즉시 등록 (확인 팝업 없음)."""
        df_snap: Optional[pd.DataFrame] = None
        aborted_stale = False
        try:
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.app.settings,
                logger_callback=self.app.log,
                data_manager=self.app.data_manager,
                selection_ohlcv_source="api_only",
            )
            if df_result is not None and not df_result.empty:
                df_snap = df_result.copy()
        except strategy_selector.SelectionDbStaleError as stale:
            msg = str(stale.args[0]) if stale.args else "일봉 DB가 최신이 아닙니다."

            def _warn_stale() -> None:
                try:
                    messagebox.showwarning(
                        "일봉 DB 미갱신",
                        "DB가 최신 장마감 일봉까지 반영되지 않았습니다.\n\n"
                        f"{msg}\n\n"
                        "데이터 수집(하베스팅)을 먼저 완료한 뒤 종목 선정을 실행하세요.",
                        parent=self.parent,
                    )
                finally:
                    try:
                        if getattr(self, "btn_select", None) is not None:
                            self.btn_select.config(state=tk.NORMAL)
                    except Exception:
                        pass

            try:
                self.app.root.after(0, _warn_stale)
            except Exception:
                _warn_stale()
            aborted_stale = True
        except Exception as e:
            import traceback

            self.app.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.app.log(f"DEBUG Traceback:\n{traceback.format_exc()}")

        if aborted_stale:
            return

        def finish_on_main() -> None:
            try:
                if df_snap is None or df_snap.empty:
                    self.app.log("⚠️ [선정] 조건에 부합하는 종목이 없습니다.")
                    return
                self._log_selection_results(self.app, df_snap)
                self.app.trading_controller.set_monitor_targets(df_snap)
                self.app.monitor_codes = self.app.trading_controller.monitor_codes
                self.app.monitor_info = self.app.trading_controller.monitor_info
                self.update_watchlist(self.app.monitor_info)
                tag = "[원격] " if auto_confirm else "[GUI] "
                self.app.log(
                    f"✅ {tag}{len(self.app.monitor_codes)}개 종목을 실시간 감시 대상으로 등록했습니다."
                )
                n_reg = len(self.app.monitor_codes or [])
                n_analyzed = len(df_snap)
                extra_note = ""
                if n_analyzed != n_reg:
                    extra_note = f"※ 선정 분석 {n_analyzed}개 → 감시 상한 적용 후 {n_reg}개 등록"
                lines: List[str] = []
                for code in self.app.monitor_codes or []:
                    info = (self.app.monitor_info or {}).get(code, {})
                    nm = str(info.get("name", code)).strip()
                    tr = str(info.get("sel_track", "")).strip().upper() or "-"
                    lines.append(f"• {nm} ({code})  [{tr}]")

                parts = _selection_telegram_chunks(
                    tag.strip(), n_reg, lines, extra_note=extra_note
                )

                def _send_telegram_parts() -> None:
                    try:
                        for i, part in enumerate(parts):
                            self.app.send_telegram_message(part, parse_mode=None)
                            if i + 1 < len(parts):
                                time.sleep(0.4)
                    except Exception as te:
                        try:
                            self.app.log(f"⚠️ [Telegram] 분할 전송 중 오류: {te}")
                        except Exception:
                            pass

                threading.Thread(target=_send_telegram_parts, daemon=True).start()
            except Exception as ex:
                self.app.log(f"❌ [선정] 등록·로그 후처리 실패: {ex}")

        try:
            self.app.root.after(0, finish_on_main)
        except Exception:
            finish_on_main()

        def _reenable_btn() -> None:
            try:
                if getattr(self, "btn_select", None) is not None:
                    self.btn_select.config(state=tk.NORMAL)
            except Exception:
                pass

        try:
            self.app.root.after(0, _reenable_btn)
        except Exception:
            _reenable_btn()

    def get_tree_display_price(self, code: str) -> Optional[float]:
        """
        감시/보유 트리에 표시된 현재가(Price 컬럼). 없으면 None.
        OrderManager 시세 검증용.
        """
        try:
            c = str(code).zfill(6)
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree or not tree.exists(c):
                    continue
                vals = tree.item(c, "values") or ()
                if len(vals) > 4:
                    return float(str(vals[4]).replace(",", "").strip())
        except (TypeError, ValueError, tk.TclError):
            pass
        except Exception:
            pass
        return None

    def _get_selected_monitor_code_name_price(self) -> Optional[Tuple[str, str, int]]:
        """감시·보유 트리에서 선택된 종목 (code, name, 참고가).

        참고가: 지정가(±1틱) 주문의 기준가로 쓰는 값(트리 표시 현재가 우선, 없으면 monitor_info).
        """
        try:
            code: Optional[str] = None
            vals_row: tuple = ()
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                sel = tree.selection()
                if not sel:
                    continue
                code = str(sel[0]).zfill(6)
                vals_row = tree.item(sel[0], "values") or ()
                break
            if not code:
                return None
            mi: Dict[str, Any] = (self.app.monitor_info or {}).get(code, {}) or {}
            name = str(mi.get("name", code))
            # 트리 "현재가" 컬럼은 실시간 갱신 우선 — monitor_info.last_price 는 지연될 수 있음
            lp = 0.0
            if len(vals_row) > 4:
                try:
                    lp = float(str(vals_row[4]).replace(",", "").strip())
                except (TypeError, ValueError):
                    lp = 0.0
            if lp <= 0:
                try:
                    lp = float(mi.get("last_price") or mi.get("price") or 0)
                except (TypeError, ValueError):
                    lp = 0.0
            if lp <= 0:
                lp = 1.0
            return code, name, int(lp)
        except Exception:
            return None

    def _test_market_order_1share(self, side: str) -> None:
        """선택 종목에 대해 1주 시장가 테스트 주문(워커 스레드에서 실행)."""
        side = (side or "buy").lower().strip()
        if side not in ("buy", "sell"):
            return

        picked = self._get_selected_monitor_code_name_price()
        if not picked:
            messagebox.showwarning(
                "테스트 주문",
                "감시 목록 또는 보유 종목 트리에서 종목 한 줄을 먼저 선택하세요.",
            )
            return
        code, name, ref_p = picked

        use_mock = getattr(config, "USE_MOCK", False)
        allow = (
            getattr(config, "ALLOW_MOCK_ORDER", False)
            if use_mock
            else getattr(config, "ALLOW_REAL_ORDER", False)
        )
        mode_kr = "모의투자" if use_mock else "실전"
        if not allow:
            messagebox.showwarning(
                "테스트 주문",
                f"{mode_kr}에서 주문이 차단되어 있습니다.\n"
                "config.py 의 ALLOW_MOCK_ORDER 또는 ALLOW_REAL_ORDER 를 True 로 설정하세요.",
            )
            return

        act = "매수" if side == "buy" else "매도"
        if not messagebox.askyesno(
            "테스트 주문 확인",
            f"{act} 1주 지정가(참고가 기준 ±1틱)를 보냅니다.\n\n"
            f"{name} ({code})\n"
            f"참고가: {ref_p:,}원\n"
            f"모드: {mode_kr}\n\n"
            f"사유: TEST_1SHARE_MARKET",
        ):
            return

        def _run() -> None:
            try:
                om = getattr(self.app, "order_manager", None)
                if om is None:
                    self.app.root.after(
                        0,
                        lambda: self.app.log("❌ [테스트주문] OrderManager 없음"),
                    )
                    return
                ord_no = om.place_order(
                    code,
                    1,
                    ref_p,
                    order_type="00",
                    side=side,
                    name=name,
                    reason="TEST_1SHARE_MARKET",
                )
                msg = (
                    f"🧪 [테스트주문] {act} 1주 시장가 응답: "
                    f"{ord_no if ord_no else '실패/거부 — 로그 확인'}"
                )
                self.app.root.after(0, lambda m=msg: self.app.log(m))
            except Exception as e:
                err = str(e)
                self.app.root.after(
                    0,
                    lambda: self.app.log(f"❌ [테스트주문] 예외: {err}"),
                )

        threading.Thread(target=_run, daemon=True).start()

    async def _ws_client_loop(self):
        """키움 REST API 방식 웹소켓 루프 (자동 재연결 및 안정성 강화)"""
        while self.app.is_monitoring:
            try:
                self.app.root.after(0, self.app.log, "📡 [시스템] 실시간 서버 접속 시도...")
                
                access_token = self.app.auth_manager.get_auth_token()
                if not access_token:
                    self.app.root.after(0, self.app.log, "⏳ [시스템] 거래 서버 점검 또는 인증 대기 중... (10초 후 재시도)")
                    await asyncio.sleep(10)
                    continue
                
                stream_codes = []
                try:
                    stream_codes = self.app.stream_subscription_codes()
                except Exception:
                    stream_codes = list(getattr(self.app.trading_controller, "monitor_codes", []) or [])
                if not stream_codes:
                    # 일시적으로 목록이 비면 감시를 끄면 UI·틱이 영구 정지한 것처럼 보임 → 재시도만 함
                    self.app.root.after(
                        0,
                        self.app.log,
                        "⚠️ [시스템] 구독 종목 목록이 비어 있습니다. 5초 후 재접속합니다. "
                        "(선정 CSV·감시 종목을 확인하세요)",
                    )
                    await asyncio.sleep(5)
                    continue

                if getattr(config, 'USE_MOCK', False):
                    ws_url = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'
                else:
                    ws_url = 'wss://api.kiwoom.com:10000/api/dostk/websocket'
                
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    # 새 소켓마다 시그니처 초기화 — 재연결 직후 이전 세션과 비교하면 안 됨
                    self._last_stream_reg_sig = None
                    self._last_stream_reg_user_log_ts = 0.0
                    self.app.root.after(0, self.app.log, "✅ [시스템] 서버 연결 성공. 로그인 시도...")
                    
                    login_packet = {'trnm': 'LOGIN', 'token': access_token}
                    await ws.send(json.dumps(login_packet))

                    self.app.debug_packet_logged = False 
                    
                    while self.app.is_monitoring:
                        try:
                            # 보유·감시 종목 변경 시 REG 재전송 (4초 스로틀)
                            if getattr(self, "_pending_reg_refresh", False):
                                now_ts = time.time()
                                if now_ts - getattr(self, "_last_stream_reg_ts", 0) >= 4.0:
                                    try:
                                        _sc = self.app.stream_subscription_codes()
                                        if _sc:
                                            sig = self._subscription_codes_signature(_sc)
                                            prev_sig = getattr(
                                                self, "_last_stream_reg_sig", None
                                            )
                                            if sig and sig == prev_sig:
                                                # 직전 REG와 동일 구독 — 전송·로그 생략(동일 요청 반복 시 대시보드 스팸 방지)
                                                self._pending_reg_refresh = False
                                                self._last_stream_reg_ts = now_ts
                                            else:
                                                self._pending_reg_refresh = False
                                                self._last_stream_reg_ts = now_ts
                                                reg_packet = {
                                                    "trnm": "REG",
                                                    "grp_no": "1",
                                                    "refresh": "1",
                                                    "market": "AL",
                                                    "data": [{"item": _sc, "type": ["0B", "0D"]}],
                                                }
                                                await ws.send(json.dumps(reg_packet))
                                                self._last_stream_reg_sig = sig
                                                # 구독 목록이 종목마다 조금씩 늘어나도 시그니처가 매번 달라
                                                # 동일 시그니처 스킵만으로는 로그가 과다함 → 시간 스로틀
                                                try:
                                                    log_prev = float(
                                                        getattr(
                                                            self,
                                                            "_last_stream_reg_user_log_ts",
                                                            0.0,
                                                        )
                                                    )
                                                except (TypeError, ValueError):
                                                    log_prev = 0.0
                                                if now_ts - log_prev >= 25.0:
                                                    self._last_stream_reg_user_log_ts = float(
                                                        now_ts
                                                    )
                                                    n_sub = len(sig)
                                                    self.app.root.after(
                                                        0,
                                                        self.app.log,
                                                        "📡 [시스템] 실시간 스트림 재등록 "
                                                        f"(보유·감시 반영, {n_sub}종목)",
                                                    )
                                        else:
                                            self._pending_reg_refresh = False
                                            self._last_stream_reg_ts = now_ts
                                    except Exception as re:
                                        self._pending_reg_refresh = False
                                        self._last_stream_reg_ts = now_ts
                                        self.app.root.after(
                                            0,
                                            self.app.log,
                                            f"⚠️ [시스템] 스트림 재등록 실패: {re}",
                                        )
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            raw_data = json.loads(msg)
                            data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                            
                            for packet in data_list:
                                trnm = packet.get('trnm')
                                if trnm == 'LOGIN':
                                    if packet.get('return_code') == 0:
                                        self._ws_login_fail_streak = 0
                                        try:
                                            _sc = self.app.stream_subscription_codes()
                                        except Exception:
                                            _sc = list(
                                                getattr(
                                                    self.app.trading_controller,
                                                    "monitor_codes",
                                                    [],
                                                )
                                                or []
                                            )
                                        self.app.log(
                                            f"✅ [시스템] 로그인 성공. 종목 등록 ({len(_sc)}개)..."
                                        )
                                        reg_packet = {
                                            'trnm': 'REG', 'grp_no': '1', 'refresh': '1', 'market': 'AL',
                                            'data': [{'item': _sc, 'type': ['0B', '0D']}]
                                        }
                                        await ws.send(json.dumps(reg_packet))
                                        try:
                                            self._last_stream_reg_sig = (
                                                self._subscription_codes_signature(_sc)
                                            )
                                            self._last_stream_reg_ts = time.time()
                                        except Exception:
                                            pass
                                    else:
                                        # CODE=8005 등: REST TTL 캐시 토큰과 WS 요구가 어긋나 동일 문자열로 무한 실패할 수 있음 → 캐시 무효화 후 재발급
                                        self._ws_login_fail_streak += 1
                                        try:
                                            am = getattr(
                                                self.app, "auth_manager", None
                                            )
                                            if am is not None and hasattr(
                                                am, "invalidate_cached_token"
                                            ):
                                                am.invalidate_cached_token()
                                        except Exception:
                                            pass
                                        try:
                                            tm = TokenManager()
                                            for _name in (
                                                "clear_cache",
                                                "invalidate",
                                                "reset",
                                                "clear",
                                            ):
                                                _fn = getattr(tm, _name, None)
                                                if callable(_fn):
                                                    _fn()
                                                    break
                                        except Exception:
                                            pass
                                        rmsg = str(packet.get("return_msg") or "")
                                        now_ts = time.time()
                                        _log_detail = (
                                            self._ws_login_fail_streak == 1
                                            or self._ws_login_fail_streak % 8 == 0
                                            or (
                                                now_ts
                                                - getattr(
                                                    self,
                                                    "_last_ws_login_fail_log_ts",
                                                    0.0,
                                                )
                                            )
                                            >= 60.0
                                        )
                                        if _log_detail:
                                            self._last_ws_login_fail_log_ts = now_ts
                                            try:
                                                _mock = bool(
                                                    getattr(config, "USE_MOCK", True)
                                                )
                                            except Exception:
                                                _mock = True
                                            self.app.log(
                                                f"❌ [시스템] 웹소켓 로그인 실패: {rmsg}. "
                                                "REST 토큰 캐시를 비웠습니다 — 재발급 후 재접속합니다. "
                                                f"(모의={'ON' if _mock else 'OFF'}, "
                                                "모의/실전·앱키 불일치 시 지속될 수 있음) "
                                                f"[연속 {self._ws_login_fail_streak}회]"
                                            )
                                        await asyncio.sleep(
                                            5
                                            if self._ws_login_fail_streak <= 3
                                            else 15
                                        )
                                        break
                                elif trnm == 'PING':
                                    await ws.send(json.dumps(packet))
                                elif trnm in ['REAL', 'CHE'] or ('values' in packet and ('item' in packet or 'stk_cd' in packet)):
                                    self._process_realtime_packet(packet)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            self.app.log(f"⚠️ [시스템] 수신 에러: {e}")
                            break
                            
            except Exception as e:
                if self.app.is_monitoring:
                    error_msg = str(e)
                    wait_sec = 5
                    if "429" in error_msg:
                        wait_sec = 60
                        self.app.log(f"🛑 [시스템] API 호출 제한(429) 감지. {wait_sec}초 후 다시 시도합니다.")
                    else:
                        self.app.log(f"❌ [시스템] 연결 에러: {e}. {wait_sec}초 후 재연결 시도...")
                    
                    await asyncio.sleep(wait_sec)
            finally:
                if not self.app.is_monitoring:
                    self.app.root.after(0, self.app.stop_monitoring)

    def _process_realtime_packet(self, packet):
        """실시간 패킷 처리 로직 분리"""
        trnm = packet.get('trnm')
        if trnm == 'REAL':
            payload = packet.get('data', [])
            if not isinstance(payload, list): payload = [payload]
        else:
            payload = [packet]
        
        for item_data in payload:
            raw_code = item_data.get('item', item_data.get('stk_cd', item_data.get('item_code', '')))
            full_code = str(raw_code).strip()
            code = full_code.split('_')[0].zfill(6)
            
            if trnm == 'CHE':
                order_no = item_data.get('ord_no')
                cntg_qty = int(item_data.get('cntg_qty', 0))
                cntg_unpr = int(item_data.get('cntg_unpr', 0))
                buy_sell_tp = item_data.get('buy_sell_tp', '1') # 1:매도, 2:매수
                side = "buy" if buy_sell_tp == '2' else "sell"
                self.app._on_execution_received(code, order_no, cntg_qty, cntg_unpr, side)
                continue

            if code in self.app.monitor_info:
                info = self.app.monitor_info[code]
                vals = item_data.get('values', {})
                p_type = item_data.get('type', '0B')
                
                def get_val(fid, default=0):
                    v = vals.get(str(fid))
                    if v is None or v == "": return default
                    try: return float(str(v).replace('+', '').replace('-', '').strip())
                    except: return default

                if p_type == '0B':
                    now_price = int(get_val(10))
                    # 체결 전·장 시작 직후 fid 10 이 0인 패킷이 섞일 수 있음.
                    # return 이면 같은 REAL 배치의 다른 종목 틱까지 전부 버려지므로 continue.
                    if now_price == 0:
                        continue
                    volume = int(get_val(13))
                    trde_val = int(get_val(26) // 1000)
                    ratio_val = vals.get('12', '0')
                    diff_val = vals.get('11', '0')
                    vol_power = get_val(228)
                    info['price'] = now_price
                    info['ratio'] = ratio_val
                    info['diff'] = diff_val
                    info['volume'] = volume
                    info['trde_val'] = trde_val
                    info['vol_power'] = vol_power
                    self.app._on_realtime_data_received(code, now_price, volume, trde_val, vol_power=vol_power)
                elif p_type == '0D':
                    total_ask = int(get_val(121))
                    total_bid = int(get_val(125))
                    info['total_ask'] = total_ask
                    info['total_bid'] = total_bid
                    # 체결(0B) 전에 호가만 오면 price 가 0 → 틱 콜백이 전부 차단되는 것처럼 보일 수 있음
                    try:
                        lp = int(info.get("price") or 0)
                    except (TypeError, ValueError):
                        lp = 0
                    if lp <= 0:
                        continue
                    self.app._on_realtime_data_received(
                        code,
                        lp,
                        info.get("volume", 0),
                        info.get("trde_val", 0),
                        total_ask=total_ask,
                        total_bid=total_bid,
                    )
                mkt = item_data.get('market', item_data.get('mkt', ''))
                if mkt == 'NXT' or '_nx' in full_code.lower():
                    info['is_nxt'] = True

    def _apply_default_monitor_theme(self) -> None:
        """시장 레짐 비사용: 노르딕 기본(UP과 동일) 팔레트로 고정."""
        t = {
            "bg": "#2E3440",
            "field": "#242933",
            "odd": "#242933",
            "even": "#3B4252",
            "fg": "#ECEFF4",
        }
        try:
            style = ttk.Style()
            style.configure("Monitor.TFrame", background=t["bg"])
            style.configure("Monitor.TLabel", background=t["bg"], foreground=t["fg"])
            self.mo_main_frame.config(style="Monitor.TFrame")
            self.ctrl_frame.config(style="Monitor.TFrame")
            for widget in self.mo_main_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(style="Monitor.TLabel")
            for widget in self.ctrl_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(style="Monitor.TLabel")
            style.configure("Treeview", fieldbackground=t["field"], background=t["field"])
            row_font_th = ("Malgun Gothic", 9, "normal")
            up_c = "#E35F5F"
            dn_c = "#81A1C3"
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                for direction, fg in (("up", up_c), ("down", dn_c), ("stable", "#D8DEE9")):
                    tree.tag_configure(
                        f"{direction}_even",
                        background=t["even"],
                        foreground=fg,
                        font=row_font_th,
                    )
                    tree.tag_configure(
                        f"{direction}_odd",
                        background=t["odd"],
                        foreground=fg,
                        font=row_font_th,
                    )
                    tree.tag_configure(
                        f"active_{direction}_even",
                        background=t["even"],
                        foreground=fg,
                        font=row_font_th,
                    )
                    tree.tag_configure(
                        f"active_{direction}_odd",
                        background=t["odd"],
                        foreground=fg,
                        font=row_font_th,
                    )
            if self.tree_monitor:
                self.tree_monitor.tag_configure("oddrow", background=t["odd"])
                self.tree_monitor.tag_configure("evenrow", background=t["even"])
            if hasattr(self, "lbl_headwind_caution"):
                self.lbl_headwind_caution.pack_forget()
        except Exception:
            pass

    def _watchlist_codes_set(self) -> set:
        """관심목록 코드(6자리). 감시목록과 독립이며, 이 집합은 감시 화면에서 숨김."""
        try:
            return self.app.watchlist_store.codes_set()
        except Exception:
            return set()

    def _monitor_target_codes_set(self) -> set:
        """
        선정된 **실시간 감시 대상** 코드(6자리).
        보유만 하다 청산한 종목은 이 집합에 있을 때만 '실시간 감시 목록' 트리에 남긴다.
        """
        try:
            mc = getattr(self.app.trading_controller, "monitor_codes", None) or []
            return {str(c).split("_")[0].zfill(6) for c in mc}
        except Exception:
            return set()

    def _monitor_ratio_direction(self, ratio_val: object) -> str:
        """등락률 표시값 → up / down / stable (줄무늬 행 태그용)."""
        try:
            s = str(ratio_val).replace("%", "").strip()
            if not s:
                return "stable"
            v = float(s.replace("+", ""))
            if v > 0:
                return "up"
            if v < 0:
                return "down"
        except (TypeError, ValueError):
            pass
        return "stable"

    def _monitor_info_entry(self, mi: Dict[str, Any], nk: str) -> Dict[str, Any]:
        """monitor_info 키가 003490 / 3490 혼재해도 6자리 nk로 조회."""
        try:
            if nk in mi:
                return dict(mi[nk]) if isinstance(mi[nk], dict) else {}
            for k, v in mi.items():
                if str(k).split("_")[0].zfill(6) == nk:
                    return dict(v) if isinstance(v, dict) else {}
        except Exception:
            pass
        return {}

    def _compose_monitor_row_values(self, nk: str, info: Dict[str, Any]) -> Tuple[Any, ...]:
        """감시·보유 트리 한 행 values (컬럼 순서는 _setup_ui 와 동일)."""
        try:
            now_p = float(
                info.get("price")
                if info.get("price") not in (None, "", 0)
                else (info.get("last_price") or 0)
            )
        except (TypeError, ValueError):
            now_p = 0.0
        sel_tr = info.get("sel_track", info.get("SelTrack", "AB"))
        pos_for_strat: Optional[Dict[str, Any]] = None
        try:
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
            pos_for_strat = vp.get(nk)
            if pos_for_strat is None:
                for k, v in vp.items():
                    if str(k).split("_")[0].zfill(6) == nk:
                        pos_for_strat = v if isinstance(v, dict) else None
                        break
        except Exception:
            pos_for_strat = None
        strat = format_entry_signal_type_label(
            pos_for_strat, sel_tr, code=nk, app=self.app
        )
        ratio_disp = info.get("ratio", "0")
        nm = str(info.get("name", nk) or nk)
        try:
            sc = float(info.get("score", 0) or 0)
        except (TypeError, ValueError):
            sc = 0.0
        try:
            vol_pow = float(info.get("vol_power", 0) or 0)
        except (TypeError, ValueError):
            vol_pow = 0.0
        try:
            vol_i = int(float(info.get("volume", 0) or 0))
        except (TypeError, ValueError):
            vol_i = 0
        atr_cell = "—"
        try:
            pos = pos_for_strat
            if pos is not None:
                bd = (getattr(self.app, "bars_data", None) or {}).get(nk)
                if bd is not None and not getattr(bd, "empty", True):
                    # 실시간 StreamProcessor 는 ATR_A/B 를 봉에 붙이지 않음 → 전체 지표 재계산 후 마지막 행 사용
                    try:
                        df_i = TradingStrategy.calculate_indicators(
                            bd, self.app.settings
                        )
                        ir = df_i.iloc[-1].to_dict()
                    except Exception:
                        try:
                            lr = bd.iloc[-1]
                            ir = (
                                lr.to_dict()
                                if hasattr(lr, "to_dict")
                                else dict(lr)
                            )
                        except Exception:
                            ir = {}
                    np_atr = float(now_p) if now_p > 0 else float(ir.get("Close", 0) or 0)
                    if np_atr > 0:
                        atr_cell = TradingStrategy.compute_atr_exit_display_text(
                            pos,
                            self.app.settings,
                            ir,
                            np_atr,
                            bars_df=bd,
                            sel_track_hint=info.get("sel_track")
                            or info.get("SelTrack"),
                        )
        except Exception:
            atr_cell = "—"
        return (
            f"{sc:.1f}",
            nk,
            nm,
            strat,
            f"{now_p:,.0f}",
            atr_cell,
            _monitor_fmt_vs_prev(info),
            str(ratio_disp),
            info.get("status_text", "대기"),
            f"{vol_pow:.1f}%",
            _monitor_fmt_bid_ask(info),
            f"{vol_i:,}",
        )

    def maybe_reconcile_monitor_trees(
        self,
        monitor_info: Optional[Dict[str, Any]],
        virtual_positions: Any,
        min_interval_s: float = 3.0,
    ) -> None:
        """
        monitor_info 에 있어야 할 종목이 틱 부재로 트리에서 빠진 경우 복구.
        (15:30 봉 마감 등 체결이 없는 종목은 pending_tree_updates 에 안 올라와 행이 사라질 수 있음)
        """
        try:
            if not self.tree_monitor or not self.tree_active_monitor:
                return
            now = time.time()
            if now - self._last_monitor_tree_reconcile_ts < min_interval_s:
                return
            self._last_monitor_tree_reconcile_ts = now

            mi = monitor_info or getattr(self.app, "monitor_info", None) or {}
            wl = self._watchlist_codes_set()
            try:
                vp = (
                    virtual_positions
                    if virtual_positions is not None
                    else (getattr(self.app.portfolio, "virtual_positions", None) or {})
                )
            except Exception:
                vp = {}

            expected: set = set()
            for code in mi.keys():
                nk = str(code).split("_")[0].zfill(6)
                if nk in wl:
                    continue
                expected.add(nk)

            mc_targets = self._monitor_target_codes_set()

            # 관심목록 전용 숨김: 양쪽 트리에서 제거
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                for iid in list(tree.get_children()):
                    z = str(iid).split("_")[0].zfill(6)
                    if z in wl and tree.exists(iid):
                        tree.delete(iid)

            # 누락 행 복구·보유/감시 트리 오배치 보정 (monitor_info 기준, 고아 강제 삭제는 하지 않음)
            for nk in sorted(expected):
                info = self._monitor_info_entry(mi, nk)
                if not info:
                    info = {"name": nk, "sel_track": "AB"}
                values = self._compose_monitor_row_values(nk, info)
                is_active = self._virtual_position_is_active(nk, vp)
                # 비보유 + 감시 선정 외 종목 → 감시 목록으로 보내지 않고 양쪽 트리에서 제거
                if not is_active and nk not in mc_targets:
                    for tr in (self.tree_active_monitor, self.tree_monitor):
                        if tr and tr.exists(nk):
                            try:
                                tr.delete(nk)
                            except Exception:
                                pass
                    continue
                target = self.tree_active_monitor if is_active else self.tree_monitor
                other = self.tree_monitor if is_active else self.tree_active_monitor
                if other.exists(nk):
                    try:
                        other.delete(nk)
                    except Exception:
                        pass
                if target.exists(nk):
                    continue
                direction = self._monitor_ratio_direction(info.get("ratio", "0"))
                try:
                    idx = len(target.get_children())
                except Exception:
                    idx = 0
                row_type = "even" if (idx % 2 == 0) else "odd"
                tag = (
                    f"active_{direction}_{row_type}"
                    if is_active
                    else f"{direction}_{row_type}"
                )
                try:
                    target.insert("", tk.END, iid=nk, values=values, tags=(tag,))
                except Exception:
                    pass

            # VP 에 없는데 보유 트리에만 남은 행; 감시 목록 복구는 감시 대상일 때만
            self._purge_stale_active_monitor_rows(vp, mi)

            # 틱 경로 등으로 감시 트리에만 남은 '비감시' 행 정리
            if self.tree_monitor:
                for iid in list(self.tree_monitor.get_children()):
                    z = str(iid).split("_")[0].zfill(6)
                    if z in wl or z in mc_targets:
                        continue
                    try:
                        self.tree_monitor.delete(iid)
                    except Exception:
                        pass
        except Exception as e:
            try:
                print(f"[UI Error] maybe_reconcile_monitor_trees: {e}")
            except Exception:
                pass

    def _virtual_position_is_active(self, iid: str, virtual_positions: Any) -> bool:
        """
        트리 iid 와 virtual_positions 키가 3490 vs 003490 처럼 달라도 동일 종목으로 인식.
        (앞자리 0 종목만 보유/감시 트리 간 이동이 틱마다 바뀌는 현상 방지)
        """
        try:
            vp = virtual_positions or {}
            if not vp:
                return False
            nk = str(iid).split("_")[0].zfill(6)
            if nk in vp:
                return True
            for k in vp:
                if str(k).split("_")[0].zfill(6) == nk:
                    return True
            return False
        except Exception:
            return False

    def _purge_stale_active_monitor_rows(
        self,
        virtual_positions: Any,
        monitor_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        virtual_positions 에 없는데 보유 트리에만 남은 행을 제거한다.
        청산 직후 틱이 끊기면 update_view 디바운스(streak<2)로 영구히 '보유'로 남는 문제를 막는다.
        **실시간 감시 선정(monitor_codes)** 에 포함된 종목만 감시 목록 트리로 복구한다.
        """
        try:
            if not self.tree_active_monitor:
                return
            mi = monitor_info
            if mi is None:
                mi = getattr(self.app, "monitor_info", None) or {}
            vp = virtual_positions or {}
            wl = self._watchlist_codes_set()
            for iid in list(self.tree_active_monitor.get_children()):
                nk = str(iid).split("_")[0].zfill(6)
                if nk in wl:
                    continue
                if self._virtual_position_is_active(iid, vp):
                    continue
                try:
                    self.tree_active_monitor.delete(iid)
                except Exception:
                    pass
                # 다음 틱에서 '잠깐 비보유' 디바운스로 다시 보유 트리로 끌려가지 않도록
                self._vp_ui_inactive_streak[nk] = 3
                try:
                    self._vp_ever_in_holdings.pop(nk, None)
                except Exception:
                    pass
                may_watch = nk in self._monitor_target_codes_set()
                if not may_watch:
                    continue
                if not self.tree_monitor:
                    continue
                if self.tree_monitor.exists(nk):
                    continue
                info = self._monitor_info_entry(mi, nk)
                if not info:
                    info = {"name": nk, "sel_track": "AB"}
                values = self._compose_monitor_row_values(nk, info)
                direction = self._monitor_ratio_direction(info.get("ratio", "0"))
                try:
                    idx = len(self.tree_monitor.get_children())
                except Exception:
                    idx = 0
                row_type = "even" if (idx % 2 == 0) else "odd"
                tag = f"{direction}_{row_type}"
                try:
                    self.tree_monitor.insert("", tk.END, iid=nk, values=values, tags=(tag,))
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[UI Error] _purge_stale_active_monitor_rows: {e}")
            except Exception:
                pass

    def update_view(self, updates, virtual_positions):
        """[v4.5.3] App으로부터 전달받은 실시간 틱 데이터를 화면에 렌더링 (캡슐화)"""
        if not self.tree_monitor: return
        
        try:
            if updates:
                items = list(updates.items())
                updates.clear()
                wl = self._watchlist_codes_set()
                mc_targets = self._monitor_target_codes_set()

                for iid_raw, (values, tag, _) in items:
                    # pending 키·monitor_info 키가 3490/003490로 섞여도 트리 iid는 항상 6자리로 통일
                    iid = str(iid_raw).split("_")[0].zfill(6)
                    cid = iid
                    if cid in wl:
                        for tree in (self.tree_monitor, self.tree_active_monitor):
                            if tree and tree.exists(iid):
                                tree.delete(iid)
                        try:
                            self._vp_ui_inactive_streak.pop(iid, None)
                            self._vp_ever_in_holdings.pop(iid, None)
                        except Exception:
                            pass
                        continue
                    # 보유 여부에 따라 타겟 트리 결정
                    is_active_raw = self._virtual_position_is_active(
                        iid, virtual_positions
                    )
                    # 동기화·API 지연으로 VP에서 한 번만 빠지면 보유↔감시가 깜빡일 수 있어,
                    # '실제 보유 경험이 있는 종목'에 한해 비보유 2틱 연속일 때만 감시 트리로 내린다.
                    # (streak<2 만으로는 미보유 종목 첫 틱이 보유 트리로 잡혀 워밍업 중 유령 행이 뜸)
                    try:
                        if is_active_raw:
                            self._vp_ui_inactive_streak[iid] = 0
                            self._vp_ever_in_holdings[iid] = True
                        else:
                            self._vp_ui_inactive_streak[iid] = int(
                                self._vp_ui_inactive_streak.get(iid, 0)
                            ) + 1
                            if self._vp_ui_inactive_streak[iid] >= 2:
                                self._vp_ever_in_holdings.pop(iid, None)
                        is_active = is_active_raw or (
                            bool(self._vp_ever_in_holdings.get(iid, False))
                            and self._vp_ui_inactive_streak.get(iid, 0) < 2
                        )
                    except Exception:
                        is_active = is_active_raw
                    # 청산 후 비보유: 감시 선정 종목만 실시간 감시 목록에 표시
                    if not is_active and iid not in mc_targets:
                        for tr in (self.tree_active_monitor, self.tree_monitor):
                            if tr and tr.exists(iid):
                                try:
                                    tr.delete(iid)
                                except Exception:
                                    pass
                        try:
                            self._vp_ui_inactive_streak.pop(iid, None)
                            self._vp_ever_in_holdings.pop(iid, None)
                        except Exception:
                            pass
                        continue
                    target_tree = self.tree_active_monitor if is_active else self.tree_monitor
                    other_tree = self.tree_monitor if is_active else self.tree_active_monitor
                    
                    # 반대편 트리에 있으면 삭제 (이동 효과)
                    if other_tree.exists(iid):
                        other_tree.delete(iid)
                        
                    # [v4.5.42] 행의 인덱스와 기본 태그 정의 (안전한 index 추출)
                    if not target_tree.exists(iid):
                        target_tree.insert("", tk.END, iid=iid, values=values)
                    
                    # [Composite Tag Selection] v4.6.5
                    idx = target_tree.index(iid)
                    is_even = (idx % 2 == 0)
                    row_type = "even" if is_even else "odd"
                    
                    # 방향 결정
                    if "price_up" in str(tag): direction = "up"
                    elif "price_down" in str(tag): direction = "down"
                    else: direction = "stable"
                    
                    # 최종 태그 조합
                    if is_active:
                        final_tag = f"active_{direction}_{row_type}"
                    else:
                        final_tag = f"{direction}_{row_type}"
                    
                    # 데이터와 태그가 변경된 경우에만 업데이트
                    curr = target_tree.item(iid)
                    if curr['values'] != values or final_tag not in curr['tags']:
                        target_tree.item(iid, values=values, tags=(final_tag,))
        except Exception as e:
            print(f"[UI Error] MonitorTab Render Error: {e}")

    def update_watchlist(self, monitor_info):
        """[v4.5.3] 감시 종목 리스트 초기화 렌더링"""
        try:
            self._vp_ui_inactive_streak.clear()
            self._vp_ever_in_holdings.clear()
        except Exception:
            self._vp_ui_inactive_streak = {}
            self._vp_ever_in_holdings = {}
        self.tree_monitor.delete(*self.tree_monitor.get_children())
        self.tree_active_monitor.delete(*self.tree_active_monitor.get_children())
        
        # [v4.5.7] 가점(Score) 기준 내림차순 정렬하여 초기 삽입
        sorted_items = sorted(monitor_info.items(), key=lambda x: x[1].get('score', 0), reverse=True)
        wl = self._watchlist_codes_set()
        mc_tgt = self._monitor_target_codes_set()
        try:
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
        except Exception:
            vp = {}
        
        idx_active = 0
        idx_watch = 0
        for code, info in sorted_items:
            nk = str(code).split("_")[0].zfill(6)
            if nk in wl:
                continue
            ratio_disp = info.get("ratio", "0")
            direction = self._monitor_ratio_direction(ratio_disp)
            values = self._compose_monitor_row_values(nk, info)
            # 보유 중이면 아래(보유 트리)에만 넣음. 예전에는 위 트리만 채워 다음 틱까지 보유 트리가 비어 깜빡임 발생.
            if self._virtual_position_is_active(nk, vp):
                row_type = "even" if (idx_active % 2 == 0) else "odd"
                idx_active += 1
                tag = f"active_{direction}_{row_type}"
                self.tree_active_monitor.insert("", tk.END, iid=nk, values=values, tags=(tag,))
            elif nk in mc_tgt:
                row_type = "even" if (idx_watch % 2 == 0) else "odd"
                idx_watch += 1
                tag = f"{direction}_{row_type}"
                self.tree_monitor.insert("", tk.END, iid=nk, values=values, tags=(tag,))

    def load_targets(self, codes_or_info):
        """KiwoomApp과의 호환성을 위한 래퍼 (DataFrame 전달 시 app.monitor_info 사용)."""
        try:
            if isinstance(codes_or_info, dict):
                self.update_watchlist(codes_or_info)
            elif hasattr(self.app, "monitor_info") and self.app.monitor_info:
                self.update_watchlist(self.app.monitor_info)
        except Exception as e:
            print(f"[UI Error] load_targets: {e}")

    def reset_warmup_row_states(self, codes: list) -> None:
        """감시 시작 워밍업 직전: 지정 종목 행을 '대기' 표시로 통일."""
        try:
            wl = self._watchlist_codes_set()
            for code in codes:
                if str(code).zfill(6) in wl:
                    continue
                for tree in (self.tree_monitor, self.tree_active_monitor):
                    if tree and tree.exists(code):
                        vals = list(tree.item(code, "values"))
                        si = getattr(self, "_monitor_status_value_index", 8)
                        if len(vals) > si:
                            vals[si] = "⏳ 워밍업 대기…"
                        tree.item(code, values=tuple(vals), tags=("warmup_pending",))
        except Exception as e:
            print(f"[UI Error] reset_warmup_row_states: {e}")

    def update_warmup_stock_prep(
        self,
        event: str,
        cur: int,
        total: int,
        name: str,
        code: str,
        reason: str = "",
    ) -> None:
        """종목별 워밍업 진행·성공·실패를 신호상태 열·행 배경으로 구분."""
        try:
            code = str(code).zfill(6)
            if code in self._watchlist_codes_set():
                return
            if event == "prep_start":
                st = f"⏳ 데이터 준비 중… ({cur}/{total})"
                tag = "warmup_pending"
            elif event == "prep_progress":
                extra = (reason or "").strip()
                if extra:
                    st = f"⏳ ({cur}/{total}) {extra}"
                else:
                    st = f"⏳ 데이터 준비 중… ({cur}/{total})"
                tag = "warmup_pending"
            elif event == "prep_ok":
                st = "✅ 15분봉·검증 완료"
                tag = "warmup_ok_row"
            else:
                short = (reason or "실패")[:120]
                st = f"❌ 워밍업 실패: {short}"
                tag = "warmup_fail_row"
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree or not tree.exists(code):
                    continue
                vals = list(tree.item(code, "values"))
                si = getattr(self, "_monitor_status_value_index", 8)
                if len(vals) > si:
                    vals[si] = st
                tree.item(code, values=tuple(vals), tags=(tag,))
        except Exception as e:
            print(f"[UI Error] update_warmup_stock_prep: {e}")
