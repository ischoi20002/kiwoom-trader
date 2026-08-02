import os
import logging
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import date, datetime, timedelta
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union


class SelectionDbStaleError(Exception):
    """로컬 일봉 DB가 최신 장마감까지 갱신되지 않아 실시간 종목 선정을 중단할 때."""


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
) -> list[str]:
    """
    KOSPI 시총 상위 top_n 종목코드.

    - ``pit_asof`` 지정 시: marcap 과거 시점 as-of (FDR/CSV 무시)
    - ``daily_refresh=True``: 실시간용 당월 첫 영업일 marcap as-of (월 1회 CSV 갱신)
    - 그 외: ``kospi_universe.csv`` (없으면 당월 marcap → FDR 폴백)
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
            from core.marcap_universe import ensure_realtime_kospi_universe_csv

            codes, asof_str = ensure_realtime_kospi_universe_csv(
                kospi_universe_path=kospi_universe_path,
                target_n=max(400, int(top_n)),
                logger_callback=logger_callback,
            )
            if codes:
                if logger_callback is not None and asof_str:
                    try:
                        logger_callback(
                            f"  [Marcap] 실시간 유니버스(월별) as-of {asof_str} → {len(codes)}종목"
                        )
                    except Exception:
                        pass
                return codes[: int(top_n)]
        except Exception as e:
            try:
                logging.info("실시간 marcap 일일 갱신 생략: %s", e)
            except Exception:
                pass

    if force_refresh or not os.path.isfile(kospi_universe_path) or os.path.getsize(kospi_universe_path) <= 10:
        try:
            from core.marcap_universe import refresh_kospi_universe_csv_from_marcap

            refreshed = refresh_kospi_universe_csv_from_marcap(
                kospi_universe_path=kospi_universe_path,
                target_n=max(400, int(top_n)),
                logger_callback=logger_callback,
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


def calculate_rsi(series, period=14):
    """
    RSI(Relative Strength Index)를 계산합니다.
    """
    delta = series.diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    # ewm을 사용한 매끄러운 계산 (키움/시스템 트레이딩 방식)
    ema_up = up.ewm(com=period-1, adjust=False).mean()
    ema_down = down.ewm(com=period-1, adjust=False).mean()
    rs = ema_up / (ema_down + 1e-10)
    return 100 - (100 / (1+rs))

# =============================================================================
# [v18.5] 공용 전략 로직 (대시보드 & 백테스팅 공용)
# =============================================================================

def calculate_technical_indicators(df, settings):
    """
    공통 기술적 지표 계산 (MA, RSI, MACD, BB)
    df는 최소한 'close', 'volume' 컬럼을 포함해야 함 (소문자 기준)
    """
    # 기본 이평선
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    df['MA60'] = df['close'].rolling(60).mean()
    
    
    # 거래대금
    df['TrdeVal'] = df['close'] * df['volume']
    
    # RSI
    df['RSI'] = calculate_rsi(df['close'], 14)
    
    # MACD
    m_f = int(settings.get('macd_fast_daily', 12))
    m_s = int(settings.get('macd_slow_daily', 26))
    m_sg = int(settings.get('macd_sig_daily', 9))
    df['EMA_Fast'] = df['close'].ewm(span=m_f, adjust=False).mean()
    df['EMA_Slow'] = df['close'].ewm(span=m_s, adjust=False).mean()
    df['MACD'] = df['EMA_Fast'] - df['EMA_Slow']
    df['MACD_Sig'] = df['MACD'].ewm(span=m_sg, adjust=False).mean()
    df['OSC'] = df['MACD'] - df['MACD_Sig']
    
    return df

def get_strategy_score(row, settings):
    """
    [Backtest v30.0 Priority] 단일 행(시점)에 대한 전략 점수 및 필터링 결과 반환
    """
    # 1. 환경 변수 로드
    min_억 = float(settings.get('min_trde_val', 40.0))
    conv_limit = float(settings.get('ma_convergence', 5.0)) / 100.0
    sel_rsi_min = float(settings.get('sel_rsi_min', 30.0))
    sel_rsi_max = float(settings.get('sel_rsi_max', 70.0))
    
    # 2. 필터링 판정 (Status Tracking)
    res = {"valid": False, "score": 0.0, "details": {}}
    
    # 2-1. 거래대금 (5일 평균 사용 여부는 호출부에서 판단하도록 유연성 부여, 여기선 row값 기준)
    # 실제 select_monitoring_targets에서는 tail(5).mean()을 사용하므로, 
    # row에 이미 계산된 'AvgVal5'가 있다고 가정하거나 직접 전달받아야 함.
    avg_val = row.get('AvgVal5', row.get('TrdeVal', 0))
    c1 = avg_val >= (min_억 * 100000000)
    
    # 2-2. 이평 수렴도 (Dual-Track Selection Logic v30.9)
    diff_rate = abs(row['MA5'] - row['MA20']) / row['MA20'] if row['MA20'] > 0 else 1.0
    c2_converged = (diff_rate <= conv_limit) if settings.get('use_sel_track_a', True) else False
    c2_trending = ((row['MA20'] > row['MA60']) and (row['close'] > row['MA20'])) if settings.get('use_sel_track_b', True) else False
    c2 = c2_converged or c2_trending
    
    # 2-3. RSI 범위
    c3 = (sel_rsi_min <= row['RSI'] <= sel_rsi_max)
    
    # 2-4. 거래량 분출 (VolBurst10 등 호출부 전달 필요)
    c4 = row.get('VolBurstActive', False)
    
    # 2-5. MACD/OSC 필터 (Hybrid v26.15: 양수 필터 + 우상향 가산점)
    osc_prev = row.get('OSC_Prev', row.get('OSC', 0))
    # 2-5. 일봉 MACD 필터 (OSC 양수 고정)
    c5 = (row['OSC'] > 0) 
    
    if not (c1 and c2 and c3 and c4 and c5):
        return res

    # 3. 점수 산정
    bonus = 0.0
    if row['OSC'] > osc_prev: bonus += 10.0   # 우상향 시 가산점
    
    avg_val_억 = avg_val / 100000000
    s1 = min(100, (row['RSI'] / 70) * 100) # 탄성 (RSI가 높을수록 가점)
    s2 = max(0, (1 - (diff_rate / conv_limit)) * 100) # 밀집도 (수렴할수록 가점)
    s3 = min(100, (avg_val_억 / 500) * 100) # 대금 가점 (500억 기준)
    
    pos_diff = abs(row['close'] - row['MA20']) / row['MA20'] if row['MA20'] > 0 else 1.0
    s4 = max(0, (1 - (pos_diff / 0.03)) * 100) # 20일선 근접도 (3%이내 가점)
    
    total_score = round((s2 * 0.4) + (s1 * 0.3) + (s3 * 0.2) + (s4 * 0.1) + bonus, 1)
    
    res.update({"valid": True, "score": total_score, "pos_diff": pos_diff})
    return res


def _add_vol_burst_ab_columns(df_valid: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """
    prepare_dynamic_universe 와 동일: Track A/B 각각 거래량 분출(배율·룩백 분리).
    """
    vol_ratio_a = float(settings.get("vol_ratio", 1.5))
    lookback_a = int(settings.get("vol_lookback", 10))
    df_valid = df_valid.copy()
    df_valid["VolRatio"] = df_valid["volume"] / df_valid["volume"].shift(1)
    df_valid["VolBurst_A"] = (df_valid["VolRatio"] >= vol_ratio_a) & (
        df_valid["close"] > df_valid["close"].shift(1)
    )
    df_valid["VolBurstActive_A"] = df_valid["VolBurst_A"].rolling(lookback_a).max().astype(bool)
    vol_ratio_b = float(settings.get("sel_b_vol_ratio", vol_ratio_a))
    lookback_b = int(settings.get("sel_b_vol_lookback", lookback_a))
    df_valid["VolBurst_B"] = (df_valid["VolRatio"] >= vol_ratio_b) & (
        df_valid["close"] > df_valid["close"].shift(1)
    )
    df_valid["VolBurstActive_B"] = df_valid["VolBurst_B"].rolling(lookback_b).max().astype(bool)
    return df_valid


def _settings_bool_sel(settings: dict, key: str, default: bool = False) -> bool:
    try:
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return int(v) != 0
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("true", "1", "yes", "on"):
                return True
            if t in ("false", "0", "no", "off", ""):
                return False
        return bool(v) if v is not None else default
    except Exception:
        return default


def _sel_b_ma5_close_mult(settings: dict) -> float:
    """
    Track B ``종가 ≥ MA5`` 토글 ON일 때 ``lc >= MA5 * 반환값`` 에 쓰는 배율.
    ``(1 - sel_b_ma5_eps_pct) + sel_b_ma5_min_excess_ratio`` — 후자가 0이면 기존과 동일.
    (v2 다른 필터만으로는 이미 MA5 근처 조건이 충족되는 경우가 많아, 추가 이격으로 체감 가능.)
    """
    try:
        eps = float(settings.get("sel_b_ma5_eps_pct", 0.005))
        ex = float(settings.get("sel_b_ma5_min_excess_ratio", 0.0))
    except (TypeError, ValueError):
        eps, ex = 0.005, 0.0
    try:
        return max(1e-9, (1.0 - eps) + ex)
    except Exception:
        return max(1e-9, 1.0 - 0.005)


def _sel_score_mode(settings: Optional[dict]) -> str:
    """선정 Score 모드: ``legacy`` | ``fwd_aligned_v1`` | ``s1_pnl_aligned_v1`` | ``b_s1_pnl_aligned_v1``."""
    try:
        m = str((settings or {}).get("sel_score_mode", "legacy") or "legacy").strip().lower()
        if m in ("fwd_aligned_v1", "fwd_aligned", "aligned"):
            return "fwd_aligned_v1"
        if m in ("b_s1_pnl_aligned_v1", "b_only_s1", "b_s1_aligned"):
            return "b_s1_pnl_aligned_v1"
        if m in ("s1_pnl_aligned_v1", "s1_pnl", "s1_aligned"):
            return "s1_pnl_aligned_v1"
        return "legacy"
    except Exception:
        return "legacy"


def _use_score_a_s1_pnl_aligned(mode: str) -> bool:
    """Track A에 S1손익 정렬 스코어를 쓸지."""
    return mode == "s1_pnl_aligned_v1"


def _use_score_b_s1_pnl_aligned(mode: str) -> bool:
    """Track B에 S1손익 정렬 스코어를 쓸지 (B-only 모드 포함)."""
    return mode in ("s1_pnl_aligned_v1", "b_s1_pnl_aligned_v1")


def _sel_s1_pnl_signs(settings: Optional[dict]) -> Dict[str, float]:
    """S1 손익 정렬용 피처 부호 (+1=높을수록 유리, -1=낮을수록 유리)."""
    try:
        raw = (settings or {}).get("sel_s1_pnl_signs")
        if isinstance(raw, dict) and raw:
            out: Dict[str, float] = {}
            for k, v in raw.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    out[str(k)] = 0.0
            return out
    except Exception:
        pass
    return {}


def _signed_band_score(val: Any, lo: float, hi: float, sign: float) -> float:
    """sign>0: 높을수록 고점, sign<0: 낮을수록 고점, sign=0: 중립 50."""
    try:
        s = float(sign)
    except Exception:
        s = 0.0
    if abs(s) < 1e-12:
        return 50.0
    if s > 0:
        return _band_high_is_better(val, lo, hi)
    return _band_low_is_better(val, lo, hi)


def _score_a_s1_pnl_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """Valid_A 순위 — S1 체결 손익 상관 부호(sel_s1_pnl_signs) 반영."""
    try:
        signs = _sel_s1_pnl_signs(settings)
        if not signs or all(abs(float(v)) < 1e-12 for v in signs.values()):
            return _score_a_fwd_aligned_series(df, settings)
        pb_lo = float(settings.get("sel_a_pb_min_pct", 5.0))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.01))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.35))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.8))
        parts: List[pd.Series] = []
        weights: List[float] = []

        def _add(series: pd.Series, w: float) -> None:
            parts.append(series.fillna(50.0))
            weights.append(abs(float(w)))

        if abs(float(signs.get("a_pb", 0))) > 0:
            pb = pd.to_numeric(df.get("_sel_pb_pct"), errors="coerce")
            _add(
                pb.apply(lambda x: _signed_band_score(x, pb_lo, pb_hi, signs.get("a_pb", 0))),
                0.30,
            )
        if abs(float(signs.get("a_atr", 0))) > 0:
            atr = pd.to_numeric(df.get("_sel_atr_pct"), errors="coerce")
            _add(
                atr.apply(lambda x: _signed_band_score(x, atr_lo, atr_hi, signs.get("a_atr", 0))),
                0.20,
            )
        if abs(float(signs.get("a_ma_diff", 0))) > 0 and "MA_Diff" in df.columns:
            conv = max(float(settings.get("ma_convergence", 5.0)), 1e-6)
            md = pd.to_numeric(df["MA_Diff"], errors="coerce").abs()
            # sign+: 큰 MA_Diff 유리 → md/conv, sign-: 작은 유리
            if float(signs.get("a_ma_diff", 0)) > 0:
                ma_s = (md / conv).clip(0.0, 1.0) * 100.0
            else:
                ma_s = (1.0 - (md / conv)).clip(0.0, 1.0) * 100.0
            _add(ma_s, 0.20)
        if abs(float(signs.get("a_liq", 0))) > 0:
            if "AvgVal5" in df.columns:
                liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
                liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
            else:
                liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
                liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
            if float(signs.get("a_liq", 0)) < 0:
                liq_s = 100.0 - liq_s
            _add(liq_s, 0.15)
        if abs(float(signs.get("a_rs", 0))) > 0 and "_sel_rs_pct" in df.columns:
            rs = pd.to_numeric(df["_sel_rs_pct"], errors="coerce")
            _add(
                rs.apply(lambda x: _signed_band_score(x, rk_lo, rk_hi, signs.get("a_rs", 0))),
                0.15,
            )
        if not parts:
            return _score_a_fwd_aligned_series(df, settings)
        wsum = sum(weights) or 1.0
        sc = sum(p * (w / wsum) for p, w in zip(parts, weights))
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _score_b_s1_pnl_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """Valid_B 순위 — S1 체결 손익 상관 부호 반영."""
    try:
        signs = _sel_s1_pnl_signs(settings)
        if not signs or all(abs(float(v)) < 1e-12 for v in signs.values()):
            return _score_b_fwd_aligned_series(df, settings)
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        parts: List[pd.Series] = []
        weights: List[float] = []

        def _add(series: pd.Series, w: float) -> None:
            parts.append(series.fillna(50.0))
            weights.append(abs(float(w)))

        if abs(float(signs.get("b_rs", 0))) > 0 and "_sel_b_rs_pct" in df.columns:
            rs = pd.to_numeric(df["_sel_b_rs_pct"], errors="coerce").fillna(0.0)
            if float(signs.get("b_rs", 0)) > 0:
                rs_s = rs * 100.0
            else:
                rs_s = (1.0 - rs) * 100.0
            _add(rs_s, 0.30)
        if abs(float(signs.get("b_hi", 0))) > 0:
            hip = pd.to_numeric(df.get("_sel_b_hi_pct"), errors="coerce")
            _add(
                hip.apply(lambda x: _signed_band_score(x, hi_lo, hi_hi, signs.get("b_hi", 0))),
                0.25,
            )
        if abs(float(signs.get("b_atr", 0))) > 0:
            atr = pd.to_numeric(df.get("_sel_b_atr_pct"), errors="coerce")
            _add(
                atr.apply(lambda x: _signed_band_score(x, atr_lo, atr_hi, signs.get("b_atr", 0))),
                0.15,
            )
        if abs(float(signs.get("b_liq", 0))) > 0:
            if "AvgVal5" in df.columns:
                liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
                liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
            else:
                liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
                liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
            if float(signs.get("b_liq", 0)) < 0:
                liq_s = 100.0 - liq_s
            _add(liq_s, 0.15)
        if abs(float(signs.get("b_osc", 0))) > 0:
            osc = pd.to_numeric(df.get("OSC", 0), errors="coerce").fillna(0.0)
            # 교차섹션 분위
            try:
                osc_pct = osc.rank(pct=True)
            except Exception:
                osc_pct = (osc > 0).astype(float)
            if float(signs.get("b_osc", 0)) > 0:
                osc_s = osc_pct * 100.0
            else:
                osc_s = (1.0 - osc_pct) * 100.0
            _add(osc_s, 0.15)
        if not parts:
            return _score_b_fwd_aligned_series(df, settings)
        wsum = sum(weights) or 1.0
        sc = sum(p * (w / wsum) for p, w in zip(parts, weights))
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _band_low_is_better(val: Any, lo: float, hi: float) -> float:
    """밴드 [lo,hi]에서 낮을수록 100점에 가깝게."""
    try:
        x = float(val)
        span = max(float(hi) - float(lo), 1e-9)
        return float(np.clip((float(hi) - x) / span, 0.0, 1.0) * 100.0)
    except Exception:
        return 0.0


def _band_high_is_better(val: Any, lo: float, hi: float) -> float:
    """밴드 [lo,hi]에서 높을수록 100점에 가깝게."""
    try:
        x = float(val)
        span = max(float(hi) - float(lo), 1e-9)
        return float(np.clip((x - float(lo)) / span, 0.0, 1.0) * 100.0)
    except Exception:
        return 0.0


def _score_a_fwd_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """
    Valid_A 후보 순위용 — 선행수익과 부호가 맞게(얕은 되돌림·낮은 ATR·낮은 MA갭·유동성).
    Valid 게이트는 호출 측에서 적용.
    """
    try:
        pb_lo = float(settings.get("sel_a_pb_min_pct", 5.0))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.01))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        pb = pd.to_numeric(df.get("_sel_pb_pct"), errors="coerce")
        atr = pd.to_numeric(df.get("_sel_atr_pct"), errors="coerce")
        pb_s = ((pb_hi - pb) / max(pb_hi - pb_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        atr_s = ((atr_hi - atr) / max(atr_hi - atr_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        if "MA_Diff" in df.columns:
            conv = max(float(settings.get("ma_convergence", 5.0)), 1e-6)
            md = pd.to_numeric(df["MA_Diff"], errors="coerce").abs()
            ma_s = (1.0 - (md / conv)).clip(0.0, 1.0) * 100.0
        else:
            ma_s = pd.Series(50.0, index=df.index)
        if "AvgVal5" in df.columns:
            liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
            liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
        else:
            liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
            liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
        sc = pb_s * 0.40 + atr_s * 0.20 + ma_s * 0.20 + liq_s * 0.20
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _score_b_fwd_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """
    Valid_B 후보 순위용 — RS 추격 비중을 뒤집고(낮은 RS% 우대),
    고점 근접·유동성·OSC·낮은 ATR을 가점.
    """
    try:
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        rs = pd.to_numeric(df.get("_sel_b_rs_pct"), errors="coerce").fillna(0.0)
        rs_inv = (1.0 - rs) * 100.0
        hip = pd.to_numeric(df.get("_sel_b_hi_pct"), errors="coerce")
        hi_s = ((hip - hi_lo) / max(hi_hi - hi_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        atr = pd.to_numeric(df.get("_sel_b_atr_pct"), errors="coerce")
        atr_s = ((atr_hi - atr) / max(atr_hi - atr_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        if "AvgVal5" in df.columns:
            liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
            liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
        else:
            liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
            liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
        osc = pd.to_numeric(df.get("OSC", 0), errors="coerce").fillna(0.0)
        if "OSC_Prev" in df.columns:
            osc_p = pd.to_numeric(df["OSC_Prev"], errors="coerce").fillna(osc)
            osc_s = (osc > osc_p).astype(float) * 70.0 + (osc > 0).astype(float) * 30.0
        else:
            osc_s = (osc > 0).astype(float) * 100.0
        sc = rs_inv * 0.40 + hi_s * 0.25 + atr_s * 0.15 + liq_s * 0.10 + osc_s * 0.10
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _compute_sel_a_aux_from_daily_tail(
    df_valid: pd.DataFrame,
    last_row: pd.Series,
    settings: dict,
) -> Optional[dict]:
    """
    실시간 선정용: 일봉 꼬리에서 Track A v2 지표(상대강도 원시·되돌림·ATR%·갭·이평근접)를 계산한다.
    교차섹션 순위는 호출부에서 부여한다.
    """
    try:
        min_b = max(30, int(float(settings.get("sel_a_min_history_bars", 65))))
        if df_valid is None or len(df_valid) < min_b:
            return None
        rs_n = max(1, int(float(settings.get("sel_a_rs_lookback", 5))))
        pb_n = max(5, int(float(settings.get("sel_a_pb_lookback", 20))))
        atr_w = max(5, int(float(settings.get("sel_a_atr_period", 14))))
        need = max(rs_n + 2, pb_n + 1, atr_w + 2, min_b)
        if len(df_valid) < need:
            return None

        dfv = df_valid.copy()
        if "high" not in dfv.columns or dfv["high"].isna().all():
            dfv["high"] = dfv["close"]
        if "low" not in dfv.columns or dfv["low"].isna().all():
            dfv["low"] = dfv["close"]
        if "open" not in dfv.columns:
            dfv["open"] = dfv["close"]

        lc = float(last_row["close"] or 0)
        if lc <= 0:
            return None

        cl_rs = float(dfv["close"].iloc[-(rs_n + 1)])
        if cl_rs <= 0:
            return None
        ret5_raw = (lc / cl_rs - 1.0) * 100.0

        hh = float(dfv["high"].tail(pb_n).max())
        if hh <= 0:
            return None
        pb_pct = (hh - lc) / hh * 100.0

        prev_c = dfv["close"].shift(1)
        tr = np.maximum(
            dfv["high"] - dfv["low"],
            np.maximum((dfv["high"] - prev_c).abs(), (dfv["low"] - prev_c).abs()),
        )
        atr = float(tr.tail(atr_w).mean())
        atr_pct = atr / lc if lc > 0 else 0.0

        o0 = float(last_row.get("open", lc) or lc)
        pc1 = float(dfv["close"].iloc[-2])
        gap_pct = ((o0 - pc1) / pc1 * 100.0) if pc1 > 1e-9 else 0.0

        ma5 = float(last_row.get("MA5", 0) or 0)
        ma20 = float(last_row.get("MA20", 0) or 0)
        m20n = float(settings.get("sel_a_ma20_near_pct", 0.045))
        eps = float(settings.get("sel_a_ma5_eps_pct", 0.004))
        near20 = ma20 > 0 and abs(lc / ma20 - 1.0) <= m20n
        if _settings_bool_sel(settings, "sel_a_require_close_ge_ma5", True):
            ge5 = ma5 > 0 and lc >= ma5 * (1.0 - eps)
            ma_ok = bool(near20 or ge5)
        else:
            ma_ok = bool(near20)

        min_억 = float(settings.get("min_trde_val", 40.0)) * 100000000
        avg5 = float(last_row.get("AvgVal5", 0) or 0)
        min_liq_ok = avg5 >= min_억

        osc = float(last_row.get("OSC", 0) or 0)
        osc_ok = (osc > 0) if _settings_bool_sel(settings, "sel_a_require_osc_positive", True) else True
        ma60 = float(last_row.get("MA60", 0) or 0)
        ma60_ok = (ma60 > 0) if _settings_bool_sel(settings, "sel_a_require_ma60", True) else True

        vba = bool(last_row.get("VolBurstActive_A", False))
        burst_ok = (vba) if _settings_bool_sel(settings, "sel_a_require_vol_burst", False) else True

        rsi = float(last_row.get("RSI", 0) or 0)
        rmin = float(settings.get("sel_rsi_min", 35.0))
        rmax = float(settings.get("sel_rsi_max", 70.0))
        rsi_ok = (rmin <= rsi <= rmax) if _settings_bool_sel(
            settings, "sel_a_require_sel_rsi_band", False
        ) else True

        return {
            "_sel_ret5_raw": float(ret5_raw),
            "_sel_pb_pct": float(pb_pct),
            "_sel_atr_pct": float(atr_pct),
            "_sel_gap_pct": float(gap_pct),
            "_sel_ma_ok": bool(ma_ok),
            "_sel_min_liq_ok": bool(min_liq_ok),
            "_sel_osc_ok": bool(osc_ok),
            "_sel_ma60_ok": bool(ma60_ok),
            "_sel_burst_ok": bool(burst_ok),
            "_sel_rsi_ok": bool(rsi_ok),
        }
    except Exception:
        return None


def _apply_sel_a_live_cross_section(rows: list, settings: dict) -> list:
    """
    유니버스 전체 행에 대해 `_sel_ret5_raw` 교차섹션 분위를 매기고 Valid_A·Score_A 를 확정한다.
    """
    if not rows:
        return rows
    try:
        if not _settings_bool_sel(settings, "use_sel_track_a", True):
            return rows
        df = pd.DataFrame(rows)
        if "_sel_ret5_raw" not in df.columns:
            return rows
        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.28))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.92))
        pb_lo = float(settings.get("sel_a_pb_min_pct", 1.5))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.006))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        gap_mx = float(settings.get("sel_a_gap_abs_max_pct", 6.0))

        s = pd.to_numeric(df["_sel_ret5_raw"], errors="coerce")
        df["_sel_rs_pct"] = s.rank(pct=True, ascending=True)
        pb = pd.to_numeric(df["_sel_pb_pct"], errors="coerce")
        atrp = pd.to_numeric(df["_sel_atr_pct"], errors="coerce")
        gap = pd.to_numeric(df["_sel_gap_pct"], errors="coerce").abs()

        m_rs = s.notna() & df["_sel_rs_pct"].between(rk_lo, rk_hi, inclusive="both")
        m_pb = pb.notna() & pb.between(pb_lo, pb_hi, inclusive="both")
        m_atr = atrp.notna() & atrp.between(atr_lo, atr_hi, inclusive="both")
        m_gap = gap.notna() & (gap <= gap_mx)
        m_ma = df["_sel_ma_ok"].astype(bool)
        m_liq = df["_sel_min_liq_ok"].astype(bool)
        m_osc = df["_sel_osc_ok"].astype(bool)
        m60 = df["_sel_ma60_ok"].astype(bool)
        m_br = df["_sel_burst_ok"].astype(bool)
        m_rsi = df["_sel_rsi_ok"].astype(bool)

        va = m_rs & m_pb & m_atr & m_gap & m_ma & m_liq & m_osc & m60 & m_br & m_rsi

        mid_pb = (pb_lo + pb_hi) / 2.0
        half_w = max((pb_hi - pb_lo) / 2.0, 1e-6)
        pb_score = (1.0 - (pb - mid_pb).abs() / half_w).clip(lower=0.0, upper=1.0) * 100.0
        rs_part = df["_sel_rs_pct"].fillna(0.0) * 55.0
        pb_part = pb_score * 0.28
        ma_part = df["_sel_ma_ok"].astype(float) * 18.0
        liq_raw = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
        liq_part = (liq_raw / 500.0 * 100.0).clip(upper=100.0) * 0.12
        osc_bonus = 10.0  # 동적 배치와 유사: 우상향 가점은 일괄 경로에서 생략·단순화
        mode = _sel_score_mode(settings)
        if _use_score_a_s1_pnl_aligned(mode):
            sc = _score_a_s1_pnl_aligned_series(df, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_a_fwd_aligned_series(df, settings)
        else:
            sc = (rs_part + pb_part + ma_part + liq_part).fillna(0.0) + osc_bonus
            sc = sc.clip(lower=0.0, upper=100.0)
        df["_valid_a"] = va.fillna(False).values
        df["_score_a"] = np.where(va.fillna(False), np.round(sc, 1), 0.0)
        return df.to_dict("records")
    except Exception:
        return rows


def _compute_sel_b_aux_from_daily_tail(
    df_valid: pd.DataFrame,
    last_row: pd.Series,
    settings: dict,
) -> Optional[dict]:
    """
    실시간 선정용: Track B v2 — 단기 수익률 원시, 고가 대비 종가 비율(%), ATR%, 갭,
    MA20 기울기·정배열, 대금(B), 분출·RSI·OSC.
    """
    try:
        min_b = max(30, int(float(settings.get("sel_b_min_history_bars", 65))))
        if df_valid is None or len(df_valid) < min_b:
            return None
        rs_n = max(1, int(float(settings.get("sel_b_rs_lookback", 5))))
        hi_n = max(5, int(float(settings.get("sel_b_hi_lookback", 20))))
        atr_w = max(5, int(float(settings.get("sel_b_atr_period", 14))))
        slope_lb = max(2, int(float(settings.get("sel_b_ma20_slope_lookback", 5))))
        need = max(rs_n + 2, hi_n + 1, atr_w + 2, slope_lb + 25, min_b)
        if len(df_valid) < need:
            return None

        dfv = df_valid.copy()
        if "high" not in dfv.columns or dfv["high"].isna().all():
            dfv["high"] = dfv["close"]
        if "low" not in dfv.columns or dfv["low"].isna().all():
            dfv["low"] = dfv["close"]
        if "open" not in dfv.columns:
            dfv["open"] = dfv["close"]

        lc = float(last_row["close"] or 0)
        if lc <= 0:
            return None

        cl_rs = float(dfv["close"].iloc[-(rs_n + 1)])
        if cl_rs <= 0:
            return None
        ret_raw = (lc / cl_rs - 1.0) * 100.0

        hh = float(dfv["high"].tail(hi_n).max())
        if hh <= 0:
            return None
        hi_pct = (lc / hh) * 100.0

        prev_c = dfv["close"].shift(1)
        tr = np.maximum(
            dfv["high"] - dfv["low"],
            np.maximum((dfv["high"] - prev_c).abs(), (dfv["low"] - prev_c).abs()),
        )
        atr = float(tr.tail(atr_w).mean())
        atr_pct = atr / lc if lc > 0 else 0.0

        o0 = float(last_row.get("open", lc) or lc)
        pc1 = float(dfv["close"].iloc[-2])
        gap_pct = ((o0 - pc1) / pc1 * 100.0) if pc1 > 1e-9 else 0.0

        ma20 = float(last_row.get("MA20", 0) or 0)
        ma60 = float(last_row.get("MA60", 0) or 0)
        ma5 = float(last_row.get("MA5", 0) or 0)
        trend_ok = (ma20 > ma60) and (lc > ma20) and ma20 > 0 and ma60 > 0

        m20_series = dfv["MA20"].astype(float)
        if len(m20_series) < slope_lb + 1:
            return None
        ma20_now = float(m20_series.iloc[-1])
        ma20_prev = float(m20_series.iloc[-(slope_lb + 1)])
        slope = (ma20_now / ma20_prev - 1.0) if ma20_prev > 1e-9 else 0.0
        slope_min = float(settings.get("sel_b_ma20_slope_min", 0.001))
        slope_ok = slope >= slope_min

        if _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult = _sel_b_ma5_close_mult(settings)
            ma5_ok = ma5 > 0 and lc >= ma5 * mult
            trend_ok = bool(trend_ok and ma5_ok)

        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 45.0)) * 100000000
        avg5 = float(last_row.get("AvgVal5", 0) or 0)
        min_liq_ok = avg5 >= sel_b_min_val

        osc = float(last_row.get("OSC", 0) or 0)
        osc_prev = float(last_row.get("OSC_Prev", osc) or 0)
        osc_ok = (osc > 0) if _settings_bool_sel(settings, "sel_b_require_osc_positive", True) else True
        osc_rise_ok = (osc > osc_prev) if _settings_bool_sel(
            settings, "sel_b_require_osc_rising", False
        ) else True

        ma60_ok = (ma60 > 0) if _settings_bool_sel(settings, "sel_b_require_ma60", True) else True

        vbb = bool(last_row.get("VolBurstActive_B", False))
        burst_ok = vbb if _settings_bool_sel(settings, "sel_b_require_vol_burst", True) else True

        rsi = float(last_row.get("RSI", 0) or 0)
        rmin = float(settings.get("sel_b_rsi_min", 45.0))
        rmax = float(settings.get("sel_b_rsi_max", 75.0))
        rsi_ok = (rmin <= rsi <= rmax) if _settings_bool_sel(
            settings, "sel_b_require_sel_rsi_band", True
        ) else True

        return {
            "_sel_b_ret_raw": float(ret_raw),
            "_sel_b_hi_pct": float(hi_pct),
            "_sel_b_atr_pct": float(atr_pct),
            "_sel_b_gap_pct": float(gap_pct),
            "_sel_b_ma20_slope": float(slope),
            "_sel_b_slope_ok": bool(slope_ok),
            "_sel_b_trend_ok": bool(trend_ok),
            "_sel_b_min_liq_ok": bool(min_liq_ok),
            "_sel_b_osc_ok": bool(osc_ok),
            "_sel_b_osc_rise_ok": bool(osc_rise_ok),
            "_sel_b_ma60_ok": bool(ma60_ok),
            "_sel_b_burst_ok": bool(burst_ok),
            "_sel_b_rsi_ok": bool(rsi_ok),
        }
    except Exception:
        return None


def _apply_sel_b_live_cross_section(rows: list, settings: dict) -> list:
    """일자 유니버스 전체에 대해 Track B v2 교차섹션 분위·필터 후 ``_valid_b``·``_score_b`` 확정."""
    if not rows:
        return rows
    try:
        if not _settings_bool_sel(settings, "use_sel_track_b", True):
            return rows
        df = pd.DataFrame(rows)
        if "_sel_b_ret_raw" not in df.columns:
            return rows

        rk_lo = float(settings.get("sel_b_rs_rank_min", 0.38))
        rk_hi = float(settings.get("sel_b_rs_rank_max", 0.97))
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        gap_mx = float(settings.get("sel_b_gap_abs_max_pct", 7.0))

        s = pd.to_numeric(df["_sel_b_ret_raw"], errors="coerce")
        df["_sel_b_rs_pct"] = s.rank(pct=True, ascending=True)
        hip = pd.to_numeric(df["_sel_b_hi_pct"], errors="coerce")
        atrp = pd.to_numeric(df["_sel_b_atr_pct"], errors="coerce")
        gap = pd.to_numeric(df["_sel_b_gap_pct"], errors="coerce").abs()

        m_rs = s.notna() & df["_sel_b_rs_pct"].between(rk_lo, rk_hi, inclusive="both")
        m_hi = hip.notna() & hip.between(hi_lo, hi_hi, inclusive="both")
        m_atr = atrp.notna() & atrp.between(atr_lo, atr_hi, inclusive="both")
        m_gap = gap.notna() & (gap <= gap_mx)

        m_tr = df["_sel_b_trend_ok"].astype(bool)
        m_sl = df["_sel_b_slope_ok"].astype(bool)
        m_liq = df["_sel_b_min_liq_ok"].astype(bool)
        m_osc = df["_sel_b_osc_ok"].astype(bool)
        m_osr = df["_sel_b_osc_rise_ok"].astype(bool)
        m60 = df["_sel_b_ma60_ok"].astype(bool)
        m_br = df["_sel_b_burst_ok"].astype(bool)
        m_rsi = df["_sel_b_rsi_ok"].astype(bool)

        vb = m_rs & m_hi & m_atr & m_gap & m_tr & m_sl & m_liq & m_osc & m_osr & m60 & m_br & m_rsi

        half_hi = max((hi_hi - hi_lo) / 2.0, 1e-6)
        hi_score = (1.0 - (hip - (hi_lo + hi_hi) / 2.0).abs() / half_hi).clip(lower=0.0, upper=1.0) * 100.0
        rs_part = df["_sel_b_rs_pct"].fillna(0.0) * 52.0
        hi_part = hi_score * 0.28
        lc = pd.to_numeric(df.get("Price", df.get("close", 0)), errors="coerce")
        ma20r = pd.to_numeric(df.get("MA20", 0), errors="coerce")
        tr_st = ((lc / ma20r.replace(0, np.nan) - 1.0) * 100.0).clip(lower=0.0, upper=10.0) * 10.0
        tr_part = tr_st * 0.15
        slope_bonus = df["_sel_b_slope_ok"].astype(float) * 10.0
        liq_raw = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
        liq_part = (liq_raw / 500.0 * 100.0).clip(upper=100.0) * 0.12
        osc_r = pd.to_numeric(df.get("OSC", 0), errors="coerce")
        if "OSC_Prev" in df.columns:
            osc_p = pd.to_numeric(df["OSC_Prev"], errors="coerce")
        else:
            osc_p = osc_r
        try:
            osc_bonus = (osc_r > osc_p).astype(float) * 10.0
        except Exception:
            osc_bonus = 0.0
        mode = _sel_score_mode(settings)
        if _use_score_b_s1_pnl_aligned(mode):
            sc = _score_b_s1_pnl_aligned_series(df, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_b_fwd_aligned_series(df, settings)
        else:
            sc = (rs_part + hi_part + tr_part + slope_bonus + liq_part + osc_bonus).fillna(0.0)
            sc = sc.clip(lower=0.0, upper=100.0)
        df["_valid_b"] = vb.fillna(False).values
        df["_score_b"] = np.where(vb.fillna(False), np.round(sc, 1), 0.0)
        return df.to_dict("records")
    except Exception:
        return rows


def _sorted_dict_rows_top_with_slot_band(
    rows: List[dict],
    score_key: str,
    max_n: int,
    settings: dict,
    *,
    min_score_floor: float = 0.0,
) -> List[dict]:
    """
    - ``min_score_floor`` > 0: 해당 점수 이상만 후보(N과 무관), 이후 ``max_n`` 상한만 적용.
      한 건도 없으면 기본은 빈 목록(그날 해당 트랙 감시 없음). ``sel_track_min_score_fallback_topn`` True면 상위 ``max_n`` 폴백.
    - ``min_score_floor`` <= 0: 점수 내림차순 상위 ``max_n`` 및 (옵션) 상대 밴드 ``max_slots_score_rel_pad``.
    """
    try:
        max_n = max(0, int(max_n))
        if max_n <= 0 or not rows:
            return []
        mfloor = max(0.0, float(min_score_floor or 0.0))

        def _sk(r: dict) -> tuple[float, str]:
            try:
                sc = float(r.get(score_key, 0) or 0)
            except (TypeError, ValueError):
                sc = 0.0
            try:
                cd = str(r.get("Code", r.get("code", ""))).zfill(6)
            except Exception:
                cd = ""
            return (-sc, cd)

        arr = sorted(rows, key=_sk)
        if mfloor > 1e-15:
            pool: List[dict] = []
            for r in arr:
                try:
                    v = float(r.get(score_key, 0) or 0)
                except (TypeError, ValueError):
                    v = 0.0
                if v >= mfloor - 1e-12:
                    pool.append(r)
            if not pool and _settings_bool_sel(
                settings, "sel_track_min_score_fallback_topn", False
            ):
                return arr[:max_n]
            return pool[:max_n]

        pad = max(0.0, float(settings.get("max_slots_score_rel_pad", 0.0) or 0.0))
        extra_cap = max(0, int(float(settings.get("max_slots_band_extra_cap", 12) or 0)))
        if len(arr) <= max_n:
            return arr
        if pad <= 0:
            return arr[:max_n]
        try:
            cut = float(arr[max_n - 1].get(score_key, 0) or 0)
        except (TypeError, ValueError, IndexError):
            cut = 0.0
        floor = cut * (1.0 - pad)
        hard = max_n + extra_cap
        out: List[dict] = []
        for r in arr:
            try:
                v = float(r.get(score_key, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v >= floor - 1e-12:
                out.append(r)
            if len(out) >= hard:
                break
        return out if out else arr[:max_n]
    except Exception:
        try:
            return sorted(rows, key=lambda x: -float(x.get(score_key, 0) or 0))[: max(0, int(max_n))]
        except Exception:
            return []


def _df_top_by_score_slot_band(
    df: pd.DataFrame,
    *,
    score_col: str,
    code_col: str,
    max_n: int,
    settings: dict,
    min_score_floor: float = 0.0,
) -> pd.DataFrame:
    """
    일괄 스케줄용: ``_sorted_dict_rows_top_with_slot_band`` 와 동일 규칙.
    ``min_score_floor`` > 0 이면 점수 임계 후 ``max_n`` 상한만 적용. 임계 통과 없으면 기본 빈 프레임.
    """
    try:
        max_n = max(0, int(max_n))
        if max_n <= 0 or df is None or df.empty or score_col not in df.columns:
            return df.iloc[:0] if df is not None else pd.DataFrame()
        mfloor = max(0.0, float(min_score_floor or 0.0))
        cc = code_col if code_col in df.columns else "code"
        sort_cols = [score_col, cc] if cc in df.columns else [score_col]
        asc_b = [False, True] if len(sort_cols) == 2 else [False]
        s = df.sort_values(by=sort_cols, ascending=asc_b, na_position="last").reset_index(drop=True)

        if mfloor > 1e-15:
            pool = s[s[score_col].astype(float) >= mfloor - 1e-12].copy()
            if pool.empty and _settings_bool_sel(
                settings, "sel_track_min_score_fallback_topn", False
            ):
                return s.head(max_n).copy()
            return pool.head(max_n).copy()

        pad = max(0.0, float(settings.get("max_slots_score_rel_pad", 0.0) or 0.0))
        extra_cap = max(0, int(float(settings.get("max_slots_band_extra_cap", 12) or 0)))
        if len(s) <= max_n:
            return s
        if pad <= 0:
            return s.head(max_n).copy()
        try:
            cut = float(s.iloc[max_n - 1][score_col])
        except Exception:
            cut = 0.0
        floor = cut * (1.0 - pad)
        hard = max_n + extra_cap
        cand = s[s[score_col].astype(float) >= floor - 1e-12]
        return cand.head(hard).copy()
    except Exception as e:
        try:
            logging.warning("_df_top_by_score_slot_band 폴백(head): %s", e)
        except Exception:
            pass
        try:
            return df.sort_values(by=score_col, ascending=False).head(max(0, int(max_n))).copy()
        except Exception:
            return df.iloc[:0]


def _merge_dual_track_picks(
    selected_rows: list,
    settings: dict,
    logger_callback,
    max_a: Optional[int] = None,
    max_b: Optional[int] = None,
) -> list:
    """
    Track A 상위 max_slots_a + Track B 상위 max_slots_b 를 합치되,
    양쪽에 모두 든 종목은 SelTrack=AB·Score=max 로 1행만 유지.

    ``max_a`` / ``max_b`` 가 주어지면(브레드스 레짐 슬롯) 설정의 max_slots_* 대신 사용.
    """
    max_a_i = (
        max(0, int(max_a))
        if max_a is not None
        else max(0, int(settings.get("max_slots_a", 20)))
    )
    max_b_i = (
        max(0, int(max_b))
        if max_b is not None
        else max(0, int(settings.get("max_slots_b", 10)))
    )
    floor_a = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_b = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    cand_a = [d for d in selected_rows if d.get("_valid_a")]
    cand_b = [d for d in selected_rows if d.get("_valid_b")]
    picked_a = _sorted_dict_rows_top_with_slot_band(
        cand_a, "_score_a", max_a_i, settings, min_score_floor=floor_a
    )
    picked_b = _sorted_dict_rows_top_with_slot_band(
        cand_b, "_score_b", max_b_i, settings, min_score_floor=floor_b
    )
    code_to_sb = {str(d["Code"]).zfill(6): float(d.get("_score_b", 0)) for d in picked_b}
    seen = set()
    final = []

    def strip_internal(d: dict) -> dict:
        return {k: v for k, v in d.items() if not str(k).startswith("_")}

    for d in picked_a:
        code = str(d["Code"]).zfill(6)
        seen.add(code)
        sa = float(d.get("_score_a", 0))
        in_b_top = code in code_to_sb
        if in_b_top:
            sb = code_to_sb[code]
            row = strip_internal(d)
            row["SelTrack"] = "AB"
            row["Score"] = round(max(sa, sb), 1)
        else:
            row = strip_internal(d)
            row["SelTrack"] = "A"
            row["Score"] = round(sa, 1)
        final.append(row)

    for d in picked_b:
        code = str(d["Code"]).zfill(6)
        if code in seen:
            continue
        seen.add(code)
        row = strip_internal(d)
        row["SelTrack"] = "B"
        row["Score"] = round(float(d.get("_score_b", 0)), 1)
        final.append(row)

    logger_callback(
        f"[DualTrack] A후보 {len(cand_a)}→채택 {len(picked_a)}, "
        f"B후보 {len(cand_b)}→채택 {len(picked_b)}, 최종 행 {len(final)}"
    )
    return final


def compute_market_breadth(
    rows_or_df: Union[List[dict], pd.DataFrame],
) -> Dict[str, float]:
    """
    유니버스 스냅샷(선정 스캔 결과 행 목록 또는 일자별 DataFrame)에서 시장 브레드스를 계산한다.

    외부 지수 데이터 없이, 선정 단계에서 이미 계산된 종가·MA20·MA60만으로 시장 폭을 측정한다.

    입력:
        - list[dict]: 실시간 선정 경로. 각 행은 ``Price``(또는 ``close``)·``MA20``·``MA60`` 보유.
        - pd.DataFrame: 백테스트 일별 패널. ``close``·``MA20``·``MA60`` 컬럼 보유.

    반환:
        - ``ma20_above``: 종가 ≥ MA20 인 종목 비율(0~1). 표본 없으면 ``float('nan')``.
        - ``ma20_gt_ma60``: MA20 > MA60 비율(0~1).
        - ``n``: 유효 표본 종목 수.
    """
    nan = float("nan")
    result: Dict[str, float] = {"ma20_above": nan, "ma20_gt_ma60": nan, "n": 0.0}
    try:
        if isinstance(rows_or_df, pd.DataFrame):
            df = rows_or_df
            if df is None or df.empty:
                return result
            close_col = (
                "close"
                if "close" in df.columns
                else ("Price" if "Price" in df.columns else None)
            )
            if close_col is None or "MA20" not in df.columns:
                return result
            close = pd.to_numeric(df[close_col], errors="coerce")
            ma20 = pd.to_numeric(df["MA20"], errors="coerce")
            ma60 = (
                pd.to_numeric(df["MA60"], errors="coerce")
                if "MA60" in df.columns
                else pd.Series(np.nan, index=df.index)
            )
            valid = close.notna() & ma20.notna() & (ma20 > 0)
            n = int(valid.sum())
            if n <= 0:
                return result
            above = float((close[valid] >= ma20[valid]).mean())
            v60 = valid & ma60.notna() & (ma60 > 0)
            gt60 = float((ma20[v60] > ma60[v60]).mean()) if int(v60.sum()) > 0 else nan
            return {"ma20_above": above, "ma20_gt_ma60": gt60, "n": float(n)}

        # list[dict] 경로
        rows = rows_or_df or []
        above_cnt = 0
        gt60_cnt = 0
        gt60_den = 0
        n = 0
        for r in rows:
            try:
                price = r.get("Price", r.get("close", None))
                ma20 = r.get("MA20", None)
                ma60 = r.get("MA60", None)
                if price is None or ma20 is None:
                    continue
                price_f = float(price)
                ma20_f = float(ma20)
                if not (np.isfinite(price_f) and np.isfinite(ma20_f)) or ma20_f <= 0:
                    continue
                n += 1
                if price_f >= ma20_f:
                    above_cnt += 1
                if ma60 is not None:
                    ma60_f = float(ma60)
                    if np.isfinite(ma60_f) and ma60_f > 0:
                        gt60_den += 1
                        if ma20_f > ma60_f:
                            gt60_cnt += 1
            except (TypeError, ValueError):
                continue
        if n <= 0:
            return result
        return {
            "ma20_above": above_cnt / n,
            "ma20_gt_ma60": (gt60_cnt / gt60_den) if gt60_den > 0 else nan,
            "n": float(n),
        }
    except Exception:
        return result


def _clamp_nonneg_int(value: Any, default: int) -> int:
    """설정값을 0 이상 정수로 안전 변환."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return max(0, int(default))


