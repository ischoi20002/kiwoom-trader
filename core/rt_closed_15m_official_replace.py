# -*- coding: utf-8 -*-
"""
15분 슬롯이 바뀔 때 확정된 직전 봉을 키움 ka10080(15분)으로 조회해
메모리 ``bars_data`` 및 RT15m DB에 덮어씁니다(틱 누락 시 OHLCV 정합성).

- 완성 봉을 갱신(형성 중 현재 봉은 틱 경로 유지).
- 같은 ka10080 페이지의 이전 완성봉도 값이 다르면 덮어 MA60 정합을 회복.
- 종목·슬롯당 스케줄 1회(중복 타이머 방지).
- 조회·``bars_data`` 반영은 데몬 워커. Tk 메인에 pandas 롤링을 올리지 않습니다.

M1 평가 경로(정렬용 교체): 슬롯 완성 후 고정 지연(~120초) 뒤 **슬롯 단위 배치**로
전 감시 종목을 짧은 REST 간격으로 조회·OHLCV/MA 반영합니다.
종목별 quiet/평가큐 양보(최대 45초)로 1분 간격이 나던 경로를 쓰지 않습니다.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.ka10080_spread import (
    effective_m1_poll_spread_sec,
    effective_rt15m_stagger_spread_sec,
    _active_stream_code_count,
)
from utils.krx_bars import is_krx_15m_slot_end
from utils.normalization import canonical_stock_code

# 한 슬롯의 공식 OHLCV
OfficialOhlcv = Tuple[float, float, float, float, float]
# (봉시각, O, H, L, C, V)
OfficialBarRow = Tuple[pd.Timestamp, float, float, float, float, float]


@dataclass
class Official15mFetchHit:
    """현재 슬롯 공식봉 + 같은 응답의 이전 완성봉(형성 중 봉 제외)."""

    ohlcv: OfficialOhlcv
    completed_bars: List[OfficialBarRow] = field(default_factory=list)


def should_skip_pre_first_slot_or_prior_day_official_replace(
    bar_ts: Any,
    now: Any = None,
) -> bool:
    """
    장시작~당일 첫 15분 완성 전, 또는 전일 슬롯의 공식 15분 정렬교체를 스킵.

    - ``bar_ts`` 캘린더일이 wall 기준 오늘보다 이전이면 True
    - 당일이어도 wall < 오늘 개장+15분 이면 True (첫 15분봉 완성 전)
    """
    try:
        bt = pd.Timestamp(bar_ts).floor("s")
    except Exception:
        return False
    try:
        wall = (
            pd.Timestamp(now).floor("s")
            if now is not None
            else pd.Timestamp.now().floor("s")
        )
    except Exception:
        return False
    try:
        day0 = wall.normalize()
        if bt.normalize() < day0:
            return True
        from utils.krx_session_hours import get_session_hours

        h = get_session_hours(day0.date())
        first_slot_done = day0 + pd.Timedelta(minutes=int(h.open_minutes) + 15)
        if wall < first_slot_done:
            return True
    except Exception:
        return False
    return False


def _settings_bool(settings: Any, key: str, default: bool = True) -> bool:
    try:
        if settings is None:
            return default
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def m1_api_poller_enabled(settings: Any) -> bool:
    """감시 종목 매분 ka10080(1분) 폴러 사용 여부."""
    return _settings_bool(settings, "realtime_watchlist_m1_api_poller_enabled", True)


def official_replace_alignment_mode(settings: Any) -> bool:
    """
    M1로 평가하고 공식 15분은 정합 교체만 하는 모드.

    이 모드에서는 슬롯 직후 폴링 대신 고정 지연 후 단발 조회합니다.
    """
    return m1_api_poller_enabled(settings) and (
        not closed_15m_eval_after_official_enabled(settings)
    )


def closed_15m_eval_after_official_enabled(settings: Any) -> bool:
    """
    저정밀 경로: 공식 15분봉 교체 성공 후에만 S1·S2·청산 평가.

    ``realtime_s1_eval_on_local_15m_close`` 가 꺼져 있으면 False.
    """
    try:
        if not _settings_bool(settings, "realtime_s1_eval_on_local_15m_close", True):
            return False
        return _settings_bool(
            settings, "realtime_closed_15m_eval_after_official", False
        )
    except Exception:
        return False


def official_replace_priority_score(app: Any, code: str) -> float:
    """
    공식 15분 교체 우선순위(0~1, 높을수록 먼저).

    - 보유(청산·S2 필요): 1.0
    - 미보유: ``monitor_info.met_ratio`` (신호 완성도에 근접할수록 높음)
    """
    try:
        nk = canonical_stock_code(str(code).strip())
    except Exception:
        nk = str(code or "").strip()
    if not nk:
        return 0.0

    # 보유 종목 최우선
    try:
        pf = getattr(app, "portfolio", None)
        vp = getattr(pf, "virtual_positions", None) if pf is not None else None
        if not isinstance(vp, dict):
            vp = getattr(app, "virtual_positions", None) or {}
        if isinstance(vp, dict):
            for k in list(vp.keys()):
                try:
                    if canonical_stock_code(str(k).strip()) == nk:
                        return 1.0
                except Exception:
                    if str(k).zfill(6)[-6:] == nk:
                        return 1.0
    except Exception:
        pass

    met = 0.0
    try:
        mi = getattr(app, "monitor_info", None)
        if isinstance(mi, dict):
            info = None
            for k in (nk, str(code), code):
                if k in mi and isinstance(mi.get(k), dict):
                    info = mi[k]
                    break
            if info is not None:
                met = float(info.get("met_ratio", 0.0) or 0.0)
    except Exception:
        met = 0.0
    if met < 0.0:
        met = 0.0
    if met > 1.0:
        met = 1.0
    return float(met)


def official_replace_eval_eligible(
    app: Any,
    code: str,
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    완성봉 평가·우선 조회 대상 여부.

    - 보유: 항상 True (청산·S2)
    - 미보유: ``met_ratio >= realtime_closed_15m_official_min_met_ratio``
    """
    try:
        st = settings if settings is not None else (
            getattr(app, "settings", None) or {}
        )
    except Exception:
        st = {}
    try:
        score = float(official_replace_priority_score(app, code))
    except Exception:
        score = 0.0
    # 보유(1.0)는 항상
    if score >= 0.999:
        return True
    try:
        min_met = float(
            st.get("realtime_closed_15m_official_min_met_ratio", 0.5)
        )
    except (TypeError, ValueError):
        min_met = 0.5
    min_met = max(0.0, min(min_met, 1.0))
    return score >= min_met


