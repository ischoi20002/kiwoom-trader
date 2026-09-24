# -*- coding: utf-8 -*-
"""유니버스 종목코드 로드 (KOSPI 시총 상위 N).

KODEX200 구성 유니버스는 폐기됨. 레짐·지수 수익률용 KODEX200 ETF(069500)와는 별개.
``strategy_selector`` 에서 분리. 기존 호출은 strategy_selector re-export 로 호환.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Callable, Optional, Union

import FinanceDataReader as fdr
import pandas as pd


def normalize_universe_index_choice(_value: Any = None) -> str:
    """
    유니버스 인덱스 소스 정규화.

    KODEX200 구성종목 유니버스는 폐기되었으므로 항상 ``KOSPI200``(시총 상위 N)을 반환한다.
    """
    return "KOSPI200"


def load_kodex200_universe_codes(
    logger_callback: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """
    KRX·pykrx 미사용. 프로젝트 루트의 ``kodex200.csv``(및 대체 파일명)에서
    6자리 종목코드 리스트를 읽는다.
    """
    for f_name in ("kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"):
        p = f_name
        if not os.path.isfile(p):
            continue
        try:
            df_k = pd.read_csv(p, dtype=str)
        except (OSError, ValueError, pd.errors.EmptyDataError) as e:
            try:
                logging.info("kodex CSV 읽기 생략 %s: %s", f_name, e)
            except Exception:
                pass
            if logger_callback is not None:
                try:
                    logger_callback(f"  [Warn] {f_name} 읽기 실패: {e}")
                except Exception:
                    pass
            continue
        try:
            if "Code" in df_k.columns:
                c_col = "Code"
            elif "종목코드" in df_k.columns:
                c_col = "종목코드"
            else:
                c_col = None
            if c_col is None:
                continue
            tickers: list[str] = []
            for x in df_k[c_col].tolist():
                s = str(x).strip().zfill(6)
                if len(s) == 6 and s.isdigit():
                    tickers.append(s)
            if tickers:
                if logger_callback is not None:
                    try:
                        logger_callback(
                            f"  [Info] KRX 미사용 — 유니버스 {len(tickers)}종목 ({f_name})"
                        )
                    except Exception:
                        pass
                return tickers
        except Exception as e:
            try:
                logging.warning("kodex CSV 파싱 실패 %s: %s", f_name, e, exc_info=True)
            except Exception:
                pass
            if logger_callback is not None:
                try:
                    logger_callback(f"  [Warn] {f_name} 파싱 실패: {e}")
                except Exception:
                    pass
    return []


def _maybe_generate_kospi200_csv(
    logger_callback: Optional[Callable[[str], None]] = None,
    *,
    kospi200_path: str = "kospi200.csv",
    force: bool = False,
) -> bool:
    """
    `kospi200.csv`가 없거나 비었을 때만 FDR로 KOSPI 시총 상위 200을 생성해 저장합니다.

    생성 규칙(프로젝트 요청사항):
    - Code 끝이 '0' 인 종목만 사용
    - Marcap(없으면 MarketCap) 내림차순 정렬
    - 상위 200개만 Code/Name으로 저장
    """
    try:
        if os.path.exists(kospi200_path):
            try:
                # 비어있으면 재생성
                if os.path.getsize(kospi200_path) > 10:
                    return True
            except Exception:
                pass

        # 임시 수치(요청): KOSPI200 옵션을 'KOSPI150'로 축소
        target_n: int = 150

        # 기존 파일이 있어도 row 수가 다르면 재생성(예: 이전에 200으로 만들어진 경우)
        try:
            if os.path.exists(kospi200_path) and not force:
                try:
                    df_old = pd.read_csv(kospi200_path, dtype=str)
                    c_col = (
                        "Code"
                        if "Code" in df_old.columns
                        else ("종목코드" if "종목코드" in df_old.columns else None)
                    )
                    if c_col is not None:
                        n_old = len(df_old[c_col].dropna().tolist())
                        if int(n_old) == target_n:
                            return True
                except Exception:
                    pass
        except Exception:
            pass

        # FinanceDataReader는 프로젝트 전역 import(FDR)를 이미 사용 중
        df = fdr.StockListing("KOSPI")
        if df is None or getattr(df, "empty", True):
            return False

        df["Code"] = df["Code"].astype(str)
        df = df[df["Code"].str.endswith("0")].copy()

        m_col = "Marcap" if "Marcap" in df.columns else ("MarketCap" if "MarketCap" in df.columns else None)
        if m_col:
            df = df.sort_values(by=m_col, ascending=False)
        # 마켓캡 컬럼이 없으면 정렬 없이 상위 200(최후 수단)

        if "Name" not in df.columns:
            # FDR 스키마가 바뀌었을 경우 방어: 코드로만 저장
            df_out = df[["Code"]].copy()
            df_out["Name"] = df_out["Code"]
        else:
            df_out = df[["Code", "Name"]].copy()

        df_out["Code"] = df_out["Code"].astype(str).str.zfill(6)
        df_out = df_out[df_out["Code"].str.len() == 6]
        df_out = df_out.head(target_n)

        df_out.to_csv(kospi200_path, index=False, encoding="utf-8-sig")
        if logger_callback is not None:
            try:
                logger_callback(
                    f"  [Info] KOSPI200 csv 생성 완료: {kospi200_path} ({len(df_out)}종목)"
                )
            except Exception:
                pass
        return True
    except Exception as e:
        if logger_callback is not None:
            try:
                logger_callback(f"  [Warn] KOSPI200 csv 자동 생성 실패: {e}")
            except Exception:
                pass
        return False


def _maybe_generate_kospi_universe_csv(
    logger_callback: Optional[Callable[[str], None]] = None,
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    target_n: int = 400,
    force_refresh: bool = False,
    allow_fdr_generate: bool = True,
) -> list[str]:
    """
    `kospi_universe.csv`를 생성/확보합니다.

    ``allow_fdr_generate=False`` 이면 기존 파일만 읽고 FDR로 덮어쓰지 않습니다
    (백테 재현성·marcap PIT 경로용).
    """
    try:
        need_gen = False
        if force_refresh or not os.path.isfile(kospi_universe_path) or os.path.getsize(kospi_universe_path) <= 10:
            need_gen = True

        if not need_gen:
            # 기존 파일이 너무 작으면(예: 이전 실험에서 150으로 저장) 재생성
            try:
                df_old = pd.read_csv(kospi_universe_path, dtype=str)
                if "Code" in df_old.columns:
                    n_old = len(df_old["Code"].dropna().tolist())
                elif "종목코드" in df_old.columns:
                    n_old = len(df_old["종목코드"].dropna().tolist())
                else:
                    n_old = 0
                if int(n_old) < int(target_n):
                    need_gen = True
            except Exception:
                need_gen = True

        if need_gen and allow_fdr_generate:
            try:
                import FinanceDataReader as fdr
            except Exception:
                fdr = None  # type: ignore[assignment]

            if fdr is None:
                return []

            df = fdr.StockListing("KOSPI")
            if df is None or getattr(df, "empty", True):
                return []

            df["Code"] = df["Code"].astype(str)
            df = df[df["Code"].str.endswith("0")].copy()
            m_col = "Marcap" if "Marcap" in df.columns else ("MarketCap" if "MarketCap" in df.columns else None)
            if m_col:
                df = df.sort_values(by=m_col, ascending=False)

            if "Name" not in df.columns:
                df_out = df[["Code"]].copy()
                df_out["Name"] = df_out["Code"]
            else:
                df_out = df[["Code", "Name"]].copy()

            df_out["Code"] = df_out["Code"].astype(str).str.zfill(6)
            df_out = df_out[df_out["Code"].str.len() == 6]
            df_out = df_out.head(int(target_n))

            df_out.to_csv(kospi_universe_path, index=False, encoding="utf-8-sig")
            if logger_callback is not None:
                try:
                    logger_callback(
                        f"  [Info] KOSPI_universe csv 생성 완료: {kospi_universe_path} ({len(df_out)}종목)"
                    )
                except Exception:
                    pass
    except Exception:
        # 생성 실패해도, 이후 csv 로드 시도는 해봅니다.
        pass

    if not os.path.isfile(kospi_universe_path):
        return []

    try:
        df_k = pd.read_csv(kospi_universe_path, dtype=str)
    except Exception as e:
        if logger_callback is not None:
            try:
                logger_callback(f"  [Warn] kospi_universe.csv 읽기 실패: {e}")
            except Exception:
                pass
        return []

    try:
        if "Code" in df_k.columns:
            c_col = "Code"
        elif "종목코드" in df_k.columns:
            c_col = "종목코드"
        else:
            return []

        tickers: list[str] = []
        for x in df_k[c_col].tolist():
            s = str(x).strip().zfill(6)
            if len(s) == 6 and s.isdigit():
                tickers.append(s)
        return tickers
    except Exception:
        return []


def load_kospi_universe_codes(
    logger_callback: Optional[Callable[[str], None]] = None,
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    top_n: int = 200,
    force_refresh: bool = False,
    allow_fdr_generate: bool = True,
    pit_asof: Optional[Union[date, datetime, str, pd.Timestamp]] = None,
    daily_refresh: bool = False,
    refresh_mode: Optional[str] = None,
) -> list[str]:
    """
    KOSPI 시총 상위 top_n 종목코드.

    - ``pit_asof`` 지정 시: marcap 과거 시점 as-of (FDR/CSV 무시)
    - ``daily_refresh=True``: 실시간용 ``kospi_universe_refresh`` 주기 marcap as-of (CSV 갱신)
    - 그 외: ``kospi_universe.csv`` (없으면 당기 marcap → FDR 폴백)
    """
    if pit_asof is not None:
        try:
            from core.marcap_universe import load_kospi_top_n_asof

            codes, asof_str, _ = load_kospi_top_n_asof(
                pit_asof, int(top_n), logger_callback=logger_callback
            )
            if codes and logger_callback is not None and asof_str:
                try:
                    logger_callback(
                        f"  [Marcap] as-of {asof_str} KOSPI top{int(top_n)} → {len(codes)}종목"
                    )
                except Exception:
                    pass
            return codes
        except Exception as e:
            try:
                logging.warning("marcap PIT 유니버스 실패: %s", e, exc_info=True)
            except Exception:
                pass
            if logger_callback is not None:
                try:
                    logger_callback(f"  [Warn] marcap PIT 유니버스 실패: {e}")
                except Exception:
                    pass
            return []

    if daily_refresh and pit_asof is None:
        try:
            from core.marcap_universe import (
                ensure_realtime_kospi_universe_csv,
                normalize_refresh_mode,
            )

            mode = normalize_refresh_mode(refresh_mode or "quarterly")
            codes, asof_str = ensure_realtime_kospi_universe_csv(
                kospi_universe_path=kospi_universe_path,
                target_n=max(400, int(top_n)),
                logger_callback=logger_callback,
                refresh_mode=mode,
            )
            if codes:
                if logger_callback is not None and asof_str:
                    try:
                        logger_callback(
                            f"  [Marcap] 실시간 유니버스({mode}) as-of {asof_str} → {len(codes)}종목"
                        )
                    except Exception:
                        pass
                return codes[: int(top_n)]
        except Exception as e:
            try:
                logging.info("실시간 marcap 갱신 생략: %s", e)
            except Exception:
                pass

    if force_refresh or not os.path.isfile(kospi_universe_path) or os.path.getsize(kospi_universe_path) <= 10:
        try:
            from core.marcap_universe import (
                normalize_refresh_mode,
                refresh_kospi_universe_csv_from_marcap,
            )

            mode = normalize_refresh_mode(refresh_mode or "quarterly")
            refreshed = refresh_kospi_universe_csv_from_marcap(
                kospi_universe_path=kospi_universe_path,
                target_n=max(400, int(top_n)),
                logger_callback=logger_callback,
                refresh_mode=mode,
            )
            if refreshed:
                return refreshed[: int(top_n)]
        except Exception as e:
            try:
                logging.info("marcap 최신 kospi_universe 갱신 생략: %s", e)
            except Exception:
                pass

    codes = _maybe_generate_kospi_universe_csv(
        logger_callback=logger_callback,
        kospi_universe_path=kospi_universe_path,
        target_n=max(400, int(top_n)),
        force_refresh=force_refresh,
        allow_fdr_generate=allow_fdr_generate,
    )
    if not codes:
        return []
    return codes[: int(top_n)]


def resolve_universe_codes(
    settings: Optional[dict],
    logger_callback: Optional[Callable[[str], None]] = None,
    *,
    pit_asof: Optional[Union[date, datetime, str]] = None,
    daily_refresh: bool = False,
    allow_fdr_generate: bool = True,
) -> list[str]:
    """
    감시·백테 유니버스 코드 반환 (항상 KOSPI 시총 상위 N).

    ``universe_index_choice`` 의 KODEX200 값은 무시한다(폐기).
    """
    s = settings if isinstance(settings, dict) else {}
    choice = normalize_universe_index_choice(s.get("universe_index_choice"))
    refresh = str(s.get("kospi_universe_refresh", "quarterly") or "quarterly")

    if choice == "KOSPI200":
        try:
            top_n = int(s.get("kospi_top_n", 200) or 200)
        except Exception:
            top_n = 200
        return load_kospi_universe_codes(
            logger_callback=logger_callback,
            top_n=top_n,
            refresh_mode=refresh,
            pit_asof=pit_asof,
            daily_refresh=daily_refresh,
            allow_fdr_generate=allow_fdr_generate,
        )

    # 방어: normalize 가 항상 KOSPI200 이지만 타입 체커·향후 확장 대비
    return load_kospi_universe_codes(
        logger_callback=logger_callback,
        top_n=200,
        refresh_mode=refresh,
        pit_asof=pit_asof,
        daily_refresh=daily_refresh,
        allow_fdr_generate=allow_fdr_generate,
    )

