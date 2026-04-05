# -*- coding: utf-8 -*-
"""
금융위원회 KRX 상장종목정보 오픈API (공공데이터포털) 클라이언트.

- 서비스: GetKrxListedInfoService / getItemInfo
- 인증: 환경변수 DATA_GO_KR_SERVICE_KEY 또는 config.DATA_GO_KR_SERVICE_KEY

정책 (하베스팅·DB):
- 최신 공식 목록에 없어진 종목은 DB의 봉 데이터를 지우지 않습니다 (백테스트·감사용 보존).
- `listing_universe_state`에서 state=EXITED로만 전환하고, 하베스팅 루프에서는 ACTIVE 목록만 갱신 대상으로 삼습니다.

목록 조회:
- 휴장일/미갱신을 이유로 basDt를 하루씩 재시도하지 않습니다.
- 기본은 beginBasDt~endBasDt 구간으로 조회한 뒤, 응답의 basDt 중 최대값(가장 최근 공시일)만 사용합니다.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

# 공공데이터 API (JSON)
_DEFAULT_URL = (
    "https://apis.data.go.kr/1160100/service/GetKrxListedInfoService/getItemInfo"
)


def _default_bas_dt_window_days() -> int:
    """최신 스냅샷 탐색용 조회 구간(일). 장기 연휴·미갱신 대비 여유."""
    return 120


def _normalize_items(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """response.body.items.item 을 항상 list[dict]로 만듭니다."""
    try:
        items = (body or {}).get("items")
        if not items:
            return []
        it = items.get("item")
        if it is None:
            return []
        if isinstance(it, dict):
            return [it]
        if isinstance(it, list):
            return it
    except Exception:
        pass
    return []


def _parse_page_json(data: Dict[str, Any]) -> tuple:
    """(rows, total_count, result_msg) — 오류 시 ([], 0, msg)."""
    try:
        resp = data.get("response") or data
        head = (resp.get("header") or {})
        code = str(head.get("resultCode", "")).strip()
        msg = str(head.get("resultMsg", "")).strip()
        if code and code != "00":
            return [], 0, msg or f"API resultCode={code}"
        body = resp.get("body") or {}
        total = int(body.get("totalCount") or 0)
        rows = _normalize_items(body)
        return rows, total, msg or "OK"
    except Exception as e:
        return [], 0, str(e)


def fetch_krx_listed_items_page(
    service_key: str,
    *,
    page_no: int = 1,
    num_of_rows: int = 1000,
    bas_dt: Optional[str] = None,
    begin_bas_dt: Optional[str] = None,
    end_bas_dt: Optional[str] = None,
    url: str = _DEFAULT_URL,
    timeout: float = 45.0,
) -> tuple:
    """
    단일 페이지 조회.
    - bas_dt 가 있으면 해당 일자 단일 조회(basDt).
    - begin_bas_dt·end_bas_dt 가 있으면 구간 조회(최신 스냅샷 선별은 호출측에서 처리).
    Returns
    -------
    (list of row dict, total_count, error_message)
    error_message 가 비어 있지 않으면 실패.
    """
    if not (service_key or "").strip():
        return [], 0, "serviceKey 비어 있음"

    params: Dict[str, Any] = {
        "serviceKey": service_key.strip(),
        "numOfRows": int(num_of_rows),
        "pageNo": int(page_no),
        "resultType": "json",
    }
    if begin_bas_dt and end_bas_dt:
        params["beginBasDt"] = begin_bas_dt.strip()
        params["endBasDt"] = end_bas_dt.strip()
    elif bas_dt:
        params["basDt"] = bas_dt.strip()
    else:
        end_d = datetime.now().date()
        begin_d = end_d - timedelta(days=_default_bas_dt_window_days())
        params["beginBasDt"] = begin_d.strftime("%Y%m%d")
        params["endBasDt"] = end_d.strftime("%Y%m%d")
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return [], 0, f"HTTP 오류: {e}"
    except ValueError as e:
        return [], 0, f"JSON 파싱 실패: {e}"

    rows, total, msg = _parse_page_json(data)
    if not rows and total == 0 and msg != "OK":
        return [], 0, msg
    return rows, total, ""


def _rows_latest_bas_dt_only(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """응답 행들 중 basDt 문자열이 최대인 날짜의 행만 남깁니다. basDt 없으면 원본 유지."""
    if not rows:
        return rows
    dates: List[str] = []
    for r in rows:
        b = str(r.get("basDt") or "").strip()
        if len(b) == 8 and b.isdigit():
            dates.append(b)
    if not dates:
        return rows
    max_bas = max(dates)
    return [r for r in rows if str(r.get("basDt") or "").strip() == max_bas]


def fetch_kospi_official_dataframe(
    service_key: Optional[str] = None,
    *,
    bas_dt: Optional[str] = None,
    num_of_rows: int = 1000,
    url: str = _DEFAULT_URL,
) -> pd.DataFrame:
    """
    KOSPI 구분(mrktCtg)만 모아 Code, Name, basDt, mrktCtg DataFrame 반환.
    service_key 가 없으면 환경변수·config 순으로 시도합니다.

    bas_dt 가 None 이면: beginBasDt~endBasDt 로 **한 번** 구간 조회 후,
    응답에 나온 basDt 중 **가장 최근** 일자만 사용 (휴장일별 basDt 재시도 없음).
    bas_dt 를 지정하면 해당 일자 단일 조회.
    """
    key = (service_key or "").strip() or os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        try:
            import config as _cfg

            key = str(getattr(_cfg, "DATA_GO_KR_SERVICE_KEY", "") or "").strip()
        except Exception:
            key = ""

    if not key:
        raise ValueError(
            "공공데이터포털 serviceKey가 없습니다. "
            "환경변수 DATA_GO_KR_SERVICE_KEY 또는 config.DATA_GO_KR_SERVICE_KEY 를 설정하세요."
        )

    all_rows: List[Dict[str, Any]] = []
    page = 1
    total = None
    begin_r: Optional[str] = None
    end_r: Optional[str] = None
    single_bd: Optional[str] = None
    if bas_dt:
        single_bd = str(bas_dt).strip()
    else:
        end_d = datetime.now().date()
        begin_d = end_d - timedelta(days=_default_bas_dt_window_days())
        begin_r = begin_d.strftime("%Y%m%d")
        end_r = end_d.strftime("%Y%m%d")

    while True:
        rows, total_count, err = fetch_krx_listed_items_page(
            key,
            page_no=page,
            num_of_rows=num_of_rows,
            bas_dt=single_bd,
            begin_bas_dt=begin_r,
            end_bas_dt=end_r,
            url=url,
        )
        if err:
            raise RuntimeError(f"KRX 상장종목 API 오류: {err}")
        if total is None:
            total = total_count
        all_rows.extend(rows)
        if not rows:
            break
        if len(all_rows) >= total_count:
            break
        page += 1
        if page > max(3, int(math.ceil(total_count / max(1, num_of_rows))) + 2):
            break

    if not bas_dt:
        all_rows = _rows_latest_bas_dt_only(all_rows)

    out: List[Dict[str, str]] = []
    fallback_bas = single_bd or ""
    if all_rows and not bas_dt:
        try:
            fallback_bas = str(all_rows[0].get("basDt") or "").strip()
        except Exception:
            fallback_bas = ""

    for row in all_rows:
        try:
            mkt = str(row.get("mrktCtg") or row.get("mrktId") or "").strip().upper()
            if mkt != "KOSPI":
                continue
            sc = str(row.get("srtnCd") or "").strip()
            if not sc.isdigit():
                continue
            code = sc.zfill(6)
            name = str(row.get("itmsNm") or row.get("itmsNm1") or "").strip()
            bdt = str(row.get("basDt") or fallback_bas).strip()
            out.append(
                {
                    "Code": code,
                    "Name": name,
                    "basDt": bdt,
                    "mrktCtg": mkt,
                }
            )
        except Exception:
            continue

    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.drop_duplicates(subset=["Code"], keep="last").reset_index(drop=True)


def describe_delisting_policy() -> str:
    """사용자 안내용 짧은 정책 문구."""
    return (
        "최신 공식 목록에 없는 종목: DB 봉 데이터는 유지하고, "
        "listing_universe_state.state=EXITED 로만 표시합니다. "
        "하베스팅은 ACTIVE 목록(및 지수)만 순회합니다."
    )