def compute_rt15m_official_replace_delay_sec(
    app: Any,
    code: str,
    bar_ts: Any,
    *,
    fallback_delay_sec: float = 2.5,
) -> float:
    """
    15분 확정 ka10080 조회 지연(초).

    - M1 폴러 OFF 또는 공식교체 후 평가 모드: M1 가드 없이
      ``min_delay + (1 - priority) * priority_spread`` (신호 근접·보유 우선).
    - M1 폴러 ON(레거시): ``m1_spread + guard`` 이후 hash stagger.
    """
    try:
        settings = getattr(app, "settings", None) or {}
    except Exception:
        settings = {}

    use_priority = (not m1_api_poller_enabled(settings)) or closed_15m_eval_after_official_enabled(
        settings
    )

    # 정렬용(M1 평가): 지연은 워커의 슬롯+고정대기에서 처리
    if official_replace_alignment_mode(settings):
        return 0.0

    if use_priority:
        try:
            min_d = float(
                settings.get(
                    "realtime_closed_15m_official_replace_min_delay_sec", 0.5
                )
            )
        except (TypeError, ValueError):
            min_d = 0.5
        min_d = max(0.0, min(min_d, 30.0))
        if min_d <= 0.0:
            try:
                min_d = max(0.3, float(fallback_delay_sec))
            except (TypeError, ValueError):
                min_d = 0.5

        try:
            n_codes = _active_stream_code_count(app)
        except Exception:
            n_codes = 1
        try:
            per = float(
                settings.get(
                    "realtime_closed_15m_priority_spacing_sec", 0.45
                )
            )
        except (TypeError, ValueError):
            per = 0.45
        per = max(0.15, min(per, 3.0))
        # 최하위 우선순위가 전역 페이싱(0.5s×N)을 크게 넘지 않도록 캡
        try:
            gmin = float(
                settings.get("realtime_ka10080_global_min_interval_sec", 1.05)
            )
        except (TypeError, ValueError):
            gmin = 1.05
        spread = max(float(n_codes) * per, float(n_codes) * max(0.8, gmin))
        spread = max(2.0, min(spread, 45.0))

        try:
            score = float(official_replace_priority_score(app, code))
        except Exception:
            score = 0.0
        score = max(0.0, min(1.0, score))
        # 동일 점수 미세 분산(해시) — 동시 폭주 완화
        try:
            nk = canonical_stock_code(str(code).strip())
            sk = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
            h = abs(hash((nk, sk)))
            jitter = ((h % 1000) / 1000.0) * min(0.35, per)
        except Exception:
            jitter = 0.0
        return float(min_d + (1.0 - score) * spread + jitter)

    # ---- 레거시: M1 폴러와 경쟁 회피 ----
    if not _settings_bool(
        settings, "realtime_closed_15m_official_replace_stagger", True
    ):
        try:
            return max(0.5, float(fallback_delay_sec))
        except (TypeError, ValueError):
            return 2.5

    try:
        n_codes = _active_stream_code_count(app)
    except Exception:
        n_codes = 1

    m1_spread = effective_m1_poll_spread_sec(settings, n_codes)

    try:
        guard = float(settings.get("realtime_m1_ka10080_guard_sec", 12.0))
    except (TypeError, ValueError):
        guard = 12.0
    guard = max(0.0, min(guard, 120.0))

    try:
        min_d = float(
            settings.get("realtime_closed_15m_official_replace_min_delay_sec", 0.0)
        )
    except (TypeError, ValueError):
        min_d = 0.0
    floor_d = m1_spread + guard
    if min_d <= 0.0:
        min_d = floor_d
    else:
        min_d = max(min_d, floor_d)

    spread = effective_rt15m_stagger_spread_sec(settings, n_codes)

    try:
        nk = canonical_stock_code(str(code).strip())
        sk = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        h = abs(hash((nk, sk)))
        frac = (h % 1_000_003) / 1_000_003.0
    except Exception:
        frac = 0.0
    return min_d + spread * frac


def ts_to_slot_hhmm(ts: Any) -> str:
    """완성 15분봉 인덱스(봉 종가 시각) → ``time`` 컬럼용 HHMM."""
    try:
        t = pd.Timestamp(ts)
        return f"{t.hour:02d}{t.minute:02d}"
    except Exception:
        return "0000"


