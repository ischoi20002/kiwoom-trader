import os
import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock  # [v16.0] 과거 지수 구성 종목 조회용
from datetime import datetime, timedelta
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal, Optional


class SelectionDbStaleError(Exception):
    """로컬 일봉 DB가 최신 장마감까지 갱신되지 않아 실시간 종목 선정을 중단할 때."""


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


def dual_track_selection_metrics(last_row: pd.Series, settings: dict) -> dict:
    """
    일봉 최신 행에 대해 prepare_dynamic_universe 의 Valid_A/B·Score_A/B 와 동일한 판정.
    반환: valid_a, score_a, valid_b, score_b (무효 트랙은 점수 0).
    """
    try:
        min_억_a = float(settings.get("min_trde_val", 40.0)) * 100000000
        conv = max(float(settings.get("ma_convergence", 3.0)) / 100.0, 1e-9)
        use_a = bool(settings.get("use_sel_track_a", True))
        use_b = bool(settings.get("use_sel_track_b", True))
        sel_rsi_min = float(settings.get("sel_rsi_min", 35.0))
        sel_rsi_max = float(settings.get("sel_rsi_max", 70.0))
        sel_b_rsi_min = float(settings.get("sel_b_rsi_min", 45.0))
        sel_b_rsi_max = float(settings.get("sel_b_rsi_max", 75.0))
        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 60.0)) * 100000000

        avg5 = float(last_row.get("AvgVal5", 0) or 0)
        ma5 = float(last_row.get("MA5", 0) or 0)
        ma20 = float(last_row.get("MA20", 0) or 0)
        ma60 = float(last_row.get("MA60", 0) or 0)
        close = float(last_row.get("close", 0) or 0)
        rsi = float(last_row.get("RSI", 0) or 0)
        osc = float(last_row.get("OSC", 0) or 0)
        osc_prev = float(last_row.get("OSC_Prev", osc) or 0)
        vba = bool(last_row.get("VolBurstActive_A", False))
        vbb = bool(last_row.get("VolBurstActive_B", False))

        ma_diff = abs(ma5 - ma20) / ma20 if ma20 > 0 else 1.0
        pos_diff = abs(close - ma20) / ma20 if ma20 > 0 else 1.0

        c1_a = avg5 >= min_억_a
        c2_a = (ma_diff <= conv) if use_a else False
        c3_a = (sel_rsi_min <= rsi <= sel_rsi_max)
        c4_a = vba
        c5 = osc > 0
        c_ma60 = pd.notna(last_row.get("MA60")) and ma60 > 0
        valid_a = bool(c1_a and c2_a and c3_a and c4_a and c5 and c_ma60)

        c1_b = avg5 >= sel_b_min_val
        c2_b = (ma20 > ma60) and (close > ma20) if use_b else False
        c3_b = (sel_b_rsi_min <= rsi <= sel_b_rsi_max)
        c4_b = vbb
        valid_b = bool(c1_b and c2_b and c3_b and c4_b and c5 and c_ma60)

        score_a = 0.0
        if valid_a:
            score_a = (
                max(0.0, (1 - (ma_diff / conv)) * 100) * 0.4
                + min(100.0, (rsi / 70.0) * 100) * 0.3
                + min(100.0, (avg5 / 50000000000.0) * 100) * 0.2
                + max(0.0, (1 - (pos_diff / 0.03)) * 100) * 0.1
            )
            if osc > osc_prev:
                score_a += 10.0

        score_b = 0.0
        if valid_b:
            trend_strength = min(10.0, max(0.0, (close / ma20 - 1.0) * 100)) * 10.0
            osc_term = (
                min(
                    100.0,
                    max(0.0, osc) / (abs(osc) + 1e-10) * 100.0,
                )
                * 0.1
            )
            score_b = (
                trend_strength * 0.4
                + min(100.0, (rsi / 70.0) * 100) * 0.3
                + min(100.0, (avg5 / 50000000000.0) * 100) * 0.2
                + osc_term
            )
            if osc > osc_prev:
                score_b += 10.0

        return {
            "valid_a": valid_a,
            "score_a": round(float(score_a), 1),
            "valid_b": valid_b,
            "score_b": round(float(score_b), 1),
        }
    except Exception:
        return {
            "valid_a": False,
            "score_a": 0.0,
            "valid_b": False,
            "score_b": 0.0,
        }


