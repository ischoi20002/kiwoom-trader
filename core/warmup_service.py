# -*- coding: utf-8 -*-

import contextlib
import queue
import random
import threading
import time
import pandas as pd
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from backtest_engine import TradingStrategy
from core.defaults import effective_min_15m_bars_for_monitoring
from core.terminal_trace import terminal_trace
from utils.normalization import canonical_stock_code


def _bar_slot_hhmm(ts: Any) -> str:
    """
    완성 15분봉 인덱스(봉 종가 시각) → HHMM.
    StreamProcessor 는 '지금' 틱에 대해 ((분//15)+1)*15 규칙을 쓰므로,
    예: 인덱스 10:15 종가 봉은 '1015' → 10:14 틱의 슬롯 1015 와 맞물린다.
    (인덱스에 동일 공식을 쓰면 10:15 → 1030 이 되어 한 봉 건너뛰는 오류가 난다)
    """
    try:
        t = pd.Timestamp(ts)
        return f"{t.hour:02d}{t.minute:02d}"
    except Exception:
        return "0000"


class WarmupService:
    """
    [v4.5.3] 데이터 워밍업 전담 서비스
    실시간 감시 시작 전 bars_data 를 **15분봉 연속 시계열**로 맞춘 뒤 지표를 계산합니다.
    (마지막 봉 이후 틱은 StreamProcessor 가 동일 슬롯 규칙으로 이어 붙임)
    """

    def __init__(
        self,
        app,
        logger_callback=None,
        stock_prep_callback: Optional[Callable[..., None]] = None,
    ):
        self.app = app
        self.data_manager = app.data_manager
        self.auth_manager = app.auth_manager
        self.config_handler = app.config_handler
        self.log = logger_callback or print
        # event: prep_start | prep_progress | prep_ok | prep_fail (reason=부가문자열)
        self.stock_prep_cb = stock_prep_callback

        self.is_running = False
        # run() 종목 루프에서만 설정 — ka10080 연속조회 중 UI 진행 표시용
        self._warmup_prep_ui: Optional[Dict[str, Any]] = None
        # 캘린더·깊이 보강을 메인 워밍업과 분리해 순차 백그라운드 처리
        self._deep_history_queue: Optional[queue.Queue] = None
        self._deep_history_worker_started: bool = False
        # run() 한 번당 1m→15m 리샘플 안내 로그 상한(대시보드 스팸 방지)
        self._warmup_resample_hint_count: int = 0

    def _tt(self, msg: str) -> None:
        """터미널(stderr) 전용 — GUI 로그 배치와 무관하게 막힘 구간 확인용."""
        try:
            terminal_trace(msg, app=self.app, component="Warmup")
        except Exception:
            pass

    def _warmup_query_limit(self) -> int:
        try:
            return max(200, int(float(self.app.settings.get("realtime_warmup_15m_query_limit", 7000))))
        except (TypeError, ValueError):
            return 7000

    def _warmup_retention_calendar_days(self) -> int:
        """실시간 15분 DB 롤링과 동일한 캘린더 보존 일수."""
        try:
            d = int(float(self.app.settings.get("realtime_15m_calendar_retention_days", 365)))
        except (TypeError, ValueError):
            d = 365
        return max(1, min(d, 3650))

    def _warmup_ka10080_tic_scope(self) -> str:
        """
        실시간 감시 워밍업·RT15m DB 보충용 ka10080 봉 주기(키움 tic_scope).
        기본 \"15\" — 15분봉만 수신하고 1분→15분 리샘플을 하지 않는다.
        """
        try:
            v = self.app.settings.get("realtime_warmup_ka10080_tic_scope", "15")
            s = str(v).strip() if v is not None else "15"
            return s if s else "15"
        except Exception:
            return "15"

    def _warmup_fetch_calendar_history_enabled(self) -> bool:
        """True: ka10080 를 캘린더 보존일만큼 과거까지 채울 때까지 연속조회(신호 최소봉만으로 조기종료 안 함)."""
        try:
            return bool(self.app.settings.get("realtime_warmup_fetch_calendar_history", True))
        except Exception:
            return True

    def _warmup_depth_calendar_slack_days(self) -> int:
        """깊이 판정 시 허용하는 캘린더 일수 여유(주말·공휴일·첫 봉 시각)."""
        try:
            s = int(float(self.app.settings.get("realtime_warmup_depth_calendar_slack_days", 21)))
        except (TypeError, ValueError):
            s = 21
        return max(0, min(s, 120))

    def _retention_calendar_depth_ok(self, oldest_ts: Any, retention_days: int) -> bool:
        """
        가장 오래된 봉이 '캘린더 보존 N일' 요구를 충족하는지.

        시각 비교 `oldest <= now - N일` 만 쓰면 롤링 보존 후 남는 가장 오래된 봉이
        컷오프 **이후**(같은 날 장 시작 등)인 경우가 대부분이라 조건이 거짓으로 고정될 수 있다.
        (오늘 날짜 − 최초 봉 날짜) 일수가 (N − 슬랙) 이상이면 충족으로 본다.
        """
        try:
            rd = max(1, min(int(retention_days), 3650))
            slack = self._warmup_depth_calendar_slack_days()
            oldest = pd.Timestamp(oldest_ts)
            if pd.isna(oldest):
                return False
            age_days = (
                pd.Timestamp.now().normalize() - oldest.normalize()
            ).days
            return bool(age_days >= rd - slack)
        except Exception:
            return False

    def _calendar_depth_satisfied(
        self, merged: pd.DataFrame, retention_days: int
    ) -> bool:
        """
        가장 오래된 15분봉이 캘린더 보존 일수(슬랙 적용)를 만족하면 과거 깊이 충족.
        """
        try:
            if merged is None or merged.empty:
                return False
            if not isinstance(merged.index, pd.DatetimeIndex):
                return False
            oldest = pd.Timestamp(merged.index.min())
            return self._retention_calendar_depth_ok(oldest, retention_days)
        except Exception:
            return False

    def _calendar_depth_satisfied_for_code(
        self,
        code: str,
        merged: pd.DataFrame,
        retention_days: int,
    ) -> bool:
        """
        깊이 충족 여부.
        realtime_15m DB 는 워밍업 로드가 `ORDER BY datetime DESC LIMIT N` 이라
        메모리 프레임의 index.min() 이 DB 안의 **진짜** 최저 시각이 아닐 수 있다(가장 흔한 재API 원인).
        이 경우 RT15m SQLite 에서 MIN(datetime) 으로 판정한다.
        또한 로컬이 캘린더 N일을 못 채워도, ka10080 이 next-key 없이 끝나 **브로커가 주는 과거 한계까지**
        이미 받은 종목은 깊이 요구를 만족한 것으로 본다(종목별 메타).
        """
        try:
            src = self._warmup_ohlcv_source()
            if src == "realtime_15m_db_then_api":
                store = getattr(self.app, "realtime_monitor_bars_store", None)
                if store is not None:
                    if store.is_ka10080_deep_exhausted_ok(code, retention_days):
                        return True
                    tmin = store.earliest_bar_datetime_in_retention(
                        code, retention_days
                    )
                    if tmin is not None and pd.notna(tmin):
                        return self._retention_calendar_depth_ok(
                            tmin, retention_days
                        )
        except Exception:
            pass
        return self._calendar_depth_satisfied(merged, retention_days)

    def _bars_max_keep(self) -> int:
        try:
            return max(60, int(float(self.app.settings.get("realtime_bars_max_keep", 300))))
        except (TypeError, ValueError):
            return 300

    def _indicator_calc_input_tail(self, df: Optional[pd.DataFrame]) -> pd.DataFrame:
        """
        실시간 감시용 지표는 결국 `realtime_bars_max_keep` 꼬리만 쓴다.
        1년치 수천 봉 전체에 RSI/MACD/ATR 을 돌리면 CPU·GIL 부하만 커지므로,
        롤링 안정화에 충분한 꼬리만 잘라 계산한다. (DB 적재·검증용 전체 df 는 그대로 유지)
        """
        try:
            if df is None or getattr(df, "empty", True):
                return df if df is not None else pd.DataFrame()
            try:
                need = effective_min_15m_bars_for_monitoring(self.app.settings)
            except Exception:
                need = 120
            keep = self._bars_max_keep()
            try:
                cap_max = int(
                    float(
                        self.app.settings.get(
                            "realtime_warmup_indicator_calc_max_rows", 2000
                        )
                    )
                )
            except (TypeError, ValueError):
                cap_max = 2000
            cap_max = max(400, min(cap_max, 20000))
            cap = max(int(need * 3), int(keep * 2), 400)
            cap = min(cap, cap_max)
            if len(df) <= cap:
                return df
            return df.tail(cap).copy()
        except Exception as e:
            try:
                self.log(f"⚠️ [Warmup] 지표 계산용 tail 자르기 실패(전체 사용): {e}")
            except Exception:
                pass
            return df if df is not None else pd.DataFrame()

    def _resolve_monitor_info_key_for_seed(
        self, monitor_info: Dict[str, Any], code: str
    ) -> Optional[str]:
        """워밍업 시드용: ``monitor_info`` 실제 키(monitor_tab·SignalEngine 과 동일 규칙)."""
        try:
            nk = canonical_stock_code(code)
            if not nk:
                return None
            if nk in monitor_info:
                return nk
            for k in monitor_info:
                try:
                    if canonical_stock_code(k) == nk:
                        return str(k)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _seed_monitor_signal_cache_after_warmup(
        self,
        code: str,
        monitor_info: Optional[Dict[str, Any]],
        df_ind: pd.DataFrame,
        *,
        stream_bar_count: int,
    ) -> None:
        """
        워밍업에서 이미 계산한 지표·신호를 ``monitor_info`` 에 반영합니다.
        실시간 첫 틱 전까지 신호상태 열이 비어 있거나 '대기'로 보이는 현상을 줄입니다.
        """
        try:
            if not isinstance(monitor_info, dict) or df_ind is None:
                return
            if getattr(df_ind, "empty", True):
                return
            need = effective_min_15m_bars_for_monitoring(self.app.settings)
            if len(df_ind) < need:
                return
            mi_key = self._resolve_monitor_info_key_for_seed(monitor_info, code)
            if mi_key is None:
                return
            row = monitor_info[mi_key]
            if not isinstance(row, dict):
                return

            detailed = TradingStrategy.get_buy_signals_detailed(
                df_ind, self.app.settings, "UP"
            )
            if detailed is None or getattr(detailed, "empty", True):
                return
            last_ser = detailed.iloc[-1]
            last_sig = (
                last_ser.to_dict()
                if hasattr(last_ser, "to_dict")
                else dict(last_ser)
            )
            row["last_detailed_sig"] = detailed
            row["last_bar_count"] = int(stream_bar_count)
            row["last_row_cache"] = df_ind.iloc[-1].to_dict()
            row["last_sig_cache"] = last_sig

            _sel_tr = str(
                row.get("sel_track", row.get("SelTrack", "AB")) or "AB"
            ).strip().upper() or "AB"
            se = getattr(self.app, "signal_engine", None)
            if se is not None and hasattr(se, "_format_s1_confluence_status_line"):
                try:
                    row["confluence"] = se._format_s1_confluence_status_line(
                        last_sig, _sel_tr
                    )
                except Exception:
                    row["confluence"] = row.get("confluence", "-")
            else:
                row["confluence"] = row.get("confluence", "-")
            try:
                row["met_ratio"] = float(last_sig.get("Met_Ratio", 0.0) or 0.0)
            except (TypeError, ValueError):
                row["met_ratio"] = 0.0
        except Exception as e:
            try:
                self.log(f"⚠️ [Warmup] 신호 캐시 시드 실패 ({code}): {e}")
            except Exception:
                pass

    def _min_bars_for_signal(self) -> int:
        """실시간 최소 봉수(설정)와 ATR 주기(atr_period_a/b) 상한을 함께 반영."""
        try:
            return effective_min_15m_bars_for_monitoring(self.app.settings)
        except Exception:
            try:
                return max(
                    1,
                    int(float(self.app.settings.get("min_15m_bars_for_signal", 60))),
                )
            except (TypeError, ValueError):
                return 60

    def _warmup_ohlcv_source(self) -> str:
        """
        워밍업 시 가격 봉의 출처.
        api_only — DB 미사용.
        realtime_15m_db_then_api — 실시간 전용 15분 DB(realtime_15m_*.db) 우선, 부족 시 API.
        db_then_api — 하베스팅 trading_data.db 의 bars_15m/1m 우선 후 API.
        db_then_api 이고 메인 DB에 분봉이 없으면 api_only 로 강등(무DB 배포본).
        """
        try:
            s = str(
                self.app.settings.get("realtime_warmup_ohlcv_source", "api_only")
            ).strip().lower()
        except Exception:
            s = "api_only"
        if s in ("api_only", "api", "rest_only"):
            return "api_only"
        if s in ("realtime_15m_db_then_api", "rt_15m_db", "monitor_15m_then_api"):
            return "realtime_15m_db_then_api"
        try:
            dm = self.data_manager
            if dm is not None and hasattr(dm, "main_db_has_any_intraday_rows"):
                if not dm.main_db_has_any_intraday_rows():
                    if not getattr(self, "_logged_intraday_db_empty_fallback", False):
                        self._logged_intraday_db_empty_fallback = True
                        self.log(
                            "ℹ️ [Warmup] bars_15m/bars_1m 이 비어 있어 "
                            "realtime_warmup_ohlcv_source 를 api_only 로 전환합니다. "
                            "(trading_data.db 미포함 배포·미하베스팅)"
                        )
                    return "api_only"
        except Exception:
            if not getattr(self, "_logged_intraday_db_empty_fallback", False):
                self._logged_intraday_db_empty_fallback = True
                try:
                    self.log("ℹ️ [Warmup] DB 분봉 유무 확인 실패 → api_only 로 진행합니다.")
                except Exception:
                    pass
            return "api_only"
        return "db_then_api"

    def _persist_warmup_base_to_rt15m_db(self, code: str, base: pd.DataFrame) -> None:
        """
        워밍업 검증 통과 직후: 병합된 15분 OHLCV를 realtime_15m_*.db 에 배치 저장합니다.
        다음 실행 시 로컬 우선 로드가 가능해집니다.
        """
        try:
            if not bool(
                self.app.settings.get("realtime_warmup_persist_to_rt15m_db", True)
            ):
                return
            store = getattr(self.app, "realtime_monitor_bars_store", None)
            if store is None or base is None or base.empty:
                return
            int(store.bulk_upsert_ohlcv_index_frame(code, base))
            # 성공 영속화 로그는 종목 수만큼 반복되어 생략(실패만 except 에서 기록).
        except Exception as e:
            try:
                self.log(f"⚠️ [Warmup-RT15mDB] {code} 영속화 실패: {e}")
            except Exception:
                pass

    def _load_from_realtime_monitor_db(self, code: str) -> pd.DataFrame:
        """실시간 전용 realtime_15m_*.db → 워밍업용 OHLCV 인덱스 프레임."""
        try:
            store = getattr(self.app, "realtime_monitor_bars_store", None)
            if store is None:
                return pd.DataFrame()
            raw = store.fetch_15m_bars_as_dataframe(code, self._warmup_query_limit())
            if raw is None or raw.empty or "time" not in raw.columns:
                return pd.DataFrame()
            d = raw.copy()
            d["time"] = pd.to_datetime(d["time"], errors="coerce")
            d = d.dropna(subset=["time"])
            d = d.set_index("time").sort_index()
            for c in ("Open", "High", "Low", "Close", "Volume"):
                if c not in d.columns:
                    return pd.DataFrame()
            return d[["Open", "High", "Low", "Close", "Volume"]].copy()
        except Exception as e:
            try:
                self.log(f"⚠️ [Warmup-RT15mDB] {code} 정규화 실패: {e}")
            except Exception:
                pass
            return pd.DataFrame()

    def _warmup_retry_max(self) -> int:
        """실패 시 추가 재시도 횟수(최초 시도 1회 + 이 값 = 총 시도 상한)."""
        try:
            return max(0, int(float(self.app.settings.get("realtime_warmup_retry_max", 3))))
        except (TypeError, ValueError):
            return 3

    def _warmup_retry_delay_sec(self) -> float:
        try:
            return max(0.0, float(self.app.settings.get("realtime_warmup_retry_delay_sec", 30.0)))
        except (TypeError, ValueError):
            return 30.0

    def _interruptible_sleep(self, total_sec: float) -> None:
        """
        재시도 대기 중에도 stop() 으로 워밍업이 중단되면 잠자기를 끊습니다.
        """
        try:
            remain = float(total_sec)
        except (TypeError, ValueError):
            return
        while remain > 1e-6:
            if not self.is_running:
                return
            step = min(1.0, remain)
            time.sleep(step)
            remain -= step

    def _notify_stock_prep(
        self,
        event: str,
        cur: int,
        total: int,
        name: str,
        code: str,
        reason: str = "",
    ) -> None:
        """종목별 워밍업 UI 갱신(워커 스레드에서 호출 가능 → 콜백 내부에서 main thread 위임 권장)."""
        if not self.stock_prep_cb:
            return
        try:
            self.stock_prep_cb(event, cur, total, name, code, reason)
        except Exception as e:
            self.log(f"⚠️ [Warmup] stock_prep_cb 오류: {e}")

    def _notify_ka10080_page_progress(
        self,
        pages: int,
        max_pages: int,
        deep_fetch: bool,
        *,
        stock_code: str,
        stock_name: str,
    ) -> None:
        """
        연속조회 진행을 감시 트리 신호상태 열에 반영합니다.
        메인 run() 루프의 `_warmup_prep_ui` 종목과 **실제 API 조회 종목**이 다를 수 있어
        (백그라운드 깊이 보강 등), 행 키는 항상 `stock_code` 를 쓰고 (cur/total) 은
        메인에서 같은 종목을 처리 중일 때만 전체 진행률(i/N)을 맞춥니다.
        """
        try:
            tag = "캘린더·깊이" if deep_fetch else "보충"
            ts = self._warmup_ka10080_tic_scope()
            bar_lbl = "15분봉" if str(ts).strip() == "15" else f"{ts}분봉"
            msg = f"{bar_lbl} API {pages}/{max_pages}페이지 ({tag})"
            nk = str(stock_code).split("_")[0].zfill(6)
            ctx = getattr(self, "_warmup_prep_ui", None)
            if ctx and str(ctx.get("code", "")).split("_")[0].zfill(6) == nk:
                cur = int(ctx["cur"])
                total = int(ctx["total"])
            else:
                cur, total = 1, 1
            self._notify_stock_prep(
                "prep_progress",
                cur,
                total,
                str(stock_name),
                nk,
                msg,
            )
        except Exception:
            pass

    def _validate_warmup_base(self, base: pd.DataFrame) -> Tuple[bool, str]:
        """
        DB·API 병합 직후 15분봉 시계열이 신호 엔진 기준을 만족하는지 검사.
        봉 수 부족 또는 장중 당일 구간 공백이 남으면 감시 시작 불가.
        """
        try:
            need = self._min_bars_for_signal()
            n = len(base) if base is not None and not base.empty else 0
            if n < need:
                return False, f"15분봉 부족 ({n}/{need} 필요, DB·API로 확보 불가)"
            if self._needs_intraday_api_backfill(base):
                return (
                    False,
                    "장중 당일 15분 구간 미충족(마지막 봉이 최신 슬롯에 미달, API·DB 보충 불충분)",
                )
            return True, ""
        except Exception as e:
            return False, f"워밍업 검증 오류: {e}"

    @staticmethod
    def _api_minute_row_key(item: Any) -> str:
        """ka10080 분봉 레코드에서 시각 키(중복 제거용)."""
        try:
            tm = str(
                item.get("cntr_tm")
                or item.get("cntg_tm")
                or item.get("stck_cntg_hour", "")
            ).strip()
            return tm if len(tm) >= 12 else ""
        except Exception:
            return ""

    def _fetch_warmup_minute_bars_paginated(
        self,
        code: str,
        name: str,
        token: str,
        base: pd.DataFrame,
        need: int,
        *,
        deep_fetch: bool = False,
        retention_days: int = 365,
        respect_warmup_lifecycle: bool = True,
        suppress_prep_tree_progress: bool = False,
    ) -> List[Any]:
        """
        워밍업 전용: ka10080 연속조회(설정 주기, 기본 15분봉)로 봉을 모은 뒤 15m OHLCV 병합 기준을 만족할 때까지 페이지를 이어 받음.
        deep_fetch=False: 신호 최소 봉·장중 당일 갭 해소 시 조기 종료.
        deep_fetch=True: 캘린더 retention_days 만큼 과거까지 확보되고 당일 갭도 없을 때까지 조회(상한 페이지 별도).
        respect_warmup_lifecycle=False: 메인 run() 종료로 is_running 이 꺼진 뒤에도
        백그라운드 깊이 조회가 끝까지 진행되도록 is_running 조기 break 를 쓰지 않습니다.
        suppress_prep_tree_progress=True: 신호상태 열 prep_progress 갱신을 하지 않습니다(백그라운드용).
        """
        try:
            if deep_fetch:
                max_pages = max(
                    1,
                    int(
                        float(
                            self.app.settings.get(
                                "realtime_warmup_ka10080_max_pages_deep", 500
                            )
                        )
                    ),
                )
            else:
                max_pages = max(
                    1,
                    int(float(self.app.settings.get("realtime_warmup_ka10080_max_pages", 40))),
                )
        except (TypeError, ValueError):
            max_pages = 500 if deep_fetch else 40
        try:
            # 기본값은 과도한 대기 없이 API 과부하만 피하는 쪽(느린 체감 완화)
            dmin = float(
                self.app.settings.get("realtime_warmup_ka10080_page_delay_min", 0.08)
            )
            dmax = float(
                self.app.settings.get("realtime_warmup_ka10080_page_delay_max", 0.22)
            )
        except (TypeError, ValueError):
            dmin, dmax = 0.08, 0.22
        if dmax < dmin:
            dmin, dmax = dmax, dmin

        tic_scope = self._warmup_ka10080_tic_scope()
        by_tm: Dict[str, Any] = {}
        nk: str = ""
        pages = 0
        early_stop = False
        # 빈 리스트 + 동일 next-key 가 반복되면(브로커/일시 오류) 무의미한 연속조회를 끊습니다.
        empty_same_key_streak = 0
        try:
            prog_iv = int(
                float(
                    self.app.settings.get(
                        "realtime_warmup_ka10080_prep_progress_every_n_pages", 3
                    )
                )
            )
        except (TypeError, ValueError):
            prog_iv = 3
        prog_iv = max(1, min(prog_iv, 50))
        # ka10080 응답에 next-key 가 없으면 브로커 과거 한계(또는 해당 종목 이력 끝)
        ka10080_terminal_no_next = False
        self._tt(
            f"ka10080 루프 시작 {name}({code}) max_pages={max_pages} need={need} "
            f"deep={deep_fetch} tic_scope={tic_scope!r} suppress_prog={suppress_prep_tree_progress}"
        )

        for _ in range(max_pages):
            if respect_warmup_lifecycle and (not self.is_running):
                # 메인 워밍업만 중단 시 루프 종료.백그라운드 깊이 조회는 respect_warmup_lifecycle=False.
                break
            nk_req = (nk or "").strip()
            try:
                raw_list, nk = self.data_manager.fetch_kiwoom_minute_chart_page(
                    code, token, nk_req, tic_scope=tic_scope
                )
            except Exception as e:
                self.log(f"⚠️ [Warmup-API] {name}({code}) 페이지 조회 예외: {e}")
                self._tt(f"ka10080 {code} fetch_kiwoom_minute_chart_page 예외: {e!r}")
                break

            pages += 1
            nk_out = (nk or "").strip()
            try:
                rl_n = len(raw_list) if raw_list is not None else -1
            except Exception:
                rl_n = -1
            if pages <= 3 or pages % 5 == 0 or pages >= max_pages:
                self._tt(
                    f"ka10080 {code} page={pages}/{max_pages} raw_n={rl_n} "
                    f"nk_out={(nk_out or '')[:40]!r} uniq_tm={len(by_tm)}"
                )
            if (not suppress_prep_tree_progress) and (
                pages == 1
                or pages % prog_iv == 0
                or pages >= max_pages
            ):
                self._notify_ka10080_page_progress(
                    pages,
                    max_pages,
                    deep_fetch,
                    stock_code=code,
                    stock_name=name,
                )
            # 1페이지만 찍히면 스팸에 가깝고, 장시간 조회는 120페이지 단위로만 대시보드에 기록
            if pages > 0 and pages % 120 == 0:
                try:
                    self.log(
                        f"⏳ [Warmup-API] {name}({code}) ka10080 연속조회 "
                        f"{pages}/{max_pages}페이지…"
                    )
                except Exception:
                    pass
            if raw_list is None:
                self.log(
                    f"⚠️ [Warmup-API] {name}({code}) 페이지 {pages} 응답 없음(오류)"
                )
                self._tt(f"ka10080 {code} page={pages} raw_list=None (HTTP/파싱 오류)")
                break
            if not raw_list:
                if not nk_out:
                    ka10080_terminal_no_next = True
                    break
                # 동일 next-key 로 빈 본문이 반복되면 진행이 없으므로 조기 종료
                if nk_out == nk_req and nk_req:
                    empty_same_key_streak += 1
                else:
                    empty_same_key_streak = 0
                if empty_same_key_streak >= 3:
                    try:
                        self.log(
                            f"⚠️ [Warmup-API] {name}({code}) 빈 응답+동일 next-key "
                            f"{empty_same_key_streak}회 — 연속조회 중단(p{pages}/{max_pages})"
                        )
                    except Exception:
                        pass
                    self._tt(
                        f"ka10080 {code} 빈응답+동일next-key 중단 p={pages} nk={nk_out!r}"
                    )
                    break
                # 과도한 연속 호출(429·서버 부하) 방지 — 기존에는 sleep 없이 continue 했음
                if respect_warmup_lifecycle and (not self.is_running):
                    break
                time.sleep(random.uniform(dmin, dmax))
                continue

            empty_same_key_streak = 0
            for item in raw_list:
                k = self._api_minute_row_key(item)
                if k:
                    by_tm[k] = item

            try:
                api15 = self._minute_api_list_to_15m_ohlcv(
                    list(by_tm.values()), tic_scope=tic_scope
                )
                merged = self._merge_15m_ohlcv(base, api15)
                n_merged = (
                    len(merged) if merged is not None and not merged.empty else 0
                )
                if deep_fetch:
                    # 루프 안 merged 는 누적 API+base 이므로 index.min() 으로 깊이 판정 가능.
                    # (_calendar_depth_satisfied_for_code 는 DB MIN 만 보아 페이지 중엔 과소평가될 수 있음)
                    depth_ok = self._calendar_depth_satisfied(merged, retention_days)
                    intraday_ok = not self._needs_intraday_api_backfill(merged)
                    if depth_ok and intraday_ok:
                        early_stop = True
                        break
                else:
                    if n_merged >= need and not self._needs_intraday_api_backfill(
                        merged
                    ):
                        early_stop = True
                        break
            except Exception as e:
                self.log(f"⚠️ [Warmup-API] {name}({code}) 중간 병합 검사 오류: {e}")

            nk = nk_out
            if not nk:
                ka10080_terminal_no_next = True
                break
            if respect_warmup_lifecycle and (not self.is_running):
                break
            time.sleep(random.uniform(dmin, dmax))

        self._tt(
            f"ka10080 종료 {code} pages={pages} early_stop={early_stop} "
            f"terminal_no_next={ka10080_terminal_no_next} uniq_tm={len(by_tm)}"
        )
        if (
            deep_fetch
            and ka10080_terminal_no_next
            and (not early_stop)
            and len(by_tm) > 0
            and self._warmup_ohlcv_source() == "realtime_15m_db_then_api"
        ):
            try:
                store = getattr(self.app, "realtime_monitor_bars_store", None)
                if store is not None:
                    store.set_ka10080_deep_exhausted(code, retention_days)
            except Exception as e:
                try:
                    self.log(
                        f"⚠️ [Warmup-API] {name}({code}) ka10080 꼬리 플래그 기록 실패: {e}"
                    )
                except Exception:
                    pass

        if by_tm:
            # 소량 페이지(빠른 통과)는 생략, 깊이 조회·다페이지만 요약 로그
            if deep_fetch or pages >= 20 or len(by_tm) >= 500:
                try:
                    ts = str(tic_scope).strip() or "?"
                    bar_lbl = "15분봉" if ts == "15" else f"{ts}분봉"
                    self.log(
                        f"📡 [Warmup-API] {name}({code}) ka10080({bar_lbl}) {pages}페이지, "
                        f"유일 봉 {len(by_tm)}건"
                        + (" (조기 종료)" if early_stop else "")
                        + (" (캘린더·깊이)" if deep_fetch else "")
                    )
                except Exception:
                    pass
        return list(by_tm.values())

    def _merge_15m_ohlcv(self, a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
        """동일 종목 DB/API 15분 OHLCV 병합(인덱스 중복 시 최신 우선)."""
        try:
            if a is None or a.empty:
                return b.copy() if b is not None and not b.empty else pd.DataFrame()
            if b is None or b.empty:
                return a.copy()
            out = pd.concat([a, b]).sort_index()
            out = out[~out.index.duplicated(keep="last")]
            return out
        except Exception as e:
            self.log(f"⚠️ [Warmup] 15m 병합 실패: {e}")
            return a if a is not None and not a.empty else pd.DataFrame()

    def _minute_api_list_to_15m_ohlcv(
        self, raw_api: Any, *, tic_scope: Optional[str] = None
    ) -> pd.DataFrame:
        """
        키움 ka10080 응답 리스트 → 15분 OHLCV DataFrame.
        - tic_scope \"15\"(기본): 서버가 이미 15분봉을 주므로 리샘플 없이 OHLCV만 정렬·중복 제거.
        - 그 외(예: \"1\"): 1분봉을 closed='right', label='right' 로 15분봉에 합침(레거시·비권장).
        """
        if not raw_api:
            return pd.DataFrame()
        ts = str(tic_scope).strip() if tic_scope is not None else ""
        if not ts:
            ts = self._warmup_ka10080_tic_scope()
        try:
            df = pd.DataFrame(raw_api)
            f_cl, f_op, f_hg, f_lw, f_tm, f_vl = (
                "cur_prc",
                "open_pric",
                "high_pric",
                "low_pric",
                "cntr_tm",
                "trde_qty",
            )
            for c in (f_cl, f_op, f_hg, f_lw, f_vl):
                if c not in df.columns:
                    return pd.DataFrame()
                df[c] = pd.to_numeric(
                    df[c].astype(str).str.replace("+", "").str.replace("-", ""),
                    errors="coerce",
                ).fillna(0)
            df["datetime"] = pd.to_datetime(df[f_tm].astype(str), format="%Y%m%d%H%M%S", errors="coerce")
            df = df.dropna(subset=["datetime"])
            if df.empty:
                return pd.DataFrame()
            df = df.set_index("datetime").sort_index()
            df = df[~df.index.duplicated(keep="last")]
            if str(ts).strip() == "15":
                out = df[[f_op, f_hg, f_lw, f_cl, f_vl]].copy()
                out.rename(
                    columns={
                        f_op: "Open",
                        f_hg: "High",
                        f_lw: "Low",
                        f_cl: "Close",
                        f_vl: "Volume",
                    },
                    inplace=True,
                )
                return out[["Open", "High", "Low", "Close", "Volume"]].copy()
            out = (
                df.resample("15min", closed="right", label="right")
                .agg({f_op: "first", f_hg: "max", f_lw: "min", f_cl: "last", f_vl: "sum"})
                .dropna(how="any")
            )
            out.rename(
                columns={f_op: "Open", f_hg: "High", f_lw: "Low", f_cl: "Close", f_vl: "Volume"},
                inplace=True,
            )
            return out[["Open", "High", "Low", "Close", "Volume"]].copy()
        except Exception as e:
            self.log(f"⚠️ [Warmup-API] 분봉→15m 변환 실패: {e}")
            return pd.DataFrame()

    def _inprogress_slot_recover_with_1m_enabled(self) -> bool:
        """재시작 시 진행 중 15m 슬롯을 ka10080(1분봉)으로 보강할지(기본 ON)."""
        try:
            return bool(
                self.app.settings.get(
                    "realtime_warmup_inprogress_slot_recover_with_1m", True
                )
            )
        except Exception:
            return True

    def _minute_api_raw_to_1m_ohlcv(self, raw_api: Any) -> pd.DataFrame:
        """
        ka10080(tic_scope='1') 응답 → DatetimeIndex Open/High/Low/Close/Volume(1분봉).
        cntr_tm 은 봉 종가 시각으로 해석합니다(_minute_api_list_to_15m_ohlcv 와 동일 규약).
        """
        if not raw_api:
            return pd.DataFrame()
        try:
            df = pd.DataFrame(raw_api)
            f_cl, f_op, f_hg, f_lw, f_tm, f_vl = (
                "cur_prc",
                "open_pric",
                "high_pric",
                "low_pric",
                "cntr_tm",
                "trde_qty",
            )
            for c in (f_cl, f_op, f_hg, f_lw, f_vl):
                if c not in df.columns:
                    return pd.DataFrame()
                df[c] = pd.to_numeric(
                    df[c].astype(str).str.replace("+", "").str.replace("-", ""),
                    errors="coerce",
                ).fillna(0)
            df["datetime"] = pd.to_datetime(
                df[f_tm].astype(str), format="%Y%m%d%H%M%S", errors="coerce"
            )
            df = df.dropna(subset=["datetime"])
            if df.empty:
                return pd.DataFrame()
            df = df.set_index("datetime").sort_index()
            df = df[~df.index.duplicated(keep="last")]
            out = df[[f_op, f_hg, f_lw, f_cl, f_vl]].copy()
            out.rename(
                columns={
                    f_op: "Open",
                    f_hg: "High",
                    f_lw: "Low",
                    f_cl: "Close",
                    f_vl: "Volume",
                },
                inplace=True,
            )
            return out[["Open", "High", "Low", "Close", "Volume"]].copy()
        except Exception as e:
            try:
                self.log(f"⚠️ [Warmup-1m] 1분봉 정규화 실패: {e}")
            except Exception:
                pass
            return pd.DataFrame()

    def _current_inprogress_slot_range(
        self,
    ) -> Optional[Tuple[pd.Timestamp, pd.Timestamp]]:
        """
        현재 시각 기준 진행 중 15m 슬롯의 (시작 시각, 종가 시각) 반환.
        장 시간이 아니거나 주말이면 None.
        종가 시각은 right-label 규칙(slot_start + 15분).
        """
        try:
            now = datetime.now()
            if now.weekday() >= 5:
                return None
            open_t = datetime.strptime("09:00", "%H:%M").time()
            close_t = datetime.strptime("15:30", "%H:%M").time()
            if now.time() < open_t or now.time() > close_t:
                return None
            n = pd.Timestamp(now)
            minute_floor = (n.minute // 15) * 15
            slot_start = n.replace(
                minute=minute_floor, second=0, microsecond=0
            )
            slot_end = slot_start + pd.Timedelta(minutes=15)
            return slot_start, slot_end
        except Exception:
            return None

    def _maybe_recover_inprogress_slot_with_1m(
        self,
        code: str,
        name: str,
        base: pd.DataFrame,
        token: Optional[str],
    ) -> pd.DataFrame:
        """
        재시작 시 진행 중 15m 슬롯의 V/OHLC 가 부분 손실됐을 가능성을 보정합니다.

        ka10080(tic_scope='1') 로 1분봉 1페이지를 받아 진행 중 슬롯 (slot_start, slot_end]
        구간에 속하는 1분봉을 합산해 base 의 해당 슬롯 봉을 정확한 OHLCV 로 덮어씁니다.
        - 1분봉 cntr_tm 은 _봉 종가 시각_(09:01 = 09:00:01~09:01:00).
        - 진행 중 슬롯은 아직 마감되지 않았으므로 _마감된 1분봉만_ 합산되며, 마지막 1분은
          이후 0B 차분 누적으로 stream_processor 가 채웁니다.
        - 결과 봉의 인덱스는 slot_end(right-label) — 기존 15m base 와 동일 규약.
        """
        try:
            if not self._inprogress_slot_recover_with_1m_enabled():
                return base
            if not token:
                return base
            rng = self._current_inprogress_slot_range()
            if rng is None:
                return base
            slot_start, slot_end = rng
            try:
                raw_list, _nk = self.data_manager.fetch_kiwoom_minute_chart_page(
                    code, token, "", tic_scope="1"
                )
            except Exception as e:
                try:
                    self.log(
                        f"⚠️ [Warmup-1m] {name}({code}) ka10080(1분) 조회 예외: {e}"
                    )
                except Exception:
                    pass
                return base
            if not raw_list:
                return base
            df_1m = self._minute_api_raw_to_1m_ohlcv(raw_list)
            if df_1m is None or df_1m.empty:
                return base
            mask = (df_1m.index > slot_start) & (df_1m.index <= slot_end)
            in_slot = df_1m[mask]
            if in_slot.empty:
                return base
            try:
                o_val = float(in_slot.iloc[0]["Open"])
                h_val = float(in_slot["High"].max())
                l_val = float(in_slot["Low"].min())
                c_val = float(in_slot.iloc[-1]["Close"])
                v_val = int(in_slot["Volume"].sum())
            except Exception:
                return base
            if o_val <= 0 or h_val <= 0 or l_val <= 0 or c_val <= 0:
                return base
            if base is None or base.empty:
                out = pd.DataFrame(
                    columns=["Open", "High", "Low", "Close", "Volume"]
                )
            else:
                out = base.copy()
            new_idx = pd.Timestamp(slot_end)
            out.loc[new_idx, "Open"] = o_val
            out.loc[new_idx, "High"] = h_val
            out.loc[new_idx, "Low"] = l_val
            out.loc[new_idx, "Close"] = c_val
            out.loc[new_idx, "Volume"] = v_val
            out = out.sort_index()
            out = out[~out.index.duplicated(keep="last")]
            try:
                self.log(
                    f"🔄 [Warmup-1m] {name}({code}) 진행중 "
                    f"{new_idx.strftime('%H:%M')} 슬롯 1m 보강 "
                    f"({len(in_slot)}봉 합산, V={v_val:,d})"
                )
            except Exception:
                pass
            return out
        except Exception as e:
            try:
                self.log(
                    f"⚠️ [Warmup-1m] {name}({code}) 진행중 슬롯 보강 실패: {e}"
                )
            except Exception:
                pass
            return base

    def _needs_intraday_api_backfill(self, base: pd.DataFrame) -> bool:
        """
        하베스팅은 보통 전일(이전 영업일)까지이므로, 장중에 감시를 켜면
        당일 09:00~현재에 해당하는 15분봉이 DB에 없을 수 있다.
        이 경우 봉 개수가 신호 최소 봉수를 넘더라도 API 로 당일 구간을 메워야 한다.
        """
        try:
            if not bool(self.app.settings.get("use_intraday_api_backfill", True)):
                return False
            now = datetime.now()
            if now.weekday() >= 5:
                return False
            open_t = datetime.strptime("09:00", "%H:%M").time()
            close_t = datetime.strptime("15:30", "%H:%M").time()
            if now.time() < open_t or now.time() > close_t:
                return False
            today = now.date()
            if base.empty or not isinstance(base.index, pd.DatetimeIndex):
                return True
            last_ts = pd.Timestamp(base.index.max())
            if last_ts.date() < today:
                return True
            # 당일 데이터가 있어도, 마지막 봉이 '지금 시각 기준 마지막 완결 15분'보다 오래됐으면 갭
            n = pd.Timestamp(now)
            minute_floor = (n.minute // 15) * 15
            expected_last = n.replace(minute=minute_floor, second=0, microsecond=0)
            if last_ts < expected_last - pd.Timedelta(minutes=1):
                return True
            return False
        except Exception:
            return True

    def _maybe_api_backfill_15m(
        self,
        code: str,
        name: str,
        base: pd.DataFrame,
        token: Optional[str],
        *,
        quick_only: bool = False,
        respect_warmup_lifecycle: bool = True,
        suppress_prep_tree_progress: bool = False,
    ) -> pd.DataFrame:
        """
        (1) 15m 봉 수 < 신호 기준 이거나
        (2) 장중인데 당일·최근 15분 구간이 DB 에 비어 있는 경우
        → 키움 분봉 API 로 보충 후 병합.
        quick_only=True: 과거 캘린더 깊이는 생략(빠른 워밍업). 깊이는 백그라운드 큐에서 처리.
        """
        try:
            if self._warmup_ohlcv_source() == "api_only":
                if not bool(self.app.settings.get("use_api_warmup_backfill", True)):
                    self.log(
                        f"❌ [Warmup] {name}({code}) realtime_warmup_ohlcv_source=api_only 인데 "
                        f"use_api_warmup_backfill=OFF — 워밍업 불가"
                    )
                    return base
            if not bool(self.app.settings.get("use_api_warmup_backfill", True)):
                return base
            if not token:
                return base
            need = self._min_bars_for_signal()
            short = base.empty or len(base) < need
            gap = self._needs_intraday_api_backfill(base)
            deep = self._warmup_fetch_calendar_history_enabled()
            rd = self._warmup_retention_calendar_days()
            if quick_only:
                need_depth = False
                use_deep_fetch = False
            else:
                need_depth = deep and not self._calendar_depth_satisfied_for_code(
                    code, base, rd
                )
                # 캘린더 깊이(1년) 보강이 필요할 때만 deep_fetch(다페이지·깊이 루프).
                # need_depth=False 인데 gap(장중 당일)만 있으면 얕은 연속조회로 충분 — deep 켜져 있어도
                # 매번 (캘린더·깊이) 경로로 오해·과다 조회되던 문제 방지.
                use_deep_fetch = bool(deep and need_depth)
            if not short and not gap and not need_depth:
                return base
            reason = []
            if quick_only:
                reason.append("빠른워밍업")
            if short:
                reason.append(f"봉수<{need}")
            if gap:
                reason.append("장중당일갭")
            if need_depth:
                reason.append(f"과거{rd}일미충족")
            raw_api = self._fetch_warmup_minute_bars_paginated(
                code,
                name,
                token,
                base,
                need,
                deep_fetch=use_deep_fetch,
                retention_days=rd,
                respect_warmup_lifecycle=respect_warmup_lifecycle,
                suppress_prep_tree_progress=suppress_prep_tree_progress,
            )
            api15 = self._minute_api_list_to_15m_ohlcv(
                raw_api, tic_scope=self._warmup_ka10080_tic_scope()
            )
            if api15.empty:
                if gap or short:
                    self.log(
                        f"⚠️ [Warmup-API] {name}({code}) 보충 스킵(응답없음) [{','.join(reason)}]"
                    )
                return base
            merged = self._merge_15m_ohlcv(base, api15)
            # 보충 성공은 종목 수만큼 반복되므로 대시보드에는 남기지 않음(실패·경고만 로그).
            return merged
        except Exception as e:
            self.log(f"⚠️ [Warmup-API] {code} 보충 실패: {e}")
            return base

    def _load_15m_ohlcv_frame(self, code: str) -> pd.DataFrame:
        """
        realtime_15m_db_then_api: realtime_15m_*.db 우선 (하베스팅 DB 미사용).
        db_then_api: trading_data.db bars_15m 우선. 비어 있으면 bars_1m 을 15분봉으로 리샘플.
        api_only: 빈 프레임 반환 — 이후 _maybe_api_backfill_15m 가 API 전량으로 채움.
        반환: DatetimeIndex, 열 Open/High/Low/Close/Volume
        """
        if self._warmup_ohlcv_source() == "api_only":
            return pd.DataFrame()
        if self._warmup_ohlcv_source() == "realtime_15m_db_then_api":
            base_rt = self._load_from_realtime_monitor_db(code)
            return base_rt if base_rt is not None and not base_rt.empty else pd.DataFrame()

        lim = self._warmup_query_limit()
        try:
            raw = self.data_manager.get_bars(code, interval="15m", limit=lim)
        except Exception as e:
            self.log(f"⚠️ [Warmup-15m] {code} 조회 오류: {e}")
            raw = pd.DataFrame()

        if raw is not None and not raw.empty:
            try:
                d = raw.copy()
                if "time" not in d.columns:
                    return pd.DataFrame()
                d["time"] = pd.to_datetime(d["time"], errors="coerce")
                d = d.dropna(subset=["time"])
                d = d.set_index("time").sort_index()
                for c in ("Open", "High", "Low", "Close", "Volume"):
                    if c not in d.columns:
                        return pd.DataFrame()
                return d[["Open", "High", "Low", "Close", "Volume"]].copy()
            except Exception as e:
                self.log(f"⚠️ [Warmup-15m] {code} 정규화 실패: {e}")

        # 폴백: 1분 → 15분 리샘플
        try:
            m1 = self.data_manager.get_bars(code, interval="1m", limit=max(lim * 4, 8000))
        except Exception as e:
            self.log(f"⚠️ [Warmup-1m] {code} 조회 오류: {e}")
            return pd.DataFrame()

        if m1 is None or m1.empty or "time" not in m1.columns:
            return pd.DataFrame()

        try:
            d1 = m1.copy()
            d1["time"] = pd.to_datetime(d1["time"], errors="coerce")
            d1 = d1.dropna(subset=["time"])
            d1 = d1.set_index("time").sort_index()
            out = (
                d1.resample("15min", label="right", closed="right")
                .agg(
                    {
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    }
                )
                .dropna(how="any")
            )
            if out.empty:
                return pd.DataFrame()
            try:
                if self._warmup_resample_hint_count < 3:
                    self._warmup_resample_hint_count += 1
                    self.log(
                        f"ℹ️ [Warmup] {code}: bars_15m 비어 1m→15m 리샘플 ({len(out)}봉)"
                    )
            except Exception:
                pass
            return out
        except Exception as e:
            self.log(f"⚠️ [Warmup] {code} 1m→15m 리샘플 실패: {e}")
            return pd.DataFrame()

    def _execute_warmup_one_code(
        self,
        code: str,
        name: str,
        token: Optional[str],
        bars_data: Dict[str, Any],
        on_stock_ready: Optional[Callable[[str], None]],
        monitor_info: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, int]:
        """
        한 종목 워밍업 1회 시도.
        성공 시 bars_data 반영 및 on_stock_ready 호출.
        반환: (성공 여부, 실패 사유 또는 빈 문자열, 검증 시점 15m 봉 수 또는 성공 시 스트림 봉 수)
        """
        try:
            self._tt(
                f"_execute_one begin code={code} name={name!r} token_present={bool(token)}"
            )
            # 감시 시작 조건: 최소 봉·장중 당일 갭만 — 캘린더 1년 깊이는 기다리지 않음(백그라운드 큐).
            base = self._load_15m_ohlcv_frame(code)
            try:
                n0 = len(base) if base is not None and not base.empty else 0
            except Exception:
                n0 = 0
            self._tt(f"_execute_one {code} _load_15m_ohlcv_frame rows={n0}")
            # DB만으로 이미 검증되면 ka10080 호출·페이지 대기 전체를 생략(42종목 시 누적 체감 큼)
            ok_skip_api, _rs = self._validate_warmup_base(base)
            if ok_skip_api:
                try:
                    ctx = getattr(self, "_warmup_prep_ui", None)
                    if ctx:
                        self._notify_stock_prep(
                            "prep_progress",
                            int(ctx["cur"]),
                            int(ctx["total"]),
                            name,
                            code,
                            "DB만으로 검증 통과(API 생략)",
                        )
                except Exception:
                    pass
            else:
                self._tt(
                    f"_execute_one {code} ka10080 경로 진입(quick API 보충) "
                    f"pre_rows={n0} token={bool(token)}"
                )
                base = self._maybe_api_backfill_15m(
                    code, name, base, token, quick_only=True
                )
                try:
                    n1 = len(base) if base is not None and not base.empty else 0
                except Exception:
                    n1 = 0
                self._tt(f"_execute_one {code} ka10080 경로 종료 rows={n1}")
            # 레거시 고정 sleep 제거: 필요 시에만 아주 짧은 양보(기본 0)
            try:
                mx = float(
                    self.app.settings.get(
                        "realtime_warmup_pre_validate_pause_max_sec", 0.0
                    )
                )
            except (TypeError, ValueError):
                mx = 0.0
            if mx > 0:
                time.sleep(random.uniform(0.0, min(mx, 2.0)))

            # [v0.6.39] 재시작 시 진행 중 15m 슬롯의 V/OHLC 부분 손실을 ka10080(1분봉) 합산으로 보정
            try:
                base = self._maybe_recover_inprogress_slot_with_1m(
                    code, name, base, token
                )
            except Exception as e:
                self._tt(f"_execute_one {code} 진행중 슬롯 1m 보강 예외: {e!r}")

            ok, reason = self._validate_warmup_base(base)
            nbar = len(base) if base is not None and not base.empty else 0
            if not ok:
                self._tt(
                    f"_execute_one {code} _validate_warmup_base 실패 nbar={nbar} "
                    f"reason={reason!r}"
                )
                try:
                    bars_data.pop(code, None)
                except Exception:
                    pass
                return False, reason, nbar

            self._tt(f"_execute_one {code} validate OK nbar={nbar} → persist/지표")
            self._persist_warmup_base_to_rt15m_db(code, base)

            df_slice = self._indicator_calc_input_tail(base)
            self._tt(
                f"_execute_one {code} calculate_indicators 입력 rows="
                f"{len(df_slice) if df_slice is not None and not getattr(df_slice, 'empty', True) else 0}"
            )
            df_ind = TradingStrategy.calculate_indicators(df_slice, self.app.settings)
            fin = self._finalize_bars_for_stream(df_ind)
            lk = getattr(self.app, "_bars_data_lock", None)
            cm = lk if lk is not None else contextlib.nullcontext()
            with cm:
                bars_data[code] = fin
            try:
                n_stream = len(fin) if fin is not None and not getattr(fin, "empty", True) else 0
            except Exception:
                n_stream = 0
            try:
                self._seed_monitor_signal_cache_after_warmup(
                    code,
                    monitor_info,
                    df_ind,
                    stream_bar_count=int(n_stream),
                )
            except Exception:
                pass
            n_ok = len(bars_data[code]) if bars_data.get(code) is not None else 0
            if on_stock_ready is not None:
                self._tt(f"_execute_one {code} on_stock_ready 콜백 호출")
                try:
                    on_stock_ready(code)
                except Exception as cb_e:
                    self.log(f"⚠️ [Warmup] on_stock_ready 콜백 오류 ({code}): {cb_e}")
                    self._tt(f"_execute_one {code} on_stock_ready 예외: {cb_e!r}")
            if self._warmup_fetch_calendar_history_enabled():
                self._enqueue_deep_history_backfill(code, name)
            return True, "", n_ok
        except Exception as e:
            try:
                bars_data.pop(code, None)
            except Exception:
                pass
            return False, str(e), 0

    def _enqueue_deep_history_backfill(self, code: str, name: str) -> None:
        """캘린더 깊이 보강 작업을 백그라운드 큐에 넣습니다."""
        try:
            if not self._warmup_fetch_calendar_history_enabled():
                return
            if self._deep_history_queue is None:
                self._deep_history_queue = queue.Queue()
            self._deep_history_queue.put((str(code).zfill(6), str(name)))
            self._start_deep_history_worker_once()
        except Exception as e:
            self.log(f"⚠️ [Warmup-DeepBG] 큐 등록 실패 ({code}): {e}")

    def _start_deep_history_worker_once(self) -> None:
        if self._deep_history_worker_started:
            return
        self._deep_history_worker_started = True
        t = threading.Thread(
            target=self._deep_history_worker_loop,
            daemon=True,
            name="WarmupDeepHistory",
        )
        t.start()

    def _deep_history_worker_loop(self) -> None:
        while True:
            q = self._deep_history_queue
            if q is None:
                time.sleep(0.5)
                continue
            try:
                code, name = q.get()
            except Exception:
                time.sleep(0.5)
                continue
            try:
                self._run_background_deep_history(code, name)
            except Exception as e:
                try:
                    self.log(f"⚠️ [Warmup-DeepBG] {name}({code}) 처리 예외: {e}")
                except Exception:
                    pass
            finally:
                try:
                    q.task_done()
                except Exception:
                    pass

    def _merge_deep_ohlcv_with_live(
        self, hist: pd.DataFrame, cur: Optional[pd.DataFrame]
    ) -> pd.DataFrame:
        """깊이 보강 OHLCV와 실시간 bars_data 병합 — 겹치는 시각은 cur(스트림) 우선."""
        try:
            if hist is None or hist.empty:
                return pd.DataFrame()
            if cur is None or getattr(cur, "empty", True):
                return hist.copy()
            if not isinstance(cur.index, pd.DatetimeIndex):
                return hist.copy()
            ohlc = ("Open", "High", "Low", "Close", "Volume")
            if not all(c in cur.columns for c in ohlc):
                return hist.copy()
            cut = pd.Timestamp(cur.index.min())
            hpart = hist[hist.index < cut].copy() if len(hist.index) else pd.DataFrame()
            cpart = cur[list(ohlc)].copy()
            out = pd.concat([hpart, cpart]).sort_index()
            out = out[~out.index.duplicated(keep="last")]
            return out
        except Exception as e:
            self.log(f"⚠️ [Warmup-DeepBG] OHLCV 병합 실패: {e}")
            return pd.DataFrame()

    def _apply_deep_history_to_bars_data(self, code: str, merged_ohlcv: pd.DataFrame) -> None:
        """깊이 보강 결과로 bars_data[code] 지표 재계산(락 사용)."""
        lk = getattr(self.app, "_bars_data_lock", None)
        cm = lk if lk is not None else contextlib.nullcontext()
        with cm:
            cur = (self.app.bars_data or {}).get(code)
            combined = self._merge_deep_ohlcv_with_live(merged_ohlcv, cur)
            if combined is None or combined.empty:
                return
            df_slice = self._indicator_calc_input_tail(combined)
            df_ind = TradingStrategy.calculate_indicators(df_slice, self.app.settings)
            fin = self._finalize_bars_for_stream(df_ind)
            # _finalize_bars_for_stream 은 cum_v_start 를 0 으로 초기화하므로,
            # 장중 backfill 시 _진행 중 봉_ 의 cum_v_start 가 0 으로 리셋되어
            # 그 슬롯 후반부 차분만 잡히는 문제가 생김 → 교체 전 bars_data 의
            # 마지막 봉 cum_v_start 를 동일 슬롯에 한해 보존합니다.
            try:
                if (
                    cur is not None
                    and not getattr(cur, "empty", True)
                    and fin is not None
                    and not getattr(fin, "empty", True)
                    and "cum_v_start" in cur.columns
                    and "cum_v_start" in fin.columns
                ):
                    cur_last_idx = cur.index[-1]
                    fin_last_idx = fin.index[-1]
                    if cur_last_idx == fin_last_idx:
                        prev_start = cur.at[cur_last_idx, "cum_v_start"]
                        if hasattr(prev_start, "iloc"):
                            prev_start = prev_start.iloc[0]
                        if float(prev_start) > 0:
                            fin.at[fin_last_idx, "cum_v_start"] = float(prev_start)
            except Exception:
                pass
            if not isinstance(self.app.bars_data, dict):
                self.app.bars_data = {}
            self.app.bars_data[code] = fin

    def _run_background_deep_history(self, code: str, name: str) -> None:
        """캘린더 깊이 ka10080 보강 → RT DB 영속화 → bars_data 병합 갱신."""
        token: Optional[str] = None
        try:
            token = self.auth_manager.get_auth_token()
        except Exception:
            pass
        if not token:
            self.log(f"⚠️ [Warmup-DeepBG] {code} 토큰 없음 — 건너뜀")
            return
        base = self._load_15m_ohlcv_frame(code)
        merged = self._maybe_api_backfill_15m(
            code,
            name,
            base,
            token,
            quick_only=False,
            respect_warmup_lifecycle=False,
            suppress_prep_tree_progress=True,
        )
        if merged is None or merged.empty:
            self.log(f"⚠️ [Warmup-DeepBG] {name}({code}) 병합 결과 없음")
            return
        self._persist_warmup_base_to_rt15m_db(code, merged)
        self._apply_deep_history_to_bars_data(code, merged)
        # 깊이 보강은 suppress_prep_tree_progress 로 신호상태 열을 건드리지 않음.
        # 여기서 prep_ok 를 다시 쏘면 메인 워밍업 이후 SignalEngine 이 넣은 감시·신호 문구가
        # 「✅ 15분봉·검증 완료」로 되돌아가는 회귀가 생기므로 UI 알림은 보내지 않음.

    def _finalize_bars_for_stream(self, df_ind: pd.DataFrame) -> pd.DataFrame:
        """StreamProcessor 가 기대하는 time(HHMM)·cum_v_start 를 붙이고 길이를 제한."""
        try:
            if df_ind is None or df_ind.empty:
                return df_ind
            out = df_ind.copy()
            if not isinstance(out.index, pd.DatetimeIndex):
                return out
            out["time"] = out.index.map(_bar_slot_hhmm)
            if "cum_v_start" not in out.columns:
                out["cum_v_start"] = 0
            keep = self._bars_max_keep()
            return out.tail(keep)
        except Exception as e:
            self.log(f"⚠️ [Warmup] 스트림용 컬럼 부착 실패: {e}")
            return df_ind

    def run(
        self,
        monitor_codes,
        monitor_info,
        bars_data,
        on_stock_ready: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        DB·API로 15분봉 워밍업 후 종목별 성공 여부 dict 반환.
        (1분 볼륨 프로파일·배경 API 동기화는 신호 경로와 무관하여 제거됨.)

        on_stock_ready: 종목별 검증·지표 적재가 끝난 직후(bars_data 반영 후) 호출.
            워밍업 워커 스레드에서 실행되므로 UI·스트림 갱신은 호출부에서 메인 스레드로 위임할 것.

        실패 시 realtime_warmup_retry_max·realtime_warmup_retry_delay_sec 설정에 따라
        동일 종목을 자동 재시도합니다(일시적 API·토큰 오류 대응).
        """
        results: Dict[str, Dict[str, Any]] = {}
        self.is_running = True
        self._warmup_resample_hint_count = 0
        total = len(monitor_codes)
        retry_max = self._warmup_retry_max()
        retry_delay = self._warmup_retry_delay_sec()
        max_attempts = max(1, 1 + retry_max)

        _src = self._warmup_ohlcv_source()
        mp_deep = 500
        if self._warmup_fetch_calendar_history_enabled():
            try:
                mp_deep = int(
                    float(
                        self.app.settings.get(
                            "realtime_warmup_ka10080_max_pages_deep", 500
                        )
                    )
                )
            except (TypeError, ValueError):
                mp_deep = 500
        hint_parts: List[str] = []
        if _src == "api_only":
            hint_parts.append("DB 미사용·ka10080 전용")
        elif _src == "realtime_15m_db_then_api":
            hint_parts.append("실시간15mDB→API·통과 시 RT DB 영속화")
        else:
            hint_parts.append("하베스팅 DB→API")
        if self._warmup_fetch_calendar_history_enabled():
            hint_parts.append(
                f"캘린더~{self._warmup_retention_calendar_days()}일·깊이≤{mp_deep}p/종목→백그라운드"
            )
        try:
            self.log(
                f"📦 [Warmup] {total}개 종목 검증·적재 (출처={_src}, 재시도≤{retry_max}회·"
                f"{retry_delay:.0f}s) — "
                + " · ".join(hint_parts)
            )
        except Exception:
            pass
        self._tt(
            f"run() 시작 merged={total} 재시도≤{retry_max} delay={retry_delay}s "
            f"출처={_src} thread={threading.current_thread().name!r}"
        )
        for i, code in enumerate(monitor_codes):
            if not self.is_running:
                break
            name = monitor_info.get(code, {}).get("name", "Unknown")
            self._tt(f"[{i + 1}/{total}] 종목 루프 진입 code={code} name={name!r}")
            self._warmup_prep_ui = {
                "cur": i + 1,
                "total": total,
                "name": name,
                "code": code,
            }
            try:
                self._notify_stock_prep("prep_start", i + 1, total, name, code, "")

                final_reason = ""
                final_nbar = 0

                for attempt in range(max_attempts):
                    if not self.is_running:
                        break
                    self._tt(
                        f"[{i + 1}/{total}] attempt {attempt + 1}/{max_attempts} "
                        f"code={code} → get_auth_token 호출"
                    )
                    token = self.auth_manager.get_auth_token()
                    self._tt(
                        f"[{i + 1}/{total}] attempt {attempt + 1} "
                        f"code={code} token_ok={bool(token)}"
                    )
                    self._tt(
                        f"[{i + 1}/{total}] attempt {attempt + 1} "
                        f"code={code} → _execute_warmup_one_code 진입"
                    )
                    ok, reason, nbar = self._execute_warmup_one_code(
                        code,
                        name,
                        token,
                        bars_data,
                        on_stock_ready,
                        monitor_info,
                    )
                    self._tt(
                        f"[{i + 1}/{total}] attempt {attempt + 1} "
                        f"code={code} _execute 종료 ok={ok} nbar={nbar} "
                        f"reason={(reason or '')[:160]!r}"
                    )
                    if ok:
                        results[code] = {
                            "ok": True,
                            "reason": "",
                            "bars": nbar,
                        }
                        if attempt > 0:
                            self.log(
                                f"✅ [Warmup] {name}({code}) "
                                f"{attempt + 1}번째 시도에서 성공 (bars={nbar})"
                            )
                        # 1차 성공은 종목별 로그 생략 — 루프 종료 후 요약 한 줄만 출력
                        self._notify_stock_prep(
                            "prep_ok", i + 1, total, name, code, ""
                        )
                        break

                    final_reason, final_nbar = reason, nbar
                    last_try = attempt >= max_attempts - 1
                    if not last_try and self.is_running:
                        self.log(
                            f"⏳ [Warmup] {name}({code}) 실패 "
                            f"(시도 {attempt + 1}/{max_attempts}): {reason} — "
                            f"{retry_delay:.0f}초 후 재시도…"
                        )
                        self._interruptible_sleep(retry_delay)
                    elif last_try:
                        results[code] = {
                            "ok": False,
                            "reason": final_reason,
                            "bars": final_nbar,
                        }
                        self.log(
                            f"❌ [Warmup] {name}({code}) {max_attempts}회 시도 후 최종 실패: {final_reason}"
                        )
                        self._notify_stock_prep(
                            "prep_fail", i + 1, total, name, code, final_reason
                        )
                # 재시도 루프 중 stop() 등으로 중단되면 results 가 비어 있을 수 있음
                if code not in results and not self.is_running:
                    results[code] = {
                        "ok": False,
                        "reason": "워밍업 중단(재시도 중)",
                        "bars": 0,
                    }
                    self._notify_stock_prep(
                        "prep_fail",
                        i + 1,
                        total,
                        name,
                        code,
                        "워밍업 중단(재시도 중)",
                    )
            finally:
                self._warmup_prep_ui = None

        for code in monitor_codes:
            if code not in results:
                results[code] = {
                    "ok": False,
                    "reason": "워밍업 중단 또는 미처리",
                    "bars": 0,
                }
                self._notify_stock_prep(
                    "prep_fail",
                    total,
                    total,
                    monitor_info.get(code, {}).get("name", "Unknown"),
                    code,
                    "워밍업 중단 또는 미처리",
                )

        self.is_running = False
        try:
            nres = len(results)
        except Exception:
            nres = 0
        self._tt(f"run() 종료 thread={threading.current_thread().name!r} results={nres}건")
        if total > 0:
            try:
                n_ok = sum(
                    1
                    for c in monitor_codes
                    if isinstance(results.get(c), dict) and results[c].get("ok")
                )
                n_fail = int(total) - n_ok
                if n_fail > 0:
                    self.log(
                        f"✅ [Warmup] 메인 요약: 성공 {n_ok}/{total}종목, 실패 {n_fail}종목 "
                        f"(실패 상세는 위 ❌ 로그 참고)"
                    )
                else:
                    self.log(
                        f"✅ [Warmup] 메인 요약: 전 종목 성공 {n_ok}/{total}종목"
                    )
            except Exception as e:
                try:
                    self.log(f"⚠️ [Warmup] 요약 로그 출력 실패: {e}")
                except Exception:
                    pass
        return results

    def stop(self):
        """워밍업 중단"""
        self.is_running = False