def is_chop_regime_day(
    valid_a: int,
    valid_b: int,
    settings: Optional[dict] = None,
) -> bool:
    """
    A 과밀·B 고갈 횡보(chop) 레짐 여부.

    조건 (OR):
      1) valid_a >= chop_valid_a_min AND valid_b <= chop_valid_b_max
      2) valid_b_share <= chop_valid_b_share_max_pct AND valid_a >= chop_valid_a_floor

    선정 스케줄 생성 시 A 슬롯만 축소하는 데 사용한다.
    """
    s = settings or {}
    try:
        va = max(0, int(valid_a))
        vb = max(0, int(valid_b))
        a_min = max(0, int(float(s.get("chop_valid_a_min", 15) or 15)))
        b_max = max(0, int(float(s.get("chop_valid_b_max", 3) or 3)))
        share_max = float(s.get("chop_valid_b_share_max_pct", 15.0) or 15.0)
        a_floor = max(0, int(float(s.get("chop_valid_a_floor", 10) or 10)))
        total = va + vb
        share = (vb / total * 100.0) if total > 0 else 100.0
        if va >= a_min and vb <= b_max:
            return True
        if va >= a_floor and share <= share_max:
            return True
        return False
    except Exception:
        return False


def _fetch_daily_ohlcv_batch_fdr(
    data_manager: Any,
    codes: List[str],
    fetch_start: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
    *,
    max_workers: int = 12,
) -> pd.DataFrame:
    """
    레짐·전환 게이트용: 유니버스 종목 일봉을 FDR에서 병렬 조회한다.
    ``bars_1d`` DB 동기화 없이도 동작한다.
    """
    empty = pd.DataFrame(
        columns=["code", "time", "open", "high", "low", "close", "volume"]
    )
    log = logger_callback or (lambda *_a, **_k: None)
    if data_manager is None or not codes:
        return empty
    fetch_fn = getattr(data_manager, "fetch_fdr_daily_dataframe", None)
    if not callable(fetch_fn):
        try:
            log("⚠️ [Transition] data_manager.fetch_fdr_daily_dataframe 없음")
        except Exception:
            pass
        return empty

    norm_codes = [str(c).strip().zfill(6) for c in codes if str(c).strip()]
    if not norm_codes:
        return empty

    def _one(code: str) -> Optional[pd.DataFrame]:
        try:
            raw = fetch_fn(code, fetch_start, end_date)
            if raw is None or raw.empty:
                return None
            part = raw.copy()
            part["code"] = code
            part["time"] = pd.to_datetime(part["datetime"], errors="coerce")
            for col in ("open", "high", "low", "close", "volume"):
                if col not in part.columns:
                    if col == "volume":
                        part[col] = 0
                    else:
                        part[col] = part.get("close", np.nan)
                part[col] = pd.to_numeric(part[col], errors="coerce")
            part = part.dropna(subset=["time", "close"])
            if part.empty:
                return None
            return part[["code", "time", "open", "high", "low", "close", "volume"]]
        except Exception:
            return None

    parts: List[pd.DataFrame] = []
    workers = max(1, min(int(max_workers or 12), 24))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for df_part in pool.map(_one, norm_codes):
                if df_part is not None and not df_part.empty:
                    parts.append(df_part)
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] FDR 일괄 조회 실패: {ex}")
        except Exception:
            pass
        return empty

    if not parts:
        try:
            log(
                f"⚠️ [Transition] FDR 일봉 0건 ({fetch_start}~{end_date}, "
                f"요청 {len(norm_codes)}종목)"
            )
        except Exception:
            pass
        return empty

    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["code", "time"]).reset_index(drop=True)
    try:
        log(
            f"[Transition] FDR 일봉 {len(out)}행 / "
            f"{out['code'].nunique()}종목 ({fetch_start}~{end_date})"
        )
    except Exception:
        pass
    return out