def _minute_api_raw_to_15m_ohlcv_df(raw_api: Any) -> pd.DataFrame:
    """
    ka10080 응답 리스트 → 15분봉 OHLCV DataFrame (tic_scope=15 전제).
    ``WarmupService._minute_api_list_to_15m_ohlcv`` 와 동일 규약.
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
    except Exception:
        return pd.DataFrame()


def _norm_bar_ts(ts: Any) -> pd.Timestamp:
    try:
        return pd.Timestamp(ts).floor("s")
    except Exception:
        return pd.Timestamp(ts)


def _elapsed_sec_since_slot(bar_ts: Any) -> float:
    """벽시계 기준 슬롯 **완성** 시각 이후 경과 초(좌측 라벨 → complete_ts)."""
    try:
        from datetime import datetime

        from utils.krx_bars import krx_15m_slot_complete_ts

        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        return max(0.0, (datetime.now() - slot).total_seconds())
    except Exception:
        return 0.0


def _monotonic_deadline_for_slot(bar_ts: Any, max_wait_sec: float) -> float:
    """
    슬롯 **완성** 시각 + max_wait 까지의 남은 시간으로 monotonic deadline 산출.

    늦게 시작된 백필도 프로브와 같은 「슬롯 완성 후 N초」 창을 공유합니다.
    이미 창을 지났으면 최소 5초만 더 시도합니다.
    """
    try:
        from datetime import datetime, timedelta

        from utils.krx_bars import krx_15m_slot_complete_ts

        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        remain = (slot + timedelta(seconds=float(max_wait_sec)) - datetime.now()).total_seconds()
        remain = max(5.0, float(remain))
        return time.monotonic() + remain
    except Exception:
        return time.monotonic() + max(5.0, float(max_wait_sec))


def _alignment_fixed_delay_sec(settings: Any) -> float:
    """정렬용 공식15 교체: 슬롯 완성 후 대기 초(기본 120)."""
    try:
        v = float(
            settings.get("realtime_closed_15m_official_replace_fixed_delay_sec", 120.0)
        )
    except (TypeError, ValueError):
        v = 120.0
    return max(60.0, min(v, 300.0))


def _alignment_retry_params(settings: Any) -> Tuple[int, float]:
    """고정 지연 후 조회 횟수·재시도 간격."""
    try:
        n = int(
            float(
                settings.get(
                    "realtime_closed_15m_official_replace_retry_attempts", 2
                )
            )
        )
    except (TypeError, ValueError):
        n = 2
    n = max(1, min(n, 4))
    try:
        gap = float(
            settings.get(
                "realtime_closed_15m_official_replace_retry_gap_sec", 15.0
            )
        )
    except (TypeError, ValueError):
        gap = 15.0
    gap = max(3.0, min(gap, 60.0))
    return n, gap


def _alignment_code_stagger_sec(app: Any, code: str, bar_ts: Any, settings: Any) -> float:
    """종목별 미세 분산(동시 REST 폭주 완화, 상한 기본 45초)."""
    try:
        cap = float(
            settings.get(
                "realtime_closed_15m_official_replace_stagger_cap_sec", 45.0
            )
        )
    except (TypeError, ValueError):
        cap = 45.0
    cap = max(0.0, min(cap, 120.0))
    if cap <= 0.0:
        return 0.0
    try:
        n_codes = max(1, int(_active_stream_code_count(app)))
    except Exception:
        n_codes = 1
    # 종목 수에 비례하되 cap 이하
    spread = min(cap, max(5.0, float(n_codes) * 2.0))
    try:
        nk = canonical_stock_code(str(code).strip())
        sk = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        h = abs(hash((nk, sk, "align15")))
        frac = (h % 1_000_003) / 1_000_003.0
    except Exception:
        frac = 0.0
    return float(spread * frac)


def _wait_official_quiet_chunks(app: Any, *, chunk_sec: float = 1.5) -> None:
    """공식 조용 구간이 끝날 때까지 짧게 나눠 대기(필수 작업 양보)."""
    try:
        from core.closed_15m_fast_path import rt15_official_quiet_remaining_sec
    except Exception:
        return
    for _ in range(120):
        try:
            rem = float(rt15_official_quiet_remaining_sec(app))
        except Exception:
            rem = 0.0
        if rem <= 0.05:
            return
        try:
            time.sleep(min(float(chunk_sec), rem))
        except Exception:
            return


def wait_for_alignment_replace_window(
    app: Any,
    code: str,
    bar_ts: Any,
    settings: Any,
) -> None:
    """
    슬롯 완성 + 고정지연(+종목 stagger)까지 대기.

    대기 중 공식 조용 구간이 켜지면 그 시간을 존중해 필수 M1/평가 REST 에 양보합니다.
    (단종목 폴백·비배치 경로용. 정렬 배치는 ``wait_for_alignment_batch_window``.)
    """
    from datetime import datetime, timedelta

    from utils.krx_bars import krx_15m_slot_complete_ts

    fixed = _alignment_fixed_delay_sec(settings)
    stag = _alignment_code_stagger_sec(app, code, bar_ts, settings)
    try:
        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        target = slot + timedelta(seconds=float(fixed) + float(stag))
    except Exception:
        # 폴백: 지금부터 fixed 초
        try:
            time.sleep(float(fixed))
        except Exception:
            pass
        _wait_official_quiet_chunks(app)
        return

    while True:
        _wait_official_quiet_chunks(app)
        try:
            left = (target - datetime.now()).total_seconds()
        except Exception:
            left = 0.0
        if left <= 0.05:
            break
        try:
            time.sleep(min(2.0, left))
        except Exception:
            break
    _wait_official_quiet_chunks(app)


def wait_for_alignment_batch_window(app: Any, bar_ts: Any, settings: Any) -> None:
    """
    정렬 배치: 슬롯 완성 + 고정지연만 대기(종목 stagger·quiet 연장 없음).

    quiet/평가큐를 존중하느라 목표 시각을 계속 미루면 종목마다 1분씩 벌어지므로,
    wall-clock 기준으로만 기다린 뒤 짧은 quiet 양보(최대 5초)만 합니다.
    """
    from datetime import datetime, timedelta

    from utils.krx_bars import krx_15m_slot_complete_ts

    fixed = _alignment_fixed_delay_sec(settings)
    try:
        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        target = slot + timedelta(seconds=float(fixed))
    except Exception:
        try:
            time.sleep(float(fixed))
        except Exception:
            pass
        return

    while True:
        try:
            left = (target - datetime.now()).total_seconds()
        except Exception:
            left = 0.0
        if left <= 0.05:
            break
        try:
            time.sleep(min(2.0, left))
        except Exception:
            break

    # 틱말분 평가가 막 돌아가면 짧게만 양보(배치 전체를 45초씩 밀지 않음)
    try:
        from core.closed_15m_fast_path import rt15_official_quiet_remaining_sec

        rem = float(rt15_official_quiet_remaining_sec(app))
        if rem > 0.05:
            time.sleep(min(rem, 5.0))
    except Exception:
        pass


def _alignment_batch_fetch_spacing_sec(settings: Any) -> float:
    """배치 내 종목 간 ka10080 간격(초)."""
    try:
        v = float(
            settings.get(
                "realtime_closed_15m_alignment_batch_spacing_sec",
                settings.get("realtime_closed_15m_backfill_spacing_sec", 0.45),
            )
        )
    except (TypeError, ValueError):
        v = 0.45
    return max(0.2, min(v, 2.0))


def _find_row_for_bar_end(
    merged: pd.DataFrame, bar_ts: Any
) -> Optional[pd.Series]:
    """API로 모은 15m 프레임에서 ``bar_ts``(초 단위)와 일치하는 행."""
    try:
        if merged is None or getattr(merged, "empty", True):
            return None
        want = _norm_bar_ts(bar_ts)
        idx = merged.index
        try:
            loc = idx.get_loc(want)
        except KeyError:
            for i, t in enumerate(idx):
                if _norm_bar_ts(t) == want:
                    return merged.iloc[i]
            return None
        if isinstance(loc, slice):
            row = merged.iloc[loc.start : loc.stop]
            if row.empty:
                return None
            return row.iloc[-1]
        if isinstance(loc, int):
            return merged.iloc[loc]
        sub = merged.loc[merged.index == want]
        if sub.empty:
            return None
        return sub.iloc[-1]
    except Exception:
        return None


def _row_ohlcv(
    row: Any,
) -> Optional[OfficialOhlcv]:
    """OHLCV 행에서 양수 가격이 하나라도 있으면 튜플."""
    try:
        o = float(row["Open"])
        h = float(row["High"])
        l_ = float(row["Low"])
        c = float(row["Close"])
        v = float(row["Volume"])
        if o > 0 or h > 0 or l_ > 0 or c > 0:
            return (o, h, l_, c, v)
    except Exception:
        return None
    return None


def _completed_bars_upto(
    merged: pd.DataFrame,
    want: Any,
    *,
    max_bars: Optional[int] = None,
) -> List[OfficialBarRow]:
    """
    API 프레임에서 ``want`` 이하 완성봉만 추출합니다.

    형성 중(``want`` 이후) 봉은 틱 경로를 유지하기 위해 제외합니다.
    ``max_bars`` 가 있으면 최신 N봉만(정렬교체: 1페이지에 수십~수백 봉이
    와도 MA 정합에 필요한 최근만 반영).
    """
    out: List[OfficialBarRow] = []
    try:
        if merged is None or getattr(merged, "empty", True):
            return out
        want_n = _norm_bar_ts(want)
        for ts, row in merged.iterrows():
            try:
                t = _norm_bar_ts(ts)
                if t > want_n:
                    continue
                oh = _row_ohlcv(row)
                if oh is None:
                    continue
                out.append((t, oh[0], oh[1], oh[2], oh[3], oh[4]))
            except Exception:
                continue
    except Exception:
        return out
    if max_bars is not None:
        try:
            lim = max(1, int(max_bars))
        except (TypeError, ValueError):
            lim = 1
        if len(out) > lim:
            try:
                out = sorted(out, key=lambda r: r[0])[-lim:]
            except Exception:
                out = out[-lim:]
    return out


def fetch_official_15m_hit(
    data_manager: Any,
    code: str,
    token: str,
    bar_ts: Any,
    *,
    tic_scope: str = "15",
    max_pages: int = 8,
    page_delay_min: float = 0.08,
    page_delay_max: float = 0.22,
    max_retries: int = 3,
    inter_retry_sec: float = 0.6,
    heal_max_bars: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
    app: Any = None,
) -> Optional[Official15mFetchHit]:
    """
    ka10080(15분) 연속조회로 ``bar_ts`` 완성 봉과, 같은 응답의 이전 완성봉을 반환합니다.

    ``heal_max_bars``: 정렬용으로 이전 봉 개수 상한(None 이면 페이지 내 전체).
    """
    nc = canonical_stock_code(str(code).strip())
    if not nc or not token:
        return None
    want = _norm_bar_ts(bar_ts)
    mp = max(1, min(int(max_pages), 40))
    retries = max(1, min(int(max_retries), 8))
    try:
        heal_lim = (
            None
            if heal_max_bars is None
            else max(1, min(int(heal_max_bars), 64))
        )
    except (TypeError, ValueError):
        heal_lim = None

    for attempt in range(retries):
        try:
            merged_rows: List[pd.DataFrame] = []
            nk = ""
            for _page in range(mp):
                raw_list, nk_out = data_manager.fetch_kiwoom_minute_chart_page(
                    nc,
                    token,
                    nk,
                    tic_scope=str(tic_scope).strip() or "15",
                    fetch_context="rt15m_official",
                )
                if not raw_list:
                    break
                chunk = _minute_api_raw_to_15m_ohlcv_df(raw_list)
                if chunk is not None and not chunk.empty:
                    merged_rows.append(chunk)
                nk = (nk_out or "").strip()
                if not nk:
                    break
                try:
                    lo = float(page_delay_min)
                    hi = float(page_delay_max)
                except (TypeError, ValueError):
                    lo, hi = 0.08, 0.22
                hi = max(lo, hi)
                time.sleep(random.uniform(max(0.0, lo), hi))

            if not merged_rows:
                if attempt + 1 >= retries:
                    try:
                        meta = getattr(data_manager, "last_ka10080_fetch_meta", None) or {}
                        if bool(meta.get("ok")):
                            from core.ka10080_fetch_diag import ka10080_fetch_diag

                            ka10080_fetch_diag.record_fail(
                                nc,
                                tic_scope=str(tic_scope or "15"),
                                context="rt15m_official",
                                reason="merged_empty",
                                http_status=int(meta.get("http_status") or 0),
                                detail=f"no_15m_rows bar={want}",
                                attempt=attempt + 1,
                            )
                    except Exception:
                        pass
                break
            merged = pd.concat(merged_rows).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            row = _find_row_for_bar_end(merged, want)
            if row is None:
                if attempt + 1 >= retries:
                    halt_slot = False
                    try:
                        if app is not None:
                            from core.krx_market_halt_slots import (
                                is_market_halt_15m_slot,
                                record_rt15m_official_miss_and_maybe_halt,
                            )

                            halt_slot = (
                                is_market_halt_15m_slot(app, want)
                                or record_rt15m_official_miss_and_maybe_halt(
                                    app, want, nc
                                )
                            )
                    except Exception:
                        halt_slot = False
                    if not halt_slot:
                        try:
                            from core.ka10080_fetch_diag import ka10080_fetch_diag

                            ka10080_fetch_diag.record_fail(
                                nc,
                                tic_scope=str(tic_scope or "15"),
                                context="rt15m_official",
                                reason="row_not_found",
                                detail=f"bar_ts={want} pages≤{mp}",
                                attempt=attempt + 1,
                            )
                        except Exception:
                            pass
                        if log:
                            try:
                                log(
                                    f"ℹ️ [RT15m-Official] {nc} {want} 행 미발견 "
                                    f"(pages≤{mp}, attempt {attempt + 1}/{retries})"
                                )
                            except Exception:
                                pass
            else:
                oh = _row_ohlcv(row)
                if oh is not None:
                    bars = _completed_bars_upto(merged, want, max_bars=heal_lim)
                    try:
                        if not any(_norm_bar_ts(t) == want for t, *_rest in bars):
                            bars.append((want, oh[0], oh[1], oh[2], oh[3], oh[4]))
                            if heal_lim is not None and len(bars) > heal_lim:
                                bars = sorted(bars, key=lambda r: r[0])[-heal_lim:]
                    except Exception:
                        bars.append((want, oh[0], oh[1], oh[2], oh[3], oh[4]))
                    return Official15mFetchHit(ohlcv=oh, completed_bars=bars)
        except Exception as e:
            if attempt + 1 >= retries:
                try:
                    from core.ka10080_fetch_diag import ka10080_fetch_diag

                    ka10080_fetch_diag.record_fail(
                        nc,
                        tic_scope=str(tic_scope or "15"),
                        context="rt15m_official",
                        reason="exception",
                        detail=repr(e),
                        attempt=attempt + 1,
                    )
                except Exception:
                    pass
            if log:
                try:
                    log(
                        f"⚠️ [RT15m-Official] {nc} 조회 예외 "
                        f"(attempt {attempt + 1}/{retries}): {e!r}"
                    )
                except Exception:
                    pass
        if attempt + 1 < retries:
            try:
                time.sleep(max(0.1, float(inter_retry_sec)))
            except (TypeError, ValueError):
                time.sleep(0.6)
    return None


def fetch_official_15m_row_for_timestamp(
    data_manager: Any,
    code: str,
    token: str,
    bar_ts: Any,
    *,
    tic_scope: str = "15",
    max_pages: int = 8,
    page_delay_min: float = 0.08,
    page_delay_max: float = 0.22,
    max_retries: int = 3,
    inter_retry_sec: float = 0.6,
    log: Optional[Callable[[str], None]] = None,
    app: Any = None,
) -> Optional[OfficialOhlcv]:
    """
    ka10080(15분) 연속조회로 ``bar_ts`` 에 해당하는 완성 봉 한 줄을 찾습니다.

    Returns:
        (open, high, low, close, volume) 또는 실패 시 None.
    """
    try:
        hit = fetch_official_15m_hit(
            data_manager,
            code,
            token,
            bar_ts,
            tic_scope=tic_scope,
            max_pages=max_pages,
            page_delay_min=page_delay_min,
            page_delay_max=page_delay_max,
            max_retries=max_retries,
            inter_retry_sec=inter_retry_sec,
            log=log,
            app=app,
        )
    except Exception:
        return None
    if hit is None:
        return None
    try:
        return hit.ohlcv
    except Exception:
        return None


class RtClosed15mOfficialReplaceScheduler:
    """
    슬롯 확정 콜백에서 ``schedule`` 만 호출합니다.
    HTTP는 지연 후 데몬 스레드에서 수행하고, 결과는
    ``KiwoomApp._apply_official_closed_15m_bars`` 로 전달합니다
    (현재 슬롯 + 같은 응답의 이전 완성봉).

    공식교체 후 평가 모드:
    - 1차: 보유·합치도(임박) 종목만 스케줄
    - 슬롯당 1종(프로브)만 공식봉 등장까지 재시도
    - 프로브 성공 후 나머지 감시 종목 백필 교체·평가(간격 분산, 1회 조회)
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._scheduled: Dict[str, bool] = {}
        # 실패(행 미발견 등) 이후 동일 키를 일정 시간 동안 재시도/로그하지 않기 위한 쿨다운
        self._fail_cooldown_until: Dict[str, float] = {}
        self._fail_counts: Dict[str, int] = {}
        # 슬롯(봉 시각)별 공식봉 준비 게이트 — 프로브 1종만 재시도
        self._slot_gate: Dict[str, Dict[str, Any]] = {}
        # 정렬교체 로그 집계: 슬롯에 예약한 종목이 모두 끝난 뒤 1회
        self._align_log_agg: Dict[str, Dict[str, Any]] = {}
        # 정렬 모드: 슬롯당 1개 배치 워커 (종목별 스레드·1분 간격 방지)
        self._align_batch_codes: Dict[str, set] = {}
        self._align_batch_started: Dict[str, bool] = {}

    def _align_register_pending(self, code: Any, bar_ts: Any) -> None:
        """정렬교체 예약 시 슬롯 pending에 종목 등록."""
        try:
            nk = canonical_stock_code(str(code).strip())
            slot_key = self._slot_ts_key(bar_ts)
        except Exception:
            return
        if not nk:
            return
        try:
            with self._lock:
                st = self._align_log_agg.get(slot_key)
                if st is None:
                    st = {
                        "pending": set(),
                        "ok": 0,
                        "fail": 0,
                        "skip": 0,
                        "flushed": False,
                        "bar_label": str(_norm_bar_ts(bar_ts)),
                        "elapsed": 0.0,
                    }
                    self._align_log_agg[slot_key] = st
                try:
                    st["pending"].add(nk)
                except Exception:
                    pass
                st["flushed"] = False
        except Exception:
            pass

    def _align_batch_offer(self, app: Any, code: Any, bar_ts: Any) -> None:
        """정렬 모드: 슬롯 배치에 종목을 넣고, 미시작이면 배치 워커 1개만 기동."""
        try:
            nk = canonical_stock_code(str(code).strip())
            slot_key = self._slot_ts_key(bar_ts)
        except Exception:
            return
        if not nk:
            return
        start_worker = False
        try:
            with self._lock:
                codes = self._align_batch_codes.get(slot_key)
                if codes is None:
                    codes = set()
                    self._align_batch_codes[slot_key] = codes
                codes.add(nk)
                if not bool(self._align_batch_started.get(slot_key)):
                    self._align_batch_started[slot_key] = True
                    start_worker = True
        except Exception:
            return
        if not start_worker:
            return
        th = threading.Thread(
            target=self._align_batch_worker,
            args=(app, bar_ts),
            name="rt15m-official-align-batch",
            daemon=True,
        )
        th.start()

    def _align_batch_worker(self, app: Any, bar_ts: Any) -> None:
        """
        정렬교체 슬롯 배치: 고정 지연 후 전 종목 조회 → 한 락으로 OHLCV/MA 반영.

        종목마다 wait_out(최대 45초)·quiet 연장·전역 락을 쓰면 1분 간격이 납니다.
        """
        slot_key = ""
        try:
            slot_key = self._slot_ts_key(bar_ts)
        except Exception:
            slot_key = str(bar_ts)

        try:
            settings = getattr(app, "settings", None) or {}
        except Exception:
            settings = {}

        try:
            wait_for_alignment_batch_window(app, bar_ts, settings)
        except Exception:
            pass

        # 슬롯 롤 직후 schedule 폭주가 모두 들어오도록 짧게 대기
        try:
            settle = float(
                settings.get("realtime_closed_15m_alignment_batch_settle_sec", 1.5)
            )
        except (TypeError, ValueError):
            settle = 1.5
        settle = max(0.3, min(settle, 5.0))
        try:
            time.sleep(settle)
        except Exception:
            pass

        codes: List[str] = []
        try:
            with self._lock:
                raw = self._align_batch_codes.get(slot_key) or set()
                codes = sorted(str(c) for c in raw if c)
        except Exception:
            codes = []

        if not codes:
            try:
                with self._lock:
                    self._align_batch_started.pop(slot_key, None)
                    self._align_batch_codes.pop(slot_key, None)
            except Exception:
                pass
            return

        try:
            mon = bool(getattr(app, "is_monitoring", False)) or bool(
                getattr(
                    getattr(app, "trading_controller", None),
                    "is_monitoring",
                    False,
                )
            )
        except Exception:
            mon = False
        if not mon:
            for nk in codes:
                try:
                    k = self._key(nk, bar_ts)
                    with self._lock:
                        self._scheduled.pop(k, None)
                    self._align_finish(nk, bar_ts, ok=None)
                except Exception:
                    pass
            try:
                with self._lock:
                    self._align_batch_started.pop(slot_key, None)
                    self._align_batch_codes.pop(slot_key, None)
            except Exception:
                pass
            return

        token = None
        try:
            token = app.auth_manager.get_auth_token()
        except Exception:
            token = None
        if not token:
            if self._log:
                try:
                    self._log(
                        f"⚠️ [RT15m-Official] {slot_key} 정렬 배치 토큰 없음 — "
                        f"{len(codes)}종 스킵"
                    )
                except Exception:
                    pass
            for nk in codes:
                try:
                    k = self._key(nk, bar_ts)
                    with self._lock:
                        self._scheduled.pop(k, None)
                    self._align_finish(nk, bar_ts, ok=None)
                except Exception:
                    pass
            try:
                with self._lock:
                    self._align_batch_started.pop(slot_key, None)
                    self._align_batch_codes.pop(slot_key, None)
            except Exception:
                pass
            return

        dm = getattr(app, "data_manager", None)
        if dm is None:
            for nk in codes:
                try:
                    k = self._key(nk, bar_ts)
                    with self._lock:
                        self._scheduled.pop(k, None)
                    self._align_finish(nk, bar_ts, ok=False)
                except Exception:
                    pass
            try:
                with self._lock:
                    self._align_batch_started.pop(slot_key, None)
                    self._align_batch_codes.pop(slot_key, None)
            except Exception:
                pass
            return

        try:
            tic = str(
                settings.get("realtime_closed_15m_official_replace_tic", "15")
            ).strip() or "15"
        except Exception:
            tic = "15"
        try:
            mp = int(
                float(settings.get("realtime_closed_15m_official_replace_max_pages", 8))
            )
        except (TypeError, ValueError):
            mp = 8
        mp = max(1, min(mp, 40))
        try:
            dmin = float(
                settings.get(
                    "realtime_closed_15m_official_replace_page_delay_min", 0.08
                )
            )
            dmax = float(
                settings.get(
                    "realtime_closed_15m_official_replace_page_delay_max", 0.22
                )
            )
        except (TypeError, ValueError):
            dmin, dmax = 0.08, 0.22
        try:
            ir = float(
                settings.get(
                    "realtime_closed_15m_official_replace_inter_retry_sec", 0.6
                )
            )
        except (TypeError, ValueError):
            ir = 0.6
        attempts, gap_sec = _alignment_retry_params(settings)
        # 정렬 배치는 최근 봉만 필요 — 페이지·재시도가 길면 완료 로그가 수분 지연됨
        try:
            mp_align = int(
                float(
                    settings.get(
                        "realtime_closed_15m_alignment_batch_max_pages", 1
                    )
                )
            )
        except (TypeError, ValueError):
            mp_align = 1
        mp = max(1, min(mp_align, mp, 3))
        try:
            gap_align = float(
                settings.get(
                    "realtime_closed_15m_alignment_batch_retry_gap_sec", 3.0
                )
            )
        except (TypeError, ValueError):
            gap_align = 3.0
        gap_sec = max(1.0, min(float(gap_align), float(gap_sec), 15.0))
        try:
            attempts_align = int(
                float(
                    settings.get(
                        "realtime_closed_15m_alignment_batch_retry_attempts", 1
                    )
                )
            )
        except (TypeError, ValueError):
            attempts_align = 1
        attempts = max(1, min(int(attempts_align), int(attempts), 2))
        spacing = _alignment_batch_fetch_spacing_sec(settings)

        try:
            heal_max = int(
                float(
                    settings.get(
                        "realtime_closed_15m_alignment_heal_max_bars", 4
                    )
                )
            )
        except (TypeError, ValueError):
            heal_max = 4
        heal_max = max(1, min(heal_max, 16))

        if self._log:
            try:
                el = _elapsed_sec_since_slot(bar_ts)
                self._log(
                    f"ℹ️ [RT15m-Official] {slot_key} 정렬 배치 시작 "
                    f"{len(codes)}종목 (슬롯 후 +{el:.1f}s, "
                    f"pages≤{mp}, retry={attempts})"
                )
            except Exception:
                pass

        # (code, hit|None)
        fetched: List[Tuple[str, Optional[Official15mFetchHit]]] = []
        n_codes = len(codes)
        for i, nk in enumerate(codes):
            if i > 0:
                try:
                    time.sleep(spacing)
                except Exception:
                    pass
            # 진행 로그(막힘 여부 확인용) — 5종마다·마지막
            if self._log and (i == 0 or (i + 1) % 5 == 0 or i + 1 == n_codes):
                try:
                    self._log(
                        f"ℹ️ [RT15m-Official] {slot_key} 정렬 배치 조회 "
                        f"{i + 1}/{n_codes} ({nk})"
                    )
                except Exception:
                    pass
            hit: Optional[Official15mFetchHit] = None
            for ai in range(int(attempts)):
                try:
                    tok = token
                    try:
                        tok2 = app.auth_manager.get_auth_token()
                        if tok2:
                            tok = tok2
                    except Exception:
                        pass
                    hit = fetch_official_15m_hit(
                        dm,
                        nk,
                        tok,
                        bar_ts,
                        tic_scope=tic,
                        max_pages=mp,
                        page_delay_min=dmin,
                        page_delay_max=dmax,
                        max_retries=1,
                        inter_retry_sec=ir,
                        heal_max_bars=heal_max,
                        log=self._log,
                        app=app,
                    )
                except Exception:
                    hit = None
                if hit is not None:
                    break
                if ai + 1 >= int(attempts):
                    break
                try:
                    time.sleep(float(gap_sec))
                except Exception:
                    break
            fetched.append((nk, hit))

        if self._log:
            try:
                ok_f = sum(1 for _n, h in fetched if h is not None)
                self._log(
                    f"ℹ️ [RT15m-Official] {slot_key} 정렬 배치 조회 끝 "
                    f"hit={ok_f}/{len(fetched)} — 메모리 반영"
                )
            except Exception:
                pass

        apply_results: Dict[str, bool] = {}
        apply_t0 = time.perf_counter()

        def _apply_all() -> None:
            for nk, hit in fetched:
                if hit is None:
                    apply_results[nk] = False
                    continue
                try:
                    o, h, l_, c, v = hit.ohlcv
                except Exception:
                    o = h = l_ = c = v = 0.0
                try:
                    heal_bars = list(hit.completed_bars or [])
                except Exception:
                    heal_bars = []
                if not heal_bars:
                    try:
                        heal_bars = [(_norm_bar_ts(bar_ts), o, h, l_, c, v)]
                    except Exception:
                        heal_bars = []
                applied = False
                try:
                    if hasattr(app, "_apply_official_closed_15m_bars"):
                        ret = app._apply_official_closed_15m_bars(  # type: ignore[attr-defined]
                            nk,
                            heal_bars,
                            primary_ts=bar_ts,
                            log_extra_heal=False,
                        )
                        applied = True if ret is None else bool(ret)
                    else:
                        ret = app._apply_official_closed_15m_bar(  # type: ignore[attr-defined]
                            nk, bar_ts, o, h, l_, c, v
                        )
                        applied = True if ret is None else bool(ret)
                except Exception as ex:
                    if self._log:
                        try:
                            self._log(
                                f"⚠️ [RT15m-Official] 배치 반영 실패 ({nk}): {ex!r}"
                            )
                        except Exception:
                            pass
                    applied = False
                apply_results[nk] = bool(applied)

        try:
            from core.rt_ui_responsiveness import run_official_15m_memory_apply

            run_official_15m_memory_apply(_apply_all)
        except Exception:
            try:
                _apply_all()
            except Exception:
                for nk, _hit in fetched:
                    apply_results.setdefault(nk, False)

        try:
            apply_sec = float(time.perf_counter() - apply_t0)
        except Exception:
            apply_sec = 0.0
        if self._log:
            try:
                ok_a = sum(1 for v in apply_results.values() if v)
                self._log(
                    f"ℹ️ [RT15m-Official] {slot_key} 메모리 반영 끝 "
                    f"{ok_a}/{len(fetched)}종 {apply_sec:.2f}s"
                )
            except Exception:
                pass

        for nk, hit in fetched:
            try:
                k = self._key(nk, bar_ts)
                with self._lock:
                    self._scheduled.pop(k, None)
                    if hit is None:
                        cnt = int(self._fail_counts.get(k, 0) or 0) + 1
                        self._fail_counts[k] = cnt
                        delay = min(900.0, 60.0 * (2 ** max(0, cnt - 1)))
                        self._fail_cooldown_until[k] = time.monotonic() + delay
            except Exception:
                pass
            try:
                if hit is None:
                    self._align_finish(nk, bar_ts, ok=False)
                else:
                    self._align_finish(
                        nk, bar_ts, ok=bool(apply_results.get(nk, False))
                    )
            except Exception:
                pass

        # 스냅샷 밖·고아 pending 이 있으면 완료 로그가 영원히 안 나옴 → 강제 스킵
        try:
            leftovers: List[str] = []
            with self._lock:
                st = self._align_log_agg.get(slot_key)
                if st is not None:
                    pending = st.get("pending")
                    if isinstance(pending, set) and pending:
                        leftovers = sorted(str(x) for x in pending if x)
            for nk in leftovers:
                try:
                    k = self._key(nk, bar_ts)
                    with self._lock:
                        self._scheduled.pop(k, None)
                    self._align_finish(nk, bar_ts, ok=None)
                except Exception:
                    continue
        except Exception:
            pass

        try:
            with self._lock:
                self._align_batch_started.pop(slot_key, None)
                self._align_batch_codes.pop(slot_key, None)
        except Exception:
            pass

    def _align_finish(
        self,
        code: Any,
        bar_ts: Any,
        *,
        ok: Optional[bool] = None,
    ) -> None:
        """
        정렬교체 1종 종료.

        ``ok=True`` 성공, ``ok=False`` 실패, ``ok=None`` 스킵(조기 return).
        pending 이 비면 슬롯 전체 완료 로그 1회.
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            slot_key = self._slot_ts_key(bar_ts)
        except Exception:
            return
        if not nk:
            return
        should_flush = False
        try:
            with self._lock:
                st = self._align_log_agg.get(slot_key)
                if st is None:
                    return
                pending = st.get("pending")
                if isinstance(pending, set):
                    pending.discard(nk)
                if ok is True:
                    st["ok"] = int(st.get("ok", 0) or 0) + 1
                elif ok is False:
                    st["fail"] = int(st.get("fail", 0) or 0) + 1
                else:
                    st["skip"] = int(st.get("skip", 0) or 0) + 1
                try:
                    st["elapsed"] = float(_elapsed_sec_since_slot(bar_ts))
                except Exception:
                    pass
                empty = not pending
                flushed = bool(st.get("flushed", False))
                if empty and (not flushed):
                    st["flushed"] = True
                    should_flush = True
        except Exception:
            return
        if should_flush:
            try:
                self._flush_alignment_log(slot_key)
            except Exception:
                pass

    def _flush_alignment_log(self, slot_key: str) -> None:
        """슬롯 정렬교체 전체 완료 로그 1회."""
        st: Optional[Dict[str, Any]] = None
        try:
            with self._lock:
                st = self._align_log_agg.pop(slot_key, None)
        except Exception:
            st = None
        if not st or not self._log:
            return
        try:
            ok_n = int(st.get("ok", 0) or 0)
            fail_n = int(st.get("fail", 0) or 0)
            skip_n = int(st.get("skip", 0) or 0)
            label = str(st.get("bar_label") or slot_key)
            el = float(st.get("elapsed", 0.0) or 0.0)
            total = ok_n + fail_n + skip_n
        except Exception:
            return
        if total <= 0:
            return
        try:
            if fail_n <= 0 and skip_n <= 0:
                self._log(
                    f"✅ [RT15m-Official] {label} 정렬교체 완료 {ok_n}종목 "
                    f"(슬롯 후 +{el:.1f}s)"
                )
            elif ok_n <= 0 and fail_n > 0 and skip_n <= 0:
                self._log(
                    f"ℹ️ [RT15m-Official] {label} 정렬교체 실패 {fail_n}종목 "
                    f"(슬롯 후 {el:.0f}s) — 평가 영향 없음"
                )
            else:
                parts = [f"성공 {ok_n}"]
                if fail_n > 0:
                    parts.append(f"실패 {fail_n}")
                if skip_n > 0:
                    parts.append(f"스킵 {skip_n}")
                self._log(
                    f"✅ [RT15m-Official] {label} 정렬교체 완료 {total}종목 "
                    f"({', '.join(parts)}, 슬롯 후 +{el:.1f}s)"
                )
        except Exception:
            pass

    def _key(self, code: str, bar_ts: Any) -> str:
        nc = canonical_stock_code(str(code).strip())
        try:
            s = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            s = str(bar_ts)
        return f"{nc}|{s}"

    def _slot_ts_key(self, bar_ts: Any) -> str:
        try:
            return _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(bar_ts)

    def _get_slot_gate(self, slot_key: str) -> Dict[str, Any]:
        with self._lock:
            g = self._slot_gate.get(slot_key)
            if g is None:
                g = {
                    "probe_lock": threading.Lock(),
                    "probe_code": None,
                    "ready_event": threading.Event(),
                    "ready": False,
                    "backfill_started": False,
                }
                self._slot_gate[slot_key] = g
            # 오래된 슬롯 정리(최대 ~8개 유지)
            if len(self._slot_gate) > 12:
                try:
                    keys = sorted(self._slot_gate.keys())
                    for old in keys[:-8]:
                        if old != slot_key:
                            self._slot_gate.pop(old, None)
                except Exception:
                    pass
            return g

    def _list_watch_codes(self, app: Any) -> List[str]:
        out: List[str] = []
        seen = set()
        try:
            ac = getattr(app, "_active_stream_codes", None) or []
            for c in ac:
                try:
                    nk = canonical_stock_code(str(c).strip())
                except Exception:
                    nk = str(c or "").strip().zfill(6)[-6:]
                if nk and len(nk) == 6 and nk not in seen:
                    seen.add(nk)
                    out.append(nk)
        except Exception:
            pass
        return out

    def _allow_fallback_probe(
        self, app: Any, code: str, settings: Dict[str, Any]
    ) -> bool:
        """임박·보유가 없을 때, 감시 중 합치도 최고 종목 1만 프로브 허용."""
        try:
            nk = canonical_stock_code(str(code).strip())
        except Exception:
            nk = str(code or "").strip()
        if not nk:
            return False
        best_c = ""
        best_s = -1.0
        for c in self._list_watch_codes(app):
            try:
                sc = float(official_replace_priority_score(app, c))
            except Exception:
                sc = 0.0
            if sc > best_s or (sc == best_s and (not best_c or c < best_c)):
                best_s = sc
                best_c = c
        return bool(best_c) and best_c == nk

    def _enqueue_slot_backfill(
        self,
        app: Any,
        bar_ts: Any,
        *,
        probe_code: str,
        delay_sec: float,
    ) -> None:
        """
        프로브로 공식봉 등장이 확인된 뒤, 나머지 감시 종목도 느리게 교체·평가.

        임박이 아니어도 해당 봉에서 갑자기 Signal 나는 종목을 BT와 맞추기 위함.
        """
        try:
            settings = getattr(app, "settings", None) or {}
        except Exception:
            settings = {}
        if not closed_15m_eval_after_official_enabled(settings):
            return
        try:
            if not _settings_bool(
                settings, "realtime_closed_15m_backfill_after_ready", True
            ):
                return
        except Exception:
            pass

        slot_key = self._slot_ts_key(bar_ts)
        gate = self._get_slot_gate(slot_key)
        with self._lock:
            if gate.get("backfill_started"):
                return
            gate["backfill_started"] = True

        try:
            probe_nk = canonical_stock_code(str(probe_code).strip())
        except Exception:
            probe_nk = str(probe_code or "").strip()

        codes = self._list_watch_codes(app)
        pending: List[Tuple[float, str]] = []
        for nk in codes:
            if nk == probe_nk:
                continue
            k = self._key(nk, bar_ts)
            with self._lock:
                if self._scheduled.get(k):
                    continue
            try:
                sc = float(official_replace_priority_score(app, nk))
            except Exception:
                sc = 0.0
            pending.append((sc, nk))
        if not pending:
            return
        # 합치도 높은 쪽을 조금 더 먼저(여유 백필)
        pending.sort(key=lambda x: (-x[0], x[1]))

        try:
            spacing = float(
                settings.get("realtime_closed_15m_backfill_spacing_sec", 0.55)
            )
        except (TypeError, ValueError):
            spacing = 0.55
        spacing = max(0.35, min(spacing, 3.0))
        try:
            base = float(
                settings.get("realtime_closed_15m_backfill_base_delay_sec", 0.3)
            )
        except (TypeError, ValueError):
            base = 0.3
        base = max(0.0, min(base, 5.0))

        for i, (_sc, nk) in enumerate(pending):
            try:
                self.schedule(
                    app=app,
                    code=nk,
                    bar_ts=bar_ts,
                    delay_sec=max(0.3, float(delay_sec)),
                    prepend_delay_sec=0.0,
                    backfill_after_ready=True,
                    backfill_extra_delay_sec=base + float(i) * spacing,
                )
            except Exception:
                continue
        if self._log:
            try:
                self._log(
                    f"ℹ️ [RT15m-Official] {slot_key} 공식봉 확인 — "
                    f"나머지 {len(pending)}종 백필 교체·평가 예약"
                )
            except Exception:
                pass

    def schedule(
        self,
        *,
        app: Any,
        code: str,
        bar_ts: Any,
        delay_sec: float,
        force_halt_retry: bool = False,
        halt_retry_fail_cooldown_sec: float = 60.0,
        prepend_delay_sec: float = 0.0,
        backfill_after_ready: bool = False,
        backfill_extra_delay_sec: float = 0.0,
    ) -> None:
        """이미 동일 종목·동일 봉 시각이 예약돼 있으면 무시합니다."""
        try:
            if not is_krx_15m_slot_end(bar_ts):
                return
        except Exception:
            return

        # 전일 슬롯·당일 첫 15분 완성 전 정렬교체 스킵 (장시작 15:30 잔여 등)
        try:
            if should_skip_pre_first_slot_or_prior_day_official_replace(bar_ts):
                return
        except Exception:
            pass

        try:
            settings = getattr(app, "settings", None) or {}
        except Exception:
            settings = {}

        # 저정밀 공식교체 후 평가: 1차=임박·보유만. 백필은 프로브 성공 후 전 감시.
        # 임박·보유가 전무하면 합치도 최고 1종을 프로브로 허용(성공 시 백필).
        if not force_halt_retry and not backfill_after_ready:
            try:
                if closed_15m_eval_after_official_enabled(settings):
                    if not official_replace_eval_eligible(app, code, settings):
                        if not self._allow_fallback_probe(app, code, settings):
                            return
            except Exception:
                pass

        k = self._key(code, bar_ts)
        with self._lock:
            if self._scheduled.get(k):
                return
            if not force_halt_retry:
                try:
                    now_m = time.monotonic()
                    until = float(self._fail_cooldown_until.get(k, 0.0) or 0.0)
                    if until > now_m:
                        return
                except Exception:
                    pass
            self._scheduled[k] = True

        if not force_halt_retry:
            try:
                from core.krx_market_halt_slots import should_skip_rt15m_official_replace

                if should_skip_rt15m_official_replace(app, bar_ts):
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
            except Exception:
                pass

        try:
            if official_replace_alignment_mode(settings):
                self._align_register_pending(code, bar_ts)
        except Exception:
            pass

        # 정렬 모드: 슬롯당 배치 1워커 (종목별 스레드 + wait_out → 1분 간격 방지)
        try:
            if (
                official_replace_alignment_mode(settings)
                and (not force_halt_retry)
                and (not backfill_after_ready)
            ):
                self._align_batch_offer(app, code, bar_ts)
                return
        except Exception:
            pass

        def _delay() -> float:
            if backfill_after_ready:
                try:
                    return max(0.0, float(backfill_extra_delay_sec))
                except (TypeError, ValueError):
                    return 0.3
            try:
                return compute_rt15m_official_replace_delay_sec(
                    app, code, bar_ts, fallback_delay_sec=delay_sec
                )
            except Exception:
                try:
                    return max(0.5, float(delay_sec))
                except (TypeError, ValueError):
                    return 2.5

        def _worker() -> None:
            try:
                try:
                    pre = max(0.0, float(prepend_delay_sec))
                except (TypeError, ValueError):
                    pre = 0.0
                if pre > 0.0:
                    time.sleep(pre)
                wait_s = _delay()
                time.sleep(wait_s)
            except Exception:
                pass
            try:
                mon = bool(getattr(app, "is_monitoring", False)) or bool(
                    getattr(
                        getattr(app, "trading_controller", None),
                        "is_monitoring",
                        False,
                    )
                )
                if not mon:
                    with self._lock:
                        self._scheduled.pop(k, None)
                    try:
                        self._align_finish(code, bar_ts, ok=None)
                    except Exception:
                        pass
                    return
                token = None
                try:
                    token = app.auth_manager.get_auth_token()
                except Exception:
                    token = None
                if not token:
                    if self._log:
                        try:
                            cc = canonical_stock_code(str(code).strip())
                            self._log(
                                f"⚠️ [RT15m-Official] 토큰 없음 — 스킵 ({cc})"
                            )
                        except Exception:
                            pass
                    with self._lock:
                        self._scheduled.pop(k, None)
                    try:
                        self._align_finish(code, bar_ts, ok=None)
                    except Exception:
                        pass
                    return
                dm = getattr(app, "data_manager", None)
                if dm is None:
                    with self._lock:
                        self._scheduled.pop(k, None)
                    try:
                        self._align_finish(code, bar_ts, ok=None)
                    except Exception:
                        pass
                    return
                try:
                    settings = getattr(app, "settings", None) or {}
                except Exception:
                    settings = {}
                try:
                    tic = str(
                        settings.get("realtime_warmup_ka10080_tic_scope", "15")
                    ).strip() or "15"
                except Exception:
                    tic = "15"
                try:
                    mp = int(
                        float(
                            settings.get(
                                "realtime_closed_15m_official_replace_max_pages",
                                8,
                            )
                        )
                    )
                except (TypeError, ValueError):
                    mp = 8
                try:
                    mr = int(
                        float(
                            settings.get(
                                "realtime_closed_15m_official_replace_max_retries",
                                3,
                            )
                        )
                    )
                except (TypeError, ValueError):
                    mr = 3
                try:
                    ir = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_inter_retry_sec",
                            0.6,
                        )
                    )
                except (TypeError, ValueError):
                    ir = 0.6
                try:
                    dmin = float(
                        settings.get("realtime_warmup_ka10080_page_delay_min", 0.08)
                    )
                    dmax = float(
                        settings.get("realtime_warmup_ka10080_page_delay_max", 0.22)
                    )
                except (TypeError, ValueError):
                    dmin, dmax = 0.08, 0.22

                # M1 평가 + 정합 교체: 슬롯+~2분 후 단발(짧은 재시도), 조기 폴링 금지
                alignment = (
                    (not force_halt_retry)
                    and (not backfill_after_ready)
                    and official_replace_alignment_mode(settings)
                )
                if alignment:
                    try:
                        wait_for_alignment_replace_window(
                            app, code, bar_ts, settings
                        )
                    except Exception:
                        pass
                    try:
                        mon2 = bool(getattr(app, "is_monitoring", False)) or bool(
                            getattr(
                                getattr(app, "trading_controller", None),
                                "is_monitoring",
                                False,
                            )
                        )
                        if not mon2:
                            with self._lock:
                                self._scheduled.pop(k, None)
                            try:
                                self._align_finish(code, bar_ts, ok=None)
                            except Exception:
                                pass
                            return
                    except Exception:
                        pass
                    try:
                        token = app.auth_manager.get_auth_token()
                    except Exception:
                        token = None
                    if not token:
                        with self._lock:
                            self._scheduled.pop(k, None)
                        try:
                            self._align_finish(code, bar_ts, ok=None)
                        except Exception:
                            pass
                        return

                try:
                    max_wait = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_max_wait_sec",
                            75.0,
                        )
                    )
                except (TypeError, ValueError):
                    max_wait = 75.0
                # 실측 공식 15분 API ~50초 지연 → 기본 75, 상한 180
                max_wait = max(3.0, min(max_wait, 180.0))
                try:
                    poll_sec = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_poll_sec",
                            1.5,
                        )
                    )
                except (TypeError, ValueError):
                    poll_sec = 1.5
                poll_sec = max(0.4, min(poll_sec, 5.0))

                use_slot_probe = (
                    (not force_halt_retry)
                    and (not alignment)
                    and closed_15m_eval_after_official_enabled(settings)
                )
                slot_key = self._slot_ts_key(bar_ts)
                is_probe = True
                gate: Optional[Dict[str, Any]] = None

                # 백필: 프로브가 공식봉 존재를 확인한 뒤 나머지 종목 조회
                # (조회는 아래 공통 폴링 — 단발 1회 조회 금지)
                if backfill_after_ready and use_slot_probe:
                    is_probe = False
                    gate = self._get_slot_gate(slot_key)
                elif use_slot_probe:
                    gate = self._get_slot_gate(slot_key)
                    plock = gate["probe_lock"]
                    claimed = False
                    try:
                        claimed = bool(plock.acquire(blocking=False))
                    except Exception:
                        claimed = False
                    if claimed:
                        try:
                            if gate.get("probe_code") is None:
                                gate["probe_code"] = canonical_stock_code(
                                    str(code).strip()
                                )
                                is_probe = True
                            else:
                                is_probe = False
                        finally:
                            try:
                                plock.release()
                            except Exception:
                                pass
                    else:
                        is_probe = False

                    if not is_probe:
                        try:
                            ev = gate.get("ready_event")
                            if ev is not None:
                                # 슬롯 기준 남은 대기 + 여유
                                rem = max(
                                    5.0,
                                    _monotonic_deadline_for_slot(bar_ts, max_wait)
                                    - time.monotonic(),
                                )
                                ev.wait(timeout=rem + 2.0)
                        except Exception:
                            pass

                per_try_retries = 1 if (max_wait >= 5.0 or alignment) else max(1, min(mr, 3))
                deadline = _monotonic_deadline_for_slot(bar_ts, max_wait)
                hit: Optional[Official15mFetchHit] = None

                if use_slot_probe and not is_probe:
                    if gate is not None and not bool(gate.get("ready")):
                        with self._lock:
                            self._scheduled.pop(k, None)
                        try:
                            if self._log:
                                cc = canonical_stock_code(str(code).strip())
                                el = _elapsed_sec_since_slot(bar_ts)
                                self._log(
                                    f"ℹ️ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                                    f"프로브 미확인 — 백필 생략 "
                                    f"(슬롯 후 {el:.0f}s)"
                                )
                        except Exception:
                            pass
                        return

                if alignment:
                    # 고정 지연 후 소수 회 조회만 (M1/평가 REST 와 경쟁 최소화)
                    attempts, gap_sec = _alignment_retry_params(settings)
                    try:
                        heal_max = int(
                            float(
                                settings.get(
                                    "realtime_closed_15m_alignment_heal_max_bars",
                                    4,
                                )
                            )
                        )
                    except (TypeError, ValueError):
                        heal_max = 4
                    heal_max = max(1, min(heal_max, 16))
                    for ai in range(int(attempts)):
                        try:
                            _wait_official_quiet_chunks(app)
                        except Exception:
                            pass
                        hit = fetch_official_15m_hit(
                            dm,
                            code,
                            token,
                            bar_ts,
                            tic_scope=tic,
                            max_pages=mp,
                            page_delay_min=dmin,
                            page_delay_max=dmax,
                            max_retries=per_try_retries,
                            inter_retry_sec=ir,
                            heal_max_bars=heal_max,
                            log=self._log,
                            app=app,
                        )
                        if hit is not None:
                            break
                        if ai + 1 >= int(attempts):
                            break
                        try:
                            time.sleep(float(gap_sec))
                        except Exception:
                            break
                        try:
                            token = app.auth_manager.get_auth_token()
                        except Exception:
                            pass
                        if not token:
                            break
                else:
                    # 프로브·백필 공통: 슬롯 기준 deadline까지 폴링
                    while True:
                        hit = fetch_official_15m_hit(
                            dm,
                            code,
                            token,
                            bar_ts,
                            tic_scope=tic,
                            max_pages=mp,
                            page_delay_min=dmin,
                            page_delay_max=dmax,
                            max_retries=per_try_retries,
                            inter_retry_sec=ir,
                            log=self._log,
                            app=app,
                        )
                        if hit is not None:
                            break
                        if time.monotonic() + poll_sec >= deadline:
                            break
                        try:
                            time.sleep(poll_sec)
                        except Exception:
                            break
                        try:
                            token = app.auth_manager.get_auth_token()
                        except Exception:
                            pass
                        if not token:
                            break

                if use_slot_probe and gate is not None and is_probe:
                    try:
                        gate["ready"] = hit is not None
                        ev = gate.get("ready_event")
                        if ev is not None:
                            ev.set()
                    except Exception:
                        pass
                    # 공식봉 확인 후 나머지 감시 종목 백필
                    if hit is not None:
                        try:
                            self._enqueue_slot_backfill(
                                app,
                                bar_ts,
                                probe_code=str(code),
                                delay_sec=float(delay_sec),
                            )
                        except Exception:
                            pass

                if hit is None:
                    try:
                        from core.krx_market_halt_slots import is_market_halt_15m_slot

                        if is_market_halt_15m_slot(app, bar_ts) and not force_halt_retry:
                            with self._lock:
                                self._scheduled.pop(k, None)
                            return
                    except Exception:
                        pass
                    with self._lock:
                        if force_halt_retry:
                            try:
                                cd = max(
                                    15.0, float(halt_retry_fail_cooldown_sec)
                                )
                            except (TypeError, ValueError):
                                cd = 60.0
                            self._fail_cooldown_until[k] = time.monotonic() + cd
                        else:
                            cnt = int(self._fail_counts.get(k, 0) or 0) + 1
                            self._fail_counts[k] = cnt
                            delay = min(
                                900.0, 60.0 * (2 ** max(0, cnt - 1))
                            )
                            self._fail_cooldown_until[k] = time.monotonic() + delay
                        self._scheduled.pop(k, None)
                    if hit is None:
                        try:
                            if alignment:
                                self._align_finish(code, bar_ts, ok=False)
                            elif self._log:
                                cc = canonical_stock_code(str(code).strip())
                                el = _elapsed_sec_since_slot(bar_ts)
                                role = "프로브" if is_probe else "백필"
                                self._log(
                                    f"ℹ️ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                                    f"공식봉 대기 초과({role}, 한도 {max_wait:.0f}s, "
                                    f"슬롯 후 {el:.0f}s) — 평가 생략"
                                )
                        except Exception:
                            pass
                    return
                try:
                    o, h, l_, c, v = hit.ohlcv
                except Exception:
                    o = h = l_ = c = v = 0.0
                try:
                    heal_bars = list(hit.completed_bars or [])
                except Exception:
                    heal_bars = []
                if not heal_bars:
                    try:
                        heal_bars = [
                            (_norm_bar_ts(bar_ts), o, h, l_, c, v)
                        ]
                    except Exception:
                        heal_bars = []

                if force_halt_retry:
                    try:
                        from core.krx_market_halt_slots import (
                            is_market_halt_15m_slot,
                            notify_halt_official_retry_success,
                        )

                        if is_market_halt_15m_slot(app, bar_ts):
                            notify_halt_official_retry_success(
                                app, bar_ts, code, volume=v
                            )
                    except Exception:
                        pass

                apply_ok = [False]

                def _apply_main() -> None:
                    applied = False
                    try:
                        apply_fn = getattr(
                            app, "_apply_official_closed_15m_bars", None
                        )
                        if callable(apply_fn):
                            ret = apply_fn(
                                code, heal_bars, primary_ts=bar_ts
                            )
                        else:
                            ret = app._apply_official_closed_15m_bar(  # type: ignore[attr-defined]
                                code, bar_ts, o, h, l_, c, v
                            )
                        applied = True if ret is None else bool(ret)
                    except Exception as ex:
                        if self._log:
                            try:
                                cc = canonical_stock_code(str(code).strip())
                                self._log(
                                    f"⚠️ [RT15m-Official] 메인 반영 실패 ({cc}): {ex!r}"
                                )
                            except Exception:
                                pass
                        applied = False
                    finally:
                        apply_ok[0] = bool(applied)
                        with self._lock:
                            self._scheduled.pop(k, None)
                            self._fail_cooldown_until.pop(k, None)
                            self._fail_counts.pop(k, None)
                    if applied:
                        try:
                            st = getattr(app, "settings", None) or {}
                            if closed_15m_eval_after_official_enabled(st):
                                pool = getattr(app, "_closed_15m_eval_pool", None)
                                if pool is not None:
                                    pool.submit_eval(code, bar_ts)
                        except Exception:
                            pass

                try:
                    from core.rt_ui_responsiveness import run_official_15m_memory_apply

                    # 정렬 모드: 평가 큐 대기(최대 45초/종)하면 1분 간격이 난다.
                    # (배치 경로가 기본이며, 여기 폴백은 halt 재시도 등)
                    if not alignment:
                        from core.closed_15m_fast_path import wait_out_signal_eval_window

                        wait_out_signal_eval_window(app, max_wait_sec=45.0)
                    run_official_15m_memory_apply(_apply_main)
                except Exception:
                    try:
                        _apply_main()
                    except Exception:
                        with self._lock:
                            self._scheduled.pop(k, None)

                try:
                    if alignment:
                        self._align_finish(code, bar_ts, ok=bool(apply_ok[0]))
                    elif self._log:
                        cc = canonical_stock_code(str(code).strip())
                        el = _elapsed_sec_since_slot(bar_ts)
                        role = "프로브" if is_probe else "백필"
                        self._log(
                            f"✅ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                            f"공식봉 확인({role}, 슬롯 후 +{el:.1f}s) — 교체·평가"
                        )
                except Exception:
                    pass
            except Exception as e:
                if self._log:
                    try:
                        self._log(f"⚠️ [RT15m-Official] 워커 예외: {e!r}")
                    except Exception:
                        pass
                with self._lock:
                    self._scheduled.pop(k, None)
                try:
                    self._align_finish(code, bar_ts, ok=None)
                except Exception:
                    pass

        th = threading.Thread(
            target=_worker, name="rt15m-official-replace", daemon=True
        )
        th.start()
