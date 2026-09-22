# -*- coding: utf-8 -*-
"""키움 ka10080 분봉/15분봉 REST 조회 믹스인 + 전역 페이싱 헬퍼."""
from __future__ import annotations

import threading
import time
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

import config
from utils.normalization import normalize_code

# ka10080 전역 페이싱(워밍업·M1폴러·15분 확정 보정 등 병렬 스레드 429 완화)
_KA10080_GLOBAL_LOCK = threading.Lock()
_KA10080_GLOBAL_NEXT_MONOTONIC: float = 0.0
_KA10080_RATE_LIMIT_LOG_LOCK = threading.Lock()
_KA10080_RATE_LIMIT_LOG_AT: Dict[str, float] = {}


def _log_ka10080_rate_limit_throttled(code: str, *, status_code: int = 429) -> None:
    """종목별 ka10080 429·과다호출 로그 스팸 방지 + 전역 쿨다운 강화."""
    try:
        nk = normalize_code(str(code).strip())
    except Exception:
        nk = str(code or "").strip()
    if not nk:
        return
    now = time.monotonic()
    try:
        with _KA10080_RATE_LIMIT_LOG_LOCK:
            last = float(_KA10080_RATE_LIMIT_LOG_AT.get(nk, 0.0) or 0.0)
            if now - last < 90.0:
                return
            _KA10080_RATE_LIMIT_LOG_AT[nk] = now
    except Exception:
        pass
    try:
        print(
            f"  [Error] ka10080 {status_code} ({nk}) — "
            f"키움 호출 제한, 1~2분 후 자동 재시도"
        )
    except Exception:
        pass
    try:
        _ka10080_global_bump_cooldown(20.0)
    except Exception:
        pass


def _ka10080_global_acquire_slot() -> None:
    """다음 ka10080 POST 직전까지 대기하고, 이후 호출을 위해 전역 다음 허용 시각을 갱신합니다."""
    global _KA10080_GLOBAL_NEXT_MONOTONIC
    try:
        gap = float(getattr(config, "KA10080_GLOBAL_MIN_INTERVAL_SEC", 1.05))
    except Exception:
        gap = 1.05
    gap = max(0.8, min(gap, 3.0))
    with _KA10080_GLOBAL_LOCK:
        try:
            now = time.monotonic()
            wait_s = _KA10080_GLOBAL_NEXT_MONOTONIC - now
            if wait_s > 0:
                time.sleep(wait_s)
            _KA10080_GLOBAL_NEXT_MONOTONIC = time.monotonic() + gap
        except Exception:
            pass


def _ka10080_global_bump_cooldown(extra_sec: float) -> None:
    """429 등으로 서버가 막힌 뒤 추가 대기가 필요할 때 전역 다음 허용 시각을 뒤로 미룹니다."""
    global _KA10080_GLOBAL_NEXT_MONOTONIC
    try:
        bump = max(0.5, float(extra_sec))
    except Exception:
        bump = 1.0
    try:
        until = time.monotonic() + bump
        with _KA10080_GLOBAL_LOCK:
            if until > _KA10080_GLOBAL_NEXT_MONOTONIC:
                _KA10080_GLOBAL_NEXT_MONOTONIC = until
    except Exception:
        pass


