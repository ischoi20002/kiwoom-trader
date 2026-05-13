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
# Custom modules
import config
import strategy_selector
from core.monitor_code_sets import (
    monitor_selection_codes_set,
    watchlist_only_codes_set,
)
from core.monitor_signal_status_display import monitor_signal_status_display_text
from core.terminal_trace import terminal_trace
from backtest_engine import TradingStrategy
from kiwoom_rest_api.auth.token import TokenManager
from reporting.realtime_session_logger import RealtimeSessionLogger
from utils.normalization import canonical_stock_code

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
        # LOGIN 실패 원인( return_msg 등 )을 콘솔 스크롤 없이 찾기 위해 JSONL에 누적
        self._realtime_session_logger: RealtimeSessionLogger = RealtimeSessionLogger()
        # 최신 접속 url(모의/실전)에 대한 참고용(로그 payload에 포함)
        self._last_ws_url: str = ""
        # 감시 트리 열 폭·세로 Paned 비율 저장(구분선 / sash 드래그 후 디바운스)
        self._monitor_col_width_save_job: Optional[Any] = None
        self._monitor_sash_sync_job: Optional[Any] = None  # 세시 드래그 직후 sashpos 지연 읽기
        self._monitor_width_source_tree: Optional[ttk.Treeview] = None
        self._monitor_paned: Optional[ttk.PanedWindow] = None
        self._monitor_saved_sash_rel: Optional[float] = None  # _load_saved_monitor_col_widths 에서 채움
        # 세시(sash) 픽셀 추적 — Configure 로 드래그 종료 감지(ttk.ButtonRelease 가 세시에 안 붙는 경우 대비)
        self._monitor_prev_sash_pixel: Optional[int] = None
        self._monitor_restoring_sash: bool = False
        # 탭 재진입 시 매번 강제 복원하지 않도록, 최초 1회 복원 완료 여부를 기록
        self._monitor_sash_initialized: bool = False
        # [고정 모드] 보유/대응 종목 패널 높이를 항상 N줄 기준으로 복원
        self._monitor_fix_active_rows: int = 5
        self._monitor_tree_columns: Tuple[str, ...] = tuple()
        # 실시간 틱 경로: 계좌 동기화 1프레임 등으로 VP에만 없으면 보유→감시 트리로 잘못 옮기는 깜빡임 방지
        self._vp_ui_inactive_streak: Dict[str, int] = {}
        # 실제로 VP에 들어간 적 있을 때만 비보유 2틱 디바운스 적용(미보유 종목 첫 틱이 보유 트리로 가는 버그 방지)
        self._vp_ever_in_holdings: Dict[str, bool] = {}
        # 틱이 끊긴 종목도 monitor_info 기준으로 트리에 유지 (봉 마감·무거래 구간)
        self._last_monitor_tree_reconcile_ts: float = 0.0
        # stream_subscription_codes() 결과 캐시 — 틱마다 전체 목록 재계산 방지
        self._stream_sub_cache_set: Optional[Set[str]] = None
        self._stream_sub_cache_ts: float = 0.0
        # WS recv 타임아웃 누적(실시간 패킷 부재 시 막힘처럼 보일 때 터미널 추적용)
        self._ws_idle_recv_count: int = 0
        # 장운영구분(0s.fid215) 최신 코드 및 정규장 판별 상태
        self._market_operation_code: str = ""
        self._is_regular_session_active: bool = False
        # 키움 등에서 "03"/"04" 로 줄 수 있어 동일 취급
        self._regular_session_op_codes: Set[str] = {"3", "4", "03", "04"}
        # fid215 가 '의미 있는' 비정규 코드로 확정된 경우 True(로컬 정규장 폴백 안 함).
        # '0'/'00' 등 플레이스홀더는 제외(모의 WS 등에서 명시적 장외로 오인하면 틱이 전부 스킵됨).
        self._ws_has_explicit_fid215: bool = False
        # 0s.fid215 값이 3/4가 아니거나 비어 있을 때 간이 진단 로그(스팸 방지 쿨다운)
        self._ws_0s_diag_mismatch_last_ts: float = 0.0
        self._ws_0s_diag_empty_logged: bool = False
        self._ws_0s_diag_fid215_zero_logged: bool = False
        # 정규장 OFF로 0B가 스킵될 때 1회 안내(0s 미수신·type 대소문자 등)
        self._ws_logged_session_tick_skip_hint: bool = False
        # 로그를 너무 많이 만들지 않기 위한 최소 관측용 플래그
        self._ws_logged_first_real: bool = False
        self._ws_logged_idle_once: bool = False
        # REAL(0B)에서 fid 10(now_price)이 0으로 파싱되어 틱이 continue 되는 케이스를
        # 소수 코드에만 1회 관측(키 타입/존재 여부 확인용)
        self._real_now_price_zero_debug_codes: Set[str] = set()
        # REAL(0B) 수신 관측용(해당 코드만 주기적 기록): GUI 멈춤 상황에서도 JSONL로 확인
        self._real_0b_seen_last_ts: Dict[str, float] = {}
        # monitor_tab.update_view에서 pending_tree_updates가 실제 렌더/삭제되는지 추적(해당 코드만 throttling)
        self._render_debug_last_ts: Dict[str, float] = {}
        # update_view가 너무 오래 걸려 UI가 멈춘 것처럼 보이는 케이스 기록(쿨다운/로그 폭주 방지용)
        self._update_view_slow_last_ts: float = 0.0
        self._setup_ui()

    def _effective_regular_session_for_stream_ticks(self) -> bool:
        """
        0B/0D 실시간 처리 허용 여부.
        - fid215 로 정규장(3/4)이면 True.
        - fid215 가 아직 없거나 빈 값만 온 경우: PC 시각 기준 KRX 정규장이면 True(폴백).
        - fid215 가 비어 있지 않은데 3/4(및 03/04)가 아니면 장외로 보고 False.
        - fid215 가 '0'/'00' 등 플레이스홀더면 비어 있음과 동일(로컬 시각 폴백).
        """
        try:
            if bool(getattr(self, "_is_regular_session_active", False)):
                return True
            if bool(getattr(self, "_ws_has_explicit_fid215", False)):
                return False
            from utils.krx_session import is_krx_regular_session

            return bool(is_krx_regular_session())
        except Exception:
            return bool(getattr(self, "_is_regular_session_active", False))

    def _ws_reg_max_codes_per_chunk(self) -> int:
        """
        REG 한 그룹당 최대 종목 수.
        단일 item 배열에 수백 종목을 넣으면 서버가 일부만 구독 처리해 틱이 안 오는 경우가 있어 분할합니다.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            n = int(float(st.get("ws_reg_max_codes_per_chunk", 80)))
        except (TypeError, ValueError):
            n = 80
        return max(20, min(n, 200))

    def _build_reg_chunks(self, codes: List[str]) -> Tuple[List[str], List[List[str]]]:
        """
        Kiwoom WS REG용 종목 정규화 및 청크 분할.
        (정규화된 코드 리스트, 청크별 item 리스트) 반환.
        """
        max_c = self._ws_reg_max_codes_per_chunk()
        seen: Set[str] = set()
        norm: List[str] = []
        for x in codes or []:
            try:
                nk = str(x).split("_")[0].zfill(6)
            except Exception:
                continue
            if nk not in seen:
                seen.add(nk)
                norm.append(nk)
        if not norm:
            return [], []
        chunks: List[List[str]] = []
        i = 0
        while i < len(norm):
            chunks.append(norm[i : i + max_c])
            i += max_c
        return norm, chunks

    async def _websocket_send_reg_subscribe(
        self, ws: Any, codes: List[str], *, log_tag: str
    ) -> None:
        """
        실시간 REG 전송.
        한 패킷에 data 항목을 여러 개 넣으면 브로커가 뒤쪽을 무시하는 경우가 있어,
        청크마다 grp_no 를 달리 해 순차 전송합니다.
        """
        norm, chunks = self._build_reg_chunks(codes)
        if not chunks:
            return

        # REG 재전송마다 장운영·진단 상태 초기화(재접속 후 fid215·폴백 재평가)
        try:
            self._ws_0s_diag_empty_logged = False
            self._ws_0s_diag_fid215_zero_logged = False
            self._ws_0s_diag_mismatch_last_ts = 0.0
            self._ws_logged_session_tick_skip_hint = False
            self._ws_has_explicit_fid215 = False
            self._market_operation_code = ""
            self._is_regular_session_active = False
        except Exception:
            pass

        nchunk = len(chunks)
        for idx, ch in enumerate(chunks):
            reg_packet: Dict[str, Any] = {
                "trnm": "REG",
                "grp_no": str(idx + 1),
                "refresh": "1",
                "market": "AL",
                "data": [{"item": ch, "type": ["0B", "0D"]}],
            }
            await ws.send(json.dumps(reg_packet))
            if nchunk > 1 and idx + 1 < nchunk:
                try:
                    await asyncio.sleep(0.06)
                except Exception:
                    pass
        # 장운영구분(0s.fid215) 별도 구독: 정규장/시간외 틱 필터 기준으로 사용
        try:
            market_packet: Dict[str, Any] = {
                "trnm": "REG",
                "grp_no": "0",
                "refresh": "1",
                "market": "AL",
                "data": [{"item": [""], "type": ["0s"]}],
            }
            await ws.send(json.dumps(market_packet))
        except Exception:
            pass
        try:
            terminal_trace(
                f"WS: {log_tag} 전송 종목={len(norm)} REG패킷={nchunk} "
                f"chunk≤{self._ws_reg_max_codes_per_chunk()} grp_no=1..{nchunk}",
                app=self.app,
                component="WS",
            )
        except Exception:
            pass
        if nchunk > 1:
            try:
                self.app.root.after(
                    0,
                    lambda: self.app.log(
                        f"📡 [시스템] 실시간 구독 분할: 총 {len(norm)}종목 → "
                        f"{nchunk}회 순차 REG(grp_no 1~{nchunk}, 청크당 최대 "
                        f"{self._ws_reg_max_codes_per_chunk()}종목)"
                    ),
                )
            except Exception:
                pass

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
            if getattr(event, "widget", None) is self._monitor_paned:
                self._request_monitor_sash_sync()
                return
            if self._release_is_on_monitor_paned_sash(event):
                self._request_monitor_sash_sync()
        except Exception:
            pass

    def _on_monitor_paned_configure(self, event: Optional[tk.Event] = None) -> None:
        """Paned 높이·세시 배치가 바뀔 때 호출. 세시 픽셀 변화 시에만 저장 예약."""
        try:
            if getattr(self, "_monitor_restoring_sash", False):
                return
            p = self._monitor_paned
            if p is None:
                return
            if event is not None and getattr(event, "widget", None) is not p:
                return
            if int(p.winfo_height()) < 24:
                return
            sp = int(p.sashpos(0))
            prev = self._monitor_prev_sash_pixel
            if prev is not None and abs(sp - prev) > 1:
                self._request_monitor_sash_sync()
            self._monitor_prev_sash_pixel = sp
        except (tk.TclError, TypeError, ValueError):
            return
        except Exception:
            return

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
            p = self._monitor_paned
            if p is not None and self._monitor_sash_initialized:
                # 이미 한 번 정상 복원되었으면, 탭 전환 재진입에서는 사용자가 드래그한 높이를 유지한다.
                try:
                    # "정상 복원"을 winfo_height만으로 판단하면,
                    # 상단 pane이 0으로 접힌 상태에서도 True로 마킹될 수 있습니다.
                    # 따라서 sashpos(0)가 어느 정도 이상인 경우에만 스킵합니다.
                    sp = int(p.sashpos(0))
                    if int(p.winfo_height()) >= 160 and sp >= 60:
                        self._monitor_prev_sash_pixel = sp
                        return
                except (tk.TclError, TypeError, ValueError):
                    pass

            def _try(attempt: int = 0) -> None:
                try:
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
                    # 레이아웃 미확정 시 대기
                    # 기존의 160 임계값은 탭 전환 직후/특정 플랫폼에서 h==0이 계속되는 경우,
                    # 복원 시도가 너무 오래 지연되거나(혹은 마지막 시도에서 강제로 pos를 찍어) 레이아웃이 고정될 수 있습니다.
                    # 여기서는 "정말 0에 가까운 경우"만 대기하고, 최소한의 높이가 잡히면 sashpos를 진행합니다.
                    if h < 30 and attempt < 24:
                        root.after(100, lambda a=attempt + 1: _try(a))
                        return
                    # 저장 안정성 이슈가 반복되어 상단(보유/대응) 높이를 5줄 기준으로 고정한다.
                    fixed_rows = max(3, int(getattr(self, "_monitor_fix_active_rows", 5)))
                    row_h = 22
                    header_pad = 34
                    try:
                        tr = getattr(self, "tree_active_monitor", None)
                        if tr is not None:
                            tr.update_idletasks()
                            rh_raw = tr.tk.call("ttk::style", "lookup", tr.cget("style"), "-rowheight")
                            row_h = int(float(rh_raw)) if str(rh_raw).strip() else 22
                    except Exception:
                        row_h = 22
                    target_top = int(header_pad + (row_h * fixed_rows))
                    # 상단 패널은 "고정 5줄" 느낌으로 최소 높이를 강하게 보장하고,
                    # 하단 패널(실시간 감시)은 남는 높이를 쓰도록 sashpos 클램프를 보수적으로 잡습니다.
                    min_top = max(140, int(target_top))
                    min_bot = 80
                    pos = int(target_top)
                    # h - min_bot 을 상단 최대치로 보되, 상단이 최소 높이(min_top)보다 작아지지 않게 보정
                    pos = max(min_top, min(pos, h - min_bot))
                    # 창이 너무 작으면 하단이 소멸할 수 있으므로 하단 최소를 우선 확보
                    if h - min_bot <= 0:
                        pos = min_top
                    if pos < min_top:
                        pos = min_top
                    try:
                        self._monitor_restoring_sash = True
                        p.sashpos(0, pos)
                    finally:
                        self._monitor_restoring_sash = False
                    try:
                        self._monitor_prev_sash_pixel = int(p.sashpos(0))
                    except (tk.TclError, TypeError, ValueError):
                        pass
                    # 실제로 높이가 잡힌 경우에만 초기화 완료로 마킹(높이 0 고정이면 다음 복원 시도도 허용)
                    try:
                        sp_now = int(p.sashpos(0))
                        if sp_now >= 60:
                            self._monitor_sash_initialized = True
                    except Exception:
                        pass
                except Exception:
                    if attempt < 24:
                        root.after(100, lambda a=attempt + 1: _try(a))

            # 탭 전환 직후에는 winfo_height 가 아직 0 으로 잡히는 경우가 있어,
            # after_idle 로 레이아웃 계산이 끝난 뒤 1회 실행하고,
            # 안전장치로 약간 뒤(클릭 직후 관찰 타이밍 보정) 추가 1회 실행합니다.
            root.after_idle(lambda: _try(0))
            root.after(350, lambda: _try(0))
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

        # 종목 선정 진행 표시용(과거 하베스팅용; 현재 FDR 선정 경로에서는 미사용)
        self.selection_progress_frame = ttk.Frame(self.mo_main_frame)
        self.selection_progress_frame.pack(fill=tk.X, pady=(0, 8))
        self.selection_progress_label = ttk.Label(
            self.selection_progress_frame,
            text="",
            font=("Malgun Gothic", 9),
            foreground=self.app.colors["primary"],
        )
        self.selection_progressbar = ttk.Progressbar(
            self.selection_progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            length=260,
        )
        self.selection_progress_visible: bool = False

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
        # 세시 이동 시 Tk 가 Paned 자체가 아닌 내부 경로로 이벤트를 처리하면 Moved/Release 만으로 저장이 빠질 수 있음.
        try:
            paned.bind("<Configure>", self._on_monitor_paned_configure, add="+")
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
            # "보유 및 대응"은 대략 5종목 정도가 보이도록 최소 높이를 보강합니다.
            paned.paneconfigure(active_frame, minsize=140)
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
            "AtrExit": "도달시 매도 기준가",
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

        # height 고정 시 요청 높이가 과소해 상단 패널이 1~2줄만 보이는 현상 유발 → 부모 확장에 맡김(가상 탭과 동일).
        self.tree_active_monitor = ttk.Treeview(
            active_inner, columns=columns, show="headings", selectmode="browse"
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

    def _set_selection_progress(self, cur: int, total: int, message: str) -> None:
        """종목선정 선행 하베스팅 진행률 UI 갱신."""
        try:
            safe_total = max(int(total), 1)
            safe_cur = min(max(int(cur), 0), safe_total)
            pct = int((safe_cur / safe_total) * 100)
            if not self.selection_progress_visible:
                self.selection_progress_label.pack(side=tk.TOP, anchor=tk.W)
                self.selection_progressbar.pack(side=tk.TOP, fill=tk.X)
                self.selection_progress_visible = True
            self.selection_progressbar.configure(maximum=safe_total, value=safe_cur)
            self.selection_progress_label.configure(
                text=f"⏳ {message} ({safe_cur}/{safe_total}, {pct}%)"
            )
        except Exception as e:
            try:
                self.app.log(f"⚠️ [선정 진행률] UI 갱신 실패: {e}")
            except Exception:
                pass

    def _hide_selection_progress(self, done_message: str = "") -> None:
        """선정 진행률 표시를 숨깁니다."""
        try:
            if done_message:
                self.lbl_monitor_status.config(
                    text=f"상태: {done_message}", foreground=self.app.colors["primary"]
                )
            self.selection_progress_label.pack_forget()
            self.selection_progressbar.pack_forget()
            self.selection_progress_visible = False
            self.selection_progressbar.configure(value=0)
            self.selection_progress_label.configure(text="")
        except Exception:
            pass

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
        """선정 스캔 후 로그 출력 + 감시 대상 즉시 등록 (확인 팝업 없음).

        일봉은 로컬 DB 하베스팅 없이 ``strategy_selector.select_monitoring_targets`` 의
        ``api_only`` 경로로 **FDR 일봉(종목코드 직접)** 만 조회합니다(키움 일봉 미사용).
        """
        df_snap: Optional[pd.DataFrame] = None

        def _finalize_ui(done_message: str = "종목 선정 대기") -> None:
            def _job() -> None:
                try:
                    self._hide_selection_progress(done_message)
                except Exception:
                    pass
                try:
                    if getattr(self, "btn_select", None) is not None:
                        self.btn_select.config(state=tk.NORMAL)
                except Exception:
                    pass
            try:
                self.app.root.after(0, _job)
            except Exception:
                _job()

        try:
            self.app.log(
                "📡 [선정] FDR 일봉(api_only·종목코드 직접, 키움 일봉 미사용)으로 유니버스 스캔을 시작합니다."
            )
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.app.settings,
                logger_callback=self.app.log,
                data_manager=self.app.data_manager,
                selection_ohlcv_source="api_only",
                kiwoom_access_token=None,
            )
            if df_result is not None and not df_result.empty:
                df_snap = df_result.copy()
        except Exception as e:
            import traceback

            self.app.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.app.log(f"DEBUG Traceback:\n{traceback.format_exc()}")

        def finish_on_main() -> None:
            try:
                if df_snap is None or df_snap.empty:
                    self.app.log("⚠️ [선정] 조건에 부합하는 종목이 없습니다.")
                    return
                self._log_selection_results(self.app, df_snap)
                # ``api_only`` 에서도 ``selected_targets.csv`` 를 명시 경로로 한 번 더
                # 기록한다(작업 디렉터리·인코딩 통일, 비교·백업용).
                try:
                    _csv_path = os.path.join(os.getcwd(), "selected_targets.csv")
                    df_snap.to_csv(_csv_path, index=False, encoding="utf-8-sig")
                    self.app.log(f"💾 [선정] 결과 저장: {_csv_path}")
                except Exception as _csv_e:
                    try:
                        self.app.log(f"⚠️ [선정] selected_targets.csv 저장 실패: {_csv_e}")
                    except Exception:
                        pass
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

        _finalize_ui("종목 선정 완료")

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
                terminal_trace(
                    "_ws_client_loop 바깥 루프 is_monitoring=True → 접속 시도",
                    app=self.app,
                    component="WS",
                )
                self.app.root.after(0, self.app.log, "📡 [시스템] 실시간 서버 접속 시도...")
                
                access_token = self.app.auth_manager.get_auth_token()
                if not access_token:
                    terminal_trace(
                        "WS: get_auth_token 실패 → 10초 sleep 후 재시도",
                        app=self.app,
                        component="WS",
                    )
                    self.app.root.after(0, self.app.log, "⏳ [시스템] 거래 서버 점검 또는 인증 대기 중... (10초 후 재시도)")
                    await asyncio.sleep(10)
                    continue
                
                stream_codes = []
                try:
                    stream_codes = self.app.stream_subscription_codes()
                except Exception:
                    stream_codes = list(getattr(self.app.trading_controller, "monitor_codes", []) or [])
                terminal_trace(
                    f"WS: stream_subscription_codes 개수={len(stream_codes)} "
                    f"앞10개={stream_codes[:10]!r}",
                    app=self.app,
                    component="WS",
                )
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
                self._last_ws_url = ws_url
                terminal_trace(f"WS: connect → {ws_url!r}", app=self.app, component="WS")
                
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws_idle_recv_count = 0
                    self._ws_real_trace_count = 0
                    # 새 소켓마다 시그니처 초기화 — 재연결 직후 이전 세션과 비교하면 안 됨
                    self._last_stream_reg_sig = None
                    self._last_stream_reg_user_log_ts = 0.0
                    terminal_trace(
                        "WS: TCP 연결 완료, LOGIN 패킷 전송",
                        app=self.app,
                        component="WS",
                    )
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
                                                await self._websocket_send_reg_subscribe(
                                                    ws, list(_sc), log_tag="REG 재전송"
                                                )
                                                try:
                                                    terminal_trace(
                                                        f"WS: REG 재전송 n={len(sig)} sig_head={list(sig)[:8]!r}",
                                                        app=self.app,
                                                        component="WS",
                                                    )
                                                except Exception:
                                                    pass
                                                try:
                                                    self._invalidate_stream_sub_cache()
                                                except Exception:
                                                    pass
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
                            self._ws_idle_recv_count = 0
                            raw_data = json.loads(msg)
                            data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                            
                            for packet in data_list:
                                trnm = packet.get('trnm')
                                # 디버그 로그가 과도하게 많아지는 것을 방지하기 위해
                                # 수신 트레이스는 'LOGIN/REG' 같이 문제 원인 추적에 필요한 케이스만 남깁니다.
                                if trnm in ("LOGIN", "REG"):
                                    try:
                                        terminal_trace(
                                            f"WS: 수신 trnm={trnm!r} return_code={packet.get('return_code')!r}",
                                            app=self.app,
                                            component="WS",
                                        )
                                    except Exception:
                                        pass
                                elif trnm == "REAL":
                                    try:
                                        rc = int(getattr(self, "_ws_real_trace_count", 0))
                                        if rc < 8:
                                            self._ws_real_trace_count = rc + 1
                                            terminal_trace(
                                                f"WS: REAL 수신 ({rc + 1}/8 샘플)",
                                                app=self.app,
                                                component="WS",
                                            )
                                    except Exception:
                                        pass
                                    if not self._ws_logged_first_real:
                                        self._ws_logged_first_real = True
                                        try:
                                            self.app.log("✅ [WS] REAL 틱이 처음 수신되었습니다.")
                                        except Exception:
                                            pass
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
                                        await self._websocket_send_reg_subscribe(
                                            ws, list(_sc), log_tag="LOGIN 후 REG"
                                        )
                                        terminal_trace(
                                            f"WS: LOGIN 성공 후 REG 전송 n={len(_sc)}",
                                            app=self.app,
                                            component="WS",
                                        )
                                        try:
                                            self._invalidate_stream_sub_cache()
                                        except Exception:
                                            pass
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
                                            # 콘솔 스팸과 무관하게, 로그인 실패 원인을 JSONL로 축적해 나중에 정확히 검색 가능
                                            self._realtime_session_logger.log(
                                                "ws_login_failed",
                                                {
                                                    "return_code": packet.get("return_code"),
                                                    "return_msg": packet.get("return_msg"),
                                                    "login_fail_streak": self._ws_login_fail_streak,
                                                    "use_mock": getattr(config, "USE_MOCK", True),
                                                    "ws_url": getattr(self, "_last_ws_url", ""),
                                                },
                                            )
                                        except Exception:
                                            # 로깅 실패는 매매/실시간 흐름을 막지 않도록 삼킵니다.
                                            pass
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
                            try:
                                self._ws_idle_recv_count = int(self._ws_idle_recv_count) + 1
                            except Exception:
                                self._ws_idle_recv_count = 1
                            nidle = self._ws_idle_recv_count
                            # debug_terminal_trace가 꺼져도 "REAL 수신이 끊긴 것 같은지" 확인할 수 있도록
                            # 일정 횟수 이상일 때만 1줄 앱 로그로 남깁니다.
                            if (not self._ws_logged_idle_once) and nidle >= 6:
                                self._ws_logged_idle_once = True
                                try:
                                    self.app.log(
                                        f"⏳ [WS] ws.recv 타임아웃 누적 {nidle}회 (REAL 수신 없음 가능)"
                                    )
                                except Exception:
                                    pass
                            if nidle in (1, 2, 4, 8, 16, 32, 64) or (nidle % 128 == 0):
                                terminal_trace(
                                    f"WS: ws.recv 5초 타임아웃 누적 {nidle}회 "
                                    f"(서버 패킷 없음·대기 중일 수 있음)",
                                    app=self.app,
                                    component="WS",
                                )
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

    def _invalidate_stream_sub_cache(self) -> None:
        """REG 갱신 후 구독 집합 캐시를 비웁니다."""
        try:
            self._stream_sub_cache_set = None
            self._stream_sub_cache_ts = 0.0
        except Exception:
            pass

    def _stream_subscription_zfilled_set(self) -> Set[str]:
        """현재 웹소켓에 올릴 구독 코드(6자리) 집합. 짧은 TTL 캐시."""
        try:
            now = time.time()
            ttl = 5.0
            cs = getattr(self, "_stream_sub_cache_set", None)
            ts = float(getattr(self, "_stream_sub_cache_ts", 0.0))
            if cs is not None and (now - ts) < ttl:
                return cs
        except Exception:
            pass
        try:
            sc = self.app.stream_subscription_codes()
        except Exception:
            sc = []
        try:
            out: Set[str] = set()
            for x in sc:
                try:
                    out.add(str(x).split("_")[0].zfill(6))
                except Exception:
                    continue
            self._stream_sub_cache_set = out
            self._stream_sub_cache_ts = time.time()
            return out
        except Exception:
            return set()

    def _resolve_monitor_info_key(self, nk: str) -> Optional[str]:
        """
        WS 종목코드(nk, 6자리)에 대응하는 monitor_info 딕셔너리 키.
        키가 '3490'·``2380.0`` 등 비표준일 때도 매칭합니다.
        """
        try:
            nk_canon = canonical_stock_code(nk)
            mi = getattr(self.app, "monitor_info", None) or {}
            if nk_canon and nk_canon in mi:
                return nk_canon
            if nk in mi:
                return nk
            for k in mi:
                try:
                    if canonical_stock_code(k) == nk_canon:
                        return str(k)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _ensure_monitor_info_stub_for_stream(self, nk: str) -> None:
        """
        REG 구독 목록에는 있는데 monitor_info 에 행이 없을 때 틱이 전부 버려지는 문제 방지.
        """
        try:
            if not isinstance(getattr(self.app, "monitor_info", None), dict):
                self.app.monitor_info = {}
            mi = self.app.monitor_info
            if nk in mi:
                return
            nm = nk
            try:
                ws = getattr(self.app, "watchlist_store", None)
                if ws is not None:
                    nm = str(ws.get_display_name(nk))
            except Exception:
                nm = nk
            stub: Dict[str, Any] = {
                "name": nm or nk,
                "ma20": 0.0,
                "ma60": 0.0,
                "prev_vol": 0.0,
                "score": 0.0,
                "ma_buffer_scale": 1.0,
                "open": 0.0,
                "last_price": 0.0,
                "sel_track": "B",
                "stream_stub": True,
            }
            mi[nk] = stub
            tc = getattr(self.app, "trading_controller", None)
            if tc is not None:
                if not isinstance(getattr(tc, "monitor_info", None), dict):
                    tc.monitor_info = {}
                tc.monitor_info[nk] = dict(stub)
        except Exception:
            pass

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

            vals = item_data.get('values', {}) or {}
            # 브로커에 따라 type 이 "0B"/"0S" 처럼 대문자로 올 수 있음 → 분기·필터가 영원히 안 타는 문제 방지
            try:
                _raw_pt = item_data.get("type", "0B")
                p_type = str(_raw_pt if _raw_pt is not None else "0B").strip().lower()
            except Exception:
                p_type = "0b"
            # 장운영구분(0s.fid215) 수신: 정규장 여부를 먼저 갱신하고 종목 경로는 건너뜁니다.
            if p_type == "0s":
                try:
                    op_code_raw = vals.get("215")
                    if op_code_raw is None:
                        op_code_raw = vals.get(215)
                    op_code = str(op_code_raw if op_code_raw is not None else "").strip().lower()
                    # 모의투자 등에서 215='0' 이 오면 "명시적 fid" 로 취급하면 _ws_has_explicit_fid215 가 켜져
                    # 정규장이어도 체결 틱이 전부 스킵된다(REAL 수신만 되고 UI·엔진 무반응).
                    if op_code in ("0", "00"):
                        if not self._ws_0s_diag_fid215_zero_logged:
                            self._ws_0s_diag_fid215_zero_logged = True
                            try:
                                self.app.log(
                                    "⚠️ [WS-0s 진단] fid215=0/00 — 브로커 미정·플레이스홀더로 보고 "
                                    "PC시각 KRX 정규장 폴백을 사용합니다."
                                )
                            except Exception:
                                pass
                        op_code = ""
                    if not op_code:
                        # 215 비어 있음: WS 플래그는 OFF 유지하되, 틱은 _effective_regular_session_for_stream_ticks() 에서 로컬 정규장 폴백
                        if not self._ws_0s_diag_empty_logged:
                            self._ws_0s_diag_empty_logged = True
                            try:
                                ks = list(vals.keys())[:30]
                            except Exception:
                                ks = []
                            self.app.log(
                                "⚠️ [WS-0s 진단] fid215 비어 있음·미포함 — "
                                f"raw={op_code_raw!r} type(raw)={type(op_code_raw).__name__} "
                                f"values키샘플={ks!r} → PC시각 KRX 정규장이면 체결 표시(폴백)"
                            )
                    else:
                        self._ws_has_explicit_fid215 = True
                        prev_code = str(getattr(self, "_market_operation_code", "") or "")
                        next_regular = op_code in self._regular_session_op_codes
                        prev_regular = bool(getattr(self, "_is_regular_session_active", False))
                        self._market_operation_code = op_code
                        self._is_regular_session_active = next_regular
                        if op_code != prev_code or next_regular != prev_regular:
                            self.app.log(
                                f"🕒 [WS] 장운영구분={op_code} "
                                f"(정규장={'ON' if self._is_regular_session_active else 'OFF'})"
                            )
                        # 3/4가 아닌데 값은 온 경우: 집합 불일치 의심 시 90초마다 1줄로 원문 확인
                        if not next_regular:
                            now_ts = time.time()
                            if now_ts - float(
                                getattr(self, "_ws_0s_diag_mismatch_last_ts", 0.0) or 0.0
                            ) >= 90.0:
                                self._ws_0s_diag_mismatch_last_ts = now_ts
                                self.app.log(
                                    "⚠️ [WS-0s 진단] 장운영코드가 3/4가 아님 — "
                                    f"norm={op_code!r} raw={op_code_raw!r} "
                                    f"type(raw)={type(op_code_raw).__name__} → 정규장=OFF(틱 차단)"
                                )
                except Exception:
                    pass
                continue

            mkey = self._resolve_monitor_info_key(code)
            if mkey is None:
                if code in self._stream_subscription_zfilled_set():
                    self._ensure_monitor_info_stub_for_stream(code)
                    mkey = self._resolve_monitor_info_key(code)
            if mkey is None:
                continue
            mi = getattr(self.app, "monitor_info", None) or {}
            if mkey not in mi:
                continue
            info = mi[mkey]
            # 정규장 외(브로커 fid215 명시 + 비정규) 또는 로컬 장외: 실시간 감시 경로에서 제외
            if p_type in ("0b", "0d") and not self._effective_regular_session_for_stream_ticks():
                try:
                    if (
                        p_type == "0b"
                        and not self._ws_logged_session_tick_skip_hint
                    ):
                        self._ws_logged_session_tick_skip_hint = True
                        self.app.log(
                            "⚠️ [WS 진단] 체결이 장외로 스킵됨 — "
                            "fid215가 정규장 외 코드이며 로컬 시간도 정규장이 아닙니다."
                        )
                except Exception:
                    pass
                continue

            def get_val(fid, default=0):
                v = vals.get(str(fid))
                if v is None or v == "":
                    return default
                try:
                    return float(str(v).replace("+", "").replace("-", "").strip())
                except Exception:
                    return default

            if p_type == "0b":
                now_price = int(get_val(10))
                # 체결 전·장 시작 직후 fid 10 이 0인 패킷이 섞일 수 있음.
                # return 이면 같은 REAL 배치의 다른 종목 틱까지 전부 버려지므로 continue.
                debug_codes = {"267250", "454910", "016360", "042700"}
                if now_price == 0:
                    try:
                        if code in debug_codes:
                            # 1) now_price==0 최초 관측(키/values 타입 확인용)
                            if code not in self._real_now_price_zero_debug_codes:
                                self._real_now_price_zero_debug_codes.add(code)
                            # 키가 문자열인지/정수인지 파악용 샘플
                            try:
                                keys_sample = list(vals.keys())[:25]
                            except Exception:
                                keys_sample = []
                            # fid 10이 어떤 형태로 들어오는지(문자열/정수) 확인
                            v10_str = None
                            v10_int = None
                            try:
                                v10_str = vals.get("10")
                            except Exception:
                                pass
                            try:
                                v10_int = vals.get(10)
                            except Exception:
                                pass
                            try:
                                self._realtime_session_logger.log(
                                    "ws_real_0b_now_price_zero_seen",
                                    {
                                        "code": code,
                                        "fid10_str": v10_str,
                                        "fid10_int": v10_int,
                                        "keys_sample": keys_sample,
                                        "trnm": "REAL",
                                        "p_type": p_type,
                                    },
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # 주기적(최소 5초)으로라도 수신 자체가 있는지 기록
                    try:
                        if code in debug_codes:
                            last_ts = float(self._real_0b_seen_last_ts.get(code, 0.0) or 0.0)
                            import time as _t
                            if _t.time() - last_ts >= 5.0:
                                self._real_0b_seen_last_ts[code] = _t.time()
                                self._realtime_session_logger.log(
                                    "ws_real_0b_seen_periodic",
                                    {"code": code, "now_price": now_price, "volume": int(get_val(13)), "p_type": p_type},
                                )
                    except Exception:
                        pass
                    continue
                volume = int(get_val(13))
                trde_val = int(get_val(26) // 1000)
                # fid 11(diff), 12(ratio)는 websocket payload에서 keys 타입이
                # 문자열('11','12') 또는 정수(11,12)로 올 수 있다.
                # 한쪽만 가정하면 매 틱 기본값('0')만 들어가 '대비'가 고정처럼 보일 수 있어
                # 둘 다 확인하도록 한다.
                def _normalize_ws_num_token(v: object) -> str:
                    """
                    websocket fid 토큰(문자열/숫자)을 UI/엔진이 처리 가능한 형태로 정규화한다.
                    - '▲'/'▼'가 섞여 들어오면 +/−로 치환
                    - '—'/'-'/'None'/빈 문자열은 '0'로 치환
                    """
                    try:
                        if v is None:
                            return "0"
                        s = str(v).strip()
                        if not s or s in ("-", "—", "–"):
                            return "0"
                        # 화살표 → 부호로 정규화
                        if "▲" in s or "△" in s:
                            s = s.replace("▲", "+").replace("△", "+")
                        if "▼" in s or "▽" in s:
                            s = s.replace("▼", "-").replace("▽", "-")
                        # 기존에 부호가 없는데 중간에 +/− 치환이 없었으면, 첫 글자 기준으로 처리하지 않는다.
                        # (fid 값이 예: '+1.23%' 또는 '-1.23%' 형태일 수 있어 그대로 유지)
                        # 쉼표만 제거(표시용은 남겨도 되지만, 엔진 파싱은 ',' 제거 가정이 많음)
                        s = s.replace(",", "").strip()
                        return s
                    except Exception:
                        return "0"

                ratio_raw = vals.get(12) if 12 in vals else vals.get("12", vals.get(12, "0"))
                diff_raw = vals.get(11) if 11 in vals else vals.get("11", vals.get(11, "0"))
                ratio_val = _normalize_ws_num_token(ratio_raw)
                diff_val = _normalize_ws_num_token(diff_raw)
                vol_power = get_val(228)
                # 수신 자체를 GUI 멈춤 상황에서도 추적(해당 코드만 주기적 기록)
                try:
                    if code in debug_codes:
                        last_ts = float(self._real_0b_seen_last_ts.get(code, 0.0) or 0.0)
                        import time as _t
                        if _t.time() - last_ts >= 5.0:
                            self._real_0b_seen_last_ts[code] = _t.time()
                            self._realtime_session_logger.log(
                                "ws_real_0b_seen_periodic",
                                {"code": code, "now_price": now_price, "volume": volume, "p_type": p_type},
                            )
                except Exception:
                    pass
                info['price'] = now_price
                info['ratio'] = ratio_val
                info['diff'] = diff_val
                info['volume'] = volume
                info['trde_val'] = trde_val
                info['vol_power'] = vol_power
                self.app._on_realtime_data_received(code, now_price, volume, trde_val, vol_power=vol_power)
            elif p_type == "0d":
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
                # [정합성] 호가(0D)는 봉(OHLCV) 업데이트를 유발하면 안 됩니다.
                # 체결(0B) 경로만 봉을 갱신하고, 0D는 수급/호가 정보만 전달합니다.
                self.app._on_realtime_data_received(
                    code,
                    lp,
                    info.get("volume", 0),
                    info.get("trde_val", 0),
                    total_ask=total_ask,
                    total_bid=total_bid,
                    _orderbook_only=True,
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
            return monitor_selection_codes_set(mc)
        except Exception:
            return set()

    def _watchlist_only_codes_set(self) -> set:
        """
        관심목록에만 있고 감시 선정(monitor_codes)에는 없는 코드(6자리).
        감시 선정 종목이 관심목록에도 있으면 여기에 포함되지 않으므로 상·하단 트리에 정상 표시된다.
        """
        try:
            wl = self._watchlist_codes_set()
            mc = getattr(self.app.trading_controller, "monitor_codes", None) or []
            # 가상 보유(virtual_positions)는 화면상 "보유 및 대응"에 항상 보여야 합니다.
            # watchlist_store 선적재로 인해 wl_only에 포함되더라도, update_view()에서 wl_only면
            # 두 트리 모두 삭제되기 때문에(우선순위 버그) virtual_positions 코드는 감시 선정으로 간주해 제외합니다.
            try:
                vpd = getattr(self.app.portfolio, "virtual_positions", None) or {}
                vp_codes = [str(k).split("_")[0].zfill(6) for k in vpd.keys()]
            except Exception:
                vp_codes = []
            return watchlist_only_codes_set(wl, list(mc) + vp_codes)
        except Exception:
            return set()

    def _monitor_ratio_direction(self, ratio_val: object) -> str:
        """등락률 표시값 → up / down / stable (줄무늬 행 태그용). ▲/▼ 접두와 +/- 숫자 모두 허용."""
        try:
            raw = str(ratio_val).strip()
            if not raw:
                return "stable"
            if raw.startswith(("▲", "△")):
                return "up"
            if raw.startswith(("▼", "▽")):
                return "down"
            s = raw.replace("%", "").replace(",", "").strip()
            s = s.lstrip("+")
            try:
                v = float(s)
            except ValueError:
                if "▼" in raw or "▽" in raw:
                    return "down"
                if "▲" in raw or "△" in raw:
                    return "up"
                return "stable"
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

    def _patch_monitor_info_status_text(self, nk: str, text: str) -> None:
        """
        신호상태 열은 `_compose_monitor_row_values` 가 `status_text` 없으면 '대기'를 씁니다.
        워밍업은 트리만 직접 갱신하면 이후 `update_watchlist` 등으로 행이 다시 그려질 때
        monitor_info 에 값이 없어 보유 종목만 영구히 '대기'로 보이므로, 여기서 동기화합니다.
        """
        try:
            mi = getattr(self.app, "monitor_info", None)
            if not isinstance(mi, dict):
                return
            tree_iid = str(nk).split("_")[0].zfill(6)
            key: Optional[str] = None
            if tree_iid in mi:
                key = tree_iid
            else:
                for k in mi:
                    try:
                        if str(k).split("_")[0].zfill(6) == tree_iid:
                            key = str(k)
                            break
                    except Exception:
                        continue
            if key is None:
                return
            ent = mi.get(key)
            if isinstance(ent, dict):
                ent["status_text"] = str(text)
            # app.monitor_info 와 trading_controller.monitor_info 가 얕은 복사로 갈라진 경우 동기화
            try:
                tc = getattr(self.app, "trading_controller", None)
                tmi = getattr(tc, "monitor_info", None) if tc is not None else None
                if isinstance(tmi, dict) and tmi is not mi:
                    te = tmi.get(key)
                    if isinstance(te, dict):
                        te["status_text"] = str(text)
            except Exception:
                pass
        except Exception:
            pass

    def _ratio_cell_display_for_monitor_info(
        self, nk: str, info: Dict[str, Any]
    ) -> str:
        """감시 트리 등락률 표시 — SignalEngine 과 동일(▲/▼ 고정)."""
        try:
            now_p = float(
                info.get("price")
                if info.get("price") not in (None, "", 0)
                else (info.get("last_price") or 0)
            )
        except (TypeError, ValueError):
            now_p = 0.0
        try:
            se = getattr(self.app, "signal_engine", None)
            if se is not None and hasattr(se, "ratio_cell_display_for_monitor_tree"):
                return se.ratio_cell_display_for_monitor_tree(info, float(now_p))
        except Exception:
            pass
        return str(info.get("ratio", "0"))

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
        ratio_disp = self._ratio_cell_display_for_monitor_info(nk, info)
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
            monitor_signal_status_display_text(info),
            f"{vol_pow:.1f}%",
            _monitor_fmt_bid_ask(info),
            f"{vol_i:,}",
        )

    def maybe_reconcile_monitor_trees(
        self,
        monitor_info: Optional[Dict[str, Any]],
        virtual_positions: Any,
        min_interval_s: float = 10.0,
    ) -> None:
        """
        monitor_info 에 있어야 할 종목이 틱 부재로 트리에서 빠진 경우 복구.
        (15:30 봉 마감 등 체결이 없는 종목은 pending_tree_updates 에 안 올라와 행이 사라질 수 있음)
        """
        import time as _t
        t0 = _t.perf_counter()
        try:
            if not self.tree_monitor or not self.tree_active_monitor:
                return
            now = time.time()
            if now - self._last_monitor_tree_reconcile_ts < min_interval_s:
                return
            self._last_monitor_tree_reconcile_ts = now

            mi = monitor_info or getattr(self.app, "monitor_info", None) or {}
            wl_only = self._watchlist_only_codes_set()
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
                if nk in wl_only:
                    continue
                expected.add(nk)

            mc_targets = self._monitor_target_codes_set()

            # 관심목록 전용(감시 미선정) 숨김: 양쪽 트리에서 제거
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                for iid in list(tree.get_children()):
                    z = str(iid).split("_")[0].zfill(6)
                    if z in wl_only and tree.exists(iid):
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
                direction = self._monitor_ratio_direction(
                    self._ratio_cell_display_for_monitor_info(nk, info)
                )
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
                    if z in mc_targets or self._virtual_position_is_active(z, vp):
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
        finally:
            try:
                dt = _t.perf_counter() - t0
                # UI가 멈춘 것처럼 보이는 구간을 더 잘 연결하기 위한 slow log
                if dt >= 0.50:
                    sl = getattr(self, "_realtime_session_logger", None)
                    if sl is not None:
                        sl.log(
                            "monitor_tab_maybe_reconcile_monitor_trees_slow",
                            {
                                "dt_sec": float(dt),
                                "min_interval_s": float(min_interval_s),
                                "pending_size_hint": None,
                            },
                        )
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
            wl_only = self._watchlist_only_codes_set()
            for iid in list(self.tree_active_monitor.get_children()):
                nk = str(iid).split("_")[0].zfill(6)
                if nk in wl_only:
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
                direction = self._monitor_ratio_direction(
                    self._ratio_cell_display_for_monitor_info(nk, info)
                )
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
                t0 = time.perf_counter()
                items = list(updates.items())
                updates.clear()
                wl_only = self._watchlist_only_codes_set()
                mc_targets = self._monitor_target_codes_set()
                debug_seen = False
                try:
                    st_dbg = getattr(self.app, "settings", None) or {}
                    debug_trace_enabled = bool(
                        st_dbg.get("enable_ui_update_step_debug", False)
                    )
                except Exception:
                    debug_trace_enabled = False
                # UI가 멈출 정도로 관측 로그가 폭주하면 Tk 메인스레드가 더 오래 점유될 수 있습니다.
                # 따라서 step 로그는 디버그 대상 종목을 1개로 제한합니다.
                debug_codes = (
                    {"267250", "454910", "016360", "042700"}
                    if debug_trace_enabled
                    else set()
                )
                # Tk 호출 직전/직후 위치 추적용.
                # - "5초 throttle 통과한 경우에만 step을 기록"하면 UI가 그 전에 멈출 때 로그가 0건이 될 수 있어,
                #   debug_codes 포함이면 step 기록을 항상 시도하되, step 자체를 1초 throttle로 제한합니다.
                debug_enable_by_code: Dict[str, bool] = {}
                debug_step_last_ts: Dict[Tuple[str, str], float] = {}
                # step별로 너무 촘촘한 로그를 남기지 않기 위한 허용 목록
                allowed_steps: Set[str] = {
                    "item_start",
                    "vp_active_check_start",
                    "other_tree_exists_check_start",
                    "target_tree_exists_check_start",
                    "target_tree_item_update_call",
                    "target_tree_item_values_update_done",
                    "target_tree_item_tags_update_skipped",
                    "target_tree_item_tags_update_done",
                    "target_tree_item_update_exception",
                }
                # 디버그 코드가 아닌 항목에서 먼저 Tk가 멈출 수도 있으므로,
                # items 루프의 직전 "마지막으로 처리 시작한 코드"를 공통 로그로 1초 throttle 남깁니다.
                debug_any_last_ts: float = 0.0

                # 한 번 렌더에 너무 많은 항목을 처리하면 Tk 메인 이벤트 루프가 오래 점유되어
                # "전체 UI 멈춤"처럼 보일 수 있습니다. 나머지는 pending updates에 다시 넣고 다음 프레임에 처리합니다.
                max_items = 25
                try:
                    st = getattr(self.app, "settings", None) or {}
                    max_items = int(float(st.get("realtime_monitor_max_items_per_render", 25)))
                except Exception:
                    max_items = 25
                max_items = max(5, min(max_items, 200))
                # 큐가 많이 밀린 프레임은 처리량을 동적으로 높여 "표시 지연"을 줄입니다.
                try:
                    st = getattr(self.app, "settings", None) or {}
                    busy_threshold = int(
                        float(st.get("realtime_monitor_max_items_busy_threshold", 70))
                    )
                    busy_max_items = int(
                        float(st.get("realtime_monitor_max_items_per_render_busy", 60))
                    )
                except Exception:
                    busy_threshold = 70
                    busy_max_items = 60
                busy_threshold = max(10, min(busy_threshold, 1000))
                busy_max_items = max(max_items, min(busy_max_items, 250))
                if len(items) >= busy_threshold:
                    max_items = busy_max_items
                if len(items) > max_items:
                    remain = items[max_items:]
                    items = items[:max_items]
                    try:
                        updates.update({k: v for k, v in remain})
                    except Exception:
                        # 재삽입이 실패해도 최소한 현재 프레임 렌더는 수행합니다.
                        pass
                    # 디버그 코드에서만 확인용으로 1회 로그
                    if debug_trace_enabled:
                        try:
                            sl = getattr(self, "_realtime_session_logger", None)
                            if sl is not None:
                                sl.log(
                                    "monitor_tab_update_view_chunked",
                                    {"processed": max_items, "remain": len(remain)},
                                )
                        except Exception:
                            pass

                for iid_raw, (values, tag, _) in items:
                    # pending 키·monitor_info 키가 3490/003490로 섞여도 트리 iid는 항상 6자리로 통일
                    iid = str(iid_raw).split("_")[0].zfill(6)
                    cid = iid
                    if debug_trace_enabled:
                        try:
                            now_ts = time.time()
                            if now_ts - debug_any_last_ts >= 1.0:
                                debug_any_last_ts = now_ts
                                self._realtime_session_logger.log(
                                    "monitor_tab_update_view_any_item_start",
                                    {"code": cid, "tag": str(tag)},
                                )
                        except Exception:
                            pass
                    try:
                        if cid in debug_codes:
                            debug_seen = True
                            # cid가 디버그 대상이면 step 로깅은 throttle과 무관하게 활성화합니다.
                            # (item_seen 로그만 throttle 하고, tk_step은 hang 직전까지 최대한 남기기 위함)
                            debug_enable_by_code[cid] = True
                            now_t = time.time()
                            last_t = float(self._render_debug_last_ts.get(cid, 0.0) or 0.0)
                            if now_t - last_t >= 5.0:
                                self._render_debug_last_ts[cid] = now_t
                                self._realtime_session_logger.log(
                                    "monitor_tab_update_view_item_seen",
                                    {
                                        "code": cid,
                                        "tag": str(tag),
                                        "wl_only": cid in wl_only,
                                        "values_price": values[4] if isinstance(values, (list, tuple)) and len(values) > 4 else None,
                                    },
                                )
                        else:
                            debug_enable_by_code[cid] = False
                    except Exception:
                        pass

                    def _step_log(step: str, extra: Optional[Dict[str, Any]] = None) -> None:
                        """debug_enable_by_code 된 코드에 대해서만 단계 로그를 남깁니다."""
                        try:
                            if not debug_trace_enabled:
                                return
                            if not debug_enable_by_code.get(cid, False):
                                return
                            if step not in allowed_steps:
                                return
                            now_ts = time.time()
                            last_ts = debug_step_last_ts.get((cid, step), 0.0) or 0.0
                            # step별 최소 간격: 5초
                            if now_ts - last_ts < 5.0:
                                return
                            debug_step_last_ts[(cid, step)] = now_ts
                            payload: Dict[str, Any] = {"code": cid, "step": step}
                            if extra:
                                payload.update(extra)
                            self._realtime_session_logger.log(
                                "monitor_tab_update_view_tk_step",
                                payload,
                            )
                        except Exception:
                            pass

                    # 이 로그 이후에 Tk 호출에서 멈추면, "step 직전" 로그가 마지막으로 남습니다.
                    _step_log("item_start", {"tag": str(tag), "price": values[4] if isinstance(values, (list, tuple)) and len(values) > 4 else None})
                    if cid in wl_only:
                        _step_log("wl_only_delete_loop_start")
                        for tree in (self.tree_monitor, self.tree_active_monitor):
                            if tree and tree.exists(iid):
                                _step_log(
                                    "wl_only_tree_delete_call",
                                    {"tree": "monitor" if tree is self.tree_monitor else "active"},
                                )
                                tree.delete(iid)
                        try:
                            self._vp_ui_inactive_streak.pop(iid, None)
                            self._vp_ever_in_holdings.pop(iid, None)
                        except Exception:
                            pass
                        _step_log("wl_only_continue_end")
                        continue
                    # 보유 여부에 따라 타겟 트리 결정
                    _step_log("vp_active_check_start")
                    is_active_raw = self._virtual_position_is_active(
                        iid, virtual_positions
                    )
                    _step_log("vp_active_check_done", {"is_active_raw": bool(is_active_raw)})
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
                        _step_log("not_active_and_not_in_targets_delete_loop_start")
                        for tr in (self.tree_active_monitor, self.tree_monitor):
                            if tr and tr.exists(iid):
                                _step_log(
                                    "not_active_delete_call",
                                    {"tree": "active" if tr is self.tree_active_monitor else "monitor"},
                                )
                                try:
                                    tr.delete(iid)
                                except Exception:
                                    pass
                        try:
                            self._vp_ui_inactive_streak.pop(iid, None)
                            self._vp_ever_in_holdings.pop(iid, None)
                        except Exception:
                            pass
                        _step_log("not_active_and_not_in_targets_continue_end")
                        continue
                    target_tree = self.tree_active_monitor if is_active else self.tree_monitor
                    other_tree = self.tree_monitor if is_active else self.tree_active_monitor
                    
                    # 반대편 트리에 있으면 삭제 (이동 효과)
                    _step_log("other_tree_exists_check_start", {"target": "active" if target_tree is self.tree_active_monitor else "monitor"})
                    if other_tree.exists(iid):
                        _step_log("other_tree_delete_call")
                        other_tree.delete(iid)
                        
                    # [v4.5.42] 행의 인덱스와 기본 태그 정의 (안전한 index 추출)
                    _step_log("target_tree_exists_check_start")
                    if not target_tree.exists(iid):
                        _step_log("target_tree_insert_call")
                        target_tree.insert("", tk.END, iid=iid, values=values)
                    
                    # [Composite Tag Selection] v4.6.5
                    _step_log("target_tree_index_call")
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
                    _step_log("target_tree_item_read_call")
                    curr = target_tree.item(iid)
                    try:
                        st_ui = getattr(self.app, "settings", None) or {}
                        allow_strat_type_live_update = bool(
                            st_ui.get("allow_monitor_strat_type_live_update", False)
                        )
                    except Exception:
                        allow_strat_type_live_update = False
                    _step_log("target_tree_item_compare_call", {"curr_values_len": len(curr.get("values", ())) if isinstance(curr, dict) else None})
                    curr_values = curr.get("values", ()) if isinstance(curr, dict) else ()
                    curr_tags = curr.get("tags", ()) if isinstance(curr, dict) else ()
                    # 전략유형(StratType)은 실시간 틱마다 변할 필요가 없어,
                    # 기본값으로 기존 화면 값을 유지해 깜빡임을 줄입니다.
                    if (
                        not allow_strat_type_live_update
                        and isinstance(curr_values, (list, tuple))
                        and len(curr_values) > 3
                        and isinstance(values, (list, tuple))
                        and len(values) > 3
                    ):
                        try:
                            values = list(values)
                            values[3] = curr_values[3]
                            values = tuple(values)
                        except Exception:
                            pass
                    # 도달시 매도 기준가(AtrExit)는 강제 최소 주입 경로에서 '—'가 들어올 수 있어,
                    # 기존 값이 유효하면 유지해 보유 종목 기준가 깜빡임/소실을 방지합니다.
                    if (
                        isinstance(curr_values, (list, tuple))
                        and len(curr_values) > 5
                        and isinstance(values, (list, tuple))
                        and len(values) > 5
                    ):
                        try:
                            incoming_atr = str(values[5] if values[5] is not None else "").strip()
                            current_atr = str(
                                curr_values[5] if curr_values[5] is not None else ""
                            ).strip()
                            if incoming_atr in ("", "-", "—") and current_atr not in ("", "-", "—"):
                                values = list(values)
                                values[5] = curr_values[5]
                                values = tuple(values)
                        except Exception:
                            pass
                    if curr_values != values or final_tag not in curr_tags:
                        _step_log("target_tree_item_update_call", {"final_tag": str(final_tag)})
                        # Tk Treeview item 업데이트는 일부 케이스에서 호출이 멈추는 현상이 있어,
                        # tags를 한 번에 같이 넣지 않고 values/tags를 분리 갱신합니다.
                        try:
                            # 1) values 먼저 갱신(표시 가격/수급이 핵심)
                            if curr_values != values:
                                target_tree.item(iid, values=values)
                                _step_log("target_tree_item_values_update_done", {"final_tag": str(final_tag)})
                            # 2) tags는 색상 코딩 핵심이므로 기본 활성화.
                            #    필요 시 settings.json에서만 비활성화(enable_monitor_row_tag_update=false)
                            need_tag_update = final_tag not in curr_tags
                            if need_tag_update:
                                try:
                                    st_tag = getattr(self.app, "settings", None) or {}
                                    tag_update_enabled = bool(
                                        st_tag.get("enable_monitor_row_tag_update", True)
                                    )
                                except Exception:
                                    tag_update_enabled = True
                                if tag_update_enabled:
                                    target_tree.item(iid, tags=(final_tag,))
                                    _step_log("target_tree_item_tags_update_done", {"final_tag": str(final_tag)})
                                else:
                                    _step_log("target_tree_item_tags_update_skipped", {"final_tag": str(final_tag)})
                        except Exception:
                            # 업데이트 실패는 UI만 깨질 뿐 틱 흐름은 막지 않습니다.
                            try:
                                _step_log("target_tree_item_update_exception", {"final_tag": str(final_tag)})
                            except Exception:
                                pass
                # update_view 본문이 너무 오래 걸리면 Tk 이벤트 루프가 막혀 화면이 멈춘 것처럼 보일 수 있음
                try:
                    dt = time.perf_counter() - t0
                    if dt >= 0.50 and debug_seen:
                        now_ts = time.time()
                        if now_ts - self._update_view_slow_last_ts >= 20.0:
                            self._update_view_slow_last_ts = now_ts
                            self._realtime_session_logger.log(
                                "monitor_tab_update_view_slow",
                                {
                                    "dt_sec": float(dt),
                                    "items_count": len(items),
                                },
                            )
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[UI Error] MonitorTab Render Error: {e}")
            except Exception:
                pass
            try:
                self._realtime_session_logger.log(
                    "monitor_tab_update_view_exception",
                    {
                        "error": str(e),
                    },
                )
            except Exception:
                pass

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
        wl_only = self._watchlist_only_codes_set()
        mc_tgt = self._monitor_target_codes_set()
        try:
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
        except Exception:
            vp = {}
        
        idx_active = 0
        idx_watch = 0
        for code, info in sorted_items:
            nk = str(code).split("_")[0].zfill(6)
            if nk in wl_only:
                continue
            ratio_disp = self._ratio_cell_display_for_monitor_info(nk, info)
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
            wl_only = self._watchlist_only_codes_set()
            for code in codes:
                try:
                    tree_iid = str(code).split("_")[0].zfill(6)
                except Exception:
                    continue
                if tree_iid in wl_only:
                    continue
                self._patch_monitor_info_status_text(tree_iid, "⏳ 워밍업 대기…")
                for tree in (self.tree_monitor, self.tree_active_monitor):
                    if tree and tree.exists(tree_iid):
                        vals = list(tree.item(tree_iid, "values"))
                        si = getattr(self, "_monitor_status_value_index", 8)
                        if len(vals) > si:
                            vals[si] = "⏳ 워밍업 대기…"
                        tree.item(tree_iid, values=tuple(vals), tags=("warmup_pending",))
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
            tree_iid = str(code).split("_")[0].zfill(6)
            if tree_iid in self._watchlist_only_codes_set():
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
            self._patch_monitor_info_status_text(tree_iid, st)
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree or not tree.exists(tree_iid):
                    continue
                vals = list(tree.item(tree_iid, "values"))
                si = getattr(self, "_monitor_status_value_index", 8)
                if len(vals) > si:
                    vals[si] = st
                tree.item(tree_iid, values=tuple(vals), tags=(tag,))
        except Exception as e:
            print(f"[UI Error] update_warmup_stock_prep: {e}")