def _fdr_index_daily_close_df(
    data_manager: Any,
    index_code: str,
    fetch_start: str,
    end_date: str,
) -> pd.DataFrame:
    """지수·ETF 일봉 close 시계열 (FDR 우선)."""
    empty = pd.DataFrame(columns=["time", "close"])
    if data_manager is None:
        return empty
    fetch_fn = getattr(data_manager, "fetch_fdr_daily_dataframe", None)
    if callable(fetch_fn):
        try:
            raw = fetch_fn(index_code, fetch_start, end_date)
            if raw is not None and not raw.empty:
                tmp = raw.copy()
                tmp["time"] = pd.to_datetime(tmp["datetime"], errors="coerce")
                tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
                tmp = tmp.dropna(subset=["time", "close"])
                if not tmp.empty:
                    return tmp[["time", "close"]].copy()
        except Exception:
            pass
    try:
        df = data_manager.get_daily_bars(index_code, fetch_start, end_date)
        if df is not None and not df.empty:
            tmp = df.copy()
            if "Close" in tmp.columns:
                tmp.rename(columns={"Close": "close"}, inplace=True)
            tmp["time"] = pd.to_datetime(tmp["time"], errors="coerce")
            tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
            tmp = tmp.dropna(subset=["time", "close"])
            if not tmp.empty:
                return tmp[["time", "close"]].copy()
    except Exception:
        pass
    return empty


