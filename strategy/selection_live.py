# -*- coding: utf-8 -*-
"""실시간 감시 종목 선정 진입점 (select_monitoring_targets)."""
from __future__ import annotations

import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

import FinanceDataReader as fdr
import numpy as np
import pandas as pd

from data_manager.krx_regular_daily import (
    SOURCE_KIWOOM,
    SOURCE_M15,
    apply_regular_session_overlay,
    ensure_kiwoom_regular,
    regular_daily_overlay_enabled,
)
from strategy.box_reversion_selector import (
    BoxReversionSelectionConfig,
    evaluate_box_reversion_candidate,
)
from strategy.selection_errors import SelectionDbStaleError
from strategy.selection_dynamic_score import _selection_daily_lookback_calendar_days
from strategy.selection_live_aux import (
    _apply_sel_a_live_cross_section,
    _apply_sel_b_live_cross_section,
    _compute_sel_a_aux_from_daily_tail,
    _compute_sel_b_aux_from_daily_tail,
    _merge_dual_track_picks,
)
from strategy.selection_regime import (
    resolve_transition_gate_for_live,
)
from strategy.selection_scoring import (
    _add_vol_burst_ab_columns,
    _settings_bool_sel,
    calculate_technical_indicators,
)
from strategy.universe_codes import load_kospi_universe_codes, resolve_universe_codes


