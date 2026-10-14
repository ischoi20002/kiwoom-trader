# -*- coding: utf-8 -*-
"""애프터 시행 이후 일봉 정규장 덧씌우기 (``data_manager.krx_regular_daily``)."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd

import data_manager.krx_regular_daily as krd

_OFF: Dict[str, Any] = {"order_after_close_enabled": False}
_ON: Dict[str, Any] = {"order_after_close_enabled": True}
_UNTIL = "2026-09-15"


def _bar(tm: str, o: int, h: int, l_: int, c: int) -> Dict[str, str]:
    """ka10080 레코드 (가격은 부호 문자열)."""
    return {"cntr_tm": tm, "open_pric": f"+{o}", "high_pric": f"+{h}", "low_pric": f"-{l_}", "cur_prc": f"+{c}"}


# 키움 15분봉 (최신 → 과거 순). 000001 은 원본 일봉의 2배 가격(수정 기준 차이), 애프터 봉 포함.
_KIWOOM_ROWS: Dict[str, List[Dict[str, str]]] = {
    "000001": [
        _bar("20260915160000", 2050, 2500, 2050, 2400),
        _bar("20260915153000", 2050, 2050, 2050, 2050),
        _bar("20260915090000", 2100, 2300, 2000, 2080),
        _bar("20260914190000", 2100, 2600, 2100, 2500),
        _bar("20260914153000", 2100, 2100, 2100, 2100),
        _bar("20260914091500", 2050, 2200, 1900, 2060),
        _bar("20260914090000", 2000, 2150, 1950, 2050),
    ],
    "000002": [
        _bar("20260915153000", 505, 505, 505, 505),
        _bar("20260915090000", 510, 530, 500, 508),
        _bar("20260914153000", 510, 510, 510, 510),
        _bar("20260914090000", 500, 520, 480, 505),
    ],
}


class _FakeFetcher:
    """종목별 한 페이지만 돌려주는 가짜 ka10080."""

    def __init__(self, fail: Optional[set] = None) -> None:
        self.calls: List[str] = []
        self.fail = fail or set()

    def __call__(self, code: str, token: str, next_key: str) -> Tuple[Optional[List[Any]], str]:
        self.calls.append(code)
        if code in self.fail:
            return None, ""
        return list(_KIWOOM_ROWS.get(code, [])), ""


@contextlib.contextmanager
def _temp_db() -> Iterator[str]:
    """임시 DB + 덧씌우기 내부 지연 조회도 가짜 ka10080·토큰 사용(네트워크 차단)."""
    krd.clear_memory_cache()
    orig_pf, orig_tok = krd._default_page_fetcher, krd._get_token
    krd._default_page_fetcher = lambda _dm: _FakeFetcher()  # type: ignore[assignment]
    krd._get_token = lambda _log=None: "t"  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            yield os.path.join(tmp, "t.db")
        finally:
            krd._default_page_fetcher = orig_pf  # type: ignore[assignment]
            krd._get_token = orig_tok  # type: ignore[assignment]
            krd.clear_memory_cache()


def _bulk() -> pd.DataFrame:
    """원본(네이버식) 일봉: 000001 은 키움의 1/2 가격, 종가는 애프터 마지막 체결가."""
    return pd.DataFrame(
        {
            "code": ["000001", "000001", "000001", "000002", "000002"],
            "time": pd.to_datetime(
                ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-14", "2026-09-15"]
            ),
            "open": [990, 1000, 1050, 500, 510],
            "high": [1010, 1300, 1250, 530, 535],
            "low": [980, 950, 990, 480, 500],
            "close": [1000, 1250, 1200, 525, 507],
            "volume": [10, 20, 30, 40, 50],
        }
    )


def _ensure(db: str, fetcher: _FakeFetcher, codes: Optional[List[str]] = None) -> Dict[str, Any]:
    return krd.ensure_kiwoom_regular(
        codes or ["000001", "000002"],
        until=_UNTIL,
        db_path=db,
        token="t",
        page_fetcher=fetcher,
        log=lambda *_a, **_k: None,
    )


def test_kiwoom_rows_aggregate_regular_session_only() -> None:
    m15 = krd._rows_to_m15_frame(_KIWOOM_ROWS["000001"])
    daily = krd._aggregate_regular_daily(m15, {"2026-09-14", "2026-09-15"}).set_index("date")
    # 9/14: 09:00 시가 2000, 고가 2200(19:00 애프터 2600 제외), 저가 1900, 종가 15:30 2100
    assert daily.loc["2026-09-14"].tolist() == [2000, 2200, 1900, 2100]
    assert daily.loc["2026-09-15"].tolist() == [2100, 2300, 2000, 2050]


def test_ensure_caches_and_skips_refetch() -> None:
    with _temp_db() as db:
        f = _FakeFetcher()
        res = _ensure(db, f)
        assert res["fetched"] == 2 and not res["failed"]
        assert sorted(f.calls) == ["000001", "000002"]
        # 같은 until 이면 재조회 없음 (메모·DB 메타 모두)
        _ensure(db, f)
        krd.clear_memory_cache()
        _ensure(db, f)
        assert len(f.calls) == 2
        conn = sqlite3.connect(db)
        n = conn.execute(f"SELECT COUNT(*) FROM {krd.KIWOOM_TABLE}").fetchone()[0]
        conn.close()
        assert n == 4


def test_ensure_failure_is_retried_later() -> None:
    with _temp_db() as db:
        f = _FakeFetcher(fail={"000002"})
        res = _ensure(db, f)
        assert res["failed"] == ["000002"]
        f.fail.clear()
        res = _ensure(db, f)
        assert res["fetched"] == 1
        assert f.calls == ["000001", "000002", "000002"]


def test_overlay_with_kiwoom_cache() -> None:
    with _temp_db() as db:
        _ensure(db, _FakeFetcher())
        src = _bulk()
        out = krd.apply_regular_session_overlay(src, _OFF, db_path=db, source="kiwoom")
        assert out is not src
        r = out.set_index(["code", out["time"].dt.strftime("%Y-%m-%d")])
        # 000001 9/14: 비율 1000/2000=0.5 → 종가 1050, 고가 1100, 저가 950
        assert r.loc[("000001", "2026-09-14"), "close"] == 1050
        assert r.loc[("000001", "2026-09-14"), "high"] == 1100
        assert r.loc[("000001", "2026-09-14"), "low"] == 950
        assert r.loc[("000001", "2026-09-15"), "close"] == 1025
        assert r.loc[("000002", "2026-09-14"), "close"] == 510
        # 시행 전 행·시가·거래량은 원본 유지, 원본 프레임 불변
        assert r.loc[("000001", "2026-09-11"), "close"] == 1000
        assert np.array_equal(out["open"].to_numpy(), src["open"].to_numpy())
        assert np.array_equal(out["volume"].to_numpy(), src["volume"].to_numpy())
        assert src.loc[1, "close"] == 1250


def test_on_returns_original() -> None:
    with _temp_db() as db:
        src = _bulk()
        assert krd.apply_regular_session_overlay(src, _ON, db_path=db, source="kiwoom") is src


def test_single_code_frame_with_datetime_index() -> None:
    with _temp_db() as db:
        _ensure(db, _FakeFetcher())
        src = _bulk()
        one = src[src["code"] == "000002"].drop(columns=["code"]).set_index("time")
        one.columns = [c.capitalize() for c in one.columns]
        out = krd.apply_regular_session_overlay(
            one, enabled=True, code="2", db_path=db, source="kiwoom"
        )
        assert list(out["Close"]) == [510, 505]
        assert list(out["High"]) == [520, 530]


def test_large_deviation_is_skipped() -> None:
    with _temp_db() as db:
        _ensure(db, _FakeFetcher())
        src = _bulk()
        # 원본 종가가 덧씌운 값(510)과 90% 넘게 차이 → 매칭 오류로 보고 원본 유지
        src.loc[3, "close"] = 1000
        out = krd.apply_regular_session_overlay(src, _OFF, db_path=db, source="kiwoom")
        assert out.loc[3, "close"] == 1000


def _insert_m15(path: str) -> None:
    """000001 의 9/14 15분봉 (로컬 DB, 애프터 봉 포함)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE bars_15m (code TEXT, datetime TEXT, open INTEGER, high INTEGER, "
        "low INTEGER, close INTEGER, volume INTEGER, PRIMARY KEY (code, datetime))"
    )
    conn.executemany(
        "INSERT INTO bars_15m VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("000001", "2026-09-14 09:00:00", 2000, 2200, 1950, 2100, 10),
            ("000001", "2026-09-14 09:15:00", 2100, 2150, 1900, 2050, 10),
            ("000001", "2026-09-14 15:30:00", 2100, 2100, 2100, 2100, 10),
            ("000001", "2026-09-14 16:00:00", 2100, 2400, 2100, 2300, 10),
        ],
    )
    conn.commit()
    conn.close()