def _ka10080_response_diag(
    res: Any,
    data: Any,
    raw_list: Any,
) -> Dict[str, Any]:
    """
    ka10080 실패 분석용 응답 요약.

    empty_response / 429 등에서 return_code·msg·바디 크기·키를 남겨
    모의 API 장애·한도·토큰 문제를 구분할 수 있게 합니다.
    """
    out: Dict[str, Any] = {
        "body_bytes": 0,
        "content_type": "",
        "return_code": None,
        "return_msg": "",
        "body_keys": [],
        "raw_len": 0,
        "raw_type": "",
        "body_preview": "",
    }
    try:
        out["body_bytes"] = int(len(getattr(res, "content", None) or b"") or 0)
    except Exception:
        out["body_bytes"] = 0
    try:
        hdrs = getattr(res, "headers", None)
        if hdrs is not None:
            out["content_type"] = str(hdrs.get("Content-Type") or "")[:120]
    except Exception:
        pass
    try:
        if isinstance(raw_list, list):
            out["raw_len"] = len(raw_list)
            out["raw_type"] = "list"
        elif raw_list is None:
            out["raw_type"] = "none"
        else:
            out["raw_type"] = type(raw_list).__name__
    except Exception:
        pass
    if isinstance(data, dict):
        try:
            out["body_keys"] = sorted(str(k) for k in data.keys())[:24]
        except Exception:
            out["body_keys"] = []
        try:
            rc = data.get("return_code", data.get("returnCode"))
            if rc is not None and rc != "":
                try:
                    out["return_code"] = int(rc)
                except (TypeError, ValueError):
                    out["return_code"] = rc
        except Exception:
            pass
        try:
            rm = (
                data.get("return_msg")
                or data.get("returnMsg")
                or data.get("msg")
                or data.get("message")
                or ""
            )
            out["return_msg"] = str(rm)[:300]
        except Exception:
            out["return_msg"] = ""
        try:
            # 차트 배열은 제외하고 메타만 미리보기
            slim: Dict[str, Any] = {}
            for k, v in data.items():
                ks = str(k)
                if ks in (
                    "stk_min_pole_chart_qry",
                    "output",
                    "data",
                    "chart",
                ):
                    if isinstance(v, list):
                        slim[ks] = f"<list:{len(v)}>"
                    else:
                        slim[ks] = f"<{type(v).__name__}>"
                else:
                    slim[ks] = v
            import json as _json

            out["body_preview"] = _json.dumps(slim, ensure_ascii=False)[:400]
        except Exception:
            try:
                out["body_preview"] = str(data)[:400]
            except Exception:
                out["body_preview"] = ""
    elif not data:
        # 진짜 빈 바디
        try:
            txt = getattr(res, "text", None) or ""
            out["body_preview"] = str(txt)[:200]
        except Exception:
            pass
    return out


def _ka10080_diag_detail(kind: str, diag: Dict[str, Any]) -> str:
    """record_fail detail 문자열(한 줄, 분석용)."""
    try:
        import json as _json

        payload = {"kind": str(kind), **(diag or {})}
        return _json.dumps(payload, ensure_ascii=False)[:500]
    except Exception:
        try:
            return f"{kind}|rc={diag.get('return_code')}|msg={diag.get('return_msg')}"
        except Exception:
            return str(kind)



