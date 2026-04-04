# -*- coding: utf-8 -*-

import random
import time
import pandas as pd
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from backtest_engine import TradingStrategy


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
        # (event, cur, total, name, code, reason) event: prep_start | prep_ok | prep_fail
        self.stock_prep_cb = stock_prep_callback

        self.is_running = False

    def _warmup_query_limit(self) -> int:
        try:
            return max(200, int(float(self.app.settings.get("realtime_warmup_15m_query_limit", 2500))))
        except (TypeError, ValueError):
            return 2500

    def _bars_max_keep(self) -> int:
        try:
            return max(60, int(float(self.app.settings.get("realtime_bars_max_keep", 300))))
        except (TypeError, ValueError):
            return 300

    def _min_bars_for_signal(self) -> int:
        try:
            return max(1, int(float(self.app.settings.get("min_15m_bars_for_signal", 60))))
        except (TypeError, ValueError):
            return 60

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
        이 경우 봉 개수가 60을 넘더라도 API 로 당일 구간을 메워야 한다.
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
        self, code: str, name: str, base: pd.DataFrame, token: Optional[str]
    ) -> pd.DataFrame:
        """
        (1) 15m 봉 수 < 신호 기준 이거나
        (2) 장중인데 당일·최근 15분 구간이 DB 에 비어 있는 경우
        → 키움 분봉 API 로 보충 후 병합.
        """
        try:
            if not bool(self.app.settings.get("use_api_warmup_backfill", True)):
                return base
            if not token:
                return base
            need = self._min_bars_for_signal()
            short = base.empty or len(base) < need
            gap = self._needs_intraday_api_backfill(base)
            if not short and not gap:
                return base
            reason = []
            if short:
                reason.append(f"봉수<{need}")
            if gap:
                reason.append("장중당일갭")
            raw_api = self.data_manager.fetch_kiwoom_minute_data(code, token)
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
        DB bars_15m 우선. 비어 있으면 bars_1m 을 15분봉으로 리샘플(동적 백테와 동일 규칙).
        반환: DatetimeIndex, 열 Open/High/Low/Close/Volume
        """
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
    ) -> Dict[str, Dict[str, Any]]:
        """
        DB·API로 15분봉 워밍업 후 종목별 성공 여부 dict 반환.
        (1분 볼륨 프로파일·배경 API 동기화는 신호 경로와 무관하여 제거됨.)
        """
        results: Dict[str, Dict[str, Any]] = {}
        self.is_running = True
        total = len(monitor_codes)
        token = self.auth_manager.get_auth_token()

        self.log(f"📦 [Warmup] {total}개 종목 15분봉 기준 검증·적재 중...")
        for i, code in enumerate(monitor_codes):
            if not self.is_running:
                break
            name = monitor_info.get(code, {}).get("name", "Unknown")
            self._notify_stock_prep("prep_start", i + 1, total, name, code, "")

            try:
                base = self._load_15m_ohlcv_frame(code)
                base = self._maybe_api_backfill_15m(code, name, base, token)
                time.sleep(random.uniform(0.25, 0.75))

                ok, reason = self._validate_warmup_base(base)
                nbar = len(base) if base is not None and not base.empty else 0
                if not ok:
                    bars_data.pop(code, None)
                    results[code] = {"ok": False, "reason": reason, "bars": nbar}
                    self.log(f"❌ [Warmup] {name}({code}) 검증 실패: {reason}")
                    self._notify_stock_prep("prep_fail", i + 1, total, name, code, reason)
                    continue

                df_ind = TradingStrategy.calculate_indicators(base, self.app.settings)
                bars_data[code] = self._finalize_bars_for_stream(df_ind)
                results[code] = {
                    "ok": True,
                    "reason": "",
                    "bars": len(bars_data[code]),
                }

                self._notify_stock_prep("prep_ok", i + 1, total, name, code, "")
            except Exception as e:
                bars_data.pop(code, None)
                err = str(e)
                results[code] = {"ok": False, "reason": err, "bars": 0}
                self.log(f"⚠️ [Warmup] {name}({code}) 처리 실패: {e}")
                self._notify_stock_prep("prep_fail", i + 1, total, name, code, err)

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