def test_m15_source_uses_regular_session_bars_only() -> None:
    with _temp_db() as db:
        _insert_m15(db)
        src = _bulk()
        out = krd.apply_regular_session_overlay(src, _OFF, db_path=db, source="m15")
        r = out.set_index(["code", out["time"].dt.strftime("%Y-%m-%d")])
        assert r.loc[("000001", "2026-09-14"), "close"] == 1050
        assert r.loc[("000001", "2026-09-14"), "high"] == 1100
        assert r.loc[("000001", "2026-09-14"), "low"] == 950
        # 15분봉 없는 행은 원본 유지
        assert r.loc[("000001", "2026-09-15"), "close"] == 1200
        assert r.loc[("000002", "2026-09-14"), "close"] == 525


def test_target_days_and_quiet_time() -> None:
    days = krd.target_days("2026-09-10", "2026-09-16", now=datetime(2026, 9, 16, 15, 35))
    assert days == ["2026-09-14", "2026-09-15"]
    days = krd.target_days("2026-09-10", "2026-09-16", now=datetime(2026, 9, 16, 15, 45))
    assert days[-1] == "2026-09-16"
    assert not krd.is_prefetch_quiet_time(datetime(2026, 9, 16, 10, 0))
    assert krd.is_prefetch_quiet_time(datetime(2026, 9, 16, 8, 0))
    assert krd.is_prefetch_quiet_time(datetime(2026, 9, 16, 15, 45))
    assert krd.is_prefetch_quiet_time(datetime(2026, 9, 19, 11, 0))


if __name__ == "__main__":
    test_kiwoom_rows_aggregate_regular_session_only()
    test_ensure_caches_and_skips_refetch()
    test_ensure_failure_is_retried_later()
    test_overlay_with_kiwoom_cache()
    test_on_returns_original()
    test_single_code_frame_with_datetime_index()
    test_large_deviation_is_skipped()
    test_m15_source_uses_regular_session_bars_only()
    test_target_days_and_quiet_time()
    print("ok")