def _build_index_vol_by_day(
    data_manager: Any,
    fetch_start: str,
    end_date: str,
) -> Dict[str, Dict[str, float]]:
    """
    KODEX200(069500) 일봉 기준 일별 맵 — FDR 우선.

    - ``rvol_5`` / ``rvol_20``: 연환산 변동성(%)
    - ``ret_pct``: 일수익률(%) — HARD 상승일 억제 등에 사용
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        df = _fdr_index_daily_close_df(data_manager, "069500", fetch_start, end_date)
        if df is None or df.empty:
            return out
        tmp = df.copy()
        tmp["d"] = pd.to_datetime(tmp["time"], errors="coerce").dt.normalize()
        close = pd.to_numeric(tmp["close"], errors="coerce")
        ret = close.pct_change()
        tmp["ret_pct"] = ret * 100.0
        tmp["rvol_20"] = ret.rolling(20).std() * np.sqrt(252.0) * 100.0
        tmp["rvol_5"] = ret.rolling(5).std() * np.sqrt(252.0) * 100.0
        for _, row in tmp.iterrows():
            d_raw = row.get("d")
            if pd.isna(d_raw):
                continue
            d_str = pd.Timestamp(d_raw).strftime("%Y-%m-%d")
            entry: Dict[str, float] = {}
            for key in ("rvol_20", "rvol_5", "ret_pct"):
                val = row.get(key)
                if val is not None and np.isfinite(float(val)):
                    entry[key] = float(val)
            if entry:
                out[d_str] = entry
    except Exception:
        return out
    return out


def _compute_transition_regime_map_fdr(
    settings: dict,
    data_manager: Any,
    start_date: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, str]:
    """
    FDR 일봉 기반 일별 transition 레짐 맵.
    ``prepare_dynamic_universe`` 의 레짐 분류와 동일 로직, DB ``bars_1d`` 미사용.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    regime_map: Dict[str, str] = {}
    try:
        start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
        top_n = int(settings.get("kospi_top_n", 200) or 200)
        codes = (
            load_kospi_universe_codes(logger_callback=log, top_n=top_n)
            if idx_choice == "KOSPI200"
            else load_kodex200_universe_codes(logger_callback=log)
        )
        if not codes:
            return regime_map

        fetch_start = (pd.to_datetime(start_s) - timedelta(days=120)).strftime("%Y-%m-%d")
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            return regime_map

        _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)
        df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()
        if df_sim.empty:
            return regime_map

        trading_days_list = df_sim["time"].dt.strftime("%Y-%m-%d").unique().tolist()

        xs_ret_std_by_day: Dict[str, float] = {}
        try:
            _xs_df = df_sim.sort_values(["code", "time"]).copy()
            _xs_df["ret1"] = _xs_df.groupby("code")["close"].pct_change()
            _xs_s = _xs_df.groupby("time")["ret1"].std() * 100.0
            for t_idx, val in _xs_s.items():
                if val is not None and np.isfinite(float(val)):
                    xs_ret_std_by_day[pd.Timestamp(t_idx).strftime("%Y-%m-%d")] = float(
                        val
                    )
        except Exception:
            xs_ret_std_by_day = {}

        fetch_start_iv = (pd.to_datetime(start_s) - timedelta(days=60)).strftime(
            "%Y-%m-%d"
        )
        index_vol_by_day = _build_index_vol_by_day(data_manager, fetch_start_iv, end_s)

        transition_stats_by_day: Dict[str, Dict[str, float]] = {}
        try:
            _t_rows: List[Dict[str, Any]] = []
            for _d_str in trading_days_list:
                _day = df_sim[df_sim["time"] == pd.to_datetime(_d_str)]
                _va = int(_day["Valid_A"].astype(bool).sum()) if "Valid_A" in _day.columns else 0
                _vb = int(_day["Valid_B"].astype(bool).sum()) if "Valid_B" in _day.columns else 0
                _tot = _va + _vb
                _share = (_vb / _tot * 100.0) if _tot > 0 else 0.0
                _t_rows.append({"d": _d_str, "va": _va, "vb": _vb, "share": _share})
            _tdf = pd.DataFrame(_t_rows)
            if not _tdf.empty:
                _tdf["share_delta5"] = _tdf["share"] - _tdf["share"].shift(5)
                _tdf["share_delta3"] = _tdf["share"] - _tdf["share"].shift(3)
                for _, _tr in _tdf.iterrows():
                    transition_stats_by_day[str(_tr["d"])] = {
                        "va": float(_tr["va"]),
                        "vb": float(_tr["vb"]),
                        "share": float(_tr["share"]),
                        "share_delta5": (
                            float(_tr["share_delta5"])
                            if pd.notna(_tr["share_delta5"])
                            else float("nan")
                        ),
                        "share_delta3": (
                            float(_tr["share_delta3"])
                            if pd.notna(_tr["share_delta3"])
                            else float("nan")
                        ),
                    }
        except Exception:
            transition_stats_by_day = {}

        for d_str in trading_days_list:
            try:
                _ts = transition_stats_by_day.get(d_str, {})
                _va = int(_ts.get("va", 0))
                _vb = int(_ts.get("vb", 0))
                _share = float(_ts.get("share", 0.0))
                _d5 = _ts.get("share_delta5")
                _d3 = _ts.get("share_delta3")
                _xs = xs_ret_std_by_day.get(d_str)
                _iv = index_vol_by_day.get(d_str, {})
                regime_map[d_str] = classify_transition_regime(
                    _va,
                    _vb,
                    _share,
                    _d5,
                    _d3,
                    settings,
                    index_rvol_20=_iv.get("rvol_20"),
                    xs_ret_std=_xs,
                    index_day_ret=_iv.get("ret_pct"),
                )
            except Exception:
                regime_map[d_str] = "OFF"
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] FDR 레짐 맵 실패: {ex}")
        except Exception:
            pass
    return regime_map


def classify_transition_regime(
    valid_a: int,
    valid_b: int,
    vb_share: float,
    vb_share_delta5: Optional[float],
    vb_share_delta3: Optional[float],
    settings: Optional[dict] = None,
    index_rvol_20: Optional[float] = None,
    xs_ret_std: Optional[float] = None,
    index_day_ret: Optional[float] = None,
) -> str:
    """
    B-share 급락(레짐 전환) + 변동성 확인.

    반환: ``OFF`` / ``TRANSITION`` / ``HARD``
      - TRANSITION/HARD: 신규 S1 차단 + S2 중단 (런타임)
      - ``hard_suppress_on_index_up``: 지수 일수익률(%)이 임계 이상이면
        chop→HARD를 건너뜀 (폭등일 A과밀 오발동 완화)
      - ``hard_suppress_to_off_only``: 억제 시 TRANSITION 폴백 없이 즉시 ``OFF``
    """
    s = settings or {}
    try:
        chop = is_chop_regime_day(valid_a, valid_b, settings)
        if chop:
            suppress = bool(s.get("hard_suppress_on_index_up", False))
            try:
                thr = float(s.get("hard_suppress_index_up_pct", 2.0) or 2.0)
            except (TypeError, ValueError):
                thr = 2.0
            day_ret: Optional[float] = None
            try:
                if index_day_ret is not None and np.isfinite(float(index_day_ret)):
                    day_ret = float(index_day_ret)
            except Exception:
                day_ret = None
            suppressed = bool(
                suppress and day_ret is not None and day_ret >= thr
            )
            if not suppressed:
                return "HARD"
            # 강한 상승일로 HARD 억제 — OFF만 허용하면 TRANSITION 폴백 금지
            if bool(s.get("hard_suppress_to_off_only", False)):
                return "OFF"

        va = max(0, int(valid_a))
        vb = max(0, int(valid_b))
        share = float(vb_share)
        d5 = float(vb_share_delta5) if vb_share_delta5 is not None and np.isfinite(vb_share_delta5) else None
        d3 = float(vb_share_delta3) if vb_share_delta3 is not None and np.isfinite(vb_share_delta3) else None

        delta5_thr = float(s.get("transition_share_delta5_max", -15.0) or -15.0)
        delta3_thr = float(s.get("transition_share_delta3_max", -10.0) or -10.0)
        prior_min = float(s.get("transition_prior_share_min", 45.0) or 45.0)
        rvol_min = float(s.get("transition_rvol_min", 35.0) or 35.0)
        xs_min = float(s.get("transition_xs_ret_std_min", 3.5) or 3.5)

        r20 = (
            float(index_rvol_20)
            if index_rvol_20 is not None and np.isfinite(float(index_rvol_20))
            else None
        )
        xs = (
            float(xs_ret_std)
            if xs_ret_std is not None and np.isfinite(float(xs_ret_std))
            else None
        )

        def _vol_ok() -> bool:
            if r20 is not None and r20 >= rvol_min:
                return True
            if xs is not None and xs >= xs_min:
                return True
            # 지수 rvol 결측(2026-04~) 구간: B-share 급락만으로 전환 허용
            if r20 is None and d5 is not None and d5 <= delta5_thr - 5.0:
                return True
            return False

        if d5 is not None and d5 <= delta5_thr:
            prior = share - d5
            if prior >= prior_min and _vol_ok():
                return "TRANSITION"

        if d3 is not None and d3 <= delta3_thr:
            prior3 = share - d3
            if prior3 >= prior_min and _vol_ok():
                return "TRANSITION"

        # 지수 없는 구간 fallback: A 과밀 + B 고갈
        if va >= 15 and vb <= 4 and share <= 20.0:
            return "TRANSITION"
        return "OFF"
    except Exception:
        return "OFF"


def resolve_transition_regime_policy(
    level: str,
    day_max_a: int,
    day_max_b: int,
    settings: Optional[dict] = None,
) -> Tuple[int, int]:
    """
    Transition 레짐별 슬롯 상한.

    - HARD: 0/0 (신규 감시 없음)
    - TRANSITION: transition_soft_slots (기본 6/6) — tiered 모드용
    """
    s = settings or {}
    try:
        lv = str(level or "OFF").strip().upper()
        if lv == "HARD":
            return 0, 0
        if lv == "TRANSITION":
            sa = _clamp_nonneg_int(s.get("transition_soft_slots_a", 6), 6)
            sb = _clamp_nonneg_int(s.get("transition_soft_slots_b", 6), 6)
            return min(day_max_a, sa), min(day_max_b, sb)
        return day_max_a, day_max_b
    except Exception:
        return day_max_a, day_max_b


