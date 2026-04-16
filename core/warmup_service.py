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

    def _warmup_fetch_calendar_history_enabled(self) -> bool:
        """True: ka10080 를 캘린더 보존일만큼 과거까지 채울 때까지 연속조회(신호 최소봉만으로 조기종료 안 함)."""
        try:
            return bool(self.app.settings.get("realtime_warmup_fetch_calendar_history", True))
        except Exception:
            return True

    def _quick_then_deep_background_enabled(self) -> bool:
        """
        True: 메인 워밍업은 최소 봉·당일 갭만 빠르게 채우고,
        캘린더 1년·깊이 보강은 백그라운드 큐에서 종목별 순차 처리.
        """
        try:
            return bool(
                self.app.settings.get("realtime_warmup_quick_then_deep_background", True)
            )
        except Exception:
            return True

    def _calendar_depth_satisfied(
        self, merged: pd.DataFrame, retention_days: int
    ) -> bool:
        """
        가장 오래된 15분봉 시각이 (지금 - retention_days) 이전/동일 구간이면 과거 깊이 충족.
        """
        try:
            if merged is None or merged.empty:
                return False
            if not isinstance(merged.index, pd.DatetimeIndex):
                return False
            oldest = pd.Timestamp(merged.index.min())
            cutoff = pd.Timestamp.now() - timedelta(days=int(retention_days))
            return bool(oldest <= cutoff)
        except Exception:
            return False

    def _bars_max_keep(self) -> int:
        try:
            return max(60, int(float(self.app.settings.get("realtime_bars_max_keep", 300))))
        except (TypeError, ValueError):
            return 300

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
            n = int(store.bulk_upsert_ohlcv_index_frame(code, base))
            if n > 0:
                try:
                    rd = int(
                        float(
                            self.app.settings.get(
                                "realtime_15m_calendar_retention_days", 365
                            )
                        )
                    )
                except (TypeError, ValueError):
                    rd = 365
                self.log(
                    f"💾 [Warmup-RT15mDB] {code} {n}봉 영속화 "
                    f"(캘린더 보존 약 {rd}일·롤링, realtime_15m_*.db)"
                )
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
        self, pages: int, max_pages: int, deep_fetch: bool
    ) -> None:
        """연속조회 중 트리·상태줄이 멈춘 것처럼 보이지 않도록 주기적 갱신."""
        ctx = getattr(self, "_warmup_prep_ui", None)
        if not ctx:
            return
        try:
            tag = "캘린더·깊이" if deep_fetch else "보충"
            msg = f"분봉 API {pages}/{max_pages}페이지 ({tag})"
            self._notify_stock_prep(
                "prep_progress",
                int(ctx["cur"]),
                int(ctx["total"]),
                str(ctx["name"]),
                str(ctx["code"]),
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
    ) -> List[Any]:
        """
        워밍업 전용: ka10080 연속조회로 분봉을 모은 뒤 15m 병합 기준을 만족할 때까지 페이지를 이어 받음.
        deep_fetch=False: 신호 최소 봉·장중 당일 갭 해소 시 조기 종료.
        deep_fetch=True: 캘린더 retention_days 만큼 과거까지 확보되고 당일 갭도 없을 때까지 조회(상한 페이지 별도).
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
            dmin = float(
                self.app.settings.get("realtime_warmup_ka10080_page_delay_min", 0.2)
            )
            dmax = float(
                self.app.settings.get("realtime_warmup_ka10080_page_delay_max", 0.55)
            )
        except (TypeError, ValueError):
            dmin, dmax = 0.2, 0.55
        if dmax < dmin:
            dmin, dmax = dmax, dmin

        by_tm: Dict[str, Any] = {}
        nk: str = ""
        pages = 0
        early_stop = False

        for _ in range(max_pages):
            if not self.is_running:
                self.log(f"ℹ️ [Warmup-API] {name}({code}) 연속조회 중단(워밍업 stop)")
                break
            try:
                raw_list, nk = self.data_manager.fetch_kiwoom_minute_chart_page(
                    code, token, nk
                )
            except Exception as e:
                self.log(f"⚠️ [Warmup-API] {name}({code}) 페이지 조회 예외: {e}")
                break

            pages += 1
            if pages == 1 or (pages % 15 == 0):
                self._notify_ka10080_page_progress(pages, max_pages, deep_fetch)
            if pages == 1 or (pages % 50 == 0):
                self.log(
                    f"⏳ [Warmup-API] {name}({code}) ka10080 연속조회 {pages}/{max_pages}페이지…"
                )
            if raw_list is None:
                self.log(
                    f"⚠️ [Warmup-API] {name}({code}) 페이지 {pages} 응답 없음(오류)"
                )
                break
            if not raw_list:
                if not nk:
                    break
                continue

            for item in raw_list:
                k = self._api_minute_row_key(item)
                if k:
                    by_tm[k] = item

            try:
                api15 = self._minute_api_list_to_15m_ohlcv(list(by_tm.values()))
                merged = self._merge_15m_ohlcv(base, api15)
                n_merged = (
                    len(merged) if merged is not None and not merged.empty else 0
                )
                if deep_fetch:
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

            nk = (nk or "").strip()
            if not nk:
                break
            if not self.is_running:
                break
            time.sleep(random.uniform(dmin, dmax))

        if by_tm:
            self.log(
                f"📡 [Warmup-API] {name}({code}) ka10080 {pages}페이지, "
                f"유일 분봉 {len(by_tm)}건"
                + (" (조기 종료)" if early_stop else "")
                + (" (캘린더·깊이)" if deep_fetch else "")
            )
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

    def _minute_api_list_to_15m_ohlcv(self, raw_api: Any) -> pd.DataFrame:
        """
        키움 ka10080 분봉 응답 리스트 → 15분 OHLCV (closed='right', label='right').
        data_manager.parse_to_bars / fetch_and_parse_15m_bars 와 동일 리샘플 규칙.
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
            df["datetime"] = pd.to_datetime(df[f_tm].astype(str), format="%Y%m%d%H%M%S", errors="coerce")
            df = df.dropna(subset=["datetime"])
            if df.empty:
                return pd.DataFrame()
            df = df.set_index("datetime").sort_index()
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
                need_depth = deep and not self._calendar_depth_satisfied(base, rd)
                use_deep_fetch = deep
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
            )
            api15 = self._minute_api_list_to_15m_ohlcv(raw_api)
            if api15.empty:
                if gap or short:
                    self.log(
                        f"⚠️ [Warmup-API] {name}({code}) 보충 스킵(응답없음) [{','.join(reason)}]"
                    )
                return base
            merged = self._merge_15m_ohlcv(base, api15)
            self.log(
                f"📡 [Warmup] {name}({code}) API 15m 보충 [{','.join(reason)}] "
                f"로컬 {len(base) if base is not None and not base.empty else 0} → 합계 {len(merged)}봉 (신호기준≥{need})"
            )
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
            self.log(f"ℹ️ [Warmup] {code}: bars_15m 비어 1m→15m 리샘플 ({len(out)}봉)")
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
    ) -> Tuple[bool, str, int]:
        """
        한 종목 워밍업 1회 시도.
        성공 시 bars_data 반영 및 on_stock_ready 호출.
        반환: (성공 여부, 실패 사유 또는 빈 문자열, 검증 시점 15m 봉 수 또는 성공 시 스트림 봉 수)
        """
        try:
            qdeep = self._quick_then_deep_background_enabled()
            want_cal = self._warmup_fetch_calendar_history_enabled()
            use_quick = bool(qdeep and want_cal)

            base = self._load_15m_ohlcv_frame(code)
            base = self._maybe_api_backfill_15m(
                code, name, base, token, quick_only=use_quick
            )
            time.sleep(random.uniform(0.25, 0.75))

            ok, reason = self._validate_warmup_base(base)
            nbar = len(base) if base is not None and not base.empty else 0
            if not ok:
                try:
                    bars_data.pop(code, None)
                except Exception:
                    pass
                return False, reason, nbar

            self._persist_warmup_base_to_rt15m_db(code, base)

            df_ind = TradingStrategy.calculate_indicators(base, self.app.settings)
            fin = self._finalize_bars_for_stream(df_ind)
            lk = getattr(self.app, "_bars_data_lock", None)
            cm = lk if lk is not None else contextlib.nullcontext()
            with cm:
                bars_data[code] = fin
            n_ok = len(bars_data[code]) if bars_data.get(code) is not None else 0
            if on_stock_ready is not None:
                try:
                    on_stock_ready(code)
                except Exception as cb_e:
                    self.log(f"⚠️ [Warmup] on_stock_ready 콜백 오류 ({code}): {cb_e}")
            if use_quick:
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
            if not self._quick_then_deep_background_enabled():
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
            df_ind = TradingStrategy.calculate_indicators(combined, self.app.settings)
            fin = self._finalize_bars_for_stream(df_ind)
            if not isinstance(self.app.bars_data, dict):
                self.app.bars_data = {}
            self.app.bars_data[code] = fin

    def _run_background_deep_history(self, code: str, name: str) -> None:
        """캘린더 깊이 ka10080 보강 → RT DB 영속화 → bars_data 병합 갱신."""
        self.log(f"📥 [Warmup-DeepBG] {name}({code}) 과거·깊이 보강 시작(백그라운드)…")
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
            code, name, base, token, quick_only=False
        )
        if merged is None or merged.empty:
            self.log(f"⚠️ [Warmup-DeepBG] {name}({code}) 병합 결과 없음")
            return
        self._persist_warmup_base_to_rt15m_db(code, merged)
        self._apply_deep_history_to_bars_data(code, merged)
        self.log(
            f"✅ [Warmup-DeepBG] {name}({code}) 깊이 보강·DB·메모리 반영 완료 ({len(merged)}봉)"
        )

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
        total = len(monitor_codes)
        retry_max = self._warmup_retry_max()
        retry_delay = self._warmup_retry_delay_sec()
        max_attempts = max(1, 1 + retry_max)

        _src = self._warmup_ohlcv_source()
        self.log(
            f"📦 [Warmup] {total}개 종목 15분봉 기준 검증·적재 중… "
            f"(OHLCV출처={_src}, 실패 시 최대 {retry_max}회 재시도, 간격 {retry_delay:.0f}초)"
        )
        if _src == "api_only":
            self.log(
                "ℹ️ [Warmup] OHLCV 는 DB(bars_15m/1m)를 사용하지 않습니다 — "
                "ka10080 연속조회만 신호용 워밍업에 사용합니다."
            )
        elif _src == "realtime_15m_db_then_api":
            self.log(
                "ℹ️ [Warmup] OHLCV 는 실시간 전용 DB(realtime_15m_*.db) 우선 — "
                "부족 시 ka10080 보충 (하베스팅 trading_data.db 미사용). "
                "검증 통과 시 병합 봉을 동일 DB에 영속화합니다."
            )
        if self._warmup_fetch_calendar_history_enabled():
            try:
                mp = int(
                    float(
                        self.app.settings.get(
                            "realtime_warmup_ka10080_max_pages_deep", 500
                        )
                    )
                )
            except (TypeError, ValueError):
                mp = 500
            self.log(
                f"ℹ️ [Warmup] ka10080 은 캘린더 약 {self._warmup_retention_calendar_days()}일 "
                f"과거(가장 오래된 봉 기준)까지 확보 후 조기 종료합니다. "
                f"연속조회 상한 약 {mp}페이지/종목(API·키움 응답 한도에 따라 더 짧을 수 있음). "
                f"끄려면 realtime_warmup_fetch_calendar_history=false"
            )
        if self._quick_then_deep_background_enabled() and self._warmup_fetch_calendar_history_enabled():
            self.log(
                "ℹ️ [Warmup] 메인 워밍업은 최소 봉·당일 갭만 빠르게 채웁니다. "
                "캘린더·깊이(1년) 보강은 백그라운드에서 종목별 순차 실행됩니다. "
                "끄려면 realtime_warmup_quick_then_deep_background=false"
            )
        for i, code in enumerate(monitor_codes):
            if not self.is_running:
                break
            name = monitor_info.get(code, {}).get("name", "Unknown")
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
                    token = self.auth_manager.get_auth_token()
                    ok, reason, nbar = self._execute_warmup_one_code(
                        code, name, token, bars_data, on_stock_ready
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
                        else:
                            self.log(
                                f"✅ [Warmup] {name}({code}) 준비 완료 (bars={nbar})"
                            )
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
        return results

    def stop(self):
        """워밍업 중단"""
        self.is_running = False