def _merge_dual_track_picks(
    selected_rows: list,
    settings: dict,
    logger_callback,
) -> list:
    """
    Track A 상위 max_slots_a + Track B 상위 max_slots_b 를 합치되,
    양쪽에 모두 든 종목은 SelTrack=AB·Score=max 로 1행만 유지.
    """
    max_a = max(0, int(settings.get("max_slots_a", 20)))
    max_b = max(0, int(settings.get("max_slots_b", 10)))
    cand_a = [d for d in selected_rows if d.get("_valid_a")]
    cand_b = [d for d in selected_rows if d.get("_valid_b")]
    cand_a.sort(key=lambda x: -float(x.get("_score_a", 0)))
    cand_b.sort(key=lambda x: -float(x.get("_score_b", 0)))
    picked_a = cand_a[:max_a]
    picked_b = cand_b[:max_b]
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


def select_monitoring_targets(
    settings: dict[str, Any],
    logger_callback: Callable[..., None],
    target_date: Optional[datetime] = None,
    data_manager: Any = None,
    *,
    selection_ohlcv_source: Literal["db_then_api", "api_only"] = "api_only",
) -> pd.DataFrame:
    """
    KOSPI 200을 스캔해 Track A(눌림)·Track B(추세) 유니버스를 **분리** 선정합니다.
    prepare_dynamic_universe 와 동일 필터(거래량 분출 A/B, RSI·대금 기준 분리 등)를 사용하며,
    상위 max_slots_a + max_slots_b 를 합산해 CSV/GUI에 반환합니다.

    selection_ohlcv_source:
        - api_only (기본): 실시간 선정 시 trading_data.db 일봉 미사용(FDR). 배포본 무DB 대응.
        - db_then_api: 실시간 선정 시 DB 일봉 우선 + 미갱신이면 SelectionDbStaleError.
          단, bars_1d 가 완전히 비어 있으면 신선도 검사만 생략하고 종목별로 DB→FDR 순으로 채움.
          (target_date 가 지정된 과거 선정은 data_manager 일봉 우선 유지)
    """
    logger_callback("\n" + "="*50)
    t_str = target_date.strftime('%Y-%m-%d') if target_date else "실시간"
    logger_callback(f"[Select] [전략 종목 선정] ({t_str}) 분석 시작...")

    try:
        _src = str(selection_ohlcv_source or "api_only").strip().lower()
    except Exception:
        _src = "api_only"
    api_only_live = _src in ("api_only", "api", "fdr_only", "fdr")

    if target_date is None and api_only_live:
        logger_callback(
            "[Select] 실시간 일봉: API 전용 모드(trading_data.db 미사용, FDR 일봉 기준)"
        )

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
    
    # 1. 대상 유니버스 확보 (KOSPI 200)
    tickers = []
    if target_date:
        # 과거 특정 시점의 구성 종목 조회 (pykrx)
        dt_str = target_date.strftime("%Y%m%d")
        try:
            tickers = stock.get_index_portfolio_deposit_file("1028", dt_str)
            if isinstance(tickers, pd.DataFrame):
                tickers = tickers.index.tolist() if not tickers.empty else []
            
            # 휴장일 등 리스트가 비어있을 경우 이전 영업일 리스트 탐색 (최대 5일)
            if tickers is None or len(tickers) == 0:
                for i in range(1, 6):
                    prev_dt = (target_date - timedelta(days=i)).strftime("%Y%m%d")
                    tickers = stock.get_index_portfolio_deposit_file("1028", prev_dt)
                    if isinstance(tickers, pd.DataFrame):
                        tickers = tickers.index.tolist() if not tickers.empty else []
                    if tickers and len(tickers) > 0: break
        except Exception as e:
            logger_callback(f"  [Warn] pykrx 오류: {e}")
            tickers = []
            
        # pykrx 실패 시 로컬 파일(kodex_200.csv) 또는 FDR 활용
        if tickers is None or len(tickers) == 0:
            if os.path.exists("kodex200.csv"):
                logger_callback("  [Info] Local 'kodex200.csv'에서 유니버스를 로드합니다.")
                df_csv = pd.read_csv("kodex200.csv", dtype={'종목코드': str, 'Code': str})
                if '종목코드' in df_csv.columns:
                    df_csv.rename(columns={'종목코드': 'Code', '종목명': 'Name'}, inplace=True)
                tickers = df_csv['Code'].tolist()
            else:
                logger_callback("  [Info] FDR StockListing에서 상위 종목을 추출합니다.")
                try:
                    df_fdr = fdr.StockListing('KOSPI')
                    df_fdr['Code'] = df_fdr['Code'].astype(str)
                    df_fdr = df_fdr[df_fdr['Code'].str.endswith('0')]
                    # Marcap 컬럼명 대소문자 대응
                    m_col = 'Marcap' if 'Marcap' in df_fdr.columns else 'MarketCap'
                    if m_col not in df_fdr.columns: m_col = df_fdr.columns[-1] # 최후의 수단
                    
                    tickers = df_fdr.sort_values(by=m_col, ascending=False).head(200)['Code'].tolist()
                except Exception as e:
                    logger_callback(f"  [Error] FDR fallback 실패: {e}")
            
        if tickers is None or len(tickers) == 0:
            logger_callback(f"[Error] [{dt_str}] 구성 종목 리스트를 확보하지 못했습니다.")
            return pd.DataFrame()
            
        # 종목명 매핑 (pykrx가 깨질 수 있으므로 FDR/로컬 정보 우선 활용)
        name_map = {}
        try:
            df_fdr_names = fdr.StockListing('KOSPI')
            name_map = dict(zip(df_fdr_names['Code'], df_fdr_names['Name']))
        except: pass
        
        df_top = pd.DataFrame([{'Code': t, 'Name': name_map.get(t, stock.get_market_ticker_name(t))} for t in tickers])
    else:
        input_file = "kodex200.csv"
        if os.path.exists(input_file):
            df_top = pd.read_csv(input_file, dtype={'종목코드': str, 'Code': str})
            if '종목코드' in df_top.columns:
                df_top.rename(columns={'종목코드': 'Code', '종목명': 'Name'}, inplace=True)
        else:
            # FDR 기반 자동 생성
            df_kospi = fdr.StockListing('KOSPI')
            df_kospi = df_kospi[df_kospi['Code'].str.endswith('0')]
            df_top = df_kospi.sort_values(by='Marcap', ascending=False).head(200)
        
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
                    end_dt = target_date if target_date else datetime.now()
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
                # FDR 일봉 (실시간 api_only / DB 부족 시)
                try:
                    end_anchor = target_date if target_date else datetime.now()
                except Exception:
                    end_anchor = datetime.now()
                try:
                    start_s = (end_anchor - timedelta(days=250)).strftime('%Y-%m-%d')
                except Exception:
                    start_s = (datetime.now() - timedelta(days=250)).strftime('%Y-%m-%d')
                try:
                    df = fdr.DataReader(code, start_s)
                except Exception:
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

            dm = dual_track_selection_metrics(last_full, settings)
            valid_a = bool(dm["valid_a"])
            valid_b = bool(dm["valid_b"])
            if not valid_a and not valid_b:
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
                "Volume": int(last_full["volume"]),
                "PrevVol": int(df_valid.iloc[-2]["volume"]),
                "VolMax": int(df_valid["volume"].max()),
                "MABufScale": 1.0,
                "AvgTrdeVal": int(last_full["AvgVal5"] / 100000000),
                "_valid_a": valid_a,
                "_score_a": dm["score_a"],
                "_valid_b": valid_b,
                "_score_b": dm["score_b"],
            }
            return res, status
        except Exception:
            return None, status

    # 10개 스레드로 병렬 분석
    with ThreadPoolExecutor(max_workers=10) as executor:
        raw_results = list(executor.map(process_code, [row for _, row in df_top.iterrows()]))
    
    selected = [r[0] for r in raw_results if r[0] is not None]

    # 통계 출력
    stats = {"total": 0, "data": 0, "value": 0, "trend": 0, "rsi": 0, "vol": 0}
    for _, s in raw_results:
        for k in stats:
            stats[k] += s.get(k, 0)

    logger_callback(
        f"[Stats] 분석: {stats['total']}, 대금: {stats['value']}, "
        f"수렴: {stats['trend']}, RSI: {stats['rsi']}, 분출: {stats['vol']}"
    )

    merged_rows = _merge_dual_track_picks(selected, settings, logger_callback)
    df_result = pd.DataFrame(merged_rows)
    if not df_result.empty:
        # 캐시 저장 (실시간일 때만)
        if not target_date:
            df_result.to_csv("selected_targets.csv", index=False, encoding="utf-8-sig")

    logger_callback(
        f"[Done] 최종 {len(df_result)}개 종목 선정 완료. "
        f"(Track A·B 분리 상한 적용, 동적 백테 유니버스와 동일 로직)"
    )
    return df_result

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

    # 2. 월별 코스피 200 유니버스 캐싱
    universe_cache = {}
    all_days = pd.date_range(start=start_date, end=end_date, freq='B')
    union_tickers = set()


    def get_monthly_universe(dt):
        m_key = dt.strftime("%Y%m")
        if m_key in universe_cache: return universe_cache[m_key]
        
        # [v17.8] 로컬 파일 캐시 확인 (pykrx 네트워크 행 방지)
        cache_file = os.path.join("backtest_data", f"kospi200_{m_key}.csv")
        if os.path.exists(cache_file):
            df_c = pd.read_csv(cache_file, dtype=str)
            tickers = df_c['Code'].tolist()
            universe_cache[m_key] = tickers
            return tickers

        # [v17.9] 주말/공휴일 대응: 해당월의 첫 영업일 5일간 시도
        # (pykrx는 장이 열리지 않은 날짜에 대해 빈 리스트를 반환할 수 있음)
        for offset in range(5):
            check_dt = dt + timedelta(days=offset)
            if check_dt.month != dt.month: break
            dt_str = check_dt.strftime("%Y%m%d")
            try:
                logger_callback(f"    - [API] pykrx 유니버스 조회 ({dt_str})...")
                tickers = stock.get_index_portfolio_deposit_file("1028", dt_str)
                if isinstance(tickers, pd.DataFrame): tickers = tickers.index.tolist()
                if tickers and len(tickers) > 100: 
                    universe_cache[m_key] = tickers
                    if not os.path.exists("backtest_data"): os.makedirs("backtest_data")
                    pd.DataFrame({'Code': tickers}).to_csv(cache_file, index=False)
                    return tickers
            except: pass
        
        # [v18.0] 최종 Fallback: 루트의 kodex_200.csv 사용 (네트워크 무관)
        if os.path.exists("kodex200.csv"):
            logger_callback("    - [Fallback] 'kodex200.csv' 로컬 파일 사용")
            df_f = pd.read_csv("kodex200.csv", dtype=str)
            if '종목코드' in df_f.columns:
                df_f.rename(columns={'종목코드': 'Code', '종목명': 'Name'}, inplace=True)
            tickers = df_f['Code'].tolist() if 'Code' in df_f.columns else df_f.iloc[:, 0].tolist()
            universe_cache[m_key] = tickers
            return tickers
            
        return []


    # [v17.9] Lean Mode: 6개월 미만 근거리 백테스트는 유니버스 변경이 적으므로 한 번만 조회 (2초 내 완료)
    duration_days = (all_days[-1] - all_days[0]).days if not all_days.empty else 0
    
    if duration_days < 180:
        logger_callback(f"  [Batch] Lean 모드 활성화 ({duration_days}일): 단일 유니버스 기준 분석")
        union_tickers = set(get_monthly_universe(all_days[0]))
        if not union_tickers:
            union_tickers = set(get_monthly_universe(datetime.now()))
    else:
        logger_callback("  [Batch] 정밀 모드 활성화: 월별 유니버스 구성 분석 중...")
        last_m = ""
        for d in all_days:
            curr_m = d.strftime("%Y%m")
            if curr_m != last_m or not union_tickers:
                union_tickers.update(get_monthly_universe(d))
                last_m = curr_m
                logger_callback(f"    - {d.strftime('%Y-%m')} 유니버스 체크 완료 (누적 {len(union_tickers)}종목)")
    
    if not union_tickers:
        # [v16.94] Robust Fallback: Use local kodex200.csv if monthly files are missing
        logger_callback("  [Warning] 월별 유니버스 파일이 없어 로컬 리스트 활용을 시도합니다.")
        for f_name in ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]:
            if os.path.exists(f_name):
                try:
                    df_k = pd.read_csv(f_name, dtype=str)
                    c_col = 'Code' if 'Code' in df_k.columns else ('종목코드' if '종목코드' in df_k.columns else None)
                    if c_col:
                        union_tickers = set(df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist())
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

    # 4. 벡터 지표 계산 (Vectorized Performance)
    logger_callback(f"  [Batch] {len(df_all)}개 데이터 포인트에 대해 고속 벡터 연산 중...")
    df_all['close'] = df_all['close'].astype(float)
    df_all['volume'] = df_all['volume'].astype(float)
    df_all['trde_val'] = df_all['close'] * df_all['volume']
    
    # [v17.9] Groupby.transform(lambda)는 매우 느림 -> Vectorized Rolling으로 전격 교체 
    df_all['MA5'] = df_all.groupby('code')['close'].rolling(5).mean().reset_index(0, drop=True)
    df_all['MA20'] = df_all.groupby('code')['close'].rolling(20).mean().reset_index(0, drop=True)
    df_all['MA60'] = df_all.groupby('code')['close'].rolling(60).mean().reset_index(0, drop=True)
    
    # RSI (Vectorized Grouped) - High Speed v30.10
    delta = df_all.groupby('code')['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    # Use vectorized .ewm on groupby directly (100x faster than transform(lambda))
    ema_up = gain.groupby(df_all['code']).ewm(com=13, adjust=False).mean().reset_index(0, drop=True)
    ema_down = loss.groupby(df_all['code']).ewm(com=13, adjust=False).mean().reset_index(0, drop=True)
    df_all['RSI'] = 100 - (100 / (1 + ema_up / (ema_down + 1e-10)))
    
    m_f, m_sl, m_sg = int(settings.get('macd_fast_daily', 12)), int(settings.get('macd_slow_daily', 26)), int(settings.get('macd_sig_daily', 9))
    ema_f = df_all.groupby('code')['close'].ewm(span=m_f, adjust=False).mean().reset_index(0, drop=True)
    ema_s = df_all.groupby('code')['close'].ewm(span=m_sl, adjust=False).mean().reset_index(0, drop=True)
    df_all['MACD'] = ema_f - ema_s
    macd_s = df_all.groupby('code')['MACD'].ewm(span=m_sg, adjust=False).mean().reset_index(0, drop=True)
    df_all['OSC'] = df_all['MACD'] - macd_s

    # 5. 필터링 및 점수화
    min_억 = float(settings.get('min_trde_val', 40.0))
    conv = float(settings.get('ma_convergence', 3.0)) / 100.0
    vol_lookback = int(settings.get('vol_lookback', 10))
    
    df_all['AvgVal5'] = df_all.groupby('code')['trde_val'].rolling(5).mean().reset_index(0, drop=True)
    df_all['MA_Diff'] = (df_all['MA5'] - df_all['MA20']).abs() / df_all['MA20']
    df_all['Pos_Diff'] = (df_all['close'] - df_all['MA20']).abs() / df_all['MA20']
    df_all['VolRatio'] = df_all['volume'] / df_all.groupby('code')['volume'].shift(1)
    df_all['OSC_Prev'] = df_all.groupby('code')['OSC'].shift(1)

    # [Phase 2] Track A 거래량 분출
    vol_ratio_a = float(settings.get('vol_ratio', 1.5))
    df_all['VolBurst_A'] = (df_all['VolRatio'] >= vol_ratio_a) & (df_all['close'] > df_all.groupby('code')['close'].shift(1))
    df_all['VolBurstActive_A'] = df_all.groupby('code')['VolBurst_A'].rolling(vol_lookback).max().reset_index(0, drop=True)

    # [Phase 2] Track B 전용 거래량 분출 (별도 배율/기간)
    vol_ratio_b = float(settings.get('sel_b_vol_ratio', vol_ratio_a))
    vol_lookback_b = int(settings.get('sel_b_vol_lookback', vol_lookback))
    df_all['VolBurst_B'] = (df_all['VolRatio'] >= vol_ratio_b) & (df_all['close'] > df_all.groupby('code')['close'].shift(1))
    df_all['VolBurstActive_B'] = df_all.groupby('code')['VolBurst_B'].rolling(vol_lookback_b).max().reset_index(0, drop=True)

    # 공통 필터
    c1 = df_all['AvgVal5'] >= (min_억 * 100000000)
    c5 = (df_all['OSC'] > 0)
    c_ma60 = df_all['MA60'].notna()

    # --- Track A: 눌림목 (MA 수렴 + 낮은 RSI) ---
    use_a = settings.get('use_sel_track_a', True)
    c2_a = (df_all['MA_Diff'] <= conv) if use_a else pd.Series(False, index=df_all.index)
    c3_a = (df_all['RSI'] >= float(settings.get('sel_rsi_min', 35.0))) & \
           (df_all['RSI'] <= float(settings.get('sel_rsi_max', 70.0)))
    c4_a = df_all['VolBurstActive_A'] == 1
    df_all['Valid_A'] = c1 & c4_a & c5 & c_ma60 & c2_a & c3_a

    # Track A 점수 (수렴도 중심)
    df_all['Score_A'] = (
        ((1 - (df_all['MA_Diff'] / conv)) * 100).clip(lower=0) * 0.4 +
        (df_all['RSI'] / 70 * 100).clip(upper=100) * 0.3 +
        (df_all['AvgVal5'] / 50000000000 * 100).clip(upper=100) * 0.2 +
        ((1 - (df_all['Pos_Diff'] / 0.03)) * 100).clip(lower=0) * 0.1
    )
    df_all.loc[df_all['OSC'] > df_all['OSC_Prev'], 'Score_A'] += 10.0

    # --- Track B: 추세돌파 (정배열 확산 + 높은 RSI) ---
    use_b = settings.get('use_sel_track_b', True)
    c2_b = ((df_all['MA20'] > df_all['MA60']) & (df_all['close'] > df_all['MA20'])) if use_b else pd.Series(False, index=df_all.index)
    # Track B는 별도의 RSI 범위와 거래대금 기준 적용
    sel_b_rsi_min = float(settings.get('sel_b_rsi_min', 45.0))
    sel_b_rsi_max = float(settings.get('sel_b_rsi_max', 75.0))
    sel_b_min_val = float(settings.get('sel_b_min_trde_val', 60.0)) * 100000000
    c3_b = (df_all['RSI'] >= sel_b_rsi_min) & (df_all['RSI'] <= sel_b_rsi_max)
    c1_b = df_all['AvgVal5'] >= sel_b_min_val
    c4_b = df_all['VolBurstActive_B'] == 1
    df_all['Valid_B'] = c1_b & c2_b & c3_b & c4_b & c5 & c_ma60

    # Track B 점수 (추세 강도 + 모멘텀 중심)
    trend_strength = ((df_all['close'] / df_all['MA20'] - 1) * 100).clip(lower=0, upper=10) * 10
    df_all['Score_B'] = (
        trend_strength * 0.4 +
        (df_all['RSI'] / 70 * 100).clip(upper=100) * 0.3 +
        (df_all['AvgVal5'] / 50000000000 * 100).clip(upper=100) * 0.2 +
        (df_all['OSC'].clip(lower=0) / (df_all['OSC'].abs().max() + 1e-10) * 100) * 0.1
    )
    df_all.loc[df_all['OSC'] > df_all['OSC_Prev'], 'Score_B'] += 10.0

    # 기존 호환성: Valid와 Score 유지 (합집합)
    df_all['Valid'] = df_all['Valid_A'] | df_all['Valid_B']
    df_all['Score'] = df_all[['Score_A', 'Score_B']].max(axis=1)

    # 6. 스케줄 생성 (Track A / Track B 분리)
    logger_callback("  [Batch] 날짜별 상위 종목 추출 중 (Track A/B 분리)...")
    schedule_a = {}
    schedule_b = {}
    schedule = {}  # 기존 호환용 (합집합)
    max_s_a = int(settings.get('max_slots_a', 20))
    max_s_b = int(settings.get('max_slots_b', 10))
    df_sim = df_all[df_all['time'] >= pd.to_datetime(start_date)].copy()
    
    trading_days_list = df_sim['time'].dt.strftime('%Y-%m-%d').unique().tolist()
    
    for d_str in trading_days_list:
        if duration_days < 180:
            universe = all_codes 
        else:
            universe = get_monthly_universe(pd.to_datetime(d_str))
        
        day_mask = (df_sim['time'] == pd.to_datetime(d_str)) & (df_sim['code'].isin(universe))
        
        # Track A 스케줄
        day_a = df_sim[day_mask & df_sim['Valid_A']]
        if not day_a.empty:
            schedule_a[d_str] = day_a.sort_values(by='Score_A', ascending=False).head(max_s_a)['code'].tolist()
        
        # Track B 스케줄
        day_b = df_sim[day_mask & df_sim['Valid_B']]
        if not day_b.empty:
            schedule_b[d_str] = day_b.sort_values(by='Score_B', ascending=False).head(max_s_b)['code'].tolist()
        
        # 기존 호환 스케줄 (합집합, 중복 제거 — 일별로 이미 A/B 각각 상한 적용됨)
        combined = list(dict.fromkeys(schedule_a.get(d_str, []) + schedule_b.get(d_str, [])))
        if combined:
            schedule[d_str] = combined

    a_days = len(schedule_a)
    b_days = len(schedule_b)
    if not schedule:
        logger_callback("  [Warning] 조건에 맞는 종목이 한 건도 추출되지 않았습니다. 필터 설정을 확인하세요.")
    else:
        logger_callback(f"[Batch] 완료. Track A: {a_days}일, Track B: {b_days}일, 합산: {len(schedule)}일")
    return schedule, schedule_a, schedule_b