def resolve_transition_runtime_blocks(
    regime: str,
    settings: Optional[dict] = None,
) -> Tuple[bool, bool]:
    """
    레짐별 실시간·백테스트 S1/S2 차단 플래그.

    - HARD: S1·S2 모두 차단
    - TRANSITION: ``transition_block_s1_on_soft`` / ``transition_block_s2_on_soft`` 를 그대로 따름
      (슬롯 축소는 ``resolve_transition_regime_policy`` 가 별도로 담당)
    - 그 외: 차단 없음
    """
    s = settings or {}
    try:
        r = str(regime or "OFF").strip().upper()
        if r == "HARD":
            return True, True
        if r == "TRANSITION":
            block_s1 = bool(s.get("transition_block_s1_on_soft", False))
            block_s2 = bool(s.get("transition_block_s2_on_soft", True))
            return block_s1, block_s2
        return False, False
    except Exception:
        return False, False


def resolve_transition_gate_for_live(
    settings: dict,
    data_manager: Any,
    as_of: Optional[datetime] = None,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """
    실시간 선정용 transition 레짐·슬롯·차단 플래그.

    FDR 일봉으로 최근 구간을 스코어해 당일 레짐을 판정한다 (``bars_1d`` DB 불필요).

    백테와 같이 **완료된 거래일** 기준만 사용한다.
    ``as_of`` 가 오늘(또는 미래)이면서 아직 장마감+버퍼 전이면 직전 완료 거래일로 스냅한다.
    과거 ``target_date`` 명시 시에는 그 날짜를 그대로 쓴다.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    max_a = int(settings.get("max_slots_a", 20))
    max_b = int(settings.get("max_slots_b", 15))
    result: Dict[str, Any] = {
        "regime": "OFF",
        "slots_a": max_a,
        "slots_b": max_b,
        "block_s1": False,
        "block_s2": False,
    }
    if not _settings_bool_sel(settings, "use_transition_gate", False):
        return result
    if data_manager is None:
        log("⚠️ [Transition] data_manager 없음 — 게이트 OFF")
        return result
    try:
        as_of_dt = pd.to_datetime(as_of or datetime.now()).normalize()
        # 미완성 당일 일봉으로 레짐이 바뀌지 않도록 직전 완료 거래일로 고정
        try:
            from reporting.krx_trading_calendar import completed_trading_day_asof_local

            try:
                buf = int(float(settings.get("regime_display_eod_buffer_minutes", 30) or 30))
            except (TypeError, ValueError):
                buf = 30
            now_d = datetime.now().date()
            completed = completed_trading_day_asof_local(
                datetime.now(), eod_buffer_minutes=buf
            )
            req_d = as_of_dt.date()
            # 실시간/오늘 이후만 스냅 (과거 선정일은 유지)
            if req_d >= now_d and req_d != completed:
                as_of_dt = pd.Timestamp(completed)
        except Exception:
            pass
        end_s = as_of_dt.strftime("%Y-%m-%d")
        start_s = (as_of_dt - timedelta(days=30)).strftime("%Y-%m-%d")
        idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
        top_n = int(settings.get("kospi_top_n", 200) or 200)
        codes = (
            load_kospi_universe_codes(logger_callback=log, top_n=top_n)
            if idx_choice == "KOSPI200"
            else load_kodex200_universe_codes(logger_callback=log)
        )
        if not codes:
            return result
        fetch_start = (as_of_dt - timedelta(days=120)).strftime("%Y-%m-%d")
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            log("⚠️ [Transition] FDR 일봉 부족 — 게이트 OFF")
            return result
        _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)
        df_day = df_all[df_all["time"].dt.normalize() == as_of_dt]
        if df_day.empty:
            last_t = df_all["time"].max()
            df_day = df_all[df_all["time"].dt.normalize() == pd.Timestamp(last_t).normalize()]
            end_s = pd.Timestamp(last_t).strftime("%Y-%m-%d")
            as_of_dt = pd.Timestamp(last_t).normalize()
        va = int(df_day["Valid_A"].astype(bool).sum()) if "Valid_A" in df_day.columns else 0
        vb = int(df_day["Valid_B"].astype(bool).sum()) if "Valid_B" in df_day.columns else 0
        tot = va + vb
        share = (vb / tot * 100.0) if tot > 0 else 0.0
        # share delta: 최근 6영업일
        days = sorted(df_all["time"].dt.normalize().unique())
        share_by_d: Dict[str, float] = {}
        for d in days:
            g = df_all[df_all["time"].dt.normalize() == d]
            t2 = int(g["Valid_A"].astype(bool).sum()) + int(g["Valid_B"].astype(bool).sum())
            vb2 = int(g["Valid_B"].astype(bool).sum())
            share_by_d[pd.Timestamp(d).strftime("%Y-%m-%d")] = (
                (vb2 / t2 * 100.0) if t2 > 0 else 0.0
            )
        d5 = None
        d3 = None
        if end_s in share_by_d:
            keys = sorted(share_by_d.keys())
            idx = keys.index(end_s) if end_s in keys else len(keys) - 1
            if idx >= 5:
                d5 = share - share_by_d[keys[idx - 5]]
            if idx >= 3:
                d3 = share - share_by_d[keys[idx - 3]]
        xs = np.nan
        try:
            _xsdf = df_all.sort_values(["code", "time"]).copy()
            _xsdf["ret1"] = _xsdf.groupby("code")["close"].pct_change()
            xs_s = _xsdf.groupby("time")["ret1"].std() * 100.0
            xs = float(xs_s.loc[as_of_dt]) if as_of_dt in xs_s.index else float("nan")
        except Exception:
            xs = np.nan
        iv = _build_index_vol_by_day(
            data_manager,
            (as_of_dt - timedelta(days=60)).strftime("%Y-%m-%d"),
            end_s,
        ).get(end_s, {})
        regime = classify_transition_regime(
            va,
            vb,
            share,
            d5,
            d3,
            settings,
            index_rvol_20=iv.get("rvol_20"),
            xs_ret_std=xs if np.isfinite(xs) else None,
            index_day_ret=iv.get("ret_pct"),
        )
        if regime == "TRANSITION" and bool(settings.get("transition_ignore_soft", False)):
            regime = "OFF"
        slots_a, slots_b = max_a, max_b
        if regime == "HARD":
            slots_a, slots_b = 0, 0
        elif regime == "TRANSITION":
            slots_a, slots_b = resolve_transition_regime_policy(
                "TRANSITION", max_a, max_b, settings
            )
        block_s1, block_s2 = resolve_transition_runtime_blocks(regime, settings)
        _ret_s = iv.get("ret_pct")
        _ret_txt = (
            f"{float(_ret_s):+.2f}%"
            if _ret_s is not None and np.isfinite(float(_ret_s))
            else "n/a"
        )
        log(
            f"[Transition] {end_s} 레짐={regime} Valid_A={va} Valid_B={vb} "
            f"share={share:.1f}% d5={d5 if d5 is not None else 'n/a'} "
            f"idx_ret={_ret_txt} "
            f"→ 슬롯 A={slots_a}/B={slots_b} S1차단={block_s1} S2차단={block_s2}"
        )
        result.update(
            {
                "regime": regime,
                "slots_a": slots_a,
                "slots_b": slots_b,
                "block_s1": block_s1,
                "block_s2": block_s2,
                "as_of": end_s,
            }
        )
        return result
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] 레짐 판정 실패(OFF): {ex}")
        except Exception:
            pass
        return result


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
    KOSPI 200을 스캔해 Track A(눌림)·Track B(추세) 유니버스를 **분리** 선정합니다.
    prepare_dynamic_universe 와 동일 필터(거래량 분출 A/B, RSI·대금 기준 분리 등)를 사용하며,
    상위 max_slots_a + max_slots_b 를 합산해 CSV/GUI에 반환합니다.

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
        univ_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
    except Exception:
        univ_choice = "KODEX200"
    if univ_choice not in ("KODEX200", "KOSPI200"):
        univ_choice = "KODEX200"

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
    
    # 1. 대상 유니버스 확보 (KOSPI 200) — KRX·pykrx 미사용, kodex200.csv(및 대체 경로) 고정
    tickers = []
    if target_date:
        dt_str = target_date.strftime("%Y%m%d")
        # KOSPI200 선택 시 kospi_universe.csv에서 상위 top_n만 사용
        top_n = 200
        try:
            top_n = int(settings.get("kospi_top_n", 200))
        except Exception:
            top_n = 200
        try:
            tickers = (
                load_kospi_universe_codes(
                    logger_callback=logger_callback,
                    top_n=top_n,
                    pit_asof=target_date,
                    allow_fdr_generate=False,
                )
                if univ_choice == "KOSPI200"
                else load_kodex200_universe_codes(logger_callback=logger_callback)
            )
        except Exception as e:
            try:
                logging.warning("과거 선정 유니버스 로드 예외: %s", e, exc_info=True)
            except Exception:
                pass
            logger_callback(f"  [Warn] 유니버스 CSV 로드 오류: {e}")
            tickers = []
        if not tickers:
            logger_callback("  [Info] FDR StockListing에서 상위 종목을 추출합니다. (KRX 미사용)")
            try:
                df_fdr = fdr.StockListing('KOSPI')
                df_fdr['Code'] = df_fdr['Code'].astype(str)
                df_fdr = df_fdr[df_fdr['Code'].str.endswith('0')]
                m_col = 'Marcap' if 'Marcap' in df_fdr.columns else 'MarketCap'
                if m_col not in df_fdr.columns:
                    m_col = df_fdr.columns[-1]
                tickers = (
                    df_fdr.sort_values(by=m_col, ascending=False).head(int(top_n))['Code']
                    .astype(str).str.zfill(6)
                    .tolist()
                )
            except Exception as e:
                try:
                    logging.error("FDR KOSPI 상위 200 fallback 실패: %s", e, exc_info=True)
                except Exception:
                    pass
                logger_callback(f"  [Error] FDR fallback 실패: {e}")
        if not tickers:
            logger_callback(f"[Error] [{dt_str}] 구성 종목 리스트를 확보하지 못했습니다.")
            return pd.DataFrame()
        name_map: dict[str, str] = {}
        try:
            df_fdr_names = fdr.StockListing('KOSPI')
            name_map = dict(
                zip(
                    df_fdr_names['Code'].astype(str).str.zfill(6),
                    df_fdr_names['Name'],
                )
            )
        except Exception as e:
            try:
                logging.info("FDR 종목명 매핑 생략: %s", e)
            except Exception:
                pass
        df_top = pd.DataFrame(
            [{'Code': t, 'Name': name_map.get(t, t)} for t in tickers]
        )
    else:
        # 실시간: 선택값에 따라 유니버스 소스 분기
        if univ_choice == "KOSPI200":
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
        else:
            # KODEX200: 기존 로직 유지
            try:
                from core.defaults import DEFAULT_SETTINGS

                _merged = dict(DEFAULT_SETTINGS)
                _merged.update(settings or {})
            except Exception:
                _merged = dict(settings or {})
            try:
                from core.kodex200_universe import resolve_kodex200_universe

                df_top = resolve_kodex200_universe(
                    _merged, count=200, log=logger_callback
                )
            except Exception as ex:
                logger_callback(f"⚠️ [선정] KODEX200 유니버스 모듈 실패, 레거시 경로로 시도: {ex}")
                input_file = "kodex200.csv"
                if os.path.exists(input_file):
                    df_top = pd.read_csv(
                        input_file, dtype={"종목코드": str, "Code": str}
                    )
                    if "종목코드" in df_top.columns:
                        df_top.rename(
                            columns={"종목코드": "Code", "종목명": "Name"},
                            inplace=True,
                        )
                else:
                    df_kospi = fdr.StockListing("KOSPI")
                    df_kospi = df_kospi[df_kospi["Code"].str.endswith("0")]
                    df_top = df_kospi.sort_values(by="Marcap", ascending=False).head(200)

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

    # 실시간(api_only)은 DB 일봉 미사용. 과거일(target_date) 선정은 백테스트 DB 유지.
    use_dm_daily = bool(
        data_manager is not None and (target_date is not None or not api_only_live)
    )

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
                    start_s = (end_dt - timedelta(days=300)).strftime('%Y-%m-%d')
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
                        end_anchor = datetime(
                            req_d.year, req_d.month, req_d.day, 15, 30, 0
                        )
                except Exception:
                    end_anchor = datetime.now()
                max_d = end_anchor.strftime('%Y-%m-%d')
                try:
                    start_s = (end_anchor - timedelta(days=250)).strftime('%Y-%m-%d')
                except Exception:
                    start_s = (datetime.now() - timedelta(days=250)).strftime('%Y-%m-%d')
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
                    _raw_lb = settings.get(
                        "fdr_krx_lookback_days_try", (280, 200, 150, 120)
                    )
                    lookbacks: Tuple[int, ...] = ()
                    if isinstance(_raw_lb, (list, tuple)) and len(_raw_lb) > 0:
                        try:
                            lookbacks = tuple(int(x) for x in _raw_lb)
                        except (TypeError, ValueError):
                            lookbacks = ()
                    if not lookbacks:
                        lookbacks = (280, 200, 150, 120)
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

            # 레거시 선택 로직(Track A/B v2 OFF)을 제거: 항상 v2 기준으로 판정합니다.
            use_track_a = _settings_bool_sel(settings, "use_sel_track_a", True)
            use_track_b = _settings_bool_sel(settings, "use_sel_track_b", True)

            aux = None
            aux_b = None
            if use_track_a:
                aux = _compute_sel_a_aux_from_daily_tail(df_valid, last_full, settings)
            if use_track_b:
                aux_b = _compute_sel_b_aux_from_daily_tail(df_valid, last_full, settings)

            # v2 경로에서는 _valid_a/_valid_b 컬럼이 의미 없으므로 0으로 고정
            valid_a = False
            score_a_place = 0.0
            valid_b_place = False
            score_b_place = 0.0

            has_a = use_track_a
            has_b = use_track_b
            ok_row = (has_a and aux is not None) or (has_b and aux_b is not None)
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
                "_valid_a": valid_a,
                "_score_a": score_a_place,
                "_valid_b": valid_b_place,
                "_score_b": score_b_place,
            }
            if aux:
                res.update(aux)
            if aux_b:
                res.update(aux_b)
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
        if _settings_bool_sel(settings, "use_sel_track_a", True):
            selected = _apply_sel_a_live_cross_section(selected, settings)
    except Exception as e:
        try:
            logger_callback(f"⚠️ [선정] Track A v2 교차섹션 적용 실패: {e}")
        except Exception:
            pass
    try:
        if _settings_bool_sel(settings, "use_sel_track_b", True):
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
        f"(Track A·B 분리 상한 적용, 동적 백테 유니버스와 동일 로직)"
    )
    return df_result


def _apply_sel_track_a_batch_v2(
    df_all: pd.DataFrame,
    settings: dict,
    min_억: float,
    conv: float,
) -> None:
    """
    ``prepare_dynamic_universe`` 일괄 패널용 Track A v2.
    상대강도(일자 교차섹션 분위) + 고가대비 되돌림 + 이평근접 + ATR%·갭 + 대금.
    """
    try:
        g = df_all.groupby("code", sort=False)
        if "open" not in df_all.columns:
            df_all["open"] = df_all["close"]
        for col in ("high", "low"):
            if col not in df_all.columns:
                df_all[col] = df_all["close"]

        rs_n = max(1, int(float(settings.get("sel_a_rs_lookback", 5))))
        clag = g["close"].shift(rs_n)
        df_all["_sel_ret5_raw"] = (df_all["close"] / clag.replace(0, np.nan) - 1.0) * 100.0

        pb_n = max(5, int(float(settings.get("sel_a_pb_lookback", 20))))
        hh = g["high"].transform(lambda x: x.rolling(pb_n, min_periods=pb_n).max())
        df_all["_sel_pb_pct"] = (hh - df_all["close"]) / hh.replace(0, np.nan) * 100.0

        prev_c = g["close"].shift(1)
        tr = np.maximum(
            df_all["high"] - df_all["low"],
            np.maximum((df_all["high"] - prev_c).abs(), (df_all["low"] - prev_c).abs()),
        )
        atr_w = max(5, int(float(settings.get("sel_a_atr_period", 14))))
        df_all["_sel_tr"] = tr
        atr_roll = g["_sel_tr"].transform(
            lambda x: x.rolling(atr_w, min_periods=atr_w).mean()
        )
        df_all["_sel_atr_pct"] = atr_roll / df_all["close"].replace(0, np.nan)

        o0 = df_all["open"].astype(float)
        pc = g["close"].shift(1)
        df_all["_sel_gap_pct"] = ((o0 - pc) / pc.replace(0, np.nan) * 100.0).replace(
            [np.inf, -np.inf], np.nan
        )

        ma5 = df_all["MA5"].astype(float)
        ma20 = df_all["MA20"].astype(float)
        lc = df_all["close"].astype(float)
        m20n = float(settings.get("sel_a_ma20_near_pct", 0.045))
        eps = float(settings.get("sel_a_ma5_eps_pct", 0.004))
        near20 = ma20 > 0
        near20 = near20 & ((lc / ma20.replace(0, np.nan) - 1.0).abs() <= m20n)
        if _settings_bool_sel(settings, "sel_a_require_close_ge_ma5", True):
            ge5 = (ma5 > 0) & (lc >= ma5 * (1.0 - eps))
            df_all["_sel_ma_ok"] = near20 | ge5
        else:
            df_all["_sel_ma_ok"] = near20

        c1 = df_all["AvgVal5"] >= (min_억 * 100000000)
        c5 = df_all["OSC"] > 0 if _settings_bool_sel(settings, "sel_a_require_osc_positive", True) else pd.Series(True, index=df_all.index)
        c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
        if not _settings_bool_sel(settings, "sel_a_require_ma60", True):
            c_ma60 = pd.Series(True, index=df_all.index)

        burst_ok = (df_all["VolBurstActive_A"] == 1) if _settings_bool_sel(
            settings, "sel_a_require_vol_burst", False
        ) else pd.Series(True, index=df_all.index)

        rmin = float(settings.get("sel_rsi_min", 35.0))
        rmax = float(settings.get("sel_rsi_max", 70.0))
        rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
        if not _settings_bool_sel(settings, "sel_a_require_sel_rsi_band", False):
            rsi_ok = pd.Series(True, index=df_all.index)

        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.28))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.92))
        df_all["_sel_rs_pct"] = df_all.groupby("time")["_sel_ret5_raw"].rank(
            pct=True, ascending=True
        )
        pb_lo = float(settings.get("sel_a_pb_min_pct", 1.5))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.006))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        gap_mx = float(settings.get("sel_a_gap_abs_max_pct", 6.0))

        m_rs = df_all["_sel_ret5_raw"].notna() & df_all["_sel_rs_pct"].between(
            rk_lo, rk_hi, inclusive="both"
        )
        m_pb = df_all["_sel_pb_pct"].notna() & df_all["_sel_pb_pct"].between(
            pb_lo, pb_hi, inclusive="both"
        )
        m_atr = df_all["_sel_atr_pct"].notna() & df_all["_sel_atr_pct"].between(
            atr_lo, atr_hi, inclusive="both"
        )
        m_gap = df_all["_sel_gap_pct"].notna() & (df_all["_sel_gap_pct"].abs() <= gap_mx)

        va = (
            m_rs
            & m_pb
            & m_atr
            & m_gap
            & df_all["_sel_ma_ok"]
            & c1
            & c5
            & c_ma60
            & burst_ok
            & rsi_ok
        )
        df_all["Valid_A"] = va.fillna(False)

        mid_pb = (pb_lo + pb_hi) / 2.0
        half_w = max((pb_hi - pb_lo) / 2.0, 1e-6)
        pb_score = (
            (1.0 - (df_all["_sel_pb_pct"] - mid_pb).abs() / half_w).clip(lower=0.0, upper=1.0)
            * 100.0
        )
        rs_part = df_all["_sel_rs_pct"].fillna(0.0) * 55.0
        pb_part = pb_score * 0.28
        ma_part = df_all["_sel_ma_ok"].astype(float) * 18.0
        liq_part = (
            (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
        )
        osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
        mode = _sel_score_mode(settings)
        if _use_score_a_s1_pnl_aligned(mode):
            sc = _score_a_s1_pnl_aligned_series(df_all, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_a_fwd_aligned_series(df_all, settings)
        else:
            sc = (rs_part + pb_part + ma_part + liq_part + osc_b).clip(lower=0.0, upper=100.0)
        df_all["Score_A"] = np.where(df_all["Valid_A"], np.round(sc, 1), 0.0)
    except Exception as e:
        try:
            logging.warning("_apply_sel_track_a_batch_v2 실패, 레거시로 폴백: %s", e, exc_info=True)
        except Exception:
            pass
        c1 = df_all["AvgVal5"] >= (min_억 * 100000000)
        c5 = df_all["OSC"] > 0
        c_ma60 = df_all["MA60"].notna()
        use_a = bool(settings.get("use_sel_track_a", True))
        c2_a = (df_all["MA_Diff"] <= conv) if use_a else pd.Series(False, index=df_all.index)
        c3_a = (df_all["RSI"] >= float(settings.get("sel_rsi_min", 35.0))) & (
            df_all["RSI"] <= float(settings.get("sel_rsi_max", 70.0))
        )
        c4_a = df_all["VolBurstActive_A"] == 1
        df_all["Valid_A"] = c1 & c4_a & c5 & c_ma60 & c2_a & c3_a
        df_all["Score_A"] = (
            ((1 - (df_all["MA_Diff"] / conv)) * 100).clip(lower=0) * 0.4
            + (df_all["RSI"] / 70 * 100).clip(upper=100) * 0.3
            + (df_all["AvgVal5"] / 50000000000 * 100).clip(upper=100) * 0.2
            + ((1 - (df_all["Pos_Diff"] / 0.03)) * 100).clip(lower=0) * 0.1
        )
        df_all.loc[df_all["OSC"] > df_all["OSC_Prev"], "Score_A"] += 10.0


def _apply_sel_track_b_batch_v2(
    df_all: pd.DataFrame,
    settings: dict,
    *,
    with_b_debug: bool = False,
) -> None:
    """
    ``prepare_dynamic_universe`` 일괄 패널용 Track B v2.
    단기 RS(일자 분위) + 고가 대비 종가 비율 + MA20 기울기 + 정배열 + ATR%·갭 + 대금(B) + 분출·RSI·OSC.
    """
    try:
        g = df_all.groupby("code", sort=False)
        if "open" not in df_all.columns:
            df_all["open"] = df_all["close"]
        for col in ("high", "low"):
            if col not in df_all.columns:
                df_all[col] = df_all["close"]

        rs_n = max(1, int(float(settings.get("sel_b_rs_lookback", 5))))
        clag = g["close"].shift(rs_n)
        df_all["_sel_b_ret_raw"] = (df_all["close"] / clag.replace(0, np.nan) - 1.0) * 100.0

        hi_n = max(5, int(float(settings.get("sel_b_hi_lookback", 20))))
        hh = g["high"].transform(lambda x: x.rolling(hi_n, min_periods=hi_n).max())
        df_all["_sel_b_hi_pct"] = (
            df_all["close"] / hh.replace(0, np.nan) * 100.0
        ).replace([np.inf, -np.inf], np.nan)

        prev_c = g["close"].shift(1)
        tr = np.maximum(
            df_all["high"] - df_all["low"],
            np.maximum((df_all["high"] - prev_c).abs(), (df_all["low"] - prev_c).abs()),
        )
        atr_w = max(5, int(float(settings.get("sel_b_atr_period", 14))))
        df_all["_sel_b_tr"] = tr
        atr_roll = g["_sel_b_tr"].transform(
            lambda x: x.rolling(atr_w, min_periods=atr_w).mean()
        )
        df_all["_sel_b_atr_pct"] = atr_roll / df_all["close"].replace(0, np.nan)

        o0 = df_all["open"].astype(float)
        pc = g["close"].shift(1)
        df_all["_sel_b_gap_pct"] = ((o0 - pc) / pc.replace(0, np.nan) * 100.0).replace(
            [np.inf, -np.inf], np.nan
        )

        slope_lb = max(2, int(float(settings.get("sel_b_ma20_slope_lookback", 5))))
        ma20_s = g["MA20"].shift(slope_lb)
        df_all["_sel_b_ma20_slope"] = (
            df_all["MA20"].astype(float) / ma20_s.replace(0, np.nan) - 1.0
        )
        slope_min = float(settings.get("sel_b_ma20_slope_min", 0.001))
        df_all["_sel_b_slope_ok"] = df_all["_sel_b_ma20_slope"] >= slope_min

        lc = df_all["close"].astype(float)
        ma20 = df_all["MA20"].astype(float)
        ma60 = df_all["MA60"].astype(float)
        ma5 = df_all["MA5"].astype(float)
        df_all["_sel_b_trend_ok"] = (ma20 > ma60) & (lc > ma20) & (ma20 > 0) & (ma60 > 0)
        if _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult_b2 = _sel_b_ma5_close_mult(settings)
            ge5 = (ma5 > 0) & (lc >= ma5 * mult_b2)
            df_all["_sel_b_trend_ok"] = df_all["_sel_b_trend_ok"] & ge5

        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 45.0)) * 100000000
        c1_b = df_all["AvgVal5"] >= sel_b_min_val

        c5 = df_all["OSC"] > 0 if _settings_bool_sel(settings, "sel_b_require_osc_positive", True) else pd.Series(
            True, index=df_all.index
        )
        c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
        if not _settings_bool_sel(settings, "sel_b_require_ma60", True):
            c_ma60 = pd.Series(True, index=df_all.index)

        burst_ok = (df_all["VolBurstActive_B"] == 1) if _settings_bool_sel(
            settings, "sel_b_require_vol_burst", True
        ) else pd.Series(True, index=df_all.index)

        rmin = float(settings.get("sel_b_rsi_min", 45.0))
        rmax = float(settings.get("sel_b_rsi_max", 75.0))
        rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
        if not _settings_bool_sel(settings, "sel_b_require_sel_rsi_band", True):
            rsi_ok = pd.Series(True, index=df_all.index)

        osc_rise_ok = (df_all["OSC"] > df_all["OSC_Prev"]) if _settings_bool_sel(
            settings, "sel_b_require_osc_rising", False
        ) else pd.Series(True, index=df_all.index)

        rk_lo = float(settings.get("sel_b_rs_rank_min", 0.38))
        rk_hi = float(settings.get("sel_b_rs_rank_max", 0.97))
        df_all["_sel_b_rs_pct"] = df_all.groupby("time")["_sel_b_ret_raw"].rank(
            pct=True, ascending=True
        )
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        gap_mx = float(settings.get("sel_b_gap_abs_max_pct", 7.0))

        m_rs = df_all["_sel_b_ret_raw"].notna() & df_all["_sel_b_rs_pct"].between(
            rk_lo, rk_hi, inclusive="both"
        )
        m_hi = df_all["_sel_b_hi_pct"].notna() & df_all["_sel_b_hi_pct"].between(
            hi_lo, hi_hi, inclusive="both"
        )
        m_atr = df_all["_sel_b_atr_pct"].notna() & df_all["_sel_b_atr_pct"].between(
            atr_lo, atr_hi, inclusive="both"
        )
        m_gap = df_all["_sel_b_gap_pct"].notna() & (df_all["_sel_b_gap_pct"].abs() <= gap_mx)

        vb = (
            m_rs
            & m_hi
            & m_atr
            & m_gap
            & df_all["_sel_b_trend_ok"]
            & df_all["_sel_b_slope_ok"]
            & c1_b
            & c5
            & c_ma60
            & burst_ok
            & rsi_ok
            & osc_rise_ok
        )
        df_all["Valid_B"] = vb.fillna(False)

        half_hi = max((hi_hi - hi_lo) / 2.0, 1e-6)
        hip = df_all["_sel_b_hi_pct"]
        hi_score = (
            (1.0 - (hip - (hi_lo + hi_hi) / 2.0).abs() / half_hi).clip(lower=0.0, upper=1.0) * 100.0
        )
        rs_part = df_all["_sel_b_rs_pct"].fillna(0.0) * 52.0
        hi_part = hi_score * 0.28
        tr_st = ((lc / ma20.replace(0, np.nan) - 1.0) * 100.0).clip(lower=0.0, upper=10.0) * 10.0
        tr_part = tr_st * 0.15
        slope_bonus = df_all["_sel_b_slope_ok"].astype(float) * 10.0
        liq_part = (
            (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
        )
        osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
        mode = _sel_score_mode(settings)
        if _use_score_b_s1_pnl_aligned(mode):
            sc = _score_b_s1_pnl_aligned_series(df_all, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_b_fwd_aligned_series(df_all, settings)
        else:
            sc = (rs_part + hi_part + tr_part + slope_bonus + liq_part + osc_b).clip(
                lower=0.0, upper=100.0
            )
        df_all["Score_B"] = np.where(df_all["Valid_B"], np.round(sc, 1), 0.0)
        if with_b_debug:
            try:
                df_all["Dbg_B1_avg5_ge_selB"] = c1_b.astype(bool)
                df_all["Dbg_B2_trend_mas"] = df_all["_sel_b_trend_ok"].astype(bool)
                df_all["Dbg_B3_rsi_range"] = rsi_ok.astype(bool)
                df_all["Dbg_B4_volburst1"] = burst_ok.astype(bool)
                df_all["Dbg_C5_osc_pos"] = c5.astype(bool)
                df_all["Dbg_C_ma60_ok"] = c_ma60.astype(bool)
            except Exception:
                pass
    except Exception as e:
        try:
            logging.warning("_apply_sel_track_b_batch_v2 실패, 레거시로 폴백: %s", e, exc_info=True)
        except Exception:
            pass
        use_b = bool(settings.get("use_sel_track_b", True))
        c2_b = ((df_all["MA20"] > df_all["MA60"]) & (df_all["close"] > df_all["MA20"])) if use_b else pd.Series(
            False, index=df_all.index
        )
        if use_b and _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult_fb = _sel_b_ma5_close_mult(settings)
            m5f = df_all["MA5"].astype(float)
            lcf = df_all["close"].astype(float)
            c2_b = c2_b & (m5f > 0) & (lcf >= m5f * mult_fb)
        sel_b_rsi_min = float(settings.get("sel_b_rsi_min", 45.0))
        sel_b_rsi_max = float(settings.get("sel_b_rsi_max", 75.0))
        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 60.0)) * 100000000
        c3_b = (df_all["RSI"] >= sel_b_rsi_min) & (df_all["RSI"] <= sel_b_rsi_max)
        c1_b = df_all["AvgVal5"] >= sel_b_min_val
        c4_b = df_all["VolBurstActive_B"] == 1
        c5 = df_all["OSC"] > 0
        c_ma60 = df_all["MA60"].notna()
        df_all["Valid_B"] = c1_b & c2_b & c3_b & c4_b & c5 & c_ma60
        trend_strength = ((df_all["close"] / df_all["MA20"] - 1) * 100).clip(lower=0, upper=10) * 10
        df_all["Score_B"] = (
            trend_strength * 0.4
            + (df_all["RSI"] / 70 * 100).clip(upper=100) * 0.3
            + (df_all["AvgVal5"] / 50000000000 * 100).clip(upper=100) * 0.2
            + (df_all["OSC"].clip(lower=0) / (df_all["OSC"].abs().max() + 1e-10) * 100) * 0.1
        )
        df_all.loc[df_all["OSC"] > df_all["OSC_Prev"], "Score_B"] += 10.0


def _score_dynamic_universe_daily_ohlcv(
    df_all: pd.DataFrame,
    settings: dict,
    *,
    with_b_debug: bool = False,
) -> None:
    """
    ``prepare_dynamic_universe`` 와 동일: 일봉 OHLCV → 지표·Valid_A/Valid_B(트랙 선정)까지 in-place.
    with_b_debug=True 이면 Track B 부분조건 열(Dbg_*)을 붙여 일자별 진단에 쓴다.
    """
    # 4. 벡터 지표
    df_all["close"] = df_all["close"].astype(float)
    df_all["volume"] = df_all["volume"].astype(float)
    df_all["trde_val"] = df_all["close"] * df_all["volume"]
    df_all["MA5"] = df_all.groupby("code")["close"].rolling(5).mean().reset_index(0, drop=True)
    df_all["MA20"] = df_all.groupby("code")["close"].rolling(20).mean().reset_index(0, drop=True)
    df_all["MA60"] = df_all.groupby("code")["close"].rolling(60).mean().reset_index(0, drop=True)
    delta = df_all.groupby("code")["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ema_up = gain.groupby(df_all["code"]).ewm(com=13, adjust=False).mean().reset_index(0, drop=True)
    ema_down = loss.groupby(df_all["code"]).ewm(com=13, adjust=False).mean().reset_index(0, drop=True)
    df_all["RSI"] = 100 - (100 / (1 + ema_up / (ema_down + 1e-10)))
    m_f, m_sl, m_sg = (
        int(settings.get("macd_fast_daily", 12)),
        int(settings.get("macd_slow_daily", 26)),
        int(settings.get("macd_sig_daily", 9)),
    )
    ema_f = df_all.groupby("code")["close"].ewm(span=m_f, adjust=False).mean().reset_index(0, drop=True)
    ema_s = df_all.groupby("code")["close"].ewm(span=m_sl, adjust=False).mean().reset_index(0, drop=True)
    df_all["MACD"] = ema_f - ema_s
    macd_s = df_all.groupby("code")["MACD"].ewm(span=m_sg, adjust=False).mean().reset_index(0, drop=True)
    df_all["OSC"] = df_all["MACD"] - macd_s

    min_억 = float(settings.get("min_trde_val", 40.0))
    conv = float(settings.get("ma_convergence", 3.0)) / 100.0
    vol_lookback = int(settings.get("vol_lookback", 10))
    df_all["AvgVal5"] = df_all.groupby("code")["trde_val"].rolling(5).mean().reset_index(0, drop=True)
    df_all["MA_Diff"] = (df_all["MA5"] - df_all["MA20"]).abs() / df_all["MA20"]
    df_all["Pos_Diff"] = (df_all["close"] - df_all["MA20"]).abs() / df_all["MA20"]
    df_all["VolRatio"] = df_all["volume"] / df_all.groupby("code")["volume"].shift(1)
    df_all["OSC_Prev"] = df_all.groupby("code")["OSC"].shift(1)

    vol_ratio_a = float(settings.get("vol_ratio", 1.5))
    df_all["VolBurst_A"] = (df_all["VolRatio"] >= vol_ratio_a) & (
        df_all["close"] > df_all.groupby("code")["close"].shift(1)
    )
    df_all["VolBurstActive_A"] = (
        df_all.groupby("code")["VolBurst_A"].rolling(vol_lookback).max().reset_index(0, drop=True)
    )
    vol_ratio_b = float(settings.get("sel_b_vol_ratio", vol_ratio_a))
    vol_lookback_b = int(settings.get("sel_b_vol_lookback", vol_lookback))
    df_all["VolBurst_B"] = (df_all["VolRatio"] >= vol_ratio_b) & (
        df_all["close"] > df_all.groupby("code")["close"].shift(1)
    )
    df_all["VolBurstActive_B"] = (
        df_all.groupby("code")["VolBurst_B"].rolling(vol_lookback_b).max().reset_index(0, drop=True)
    )

    use_a = bool(settings.get("use_sel_track_a", True))
    if use_a:
        _apply_sel_track_a_batch_v2(df_all, settings, min_억, conv)
    else:
        # use_a=False 여도 아래 Track B union에서 column 존재가 필요합니다.
        df_all["Valid_A"] = pd.Series(False, index=df_all.index)
        df_all["Score_A"] = pd.Series(0.0, index=df_all.index)

    # Track B·진단 열과 공유: OSC·MA60 (A v2 경로에서는 위 블록 안에서만 쓰였을 수 있음)
    c5 = df_all["OSC"] > 0
    c_ma60 = df_all["MA60"].notna()

    use_b = bool(settings.get("use_sel_track_b", True))
    if use_b:
        _apply_sel_track_b_batch_v2(df_all, settings, with_b_debug=with_b_debug)
    else:
        df_all["Valid_B"] = pd.Series(False, index=df_all.index)
        df_all["Score_B"] = pd.Series(0.0, index=df_all.index)
    df_all["Valid"] = df_all["Valid_A"] | df_all["Valid_B"]
    df_all["Score"] = df_all[["Score_A", "Score_B"]].max(axis=1)


def prepare_dynamic_universe(settings, start_date, end_date, data_manager, logger_callback):
    """
    [v17.7] True Batching: 전 기간 데이터를 한 번에 조회하고 Pandas 벡터 연산으로 스크리닝 (성능 10x+ 향상)
    """
    # [v18.8] Date Normalization (YYYY-MM-DD)
    start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
    end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
    
    logger_callback("\n" + "!" * 50)
    logger_callback(f"[Batch-v2] 유니버스 사전 선정 시작: {start_date} ~ {end_date}")
    
    # 1. 대상 영업일 리스트 확보
    trading_days = data_manager.get_trading_days(start_date, end_date)
    if not trading_days:
        logger_callback("  [Error] 거래일 데이터를 찾을 수 없습니다.")
        return {}

    # 2. 주기별 코스피 유니버스 캐싱 (monthly|weekly|biweekly|daily|bimonthly|quarterly)
    universe_cache = {}
    all_days = pd.date_range(start=start_date, end=end_date, freq='B')
    union_tickers = set()
    from core.marcap_universe import (
        get_kospi_top_n_for_refresh,
        normalize_refresh_mode,
        universe_period_key,
    )

    refresh_mode = normalize_refresh_mode(settings.get("kospi_universe_refresh"))
    # monthly 외에는 기간 내 주기 갱신 — lean 단일 유니버스 생략
    use_period_univ = refresh_mode != "monthly"

    def get_period_universe(dt):
        """유니버스: KOSPI200=marcap as-of(갱신주기), KODEX200=고정 CSV."""
        idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
        d_ts = pd.Timestamp(dt).normalize()
        cache_key = universe_period_key(d_ts, refresh_mode)
        if cache_key in universe_cache:
            return universe_cache[cache_key]
        try:
            top_n = 200
            try:
                top_n = int(settings.get("kospi_top_n", 200))
            except Exception:
                top_n = 200
            if idx_choice == "KOSPI200":
                tickers, asof_str = get_kospi_top_n_for_refresh(
                    d_ts,
                    top_n,
                    refresh_mode,
                    logger_callback=(
                        None if refresh_mode == "daily" else logger_callback
                    ),
                )
                if (
                    tickers
                    and refresh_mode == "monthly"
                    and logger_callback is not None
                ):
                    try:
                        logger_callback(
                            f"    - [Marcap] {d_ts.strftime('%Y%m')} as-of {asof_str or '?'} "
                            f"KOSPI top{top_n} → {len(tickers)}종목"
                        )
                    except Exception:
                        pass
            else:
                tickers = load_kodex200_universe_codes(logger_callback=logger_callback)
        except Exception as e:
            try:
                logging.warning("배치 유니버스 로드 실패: %s", e, exc_info=True)
            except Exception:
                pass
            tickers = []
        if tickers:
            universe_cache[cache_key] = tickers
        return tickers

    # Lean Mode: 월별 모드 + 6개월 미만만 단일 유니버스. 그 외는 주기 갱신.
    duration_days = (all_days[-1] - all_days[0]).days if not all_days.empty else 0

    if refresh_mode == "daily":
        logger_callback(
            f"  [Batch] 일별 marcap as-of 유니버스 순회… ({duration_days}일)"
        )
        for d in all_days:
            tickers = get_period_universe(d)
            if tickers:
                union_tickers.update(tickers)
        logger_callback(f"    - 일별 유니버스 누적 {len(union_tickers)}종목")
    elif use_period_univ:
        logger_callback(
            f"  [Batch] {refresh_mode} marcap as-of 유니버스 순회… ({duration_days}일)"
        )
        last_key = ""
        for d in all_days:
            curr_key = universe_period_key(d, refresh_mode)
            if curr_key != last_key or not union_tickers:
                union_tickers.update(get_period_universe(d))
                last_key = curr_key
                logger_callback(
                    f"    - {curr_key} 유니버스 체크 완료 (누적 {len(union_tickers)}종목)"
                )
    elif duration_days < 180:
        logger_callback(
            f"  [Batch] Lean 모드 활성화 ({duration_days}일): "
            f"시작월 marcap as-of 단일 유니버스"
        )
        union_tickers = set(get_period_universe(all_days[0]))
    else:
        logger_callback(
            "  [Batch] 정밀 모드: 월별 marcap as-of 유니버스 순회…"
        )
        last_m = ""
        for d in all_days:
            curr_m = d.strftime("%Y%m")
            if curr_m != last_m or not union_tickers:
                union_tickers.update(get_period_universe(d))
                last_m = curr_m
                logger_callback(f"    - {d.strftime('%Y-%m')} 유니버스 체크 완료 (누적 {len(union_tickers)}종목)")
    
    if not union_tickers:
        logger_callback(
            "  [Warning] 유니버스 파일에서 유니버스를 읽지 못해 추가 시도합니다."
        )
        candidate_files = ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]
        if str(settings.get("universe_index_choice", "KODEX200")).strip().upper() == "KOSPI200":
            candidate_files = ["kospi_universe.csv", "kospi_top_500.csv"]
        for f_name in candidate_files:
            if os.path.exists(f_name):
                try:
                    df_k = pd.read_csv(f_name, dtype=str)
                    c_col = 'Code' if 'Code' in df_k.columns else ('종목코드' if '종목코드' in df_k.columns else None)
                    if c_col:
                        # KOSPI 쪽은 kospi_top_n 슬라이스를 먼저 반영합니다.
                        top_n = 200
                        try:
                            top_n = int(settings.get("kospi_top_n", 200))
                        except Exception:
                            top_n = 200
                        vals = df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist()
                        if str(settings.get("universe_index_choice", "KODEX200")).strip().upper() == "KOSPI200":
                            vals = vals[: int(top_n)]
                        union_tickers = set(vals)
                        logger_callback(f"    - {f_name}에서 {len(union_tickers)}종목 로드 완료")
                        break
                except: pass

    if not union_tickers:
        logger_callback("  [Error] 유니버스 종목 확보 실패.")
        return {}

    # 3. 전체 기간 일봉 데이터 한 번에 로드 (True Batching 핵심)
    all_codes = list(union_tickers)
    # 120일 이전 데이터부터 가져와야 지표 계산이 정확함 (MA60 등)
    fetch_start_dt = pd.to_datetime(start_date) - timedelta(days=120)
    fetch_start = fetch_start_dt.strftime('%Y-%m-%d')
    
    logger_callback(f"  [Batch] {len(all_codes)}개 종목의 전체 일봉 데이터 로드 중...")
    placeholders = ",".join(["?"] * len(all_codes))
    query = f"""
        SELECT code, datetime as time, open, high, low, close, volume 
        FROM bars_1d 
        WHERE code IN ({placeholders}) AND datetime BETWEEN ? AND ?
        ORDER BY code, datetime
    """
    params = all_codes + [fetch_start, end_date]
    
    with data_manager._get_connection() as conn:
        df_all = pd.read_sql_query(query, conn, params=params)
    
    if df_all.empty:
        logger_callback("  [Error] DB에서 데이터를 찾을 수 없습니다. (데이터 동기화 필요)")
        return {}

    # [Bugfix] Convert time to datetime to use .dt accessor later
    df_all['time'] = pd.to_datetime(df_all['time'])

    logger_callback(f"  [Batch] {len(df_all)}개 데이터 포인트에 대해 고속 벡터 연산 중...")
    _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)

    # 6. 스케줄 생성 (Track A / Track B 분리)
    logger_callback("  [Batch] 날짜별 상위 종목 추출 중 (Track A/B 분리)...")
    schedule_a = {}
    schedule_b = {}
    schedule = {}  # 기존 호환용 (합집합)
    max_s_a = int(settings.get('max_slots_a', 20))
    max_s_b = int(settings.get('max_slots_b', 10))
    floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    df_sim = df_all[df_all['time'] >= pd.to_datetime(start_date)].copy()
    
    trading_days_list = df_sim['time'].dt.strftime('%Y-%m-%d').unique().tolist()

    # 일별 교차-section 수익률 표준편차(%)
    xs_ret_std_by_day: Dict[str, float] = {}
    try:
        _xs_df = df_sim.sort_values(["code", "time"]).copy()
        _xs_df["ret1"] = _xs_df.groupby("code")["close"].pct_change()
        _xs_s = _xs_df.groupby("time")["ret1"].std() * 100.0
        for t_idx, val in _xs_s.items():
            if val is not None and np.isfinite(float(val)):
                xs_ret_std_by_day[pd.Timestamp(t_idx).strftime("%Y-%m-%d")] = float(val)
    except Exception:
        xs_ret_std_by_day = {}

    # [Transition] 백테스트: 일별 레짐 게이트
    transition_gate_on = _settings_bool_sel(settings, "use_transition_gate", False)
    transition_days = 0
    transition_hard_days = 0
    transition_regime_by_day: Dict[str, str] = {}

    index_vol_by_day: Dict[str, Dict[str, float]] = {}
    if transition_gate_on:
        try:
            fetch_start_iv = (
                pd.to_datetime(start_date) - timedelta(days=60)
            ).strftime("%Y-%m-%d")
            index_vol_by_day = _build_index_vol_by_day(
                data_manager, fetch_start_iv, end_date
            )
        except Exception:
            index_vol_by_day = {}

    # Transition: 일별 valid/share → 5일·3일 변화율
    transition_stats_by_day: Dict[str, Dict[str, float]] = {}
    if transition_gate_on:
        try:
            _t_rows: List[Dict[str, Any]] = []
            for _d_str in trading_days_list:
                if use_period_univ or duration_days >= 180:
                    _univ = get_period_universe(pd.to_datetime(_d_str))
                else:
                    _univ = all_codes
                _dm = (df_sim["time"] == pd.to_datetime(_d_str)) & (
                    df_sim["code"].isin(_univ)
                )
                _day = df_sim[_dm]
                _va = int(_day["Valid_A"].astype(bool).sum()) if "Valid_A" in _day.columns else 0
                _vb = int(_day["Valid_B"].astype(bool).sum()) if "Valid_B" in _day.columns else 0
                _tot = _va + _vb
                _share = (_vb / _tot * 100.0) if _tot > 0 else 0.0
                _t_rows.append({"d": _d_str, "va": _va, "vb": _vb, "share": _share})
            _tdf = pd.DataFrame(_t_rows)
            if not _tdf.empty:
                _tdf["share_delta5"] = _tdf["share"] - _tdf["share"].shift(5)
                _tdf["share_delta3"] = _tdf["share"] - _tdf["share"].shift(3)
                for _, _tr in _tdf.iterrows():
                    transition_stats_by_day[str(_tr["d"])] = {
                        "va": float(_tr["va"]),
                        "vb": float(_tr["vb"]),
                        "share": float(_tr["share"]),
                        "share_delta5": (
                            float(_tr["share_delta5"])
                            if pd.notna(_tr["share_delta5"])
                            else float("nan")
                        ),
                        "share_delta3": (
                            float(_tr["share_delta3"])
                            if pd.notna(_tr["share_delta3"])
                            else float("nan")
                        ),
                    }
        except Exception:
            transition_stats_by_day = {}

    for d_str in trading_days_list:
        if use_period_univ or duration_days >= 180:
            universe = get_period_universe(pd.to_datetime(d_str))
        else:
            universe = all_codes

        day_mask = (df_sim["time"] == pd.to_datetime(d_str)) & (df_sim["code"].isin(universe))

        day_max_a = max_s_a
        day_max_b = max_s_b

        # [Transition] B-share 급락 + 변동성
        if transition_gate_on:
            try:
                _ts = transition_stats_by_day.get(d_str, {})
                _va = int(_ts.get("va", 0))
                _vb = int(_ts.get("vb", 0))
                _share = float(_ts.get("share", 0.0))
                _d5 = _ts.get("share_delta5")
                _d3 = _ts.get("share_delta3")
                _xs = xs_ret_std_by_day.get(d_str)
                _iv = index_vol_by_day.get(d_str, {})
                _tl = classify_transition_regime(
                    _va,
                    _vb,
                    _share,
                    _d5,
                    _d3,
                    settings,
                    index_rvol_20=_iv.get("rvol_20"),
                    xs_ret_std=_xs,
                    index_day_ret=_iv.get("ret_pct"),
                )
                transition_regime_by_day[d_str] = _tl
                _ignore_soft = bool(settings.get("transition_ignore_soft", False))
                if _tl == "HARD":
                    day_max_a, day_max_b = 0, 0
                    transition_hard_days += 1
                elif _tl == "TRANSITION" and not _ignore_soft:
                    day_max_a, day_max_b = resolve_transition_regime_policy(
                        "TRANSITION", day_max_a, day_max_b, settings
                    )
                    transition_days += 1
            except Exception:
                transition_regime_by_day[d_str] = "OFF"

        if transition_gate_on and day_max_a <= 0 and day_max_b <= 0:
            continue

        # Track A 스케줄
        day_a = df_sim[day_mask & df_sim["Valid_A"]]
        if not day_a.empty and day_max_a > 0:
            schedule_a[d_str] = (
                _df_top_by_score_slot_band(
                    day_a,
                    score_col="Score_A",
                    code_col="code",
                    max_n=day_max_a,
                    settings=settings,
                    min_score_floor=floor_sa,
                )["code"]
                .tolist()
            )

        # Track B 스케줄 (NEUTRAL에서도 동등 슬롯 허용 — block은 policy.slots_b=0 일 때만)
        day_b = df_sim[day_mask & df_sim["Valid_B"]]
        if not day_b.empty and day_max_b > 0:
            schedule_b[d_str] = (
                _df_top_by_score_slot_band(
                    day_b,
                    score_col="Score_B",
                    code_col="code",
                    max_n=day_max_b,
                    settings=settings,
                    min_score_floor=floor_sb,
                )["code"]
                .tolist()
            )

        combined = list(dict.fromkeys(schedule_a.get(d_str, []) + schedule_b.get(d_str, [])))
        if combined:
            schedule[d_str] = combined

    if transition_gate_on and (transition_days or transition_hard_days):
        logger_callback(
            f"  [Transition] 레짐 전환: TRANSITION {transition_days}일, HARD {transition_hard_days}일"
        )

    a_days = len(schedule_a)
    b_days = len(schedule_b)
    if not schedule:
        logger_callback("  [Warning] 조건에 맞는 종목이 한 건도 추출되지 않았습니다. 필터 설정을 확인하세요.")
    else:
        logger_callback(f"[Batch] 완료. Track A: {a_days}일, Track B: {b_days}일, 합산: {len(schedule)}일")
    return schedule, schedule_a, schedule_b, transition_regime_by_day


def compute_transition_regime_history(
    settings: dict,
    data_manager: Any,
    *,
    lookback_trading_days: int = 22,
    end_date: Optional[Union[str, datetime]] = None,
) -> List[Dict[str, Any]]:
    """
    최근 N영업일 일별 transition 레짐 이력.

    ``prepare_dynamic_universe`` 와 동일 분류 로직(게이트 ON으로 일시 강제).
    GUI 이력 차트용 — 실전 ``use_transition_gate`` 와 무관하게 분류 결과를 반환한다.

    레짐은 **완료 거래일(data_as_of)** 일봉으로 판정하고, 반환 ``date``/``apply_on`` 은
    그 레짐이 적용되는 **다음 영업일**이다 (헤더 레짐 표시와 동일).
    마지막 데이터일은 **완료된 거래일**로 캡해 미완성 당일 일봉이 섞이지 않게 한다.
    """
    out: List[Dict[str, Any]] = []
    try:
        n = max(5, min(int(lookback_trading_days or 22), 60))
        st = dict(settings or {})
        st["use_transition_gate"] = True

        end_s = pd.to_datetime(end_date or datetime.now()).strftime("%Y-%m-%d")
        if end_date is None:
            try:
                from reporting.krx_trading_calendar import (
                    completed_trading_day_asof_local,
                )

                try:
                    _buf = int(
                        float(st.get("regime_display_eod_buffer_minutes", 30) or 30)
                    )
                except (TypeError, ValueError):
                    _buf = 30
                end_s = completed_trading_day_asof_local(
                    datetime.now(), eod_buffer_minutes=_buf
                ).strftime("%Y-%m-%d")
            except Exception:
                pass
        start_s = (pd.to_datetime(end_s) - timedelta(days=75)).strftime("%Y-%m-%d")

        def _silent(*_a: Any, **_k: Any) -> None:
            pass

        _result = _compute_transition_regime_map_fdr(st, data_manager, start_s, end_s, _silent)
        regime_map: Dict[str, str] = dict(_result or {})

        # KRX 영업일과의 교집합만 사용 (FDR 이상치 일자 방어). 캘린더 실패 시 맵 그대로.
        try:
            from reporting.krx_trading_calendar import trading_days_inclusive

            krx_set = {d.strftime("%Y-%m-%d") for d in trading_days_inclusive(start_s, end_s)}
        except Exception:
            krx_set = set()

        cand = sorted(k for k in regime_map.keys() if k <= end_s)
        if krx_set:
            filtered = [d for d in cand if d in krx_set]
            if filtered:
                cand = filtered
        display_days = cand[-n:]

        labels = {
            "OFF": "유지",
            "TRANSITION": "약화",
            "HARD": "붕괴",
        }
        try:
            from reporting.krx_trading_calendar import next_trading_day as _next_td
        except Exception:
            _next_td = None  # type: ignore

        for d in display_days:
            reg = str(regime_map.get(d, "OFF")).strip().upper()
            if reg not in ("OFF", "TRANSITION", "HARD"):
                reg = "OFF"
            # UI 표시는 적용일(다음 영업일). 데이터 판정일은 data_as_of 에 보관.
            apply_on = d
            try:
                if _next_td is not None:
                    nxt = _next_td(d)
                    if nxt is not None:
                        apply_on = nxt.strftime("%Y-%m-%d")
            except Exception:
                apply_on = d
            out.append(
                {
                    "date": apply_on,
                    "data_as_of": d,
                    "apply_on": apply_on,
                    "regime": reg,
                    "label": labels.get(reg, reg),
                }
            )
    except Exception:
        return out
    return out


def diagnose_dynamic_universe_selection_by_trading_day(
    settings: dict,
    start_date: Union[str, datetime],
    end_date: Union[str, datetime],
    data_manager: Any,
) -> pd.DataFrame:
    """
    `prepare_dynamic_universe`와 동일한 1d 로드·스코어에 대해,
    **영업일(``get_trading_days``)별**로 (1) KODEX 유니버스 행 수, (2) ``Valid_B``/``Valid_A`` 건수,
    (3) Track B 부분조건(Dbg_*)을 만족한 행 수(일당 합)를 집계한다.
    4/10 이후 `schedule`에 없을 때, **'일봉 행 없음'** vs **'Valid_B 전부 False'** 를 구분하는 데 쓴다.
    """
    start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    trading_days: List[str] = list(data_manager.get_trading_days(start_s, end_s) or [])

    def _log(_: str) -> None:
        pass

    all_days = pd.date_range(start=start_s, end=end_s, freq="B")
    duration_days = (all_days[-1] - all_days[0]).days if not all_days.empty else 0
    union_cache: dict[str, list[str]] = {}
    union_tickers: set[str] = set()
    from core.marcap_universe import (
        get_kospi_top_n_for_refresh,
        normalize_refresh_mode,
        universe_period_key,
    )

    refresh_mode = normalize_refresh_mode(settings.get("kospi_universe_refresh"))
    use_period_univ = refresh_mode != "monthly"

    def get_period_universe(dt: Any) -> list[str]:
        d_ts = pd.Timestamp(dt).normalize()
        cache_key = universe_period_key(d_ts, refresh_mode)
        if cache_key in union_cache:
            return union_cache[cache_key]
        try:
            idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
            top_n = 200
            try:
                top_n = int(settings.get("kospi_top_n", 200))
            except Exception:
                top_n = 200
            if idx_choice == "KOSPI200":
                tickers, _ = get_kospi_top_n_for_refresh(
                    d_ts, top_n, refresh_mode, logger_callback=None
                )
            else:
                tickers = load_kodex200_universe_codes(logger_callback=None)
        except Exception:
            tickers = []
        if tickers:
            union_cache[cache_key] = tickers
        return tickers

    try:
        if refresh_mode == "daily":
            for d in all_days:
                union_tickers.update(get_period_universe(d))
        elif use_period_univ:
            last_key = ""
            for d in all_days:
                curr_key = universe_period_key(d, refresh_mode)
                if curr_key != last_key or not union_tickers:
                    union_tickers.update(get_period_universe(d))
                    last_key = curr_key
        elif duration_days < 180:
            union_tickers = set(get_period_universe(all_days[0]))
        else:
            last_m = ""
            for d in all_days:
                curr_m = d.strftime("%Y%m")
                if curr_m != last_m or not union_tickers:
                    union_tickers.update(get_period_universe(d))
                    last_m = curr_m
    except Exception as e:
        try:
            logging.warning("diagnose union: %s", e, exc_info=True)
        except Exception:
            pass
        union_tickers = set()

    if not union_tickers:
        for f_name in ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]:
            if not os.path.isfile(f_name):
                continue
            try:
                df_k = pd.read_csv(f_name, dtype=str)
                c_col = "Code" if "Code" in df_k.columns else (
                    "종목코드" if "종목코드" in df_k.columns else None
                )
                if c_col:
                    union_tickers = set(df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist())
                    break
            except Exception:
                pass

    if not union_tickers:
        return pd.DataFrame(
            columns=[
                "date",
                "kodex_rows_해당일",
                "Valid_A_합",
                "Valid_B_합",
                "B1_avg5",
                "B2_정배열",
                "B3_rsi",
                "B4_분출B",
                "C5_osc",
                "C_ma60",
                "nonempty_schedule",
                "비고",
            ]
        )

    all_codes = list(union_tickers)
    fetch_start_dt = pd.to_datetime(start_s) - timedelta(days=120)
    fetch_start = fetch_start_dt.strftime("%Y-%m-%d")
    ph = ",".join(["?"] * len(all_codes))
    q = f"""
        SELECT code, datetime as time, open, high, low, close, volume
        FROM bars_1d
        WHERE code IN ({ph}) AND datetime BETWEEN ? AND ?
        ORDER BY code, datetime
    """
    try:
        with data_manager._get_connection() as conn:
            df_all = pd.read_sql_query(q, conn, params=all_codes + [fetch_start, end_s])
    except Exception as e:
        try:
            logging.error("diagnose: bars_1d 조회 실패: %s", e, exc_info=True)
        except Exception:
            pass
        return pd.DataFrame()

    if df_all is None or df_all.empty:
        rows_err = []
        for d in trading_days:
            rows_err.append(
                {
                    "date": d,
                    "kodex_rows_해당일": 0,
                    "Valid_A_합": 0,
                    "Valid_B_합": 0,
                    "B1_avg5": 0,
                    "B2_정배열": 0,
                    "B3_rsi": 0,
                    "B4_분출B": 0,
                    "C5_osc": 0,
                    "C_ma60": 0,
                    "nonempty_schedule": 0,
                    "비고": "bars_1d 비어 있음(유니버스 구간)",
                }
            )
        return pd.DataFrame(rows_err)

    try:
        df_all["time"] = pd.to_datetime(df_all["time"])
        if "code" in df_all.columns:
            df_all["code"] = df_all["code"].astype(str).str.zfill(6)
    except Exception:
        pass

    _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=True)
    df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()

    dbg_cols = [
        "Dbg_B1_avg5_ge_selB",
        "Dbg_B2_trend_mas",
        "Dbg_B3_rsi_range",
        "Dbg_B4_volburst1",
        "Dbg_C5_osc_pos",
        "Dbg_C_ma60_ok",
    ]
    for c in dbg_cols:
        if c not in df_sim.columns:
            df_sim[c] = False

    # ``prepare`` 와 동일: 일자별 top(max_slots) 스케줄 키 존재 여부(중복 `prepare` 호출 없음)
    schedule: dict[str, list[str]] = {}
    try:
        max_s_a = int(settings.get("max_slots_a", 20))
        max_s_b = int(settings.get("max_slots_b", 10))
        floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
        floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
        trading_days_list2 = (
            df_sim["time"].dt.strftime("%Y-%m-%d").drop_duplicates().sort_values().tolist()
        )
        for d_str in trading_days_list2:
            try:
                universe2 = (
                    get_period_universe(pd.to_datetime(d_str))
                    if (use_period_univ or duration_days >= 180)
                    else all_codes
                )
            except Exception:
                universe2 = all_codes
            dmask = (df_sim["time"] == pd.to_datetime(d_str)) & (
                df_sim["code"].astype(str).str.zfill(6).isin(
                    {str(x).zfill(6) for x in universe2}
                )
            )
            sa = (
                _df_top_by_score_slot_band(
                    df_sim.loc[dmask & df_sim["Valid_A"]],
                    score_col="Score_A",
                    code_col="code",
                    max_n=max_s_a,
                    settings=settings,
                    min_score_floor=floor_sa,
                )["code"]
                .tolist()
            )
            sb = (
                _df_top_by_score_slot_band(
                    df_sim.loc[dmask & df_sim["Valid_B"]],
                    score_col="Score_B",
                    code_col="code",
                    max_n=max_s_b,
                    settings=settings,
                    min_score_floor=floor_sb,
                )["code"]
                .tolist()
            )
            comb = list(dict.fromkeys([str(x) for x in (sa or [])] + [str(x) for x in (sb or [])]))
            if comb:
                schedule[d_str] = comb
    except Exception as e:
        try:
            logging.warning("diagnose schedule build: %s", e, exc_info=True)
        except Exception:
            pass
        schedule = {}

    out_rows: list[dict[str, Any]] = []
    for d in trading_days:
        try:
            sub = df_sim[df_sim["time"].dt.strftime("%Y-%m-%d") == d]
        except Exception:
            sub = pd.DataFrame()
        n = int(len(sub))
        try:
            n_va = int(sub["Valid_A"].sum()) if n and "Valid_A" in sub.columns else 0
        except Exception:
            n_va = 0
        try:
            n_vb = int(sub["Valid_B"].sum()) if n and "Valid_B" in sub.columns else 0
        except Exception:
            n_vb = 0
        bparts = {c: 0 for c in dbg_cols}
        if n > 0:
            for c in dbg_cols:
                try:
                    bparts[c] = int(sub[c].astype(bool).sum())
                except Exception:
                    bparts[c] = 0
        in_sched = 1 if bool(schedule.get(d)) else 0
        note = ""
        if n == 0:
            note = "해당 영업일·KODEX·bars_1d에 (code,일) 행 없음"
        elif n_vb == 0 and n > 0:
            note = "일봉은 있으나 Valid_B=0(부분조건 열 참고)"
        else:
            note = "Valid_B>0 (스케줄 상한은 max_slots_b 등 별도)"
        rec: dict[str, Any] = {
            "date": d,
            "kodex_rows_해당일": n,
            "Valid_A_합": n_va,
            "Valid_B_합": n_vb,
            "B1_avg5": bparts[dbg_cols[0]],
            "B2_정배열": bparts[dbg_cols[1]],
            "B3_rsi": bparts[dbg_cols[2]],
            "B4_분출B": bparts[dbg_cols[3]],
            "C5_osc": bparts[dbg_cols[4]],
            "C_ma60": bparts[dbg_cols[5]],
            "nonempty_schedule": in_sched,
            "비고": note,
        }
        out_rows.append(rec)

    try:
        return pd.DataFrame(out_rows)
    except Exception:
        return pd.DataFrame()