def select_monitoring_targets(
    settings: dict[str, Any],
    logger_callback: Callable[..., None],
    target_date: Optional[datetime] = None,
    data_manager: Any = None,
    *,
    selection_ohlcv_source: Literal["db_then_api", "api_only", "db_only"] = "api_only",
    kiwoom_access_token: Optional[str] = None,
) -> pd.DataFrame:
    """
    KOSPI 200을 스캔해 Track A(눌림)·B(추세)·C(박스 회귀)를 분리 선정합니다.
    Track C는 전용 TYPE_C 진입이 구현되기 전까지 관찰 대상으로만 감시되며,
    트랙 매칭 안전장치가 기존 TYPE_A/B 매수를 차단합니다.

    selection_ohlcv_source:
        - api_only (기본): 실시간 선정 시 trading_data.db 일봉 미사용.
          ``kiwoom_access_token`` 이 있으면 키움 REST ``ka10081`` 일봉,
          없으면 FDR **6자리 종목코드** 직접 조회(시작·종료일 명시).
          (``KRX:`` 접두사 경로는 KRX 포털 정책 변경으로 400/LOGOUT 이 잦아 사용하지 않음)
        - db_then_api: 실시간 선정 시 DB 일봉 우선 + 미갱신이면 SelectionDbStaleError.
          단, bars_1d 가 완전히 비어 있으면 신선도 검사만 생략하고 종목별로 DB→FDR 순으로 채움.
          (target_date 가 지정된 과거 선정은 data_manager 일봉 우선 유지)
        - db_only: 실시간 선정 시 **로컬 bars_1d 만** 사용(FDR 폴백 없음). 하베스팅 반영분과 1:1 비교용.
          일봉 종료일은 DB 최신 일자로 고정하며, bars_1d 가 비면 빈 결과로 종료합니다.
    """
    logger_callback("\n" + "="*50)
    t_str = target_date.strftime('%Y-%m-%d') if target_date else "실시간"
    logger_callback(f"[Select] [전략 종목 선정] ({t_str}) 분석 시작...")

    try:
        from strategy.universe_codes import normalize_universe_index_choice

        # KODEX200 구성 유니버스 폐기 — 항상 KOSPI200
        _ = normalize_universe_index_choice(settings.get("universe_index_choice"))
    except Exception:
        pass

    try:
        _src = str(selection_ohlcv_source or "api_only").strip().lower()
    except Exception:
        _src = "api_only"
    db_only_no_fdr: bool = _src in ("db_only", "db-only", "local_db_only")
    api_only_live: bool = (not db_only_no_fdr) and _src in (
        "api_only",
        "api",
        "fdr_only",
        "fdr",
    )
    _kiwoom_tok = (kiwoom_access_token or "").strip()
    if target_date is None and api_only_live:
        if _kiwoom_tok:
            logger_callback(
                "[Select] 실시간 일봉: 키움 REST ka10081 (trading_data.db 미사용)"
            )
        else:
            logger_callback(
                "[Select] 실시간 일봉: FDR 일봉(종목코드 직접, 토큰 없음, trading_data.db 미사용)"
            )
    elif target_date is None and db_only_no_fdr:
        logger_callback(
            "[Select] 실시간 일봉: 로컬 DB 전용(trading_data.db bars_1d, FDR 폴백 없음)"
        )

    # db_only: 일봉 조회 상한을 DB 최신 영업일로 맞춤(달력상 '오늘'과 불일치해도 EOD 기준 통일)
    _eod_anchor: Optional[datetime] = None
    if db_only_no_fdr and target_date is None and data_manager is not None:
        try:
            d_eod = data_manager.get_latest_daily_bar_date_in_db()
            if d_eod is not None:
                _eod_anchor = datetime(d_eod.year, d_eod.month, d_eod.day)
        except Exception:
            _eod_anchor = None

    # [v4.10] 실시간 + db_then_api: 일봉 신선도 검사 (api_only 는 스킵)
    # bars_1d 가 비어 있으면 검사 생략 — 미배포·빈 DB에서도 선정 가능(FDR 폴백)
    if target_date is None and data_manager is not None and not api_only_live:
        try:
            latest_1d = data_manager.get_latest_daily_bar_date_in_db()
        except Exception:
            latest_1d = None
        if latest_1d is None:
            logger_callback(
                "ℹ️ [선정] 로컬 bars_1d 가 비어 일봉 신선도 검사를 생략합니다. "
                "(하베스팅 전 DB만 있는 배포 환경에서도 FDR 폴백으로 진행)"
            )
            if db_only_no_fdr:
                logger_callback(
                    "⛔ [선정] db_only 모드는 bars_1d 일봉이 필요합니다. 하베스팅 후 다시 실행하세요."
                )
                return pd.DataFrame()
        else:
            try:
                ok, stale_msg = data_manager.check_daily_bars_fresh_for_selection()
            except Exception as ex:
                ok, stale_msg = False, f"일봉 신선도 검사 실패: {ex}"
            if not ok:
                logger_callback(f"⛔ [선정] {stale_msg}")
                logger_callback(
                    "⛔ [선정] 데이터 수집(하베스팅)을 먼저 완료한 뒤 종목 선정을 실행하세요."
                )
                raise SelectionDbStaleError(stale_msg)
    
    # 1. 대상 유니버스 확보 — KRX·pykrx 미사용
    tickers = []
    if target_date:
        dt_str = target_date.strftime("%Y%m%d")
        top_n = 200
        try:
            top_n = int(settings.get("kospi_top_n", 200) or 200)
        except Exception:
            top_n = 200
        try:
            tickers = resolve_universe_codes(
                settings,
                logger_callback=logger_callback,
                pit_asof=target_date,
                allow_fdr_generate=False,
            )
        except Exception as e:
            try:
                logging.warning("과거 선정 유니버스 로드 예외: %s", e, exc_info=True)
            except Exception:
                pass
            logger_callback(f"  [Warn] 유니버스 로드 오류: {e}")
            tickers = []
        if not tickers:
            listing = "KOSPI"
            logger_callback(f"  [Info] FDR StockListing({listing})에서 상위 종목을 추출합니다.")
            try:
                df_fdr = fdr.StockListing(listing)
                df_fdr["Code"] = df_fdr["Code"].astype(str)
                if listing == "KOSPI":
                    df_fdr = df_fdr[df_fdr["Code"].str.endswith("0")]
                m_col = "Marcap" if "Marcap" in df_fdr.columns else "MarketCap"
                if m_col not in df_fdr.columns:
                    m_col = df_fdr.columns[-1]
                tickers = (
                    df_fdr.sort_values(by=m_col, ascending=False)
                    .head(int(top_n))["Code"]
                    .astype(str)
                    .str.zfill(6)
                    .tolist()
                )
            except Exception as e:
                try:
                    logging.error("FDR %s 상위 fallback 실패: %s", listing, e, exc_info=True)
                except Exception:
                    pass
                logger_callback(f"  [Error] FDR fallback 실패: {e}")
        if not tickers:
            logger_callback(f"[Error] [{dt_str}] 구성 종목 리스트를 확보하지 못했습니다.")
            return pd.DataFrame()
        name_map: dict[str, str] = {}
        try:
            df_fdr_names = fdr.StockListing("KOSPI")
            name_map = dict(
                zip(
                    df_fdr_names["Code"].astype(str).str.zfill(6),
                    df_fdr_names["Name"],
                )
            )
        except Exception as e:
            try:
                logging.info("FDR 종목명 매핑 생략: %s", e)
            except Exception:
                pass
        df_top = pd.DataFrame(
            [{"Code": t, "Name": name_map.get(t, t)} for t in tickers]
        )
    else:
        # 실시간: KOSPI 시총 상위 N (KODEX200 구성 유니버스 폐기)
        input_file = "kospi_universe.csv"
        df_top = pd.DataFrame()
        top_n = 200
        try:
            top_n = int(settings.get("kospi_top_n", 200))
        except Exception:
            top_n = 200
        try:
            _ = load_kospi_universe_codes(
                logger_callback=logger_callback,
                top_n=top_n,
                allow_fdr_generate=False,
                daily_refresh=True,
                refresh_mode=str(settings.get("kospi_universe_refresh", "quarterly")),
            )

            if os.path.exists(input_file):
                df_top = pd.read_csv(input_file, dtype=str)
                if "종목코드" in df_top.columns:
                    df_top.rename(
                        columns={"종목코드": "Code", "종목명": "Name"},
                        inplace=True,
                    )
                if "Code" in df_top.columns and "Name" not in df_top.columns:
                    df_top["Name"] = df_top["Code"]
                df_top["Code"] = df_top["Code"].astype(str).str.zfill(6)
                # 유니버스는 파일 내 상위 리스트를 top_n으로 슬라이스
                df_top = df_top.head(int(top_n)).copy()
        except Exception as ex:
            logger_callback(f"⚠️ [선정] KOSPI200 csv 로드 실패, FDR 폴백: {ex}")

        if df_top is None or df_top.empty:
            # csv가 없거나 스키마가 깨진 경우(최후 수단)
            try:
                tickers = load_kospi_universe_codes(
                    logger_callback=logger_callback,
                    top_n=top_n,
                    allow_fdr_generate=False,
                    daily_refresh=True,
                    refresh_mode=str(settings.get("kospi_universe_refresh", "quarterly")),
                )
                df_fdr_names = fdr.StockListing("KOSPI")
                name_map = dict(
                    zip(
                        df_fdr_names["Code"].astype(str).str.zfill(6),
                        df_fdr_names["Name"],
                    )
                )
                df_top = pd.DataFrame(
                    [{"Code": t, "Name": name_map.get(t, t)} for t in tickers]
                )
            except Exception as ex2:
                logger_callback(f"[Error] KOSPI200 유니버스 구성 실패: {ex2}")
                df_top = pd.DataFrame()

        if df_top is None or df_top.empty:
            logger_callback("[Error] 구성 종목 리스트를 확보하지 못했습니다.")
            return pd.DataFrame()

    # [선정] 거래정지 제외 — 실시간 선정만(과거 일자 선정은 현재 정지 상태와 불일치할 수 있어 생략)
    if bool(settings.get("exclude_trading_halt_on_selection", True)) and target_date is None:
        try:
            from utils.naver_stock_halt import fetch_naver_halted_codes

            codes_all = df_top["Code"].astype(str).str.zfill(6).tolist()
            halted = fetch_naver_halted_codes(codes_all)
            if halted:
                n_before = len(df_top)
                df_top = df_top[
                    ~df_top["Code"].astype(str).str.zfill(6).isin(halted)
                ].copy()
                samp = ", ".join(sorted(halted)[:20])
                more = " …" if len(halted) > 20 else ""
                logger_callback(
                    f"[선정] 거래정지 {len(halted)}종 제외 (네이버 실시간 HALTED) — "
                    f"유니버스 {n_before} → {len(df_top)}"
                )
                logger_callback(f"  제외 코드: {samp}{more}")
            if df_top is None or df_top.empty:
                logger_callback("[Error] 거래정지 제외 후 유니버스가 비었습니다.")
                return pd.DataFrame()
        except Exception as e:
            try:
                logger_callback(f"⚠️ [선정] 거래정지 필터 생략: {e}")
            except Exception:
                pass

    min_val_krw = float(settings.get('min_trde_val', 40.0)) * 100000000
    box_c_config = BoxReversionSelectionConfig.from_mapping(settings)

    # 실시간(api_only)은 DB 일봉 미사용. 과거일(target_date) 선정은 백테스트 DB 유지.
    use_dm_daily = bool(
        data_manager is not None and (target_date is not None or not api_only_live)
    )
    # 정규장 일봉 기준값: 실시간은 키움 15분봉(캐시), DB 일봉 경로는 백테와 같은 로컬 15분봉
    regular_source = SOURCE_M15 if use_dm_daily else SOURCE_KIWOOM
    if regular_source == SOURCE_KIWOOM and regular_daily_overlay_enabled(settings):
        try:
            ensure_kiwoom_regular(
                df_top["Code"].astype(str).str.zfill(6).tolist(),
                data_manager=data_manager,
                log=logger_callback,
                progress_every=50,
            )
        except Exception as e:
            try:
                logger_callback(f"⚠️ [선정] 정규장 일봉(키움) 확보 실패 — 원본 일봉 사용: {e}")
            except Exception:
                pass

    def process_code(row):
        code, name = str(row['Code']).zfill(6), row['Name']
        status = {"total": 1, "data": 0, "value": 0, "trend": 0, "rsi": 0, "vol": 0}
        try:
            # 2. 데이터 확보 (DB 우선 -> FDR 폴백) — 실시간 api_only 는 FDR만
            df = None
            if use_dm_daily:
                try:
                    if target_date:
                        end_dt = target_date
                    elif db_only_no_fdr and _eod_anchor is not None:
                        end_dt = _eod_anchor
                    else:
                        end_dt = datetime.now()
                    end_s = end_dt.strftime('%Y-%m-%d')
                    _lb_db = _selection_daily_lookback_calendar_days(settings)
                    start_s = (end_dt - timedelta(days=_lb_db)).strftime('%Y-%m-%d')
                    df = data_manager.get_daily_bars(code, start_s, end_s)
                except Exception as _dm_e:
                    try:
                        logger_callback(f"  [Warn] {code} DB 일봉 조회 실패 → FDR 폴백: {_dm_e}")
                    except Exception:
                        pass
                    df = None

            if df is None or len(df) < 50:
                if db_only_no_fdr:
                    return None, status
                # 실시간 api_only: 키움 ka10081(토큰 있을 때) → 없으면 FDR
                try:
                    if target_date:
                        end_anchor = target_date
                    else:
                        from reporting.krx_trading_calendar import (
                            required_eod_bar_date_asof_local,
                        )

                        req_d = required_eod_bar_date_asof_local()
                        from utils.krx_session_hours import get_session_hours

                        end_anchor = get_session_hours(req_d).close_datetime()
                except Exception:
                    end_anchor = datetime.now()
                max_d = end_anchor.strftime('%Y-%m-%d')
                _lb_live = _selection_daily_lookback_calendar_days(settings)
                try:
                    start_s = (end_anchor - timedelta(days=_lb_live)).strftime('%Y-%m-%d')
                except Exception:
                    start_s = (
                        datetime.now() - timedelta(days=_lb_live)
                    ).strftime('%Y-%m-%d')
                if _kiwoom_tok:
                    try:
                        from core.kiwoom_daily_bars import fetch_kiwoom_daily_ohlcv_dataframe

                        try:
                            upd_tp = str(
                                settings.get("kiwoom_daily_upd_stkpc_tp", "1") or "1"
                            ).strip()
                        except Exception:
                            upd_tp = "1"
                        try:
                            _ki_min = float(
                                settings.get(
                                    "kiwoom_daily_min_request_interval_sec", 1.1
                                )
                            )
                        except (TypeError, ValueError):
                            _ki_min = 1.1
                        _ki_min = max(_ki_min, 0.9)
                        df = fetch_kiwoom_daily_ohlcv_dataframe(
                            code,
                            _kiwoom_tok,
                            min_date=start_s,
                            max_date=max_d,
                            upd_stkpc_tp=upd_tp,
                            min_request_interval_sec=_ki_min,
                            max_429_retries_per_page=int(
                                settings.get("kiwoom_daily_max_429_retries_per_page", 8)
                            ),
                            logger_callback=logger_callback,
                        )
                    except Exception as _k_err:
                        try:
                            logger_callback(
                                f"  [Warn] {code} 키움 일봉(ka10081) 조회 실패: {_k_err}"
                            )
                        except Exception:
                            pass
                        df = None
                    if df is None or len(df) < 50:
                        return None, status
                else:
                    # 과거에는 ``KRX:{code}`` 로 KRX 오픈API 일봉을 썼으나, 포털이 400+본문 LOGOUT 을
                    # 반환하는 경우가 많아(라이브러리 메시지는 'Period is up to 2 years' 로 오인 유발)
                    # **6자리 코드 + (시작, 종료)** 직접 조회로 통일한다.
                    # BT 배치와 동일: as-of 평가일 기준 고정 lookback(기본 280일).
                    lookbacks: Tuple[int, ...] = (_lb_live,)
                    df = None
                    last_err: Optional[BaseException] = None
                    for lb in lookbacks:
                        try:
                            lb_i = int(lb)
                        except (TypeError, ValueError):
                            continue
                        if lb_i < 60:
                            continue
                        try:
                            st = (end_anchor - timedelta(days=lb_i)).strftime('%Y-%m-%d')
                            df_try = fdr.DataReader(code, st, max_d)
                        except Exception as ex:
                            last_err = ex
                            try:
                                logger_callback(
                                    f"  [Warn] {code} FDR 일봉({lb_i}일) 조회 실패: {ex}"
                                )
                            except Exception:
                                pass
                            df = None
                            continue
                        if df_try is not None and len(df_try) >= 50:
                            df = df_try
                            break
                        df = None
                    if df is None or len(df) < 50:
                        try:
                            if last_err is not None:
                                logger_callback(
                                    f"  [Warn] {code} FDR 일봉 확보 실패(50거래일 미만): {last_err}"
                                )
                        except Exception:
                            pass
                        return None, status
            
            if df is None or len(df) < 50: return None, status
            
            # 컬럼명 표준화
            df.columns = [str(c).lower() for c in df.columns]
            if 'time' in df.columns: df.rename(columns={'time': 'datetime'}, inplace=True)
            # 애프터 정책 OFF → 애프터 시행일 이후 고가·저가·종가를 정규장 기준으로 (DB·키움·FDR 공통)
            df = apply_regular_session_overlay(
                df,
                settings,
                code=code,
                data_manager=data_manager,
                source=regular_source,
                log=logger_callback,
            )
            
            status["data"] = 1
            
            # 3. 기술적 지표 계산 (공용 함수 사용)
            df = calculate_technical_indicators(df, settings)
            
            df_valid = df.dropna(subset=['MA20', 'MA60', 'RSI', 'MACD']).copy()
            if len(df_valid) < 2: return None, status
            
            # 4. 필터링 준비: 5일 평균 대금 + Track A/B 분리 거래량 분출
            df_valid["AvgVal5"] = df_valid["TrdeVal"].rolling(5).mean()
            df_valid["OSC_Prev"] = df_valid["OSC"].shift(1)
            df_valid = _add_vol_burst_ab_columns(df_valid, settings)
            last_full = df_valid.iloc[-1]

            # 레짐·진단용 Valid 는 항상 계산. 감시 편입은 use_sel_track_* 로만 제한.
            aux = _compute_sel_a_aux_from_daily_tail(df_valid, last_full, settings)
            aux_b = _compute_sel_b_aux_from_daily_tail(df_valid, last_full, settings)
            aux_c = evaluate_box_reversion_candidate(
                df,
                code=code,
                name=str(name),
                config=box_c_config,
                as_of=target_date,
            )

            # v2 경로에서는 _valid_a/_valid_b 컬럼이 의미 없으므로 0으로 고정
            # (aux / aux_b update 가 실제 Valid·Score 를 채움)
            valid_a = False
            score_a_place = 0.0
            valid_b_place = False
            score_b_place = 0.0

            ok_row = (
                aux is not None
                or aux_b is not None
                or aux_c is not None
            )
            if not ok_row:
                return None, status

            pos_diff = (
                abs(last_full["close"] - last_full["MA20"]) / last_full["MA20"]
                if last_full["MA20"] > 0
                else 1.0
            )

            # 상세 상태(통계) — 하위 호환
            if last_full["AvgVal5"] >= min_val_krw:
                status["value"] = 1
            conv_stat = float(settings.get("ma_convergence", 3.0)) / 100.0
            if last_full["MA20"] > 0 and abs(last_full["MA5"] - last_full["MA20"]) / last_full[
                "MA20"
            ] <= conv_stat:
                status["trend"] = 1
            if float(settings.get("sel_rsi_min", 40.0)) <= last_full["RSI"] <= float(
                settings.get("sel_rsi_max", 70.0)
            ):
                status["rsi"] = 1
            if bool(last_full.get("VolBurstActive_A", False)) or bool(
                last_full.get("VolBurstActive_B", False)
            ):
                status["vol"] = 1

            res = {
                "Code": code,
                "Name": name,
                "Price": int(last_full["close"]),
                "Open": int(last_full["open"]),
                "Diff": int(last_full["close"] - df_valid.iloc[-2]["close"]),
                "Ratio": round(
                    (last_full["close"] / df_valid.iloc[-2]["close"] - 1) * 100, 2
                ),
                "RSI": round(last_full["RSI"], 1),
                "MA20Dist": round(pos_diff * 100, 2),
                "MA60Ratio": round((last_full["close"] / last_full["MA60"]) * 100, 2),
                "MA20": float(last_full["MA20"]),
                "MA60": float(last_full["MA60"]),
                "MACD": float(last_full["MACD"]),
                "MACD_Sig": float(last_full["MACD_Sig"]),
                "OSC": float(last_full["OSC"]),
                "OSC_Prev": float(last_full.get("OSC_Prev", last_full.get("OSC", 0)) or 0),
                "Volume": int(last_full["volume"]),
                "PrevVol": int(df_valid.iloc[-2]["volume"]),
                "VolMax": int(df_valid["volume"].max()),
                "MABufScale": 1.0,
                "AvgTrdeVal": int(last_full["AvgVal5"] / 100000000),
                # 점수 유동성 항목용 원 단위 (백테 배치와 동일 — 억 절사 오차 제거)
                "_sel_avg_val5": float(last_full["AvgVal5"]),
                "_valid_a": valid_a,
                "_score_a": score_a_place,
                "_valid_b": valid_b_place,
                "_score_b": score_b_place,
                "_valid_c": aux_c is not None,
                "_score_c": float(aux_c.get("Score", 0.0)) if aux_c else 0.0,
            }
            if aux:
                res.update(aux)
            if aux_b:
                res.update(aux_b)
            if aux_c:
                for key in (
                    "BoxLower",
                    "BoxUpper",
                    "BoxWidthPct",
                    "BoxPositionPct",
                    "ADX",
                    "MASlopePctPerDay",
                    "LowerTouches",
                    "UpperTouches",
                    "VolumeRatio",
                    "ATRExpansionRatio",
                ):
                    if key in aux_c:
                        res[key] = aux_c[key]
            return res, status
        except Exception:
            return None, status

    # 병렬 분석 (키움 실시간 일봉은 API 제한 완화를 위해 기본 스레드 수를 낮춤)
    try:
        default_workers = 2 if (api_only_live and bool(_kiwoom_tok) and target_date is None) else 10
        max_workers = int(settings.get("selection_max_workers", default_workers))
    except Exception:
        max_workers = 2 if (api_only_live and bool(_kiwoom_tok) and target_date is None) else 10
    if max_workers < 1:
        max_workers = 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        raw_results = list(executor.map(process_code, [row for _, row in df_top.iterrows()]))

    selected = [r[0] for r in raw_results if r[0] is not None]

    try:
        # 교차섹션은 감시 ON/OFF와 무관하게 Valid/Score 정합용으로 항상 적용
        selected = _apply_sel_a_live_cross_section(selected, settings)
    except Exception as e:
        try:
            logger_callback(f"⚠️ [선정] Track A v2 교차섹션 적용 실패: {e}")
        except Exception:
            pass
    try:
        selected = _apply_sel_b_live_cross_section(selected, settings)
    except Exception as e:
        try:
            logger_callback(f"⚠️ [선정] Track B v2 교차섹션 적용 실패: {e}")
        except Exception:
            pass

    # 통계 출력
    stats = {"total": 0, "data": 0, "value": 0, "trend": 0, "rsi": 0, "vol": 0}
    for _, s in raw_results:
        for k in stats:
            stats[k] += s.get(k, 0)

    logger_callback(
        f"[Stats] 분석: {stats['total']}, 대금: {stats['value']}, "
        f"수렴: {stats['trend']}, RSI: {stats['rsi']}, 분출: {stats['vol']}"
    )

    # [Transition] B-share 급락 + 변동성 레짐 게이트
    merge_max_a = None
    merge_max_b = None
    settings.pop("transition_runtime_regime", None)
    settings.pop("transition_runtime_block_s1", None)
    settings.pop("transition_runtime_block_s2", None)
    if _settings_bool_sel(settings, "use_transition_gate", False):
        try:
            _tg = resolve_transition_gate_for_live(
                settings,
                data_manager,
                target_date,
                logger_callback,
            )
            _reg = str(_tg.get("regime", "OFF"))
            settings["transition_runtime_regime"] = _reg
            settings["transition_runtime_block_s1"] = bool(_tg.get("block_s1", False))
            settings["transition_runtime_block_s2"] = bool(_tg.get("block_s2", False))
            merge_max_a = int(_tg.get("slots_a", 0))
            merge_max_b = int(_tg.get("slots_b", 0))
            if _reg == "HARD":
                logger_callback(
                    f"[Transition] HARD — 신규 감시 0 (S1·S2 차단)"
                )
        except Exception as e:
            try:
                logger_callback(f"⚠️ [Transition] 정책 적용 실패(기본 슬롯): {e}")
            except Exception:
                pass
            settings.pop("transition_runtime_regime", None)
            settings.pop("transition_runtime_block_s1", None)
            settings.pop("transition_runtime_block_s2", None)
    else:
        settings.pop("transition_runtime_regime", None)
        settings.pop("transition_runtime_block_s1", None)
        settings.pop("transition_runtime_block_s2", None)

    settings.pop("index_dd_runtime_block_s1", None)

    merged_rows = _merge_dual_track_picks(
        selected,
        settings,
        logger_callback,
        max_a=merge_max_a,
        max_b=merge_max_b,
    )
    df_result = pd.DataFrame(merged_rows)

    if not df_result.empty:
        # 캐시 저장: 실시간 + API/DB혼합 경로만 (db_only 는 CLI 등에서 별도 경로로 저장)
        if not target_date and not db_only_no_fdr:
            df_result.to_csv("selected_targets.csv", index=False, encoding="utf-8-sig")
    else:
        # 실시간 선정 0건(HARD 등): 이전 일자 CSV가 남아 재사용되는 것을 막음
        if not target_date and not db_only_no_fdr:
            try:
                pd.DataFrame(
                    columns=[
                        "Code",
                        "Name",
                        "Price",
                        "Open",
                        "Diff",
                        "Ratio",
                        "SelTrack",
                    ]
                ).to_csv("selected_targets.csv", index=False, encoding="utf-8-sig")
                logger_callback(
                    "💾 [선정] 0건 — selected_targets.csv 를 비웠습니다(이전 일자 목록 폐기)."
                )
            except Exception as _e:
                try:
                    logger_callback(f"⚠️ [선정] 빈 CSV 저장 실패: {_e}")
                except Exception:
                    pass

    logger_callback(
        f"[Done] 최종 {len(df_result)}개 종목 선정 완료. "
        f"(Track A·B·C 분리 상한 적용, 동적 백테 유니버스와 동일 로직)"
    )
    return df_result

