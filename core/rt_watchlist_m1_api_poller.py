# -*- coding: utf-8 -*-
"""
감시 종목(_active_stream_codes)에 대해 매 분 ka10080(1분)으로 **방금 닫힌 1분봉** 한 줄을 조회합니다.

- 분 경계 직후 ``realtime_watchlist_m1_api_spread_sec`` 안에 종목별 지연을 분산(초당 과다 호출 완화).
- 동일 (종목, 닫힌 분 오픈라벨) 은 캐시 히트 시 REST 생략.
- 실패 시 지수 백오프 후 최대 ``realtime_watchlist_m1_api_max_retries`` 회 재시도.
- 성공 시 ``RtTickM1Store.apply_official_closed_minute`` 로 **BT-HP·1분 창 계산**에 API OHLCV 를 반영합니다.

15분 확정 봉은 ``RtClosed15mOfficialReplaceScheduler`` + ``_apply_official_closed_15m_bar`` 가 슬롯 전환 시 처리합니다.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
from core.ka10080_spread import effective_m1_poll_spread_sec
from utils.krx_bars import m1_bar_open_label_for_just_completed_minute
from utils.normalization import canonical_stock_code

CacheKey = Tuple[str, str]
CacheVal = Tuple[float, float, float, float, float]


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
        except Exception:
            pass

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
                codes = self._active_codes()
                if not codes:
                    continue
                # 스케줄 확정(빈 라운드는 last 갱신 안 함)
                self._last_scheduled_round = sk
                n_codes = len(codes)
                try:
                    spread = effective_m1_poll_spread_sec(
                        self._settings(), n_codes
                    )
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
                # 종목별 [0, spread) 초 분산 — 해시로 흩뜨림
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
            except Exception as e:
                try:
                    self._emit(f"⚠️ [M1Poll] wake_loop 예외: {e!r}")
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
            # 근접 매칭(초 단위)
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

    def _job_fetch_one(self, code: str, closed_lab: pd.Timestamp, sk: str) -> None:
        """Timer 스레드: 한 종목·한 닫힌 분."""
        try:
            if not self._monitoring():
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
        catchup_on = self._settings_bool(
            st, "realtime_m1_poller_apply_chart_catchup", True
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
        max_retries = max(1, min(max_retries, 8))
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
            return

        row: Optional[pd.Series] = None
        chart_df: Optional[pd.DataFrame] = None
        last_err: Optional[str] = None
        for attempt in range(max_retries):
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
                        break
                    last_err = "row_not_found"
            except Exception as e:
                last_err = repr(e)
            if attempt + 1 < max_retries:
                try:
                    sleep_s = base_backoff * (2**attempt) + random.uniform(0, 0.12)
                    time.sleep(min(sleep_s, 8.0))
                except Exception:
                    time.sleep(base_backoff)
                try:
                    time.sleep(random.uniform(dmin, dmax))
                except Exception:
                    pass

        ohlcv: Optional[CacheVal] = None
        if row is not None:
            try:
                ohlcv = (
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
                ohlcv = None

        if ohlcv is None and db_fb_on:
            try:
                from core.realtime_m1_fallback import closed_minute_ohlcv_from_db

                ohlcv = closed_minute_ohlcv_from_db(self._app, nk, closed_lab)
            except Exception:
                ohlcv = None

        if ohlcv is None:
            try:
                from core.ka10080_fetch_diag import ka10080_fetch_diag

                # HTTP 429/502 등은 data_manager 가 이미 기록. 여기서는 의미론적 실패만.
                rsn = str(last_err or "")
                if rsn == "row_not_found":
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

        if catchup_on and chart_df is not None:
            try:
                from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

                n_new = apply_chart_catchup_to_m1_store(
                    self._app, nk, chart_df, closed_lab
                )
                if n_new > 1:
                    self._emit(
                        f"ℹ️ [M1Poll] {nk} 차트 catch-up {n_new}분 반영(목표 {sk})"
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
        # [버그픽스] 방금 가져온 공식 1분 종가/거래량을 평가 트리거에 직접 전달.
        # API 502·WS 끊김 시에도 폴백 경로에서 매 분 신호 평가가 구동되어 BT 정합을 유지한다.
        self._schedule_signal_eval(nk, close_px=c, volume=v)