class KiwoomChartFetchMixin:
    def fetch_kiwoom_minute_data(self, code, token, logger=None):
        url = f"{config.URL}/api/dostk/chart"
        headers = {"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}", "cont-yn": "N", "next-key": "", "api-id": "ka10080"}
        body = {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                if res.status_code == 200: return res.json().get('stk_min_pole_chart_qry', res.json().get('output', res.json().get('data', [])))
                elif res.status_code == 429:
                    if attempt < max_retries: time.sleep((attempt + 1) * 3); continue
                print(f"  [Error] API Status {res.status_code} ({code})"); return None
            except Exception as e:
                if attempt < max_retries: time.sleep(1); continue
                print(f"  [Error] API Fetch Exception ({code}): {e}"); return None
        return None


    def fetch_kiwoom_minute_chart_page(
        self,
        code: str,
        token: str,
        next_key: str = "",
        *,
        tic_scope: str = "1",
        fetch_context: str = "",
    ) -> Tuple[Optional[List[Any]], str]:
        """
        키움 ka10080 주식분봉 1페이지 조회.
        tic_scope: 봉 주기(문자열). 예) "1"=1분, "15"=15분 — 키움 스펙에 따름.
        fetch_context: 진단 로그용 경로(m1_poll, rt15m_official, warmup 등).
        next_key 가 비어 있으면 최초 조회, 있으면 연속조회(cont-yn=Y).
        반환: (분봉 레코드 리스트 또는 오류 시 None, 다음 페이지용 next_key)
        """
        ctx = str(fetch_context or "").strip() or "unknown"
        try:
            from core.ka10080_fetch_diag import ka10080_fetch_diag
        except Exception:
            ka10080_fetch_diag = None  # type: ignore

        def _set_meta(*, ok: bool, reason: str, http_status: int = 0, detail: str = "") -> None:
            try:
                self.last_ka10080_fetch_meta = {
                    "ok": bool(ok),
                    "reason": str(reason),
                    "http_status": int(http_status or 0),
                    "detail": str(detail or "")[:500],
                    "context": ctx,
                    "tic_scope": str(tic_scope or "1"),
                    "code": str(code or "").strip(),
                }
            except Exception:
                pass

        def _fail(
            reason: str,
            *,
            http_status: int = 0,
            detail: str = "",
            attempt: int = 0,
            return_code: Any = None,
            return_msg: str = "",
            body_bytes: int = 0,
            body_keys: Any = None,
        ) -> Tuple[None, str]:
            _set_meta(ok=False, reason=reason, http_status=http_status, detail=detail)
            try:
                meta = getattr(self, "last_ka10080_fetch_meta", None)
                if isinstance(meta, dict):
                    if return_code is not None:
                        meta["return_code"] = return_code
                    if return_msg:
                        meta["return_msg"] = str(return_msg)[:300]
                    if body_bytes:
                        meta["body_bytes"] = int(body_bytes)
                    if body_keys is not None:
                        meta["body_keys"] = body_keys
            except Exception:
                pass
            if ka10080_fetch_diag is not None:
                try:
                    ka10080_fetch_diag.record_fail(
                        str(code),
                        tic_scope=str(tic_scope or "1"),
                        context=ctx,
                        reason=reason,
                        http_status=http_status,
                        detail=detail,
                        attempt=attempt,
                        return_code=return_code,
                        return_msg=return_msg,
                        body_bytes=body_bytes,
                        body_keys=body_keys,
                    )
                except Exception:
                    pass
            return None, ""

        base = config.MOCK_URL if getattr(config, "USE_MOCK", False) else config.URL
        url = f"{base}/api/dostk/chart"
        ts = str(tic_scope).strip() if tic_scope is not None else "1"
        if not ts:
            ts = "1"
        body: Dict[str, Any] = {"stk_cd": code, "tic_scope": ts, "upd_stkpc_tp": "1"}
        nk_in = (next_key or "").strip()
        max_retries = 5
        for attempt in range(max_retries):
            try:
                _ka10080_global_acquire_slot()
                headers = {
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {token}",
                    "cont-yn": "Y" if nk_in else "N",
                    "next-key": nk_in,
                    "api-id": "ka10080",
                }
                res = requests.post(url, headers=headers, json=body, timeout=15)
                if res.status_code == 200:
                    try:
                        data = res.json() if res.content else {}
                    except Exception:
                        data = {}
                    raw_list = data.get(
                        "stk_min_pole_chart_qry",
                        data.get("output", data.get("data", [])),
                    )
                    if raw_list is None:
                        raw_list = []
                    nk_out = (res.headers.get("next-key") or "").strip()
                    if not nk_out:
                        try:
                            nk_out = str(data.get("next-key") or "").strip()
                        except Exception:
                            nk_out = ""
                    if not raw_list and not nk_in:
                        diag = _ka10080_response_diag(res, data, raw_list)
                        return _fail(
                            "empty_response",
                            http_status=200,
                            detail=_ka10080_diag_detail("status200_empty_chart", diag),
                            attempt=attempt + 1,
                            return_code=diag.get("return_code"),
                            return_msg=str(diag.get("return_msg") or ""),
                            body_bytes=int(diag.get("body_bytes") or 0),
                            body_keys=diag.get("body_keys"),
                        )
                    _set_meta(ok=True, reason="ok")
                    if ka10080_fetch_diag is not None:
                        try:
                            ka10080_fetch_diag.record_ok(
                                str(code),
                                tic_scope=str(tic_scope or "1"),
                                context=ctx,
                            )
                        except Exception:
                            pass
                    return raw_list, nk_out
                if res.status_code == 429:
                    # 한도 초과 본문(return_code/msg)도 남겨 빈응답과 구분
                    try:
                        data_429 = res.json() if res.content else {}
                    except Exception:
                        data_429 = {}
                    diag_429 = _ka10080_response_diag(res, data_429, None)
                    # 유량=1 한도이므로 429 직후 최소 ~3초+ 전역 쿨다운
                    _ka10080_global_bump_cooldown(
                        max(3.0, 2.5 * (2**attempt) + random.uniform(0.3, 0.8))
                    )
                    if attempt + 1 < max_retries:
                        try:
                            time.sleep(
                                3.0 * (attempt + 1)
                                + random.uniform(0.3, 0.9)
                            )
                        except Exception:
                            time.sleep(3.0 * (attempt + 1))
                        continue
                    _log_ka10080_rate_limit_throttled(code, status_code=429)
                    return _fail(
                        "http_429",
                        http_status=429,
                        detail=_ka10080_diag_detail("rate_limit", diag_429),
                        attempt=attempt + 1,
                        return_code=diag_429.get("return_code"),
                        return_msg=str(diag_429.get("return_msg") or ""),
                        body_bytes=int(diag_429.get("body_bytes") or 0),
                        body_keys=diag_429.get("body_keys"),
                    )
                try:
                    print(f"  [Error] ka10080 Status {res.status_code} ({code})")
                except Exception:
                    pass
                try:
                    data_err = res.json() if res.content else {}
                except Exception:
                    data_err = {}
                diag_err = _ka10080_response_diag(res, data_err, None)
                if not diag_err.get("body_preview"):
                    try:
                        diag_err["body_preview"] = (res.text or "")[:200]
                    except Exception:
                        pass
                reason = (
                    f"http_{int(res.status_code)}"
                    if int(res.status_code) > 0
                    else "http_error"
                )
                return _fail(
                    reason,
                    http_status=int(res.status_code),
                    detail=_ka10080_diag_detail(reason, diag_err),
                    attempt=attempt + 1,
                    return_code=diag_err.get("return_code"),
                    return_msg=str(diag_err.get("return_msg") or ""),
                    body_bytes=int(diag_err.get("body_bytes") or 0),
                    body_keys=diag_err.get("body_keys"),
                )
            except Exception as e:
                if attempt + 1 < max_retries:
                    time.sleep(1.0)
                    continue
                try:
                    print(f"  [Error] ka10080 예외 ({code}): {e}")
                except Exception:
                    pass
                return _fail(
                    "network_error",
                    detail=repr(e),
                    attempt=attempt + 1,
                )
        return _fail("unknown", detail="retry_exhausted", attempt=max_retries)


    def fetch_15m_bars_for_trading_day(
        self,
        code: str,
        day_yyyy_mm_dd: str,
        *,
        logger: Optional[Any] = None,
        max_pages: int = 80,
    ) -> Any:
        """
        일일결산·가상 종합리포트용 15분봉.

        1) ``trading_data.db`` 의 ``bars_15m`` 구간 조회
        2) 비어 있으면 동일 일자 ``bars_1m`` 를 15분봉으로 리샘플
        3) 그래도 없으면 :meth:`fetch_15m_bars_for_trading_day_rest_only` (키움 REST)

        반환 형식은 :meth:`get_bars` 15m 과 동일(time, Open, High, Low, Close, Volume).
        가상 리포트 스레드에서 REST pacing 판별용으로 ``_last_report_15m_source`` 에
        ``db15`` / ``db1m`` / ``rest`` 를 설정합니다.
        """
        def _log(msg: str) -> None:
            if logger is not None:
                try:
                    logger(msg)
                except Exception:
                    pass

        # pacing 판별용(캐시 미스 시 호출 직후 조회). 실패·조기 반환 시 빈 문자열.
        self._last_report_15m_source = ""

        try:
            n_code = normalize_code(str(code).strip())
        except Exception:
            n_code = str(code).strip().zfill(6)
        day = str(day_yyyy_mm_dd).strip()[:10]
        day_start = f"{day} 00:00:00"
        day_end = f"{day} 23:59:59"

        df15: Optional[pd.DataFrame] = None
        try:
            df15 = self.get_bars(
                n_code, start_dt=day_start, end_dt=day_end, interval="15m"
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} DB bars_15m 조회 예외: {e}")
            df15 = None

        if df15 is not None and not df15.empty:
            try:
                nrows = len(df15)
            except Exception:
                nrows = -1
            self._last_report_15m_source = "db15"
            _log(f"[리포트15m] {n_code} {day} trading_data.db bars_15m {nrows}건")
            return df15

        df1: Optional[pd.DataFrame] = None
        try:
            df1 = self.get_bars(
                n_code, start_dt=day_start, end_dt=day_end, interval="1m"
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} DB bars_1m 조회 예외: {e}")
            df1 = None

        if df1 is not None and not df1.empty:
            try:
                from utils.krx_bars import resample_1m_ohlcv_to_15m_krx

                w = df1.copy()
                w["time"] = pd.to_datetime(w["time"], errors="coerce")
                w = w.dropna(subset=["time"])
                if w.empty:
                    raise ValueError("time 파싱 후 비어 있음")
                w = w.set_index("time").sort_index()
                # Open/High/... → open/high/...
                rename_l = {
                    c: c.lower()
                    for c in ("Open", "High", "Low", "Close", "Volume")
                    if c in w.columns
                }
                if rename_l:
                    w = w.rename(columns=rename_l)
                miss = [k for k in ("open", "high", "low", "close", "volume") if k not in w.columns]
                if miss:
                    raise ValueError(f"필수 열 없음: {miss}")
                df15_k = resample_1m_ohlcv_to_15m_krx(
                    w[["open", "high", "low", "close", "volume"]], n_code
                )
                if df15_k is None or df15_k.empty:
                    raise ValueError("리샘플 결과 없음")
                rs = df15_k.rename(
                    columns={
                        "datetime": "time",
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )
                rs["time"] = pd.to_datetime(rs["time"], errors="coerce")
                rs = rs.dropna(subset=["time"])
                if rs.empty:
                    raise ValueError("리샘플 결과 없음")
                self._last_report_15m_source = "db1m"
                _log(
                    f"[리포트15m] {n_code} {day} trading_data.db bars_1m→15m "
                    f"{len(rs)}건"
                )
                return rs[["time", "Open", "High", "Low", "Close", "Volume"]]
            except Exception as e:
                _log(f"[리포트15m] {n_code} {day} bars_1m→15m 리샘플 실패: {e}")

        self._last_report_15m_source = "rest"
        _log(f"[리포트15m] {n_code} {day} DB에 해당일 봉 없음 → REST 폴백")
        try:
            return self.fetch_15m_bars_for_trading_day_rest_only(
                code, day, logger=logger, max_pages=max_pages
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} REST 폴백 예외: {e}")
            return pd.DataFrame()


    def fetch_15m_bars_for_trading_day_rest_only(
        self,
        code: str,
        day_yyyy_mm_dd: str,
        *,
        logger: Optional[Any] = None,
        max_pages: int = 80,
    ) -> Any:
        """
        리포트용 **REST 전용** 폴백: trading_data.db 를 읽지 않습니다.
        키움 ka10080 을 **15분봉(tic_scope=15)** 연속조회만으로 해당 일자(00:00~23:59) 구간을 채웁니다.
        반환 형식은 get_bars(..., interval='15m') 와 유사(time, Open, High, Low, Close, Volume).
        일반적으로는 :meth:`fetch_15m_bars_for_trading_day` 가 DB 우선으로 이 메서드를 호출합니다.
        """
        import pandas as pd
        from kiwoom_rest_api.auth.token import TokenManager

        def _log(msg: str) -> None:
            if logger is not None:
                try:
                    logger(msg)
                except Exception:
                    pass

        try:
            n_code = normalize_code(str(code).strip())
        except Exception:
            n_code = str(code).strip().zfill(6)
        day = str(day_yyyy_mm_dd).strip()[:10]
        day_start = f"{day} 00:00:00"
        day_end = f"{day} 23:59:59"

        try:
            token = TokenManager().get_token()
        except Exception:
            token = None
        if not token:
            _log(f"[일일결산] {n_code} 토큰 없음 — 15분봉 REST 생략")
            return pd.DataFrame()

        def _clean_px(v: Any) -> int:
            try:
                return int(
                    float(
                        str(v)
                        .replace("+", "")
                        .replace("-", "")
                        .replace(",", "")
                        .strip()
                        or 0
                    )
                )
            except Exception:
                return 0

        next_key_in = ""
        rows_acc: List[Dict[str, Any]] = []
        seen_dt: set = set()
        for page in range(max(1, int(max_pages))):
            raw_list, next_key_out = self.fetch_kiwoom_minute_chart_page(
                n_code, token, next_key_in, tic_scope="15"
            )
            if not raw_list:
                break
            page_has_in_day = False
            for b in raw_list:
                if not isinstance(b, dict):
                    continue
                tm = b.get("cntr_tm") or b.get("date_time") or b.get("datetime")
                if not tm:
                    continue
                s = str(tm).strip()
                if len(s) < 14:
                    continue
                try:
                    dt_fmt = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
                except Exception:
                    continue
                if dt_fmt < day_start or dt_fmt > day_end:
                    continue
                page_has_in_day = True
                if dt_fmt in seen_dt:
                    continue
                seen_dt.add(dt_fmt)
                rows_acc.append(
                    {
                        "datetime": dt_fmt,
                        "open": _clean_px(
                            b.get("open_pric", b.get("open", 0))
                        ),
                        "high": _clean_px(
                            b.get("high_pric", b.get("high", 0))
                        ),
                        "low": _clean_px(b.get("low_pric", b.get("low", 0))),
                        "close": _clean_px(
                            b.get("cur_prc", b.get("close", b.get("stck_prpr", 0)))
                        ),
                        "volume": _clean_px(
                            b.get("trde_qty", b.get("volume", b.get("cntg_vol", 0)))
                        ),
                    }
                )
            next_key_in = (next_key_out or "").strip()
            if not next_key_in:
                break
            try:
                time.sleep(0.22)
            except Exception:
                pass
            if not page_has_in_day:
                try:
                    first_tm = str(
                        raw_list[0].get("cntr_tm") or raw_list[0].get("date_time") or ""
                    )
                    if len(first_tm) >= 8 and first_tm[:8] < day.replace("-", ""):
                        break
                except Exception:
                    pass

        if not rows_acc:
            _log(f"[일일결산] {n_code} {day} ka10080(15분) 구간 봉 없음(REST 연속조회 종료)")
            return pd.DataFrame()

        try:
            df15 = pd.DataFrame(rows_acc)
            df15["datetime"] = pd.to_datetime(df15["datetime"], errors="coerce")
            df15 = df15.dropna(subset=["datetime"])
            df15 = df15.sort_values("datetime").drop_duplicates(
                subset=["datetime"], keep="last"
            )
            df15 = df15.reset_index(drop=True)
            df15.rename(
                columns={
                    "datetime": "time",
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                },
                inplace=True,
            )
            return df15
        except Exception as e:
            _log(f"[일일결산] {n_code} 15분 가공 실패: {e}")
            return pd.DataFrame()


    def fetch_and_parse_15m_bars(self, code, token):
        n_code = normalize_code(code); query = "SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ? ORDER BY datetime DESC LIMIT 5000"
        with self._get_connection() as conn: df_db = pd.read_sql_query(query, conn, params=[n_code])
        df_api = pd.DataFrame()
        if token:
            time.sleep(0.5) # Lite spread
            raw_api = self.fetch_kiwoom_minute_data(code, token)
            if raw_api:
                df_api = pd.DataFrame(raw_api); f_cl='cur_prc'; f_op='open_pric'; f_hg='high_pric'; f_lw='low_pric'; f_tm='cntr_tm'; f_vl='trde_qty'
                for c in [f_cl, f_op, f_hg, f_lw, f_vl]: df_api[c] = pd.to_numeric(df_api[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
                df_api['datetime'] = pd.to_datetime(df_api[f_tm], format='%Y%m%d%H%M%S', errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
                df_api.rename(columns={f_op: 'open', f_hg: 'high', f_lw: 'low', f_cl: 'close', f_vl: 'volume'}, inplace=True)
                df_api = df_api[['datetime', 'open', 'high', 'low', 'close', 'volume']]
        df_combined = pd.concat([df_db, df_api]).drop_duplicates('datetime').sort_values('datetime')
        if df_combined.empty: return pd.DataFrame()
        df_final = df_combined.tail(5000).copy(); df_final['datetime'] = pd.to_datetime(df_final['datetime']); df_final.set_index('datetime', inplace=True)
        resampled = df_final.resample('15min', closed='right', label='right').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
        resampled['time'] = resampled.index.strftime('%H%M'); resampled.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        return resampled.tail(300)


    def fetch_nxt_status(self, code, token):
        """
        ka10100으로 NXT 거래가능 여부를 조회합니다.

        Returns:
            True/False — 응답에서 명시적으로 판별된 경우
            None — HTTP·파싱 실패 또는 필드 부재(캐시에 '불가'로 넣지 말 것)
        """
        if not token:
            return None
        try:
            from utils.normalization import canonical_stock_code

            nk = str(canonical_stock_code(code) or code).zfill(6)
        except Exception:
            try:
                nk = str(code or "").split("_")[0].zfill(6)
            except Exception:
                return None
        if len(nk) != 6:
            return None
        try:
            res = requests.post(
                f"{config.URL}/api/dostk/stkinfo",
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {token}",
                    "api-id": "ka10100",
                },
                json={"stk_cd": nk},
                timeout=5,
            )
            if res.status_code != 200:
                return None
            data = res.json() if res is not None else {}
            from core.nxt_eligibility_service import parse_nxt_enable_from_ka10100_payload

            return parse_nxt_enable_from_ka10100_payload(data)
        except Exception:
            return None

