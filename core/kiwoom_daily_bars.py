# -*- coding: utf-8 -*-
"""
키움 REST ``ka10081``(주식일봉차트) 일봉 조회.

- ``trading_data.db`` 를 사용하지 않음(GUI 종목선정용).
- ``prepare_dynamic_universe`` 가 쓰는 DB 일봉과 별개이나, 증권사 일봉 정의에 맞추기 위한 경로.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

try:
    import config as _config
except Exception:  # pragma: no cover
    _config = None  # type: ignore


# 프로세스 전체에서 ka10081 호출 간격을 강제해 429를 완화합니다.
_KA10081_RATE_LOCK = threading.Lock()
_KA10081_LAST_CALL_TS: float = 0.0


def _sleep_for_global_rate_limit(min_interval_sec: float) -> None:
    """전역 최소 간격(min_interval_sec) 충족까지 대기."""
    global _KA10081_LAST_CALL_TS
    if min_interval_sec <= 0:
        return
    while True:
        wait_sec = 0.0
        with _KA10081_RATE_LOCK:
            now = time.monotonic()
            elapsed = now - _KA10081_LAST_CALL_TS
            if elapsed >= min_interval_sec:
                _KA10081_LAST_CALL_TS = now
                return
            wait_sec = min_interval_sec - elapsed
        if wait_sec > 0:
            time.sleep(wait_sec)


def _log_safe(
    logger: Optional[Callable[..., None]],
    msg: str,
) -> None:
    if logger is None:
        return
    try:
        logger(msg)
    except Exception:
        pass


def _kiwoom_base_url() -> str:
    try:
        if _config is not None and bool(getattr(_config, "USE_MOCK", False)):
            return str(getattr(_config, "MOCK_URL", "https://mockapi.kiwoom.com")).rstrip("/")
        if _config is not None:
            return str(getattr(_config, "URL", "https://api.kiwoom.com")).rstrip("/")
    except Exception:
        pass
    return "https://api.kiwoom.com"


def _extract_chart_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """응답 JSON에서 일봉 행 리스트를 찾는다(키움 스펙 변형 대비)."""
    if not isinstance(data, dict):
        return []
    best: List[Dict[str, Any]] = []
    fallback_longest: List[Dict[str, Any]] = []
    for _k, v in data.items():
        if not (isinstance(v, list) and v and isinstance(v[0], dict)):
            continue
        if len(v) > len(fallback_longest):
            fallback_longest = v
        sample = v[0]
        looks_ohlc = any(
            x in sample
            for x in ("cur_prc", "open_pric", "clos_prc", "dt", "bas_dd", "trde_qty")
        )
        if looks_ohlc and len(v) > len(best):
            best = v
    return best if best else fallback_longest


def _parse_signed_num(x: Any) -> float:
    try:
        s = str(x).strip().replace("+", "").replace(",", "")
        if not s:
            return 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _parse_yyyymmdd(val: Any) -> Optional[str]:
    """날짜 키 값 → ``YYYY-MM-DD``."""
    try:
        s = re.sub(r"\D", "", str(val).strip())
        if len(s) >= 8:
            s8 = s[:8]
            return f"{s8[:4]}-{s8[4:6]}-{s8[6:8]}"
    except Exception:
        pass
    return None


def _row_to_bar(row: Dict[str, Any]) -> Optional[Tuple[str, float, float, float, float, float]]:
    """한 행 → (date_str, o, h, l, c, v). 파싱 실패 시 None."""
    if not isinstance(row, dict):
        return None
    date_raw = (
        row.get("dt")
        or row.get("date")
        or row.get("bas_dd")
        or row.get("stck_bsop_date")
        or row.get("trad_dt")
        or row.get("cntr_dd")
        or row.get("stck_cntg_date")
    )
    ds = _parse_yyyymmdd(date_raw)
    if not ds:
        return None
    o = _parse_signed_num(
        row.get("open_pric")
        or row.get("opnprc")
        or row.get("futs_oprc")
        or row.get("open")
    )
    h = _parse_signed_num(
        row.get("high_pric") or row.get("hgprc") or row.get("high")
    )
    l_ = _parse_signed_num(
        row.get("low_pric") or row.get("lwprc") or row.get("low")
    )
    c = _parse_signed_num(
        row.get("cur_prc")
        or row.get("clos_prc")
        or row.get("prpr")
        or row.get("close")
    )
    v = _parse_signed_num(
        row.get("trde_qty")
        or row.get("acml_vol")
        or row.get("vol")
        or row.get("volume")
    )
    if c <= 0 and o <= 0:
        return None
    return (ds, o, h, l_, c, v)


def fetch_kiwoom_daily_ohlcv_dataframe(
    code: str,
    token: str,
    *,
    min_date: str,
    max_date: str,
    upd_stkpc_tp: str = "1",
    max_pages: int = 30,
    page_delay_sec: float = 0.28,
    min_request_interval_sec: float = 1.0,
    max_429_retries_per_page: int = 8,
    logger_callback: Optional[Callable[..., None]] = None,
) -> pd.DataFrame:
    """
    ``ka10081`` 연속조회로 일봉을 모아 ``calculate_technical_indicators`` 호환 DataFrame 반환.

    Parameters
    ----------
    code:
        6자리 종목코드.
    token:
        Bearer 액세스 토큰.
    min_date, max_date:
        ``YYYY-MM-DD`` (둘 다 inclusive 로 필터).
    """
    nc = str(code or "").strip().zfill(6)
    if len(nc) != 6:
        return pd.DataFrame()
    tok = (token or "").strip()
    if not tok:
        return pd.DataFrame()

    base_dt = str(max_date or "").strip()[:10].replace("-", "")
    if len(base_dt) != 8:
        try:
            base_dt = datetime.now().strftime("%Y%m%d")
        except Exception:
            base_dt = "00000000"

    url = f"{_kiwoom_base_url()}/api/dostk/chart"
    body: Dict[str, Any] = {
        "stk_cd": nc,
        "base_dt": base_dt,
        "upd_stkpc_tp": str(upd_stkpc_tp or "1").strip() or "1",
    }

    merged: Dict[str, Tuple[float, float, float, float, float]] = {}
    next_key_in = ""
    for page in range(int(max_pages)):
        nk_in = (next_key_in or "").strip()
        retry_429 = 0
        res = None
        while True:
            try:
                _sleep_for_global_rate_limit(float(min_request_interval_sec))
                headers = {
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {tok}",
                    "cont-yn": "Y" if nk_in else "N",
                    "next-key": nk_in,
                    "api-id": "ka10081",
                }
                res = requests.post(url, headers=headers, json=body, timeout=20)
            except requests.RequestException as e:
                _log_safe(logger_callback, f"⚠️ [Kiwoom1D] {nc} HTTP 오류: {e}")
                res = None
                break

            if res.status_code != 429:
                break

            retry_429 += 1
            # 대량 연속조회 시 429가 잦아 지수 백오프를 넉넉히 둠(최대 30초).
            backoff = min(2.5 * (2 ** (retry_429 - 1)), 30.0)
            _log_safe(
                logger_callback,
                f"⚠️ [Kiwoom1D] {nc} HTTP 429 (요청 과다·rate limit) — "
                f"{backoff:.1f}s 후 재시도 ({retry_429}/{max_429_retries_per_page})",
            )
            time.sleep(backoff)
            if retry_429 >= int(max_429_retries_per_page):
                _log_safe(logger_callback, f"⚠️ [Kiwoom1D] {nc} 429 재시도 초과로 이 종목은 건너뜁니다.")
                res = None
                break

        if res is None:
            break
        if res.status_code == 401:
            _log_safe(logger_callback, f"⚠️ [Kiwoom1D] {nc} 401 Unauthorized — 토큰을 갱신하세요.")
            break
        if res.status_code != 200:
            _log_safe(
                logger_callback,
                f"⚠️ [Kiwoom1D] {nc} status={res.status_code} body={str(res.text)[:200]}",
            )
            break
        try:
            data = res.json() if res.content else {}
        except Exception:
            data = {}
        try:
            rc = int(str(data.get("return_code", 0)).strip() or "0")
        except (TypeError, ValueError):
            rc = 0
        if rc != 0:
            msg = str(data.get("return_msg", "") or "")
            _log_safe(logger_callback, f"⚠️ [Kiwoom1D] {nc} return_code={rc} {msg}")
            break

        raw_list = _extract_chart_list(data)
        for item in raw_list:
            parsed = _row_to_bar(item)
            if parsed is None:
                continue
            ds, o, h, l_, c, v = parsed
            merged[ds] = (o, h, l_, c, v)

        nk_out = (res.headers.get("next-key") or "").strip()
        if not nk_out:
            try:
                nk_out = str(data.get("next-key") or "").strip()
            except Exception:
                nk_out = ""
        cont = (res.headers.get("cont-yn") or "N").strip().upper()
        if cont != "Y" or not nk_out:
            break
        if nk_out == nk_in:
            break
        next_key_in = nk_out
        if page_delay_sec > 0:
            time.sleep(float(page_delay_sec))

    if not merged:
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    for ds in sorted(merged.keys()):
        o, h, l_, c, v = merged[ds]
        rows.append(
            {
                "time": pd.Timestamp(ds),
                "Open": float(o),
                "High": float(h),
                "Low": float(l_),
                "Close": float(c),
                "Volume": float(v),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    try:
        mn = pd.Timestamp(str(min_date)[:10])
        mx = pd.Timestamp(str(max_date)[:10])
        df = df[(df["time"] >= mn) & (df["time"] <= mx)]
    except Exception:
        pass
    df = df.sort_values("time").reset_index(drop=True)
    return df
