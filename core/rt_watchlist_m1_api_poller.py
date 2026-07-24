# -*- coding: utf-8 -*-
"""
감시 종목(_active_stream_codes)에 대해 매 분 ka10080(1분)으로 **방금 닫힌 1분봉** 한 줄을 조회합니다.

- **정규장 연속매매 1분 격자(평일 09:00~15:19)만** 폴링한다. 장전·장후·동시호가 분은 REST 자체를 생략.
- 분 경계 직후 ``realtime_watchlist_m1_api_spread_sec`` 안에 종목별 지연을 분산(초당 과다 호출 완화).
- 동일 (종목, 닫힌 분 오픈라벨) 은 캐시 히트 시 REST 생략.
- 매 분 **대표 종목 1건 프로브**로 차트가 목표 분까지 도달했는지 확인 후 전체 라운드를 스케줄.
- 차트 ``max(cntr_tm)`` 이 목표 분 종료 시각 미만이면 ``minute_not_ready``(개장 직후 지연)로 조용히 생략.
- 실패 시 지수 백오프 후 최대 ``realtime_watchlist_m1_api_max_retries`` 회 재시도.
- 성공 시 ``RtTickM1Store.apply_official_closed_minute`` 로 **BT-HP·1분 창 계산**에 API OHLCV 를 반영합니다.

15분 확정 봉은 ``RtClosed15mOfficialReplaceScheduler`` + ``_apply_official_closed_15m_bar`` 가 슬롯 전환 시 처리합니다.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
from core.ka10080_spread import effective_m1_poll_spread_sec
from utils.krx_bars import (
    chart_covers_m1_closed_minute,
    is_krx_m1_closed_open_label,
    m1_bar_open_label_for_just_completed_minute,
)
from utils.normalization import canonical_stock_code

CacheKey = Tuple[str, str]
CacheVal = Tuple[float, float, float, float, float]
FetchResult = Tuple[Optional[pd.Series], Optional[pd.DataFrame], str]


class RtWatchlistM1ApiPoller:
    """
    데몬 스레드 1개 + 분마다 ``threading.Timer`` 로 종목별 지연 실행.
    """

    def __init__(self, app: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self._app = app
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._last_scheduled_round: str = ""
        self._cache: Dict[CacheKey, CacheVal] = {}
        self._cache_order: List[CacheKey] = []
        self._cache_lock = threading.Lock()
        self._round_lock = threading.Lock()
        self._dormant_rounds: Set[str] = set()
        self._active_rounds: Set[str] = set()

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _settings(self) -> Dict[str, Any]:
        try:
            return getattr(self._app, "settings", None) or {}
        except Exception:
            return {}

    @staticmethod
    def _settings_bool(settings: Dict[str, Any], key: str, default: bool = True) -> bool:
        try:
            v = settings.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return default

    def _schedule_signal_eval(
        self, nk: str, *, close_px: float = 0.0, volume: float = 0.0
    ) -> None:
        """공식 M1 반영 직후(또는 실패 폴백) 신호 재평가."""
        try:
            from core.realtime_official_bars_sync import schedule_official_sync_and_signal_eval

            schedule_official_sync_and_signal_eval(
                self._app,
                nk,
                now_price_fallback=float(close_px or 0.0),
                volume_fallback=int(volume or 0),
            )
        except Exception:
            pass

    def _monitoring(self) -> bool:
        try:
            tc = getattr(self._app, "trading_controller", None)
            return bool(getattr(tc, "is_monitoring", False))
        except Exception:
            return False

    def _active_codes(self) -> List[str]:
        """웜업·감시에 올라간 구독 종목(정규 6자리)."""
        out: List[str] = []
        try:
            ac = getattr(self._app, "_active_stream_codes", None)
            if isinstance(ac, list):
                for c in ac:
                    nk = canonical_stock_code(str(c).strip())
                    if nk and nk not in out:
                        out.append(nk)
        except Exception:
            pass
        return out

    def _cache_max_entries(self) -> int:
        try:
            n = int(
                float(
                    self._settings().get(
                        "realtime_watchlist_m1_api_cache_max_entries", 4000
                    )
                )
            )
        except (TypeError, ValueError):
            n = 4000
        return max(200, min(n, 50_000))

    def _remember_cache(self, key: CacheKey, val: CacheVal) -> None:
        mx = self._cache_max_entries()
        with self._cache_lock:
            if key in self._cache:
                self._cache[key] = val
                return
            self._cache[key] = val
            self._cache_order.append(key)
            while len(self._cache_order) > mx:
                old = self._cache_order.pop(0)
                self._cache.pop(old, None)

    def get_cached_row(
        self, code: str, closed_open_label: pd.Timestamp
    ) -> Optional[CacheVal]:
        """캐시된 (O,H,L,C,V) 또는 None."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return None
            sk = pd.Timestamp(closed_open_label).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        with self._cache_lock:
            return self._cache.get((nk, sk))

    def start(self) -> None:
        """중복 기동 방지."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._wake_loop, name="RtWatchlistM1ApiPoller", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """앱 종료 등에서 호출."""
        self._stop.set()
        try:
            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass

    def reset_round_state(self) -> None:
        """감시 중단 시 호출: 다음 장중 첫 라운드가 스케줄되도록 라운드 키를 비웁니다."""
        try:
            self._last_scheduled_round = ""
            with self._round_lock:
                self._dormant_rounds.clear()
                self._active_rounds.clear()
        except Exception:
            pass

    def _is_round_dormant(self, sk: str) -> bool:
        with self._round_lock:
            return sk in self._dormant_rounds

    def _mark_round_dormant(self, sk: str) -> None:
        with self._round_lock:
            self._dormant_rounds.add(sk)
            self._active_rounds.discard(sk)

    def _mark_round_active(self, sk: str) -> None:
        with self._round_lock:
            self._active_rounds.add(sk)
            self._dormant_rounds.discard(sk)

    def _pick_probe_code(self, codes: List[str]) -> str:
        """프로브용 대표 종목(정렬 첫 번째 — 재현 가능)."""
        try:
            return sorted(codes)[0]
        except Exception:
            return str(codes[0]) if codes else ""

    @staticmethod
    def _is_pollable_m1_label(closed_lab: pd.Timestamp) -> bool:
        """
        KRX 연속매매 1분봉이 실제로 존재하는 라벨만 True.

        장전·장후·주말·15:20 이후 동시호가 분은 False → ka10080 조회 생략.
        """
        try:
            return bool(is_krx_m1_closed_open_label(closed_lab))
        except Exception:
            return False

    def _wake_loop(self) -> None:
        """새로 닫힌 1분 라벨이 바뀌었을 때만 라운드 스케줄."""
        while not self._stop.wait(0.25):
            try:
                if not self._monitoring():
                    continue
                wall = datetime.now()
                closed_lab = m1_bar_open_label_for_just_completed_minute(wall)
                sk = closed_lab.strftime("%Y-%m-%d %H:%M:%S")
                if sk == self._last_scheduled_round:
                    continue
                # 장전 empty_response·장후 row_not_found 방지: 존재하지 않는 분은 REST 자체를 안 함
                if not self._is_pollable_m1_label(closed_lab):
                    self._last_scheduled_round = sk
                    continue
                codes = self._active_codes()
                if not codes:
                    continue
                self._last_scheduled_round = sk
                with self._round_lock:
                    self._dormant_rounds = {
                        x for x in self._dormant_rounds if x == sk
                    }
                    self._active_rounds = {
                        x for x in self._active_rounds if x == sk
                    }
                if self._is_round_dormant(sk):
                    continue
                with self._round_lock:
                    already_active = sk in self._active_rounds
                if already_active:
                    self._schedule_round_jobs(codes, closed_lab, sk)
                    continue
                t = threading.Timer(
                    0.0,
                    self._job_probe_round,
                    args=(codes, closed_lab, sk, 0),
                )
                t.daemon = True
                t.start()
            except Exception as e:
                try:
                    self._emit(f"⚠️ [M1Poll] wake_loop 예외: {e!r}")
                except Exception:
                    pass

    def _schedule_round_jobs(
        self, codes: List[str], closed_lab: pd.Timestamp, sk: str
    ) -> None:
        """프로브 통과 후 종목별 분산 폴링 타이머 등록."""
        try:
            n_codes = len(codes)
            try:
                spread = effective_m1_poll_spread_sec(self._settings(), n_codes)
            except Exception:
                try:
                    spread = float(
                        self._settings().get(
                            "realtime_watchlist_m1_api_spread_sec", 40.0
                        )
                    )
                except (TypeError, ValueError):
                    spread = 40.0
                spread = max(12.0, min(spread, 55.0))
            for cc in codes:
                try:
                    h = abs(hash((cc, sk)))
                    frac = (h % 1_000_003) / 1_000_003.0
                    delay = spread * frac
                except Exception:
                    delay = 0.0
                t = threading.Timer(
                    delay,
                    self._job_fetch_one,
                    args=(cc, closed_lab, sk),
                )
                t.daemon = True
                t.start()
        except Exception:
            pass

    def _pick_row_for_closed_open(
        self, df: pd.DataFrame, closed_open: pd.Timestamp
    ) -> Optional[pd.Series]:
        """
        ka10080 1분 인덱스는 cntr_tm = **봉 종가 시각**(분 끝).
        ``closed_open`` 은 ``m1_bar_open_label_for_just_completed_minute`` = 구간 시작 라벨.
        """
        try:
            if df is None or getattr(df, "empty", True):
                return None
            end_ts = pd.Timestamp(closed_open) + pd.Timedelta(minutes=1)
            if end_ts in df.index:
                return df.loc[end_ts]
            want = end_ts.floor("s")
            for ix in df.index:
                try:
                    if pd.Timestamp(ix).floor("s") == want:
                        return df.loc[ix]
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _classify_fetch_result(
        self,
        row: Optional[pd.Series],
        chart_df: Optional[pd.DataFrame],
        closed_lab: pd.Timestamp,
        last_err: str,
    ) -> str:
        """``ok`` | ``minute_not_ready`` | ``row_not_found`` | ``fetch_error``."""
        if row is not None:
            return "ok"
        if last_err in ("no_token", "empty_raw") or last_err.startswith("http_"):
            return "fetch_error"
        if chart_df is not None and not chart_df.empty:
            if not chart_covers_m1_closed_minute(chart_df, closed_lab):
                return "minute_not_ready"
            return "row_not_found"
        if last_err == "row_not_found":
            return "row_not_found"
        return "fetch_error"

    def _fetch_closed_minute_row(
        self,
        nk: str,
        closed_lab: pd.Timestamp,
        sk: str,
        *,
        max_retries: int,
    ) -> FetchResult:
        """ka10080 1분 1페이지 조회 → (row, chart_df, last_err)."""
        st = self._settings()
        try:
            base_backoff = float(
                st.get("realtime_watchlist_m1_api_backoff_base_sec", 0.45)
            )
        except (TypeError, ValueError):
            base_backoff = 0.45
        base_backoff = max(0.1, min(base_backoff, 5.0))
        try:
            dmin = float(st.get("realtime_warmup_ka10080_page_delay_min", 0.08))
            dmax = float(st.get("realtime_warmup_ka10080_page_delay_max", 0.22))
        except (TypeError, ValueError):
            dmin, dmax = 0.08, 0.22
        if dmax < dmin:
            dmin, dmax = dmax, dmin

        dm = getattr(self._app, "data_manager", None)
        if dm is None:
            return None, None, "no_data_manager"

        row: Optional[pd.Series] = None
        chart_df: Optional[pd.DataFrame] = None
        last_err = "unknown"
        retries = max(1, min(int(max_retries), 8))

        for attempt in range(retries):
            try:
                token = None
                try:
                    token = self._app.auth_manager.get_auth_token()
                except Exception:
                    token = None
                if not token:
                    last_err = "no_token"
                    try:
                        from core.ka10080_fetch_diag import ka10080_fetch_diag

                        ka10080_fetch_diag.record_fail(
                            nk,
                            tic_scope="1",
                            context="m1_poll",
                            reason="no_token",
                            closed_minute=sk,
                            attempt=attempt + 1,
                        )
                    except Exception:
                        pass
                    break
                raw_list, _nk = dm.fetch_kiwoom_minute_chart_page(
                    nk,
                    str(token).strip(),
                    "",
                    tic_scope="1",
                    fetch_context="m1_poll",
                )
                if not raw_list:
                    try:
                        meta = getattr(dm, "last_ka10080_fetch_meta", None) or {}
                        last_err = str(meta.get("reason") or "empty_raw")
                    except Exception:
                        last_err = "empty_raw"
                else:
                    df = ka10080_raw_rows_to_1m_df(raw_list)
                    chart_df = df
                    row = self._pick_row_for_closed_open(df, closed_lab)
                    if row is not None:
                        last_err = "ok"
                        break
                    if not chart_covers_m1_closed_minute(df, closed_lab):
                        last_err = "minute_not_ready"
                        break
                    last_err = "row_not_found"
            except Exception as e:
                last_err = repr(e)

            status = self._classify_fetch_result(row, chart_df, closed_lab, last_err)
            if status in ("ok", "minute_not_ready"):
                break
            if attempt + 1 < retries:
                try:
                    sleep_s = base_backoff * (2**attempt) + random.uniform(0, 0.12)
                    time.sleep(min(sleep_s, 8.0))
                except Exception:
                    time.sleep(base_backoff)
                try:
                    time.sleep(random.uniform(dmin, dmax))
                except Exception:
                    pass

        return row, chart_df, last_err

    def _job_probe_round(
        self,
        codes: List[str],
        closed_lab: pd.Timestamp,
        sk: str,
        attempt: int,
    ) -> None:
        """대표 종목 1건으로 차트 도달 여부를 확인한 뒤 라운드 전체를 스케줄하거나 생략."""
        try:
            if not self._monitoring():
                return
            if not self._is_pollable_m1_label(closed_lab):
                return
            if self._is_round_dormant(sk):
                return
            probe = self._pick_probe_code(codes)
            nk = canonical_stock_code(str(probe).strip())
            if not nk:
                return
            row, chart_df, last_err = self._fetch_closed_minute_row(
                nk, closed_lab, sk, max_retries=2
            )
            status = self._classify_fetch_result(row, chart_df, closed_lab, last_err)

            if status == "minute_not_ready":
                self._mark_round_dormant(sk)
                return

            if status == "fetch_error" and attempt < 1:
                t = threading.Timer(
                    1.2,
                    self._job_probe_round,
                    args=(codes, closed_lab, sk, attempt + 1),
                )
                t.daemon = True
                t.start()
                return

            if status == "fetch_error":
                return

            self._mark_round_active(sk)
            self._schedule_round_jobs(codes, closed_lab, sk)
        except Exception:
            pass

    def _apply_fetch_success(
        self,
        nk: str,
        closed_lab: pd.Timestamp,
        sk: str,
        key: CacheKey,
        row: pd.Series,
        chart_df: Optional[pd.DataFrame],
    ) -> None:
        """조회 성공 시 M1 저장·캐시·신호 평가."""
        st = self._settings()
        catchup_on = self._settings_bool(
            st, "realtime_m1_poller_apply_chart_catchup", True
        )
        try:
            ohlcv: CacheVal = (
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Volume"]),
            )
        except Exception as e:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} {sk} 파싱 실패: {e!r}")
            except Exception:
                pass
            return

        o, h, l_, c, v = ohlcv
        if catchup_on and chart_df is not None:
            try:
                from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

                apply_chart_catchup_to_m1_store(
                    self._app, nk, chart_df, closed_lab
                )
            except Exception:
                pass

        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                m1s.apply_official_closed_minute(nk, closed_lab, o, h, l_, c, v)
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} RtTickM1Store 반영 실패: {ex!r}")
            except Exception:
                pass
        try:
            self._remember_cache(key, (o, h, l_, c, v))
        except Exception:
            pass
        self._schedule_signal_eval(nk, close_px=c, volume=v)

    def _job_fetch_one(self, code: str, closed_lab: pd.Timestamp, sk: str) -> None:
        """Timer 스레드: 한 종목·한 닫힌 분."""
        try:
            if not self._monitoring():
                return
            if not self._is_pollable_m1_label(closed_lab):
                return
            if self._is_round_dormant(sk):
                return
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            key: CacheKey = (nk, sk)
        except Exception:
            return

        st = self._settings()
        eval_on_fail = self._settings_bool(
            st, "realtime_m1_poller_eval_on_fetch_fail", True
        )
        eval_on_cache = self._settings_bool(
            st, "realtime_m1_poller_eval_on_cache_hit", True
        )
        db_fb_on = self._settings_bool(st, "realtime_intrabar_m1_db_fallback", True)

        cached_ohlcv: Optional[CacheVal] = None
        try:
            with self._cache_lock:
                cached_ohlcv = self._cache.get(key)
        except Exception:
            cached_ohlcv = None

        if cached_ohlcv is not None and eval_on_cache:
            try:
                _o, _h, _l, _c, _v = cached_ohlcv
                self._schedule_signal_eval(nk, close_px=_c, volume=_v)
            except Exception:
                pass
            return

        try:
            max_retries = int(
                float(st.get("realtime_watchlist_m1_api_max_retries", 3))
            )
        except (TypeError, ValueError):
            max_retries = 3

        row, chart_df, last_err = self._fetch_closed_minute_row(
            nk, closed_lab, sk, max_retries=max_retries
        )
        status = self._classify_fetch_result(row, chart_df, closed_lab, last_err)

        if status == "minute_not_ready":
            self._mark_round_dormant(sk)
            return

        if status == "ok" and row is not None:
            self._apply_fetch_success(nk, closed_lab, sk, key, row, chart_df)
            return

        ohlcv: Optional[CacheVal] = None
        if db_fb_on:
            try:
                from core.realtime_m1_fallback import closed_minute_ohlcv_from_db

                ohlcv = closed_minute_ohlcv_from_db(self._app, nk, closed_lab)
            except Exception:
                ohlcv = None

        if ohlcv is None:
            if status == "row_not_found":
                try:
                    from core.ka10080_fetch_diag import ka10080_fetch_diag

                    ka10080_fetch_diag.record_fail(
                        nk,
                        tic_scope="1",
                        context="m1_poll",
                        reason="row_not_found",
                        closed_minute=sk,
                        detail="closed_minute_not_in_chart",
                    )
                except Exception:
                    pass
            if eval_on_fail:
                self._schedule_signal_eval(nk)
            return

        o, h, l_, c, v = ohlcv
        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                m1s.apply_official_closed_minute(nk, closed_lab, o, h, l_, c, v)
        except Exception:
            pass
        try:
            self._remember_cache(key, (o, h, l_, c, v))
        except Exception:
            pass
        self._schedule_signal_eval(nk, close_px=c, volume=v)
