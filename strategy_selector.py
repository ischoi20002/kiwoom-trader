import os
import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock  # [v16.0] 과거 지수 구성 종목 조회용
from datetime import datetime, timedelta
import traceback
from concurrent.futures import ThreadPoolExecutor

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

def select_monitoring_targets(settings, logger_callback, target_date=None, data_manager=None):
    """
    [v16.0] KOSPI 200 종목 리스트를 읽어와 설정된 전략 기준에 맞는 종목을 병렬 분석하여 선정합니다.
    
    Args:
        settings (dict): 필터링 기준 설정값 객체
        logger_callback (function): 로그 출력용 콜백
        target_date (datetime, optional): 분석 기준일 (None이면 오늘)
        data_manager (DataManagerV2, optional): 로컬 DB 매니저
        
    Returns:
        pd.DataFrame: 선정 및 점수화된 종목 리스트
    """
    logger_callback("\n" + "="*50)
    t_str = target_date.strftime('%Y-%m-%d') if target_date else "실시간"
    logger_callback(f"[Select] [전략 종목 선정] ({t_str}) 분석 시작...")
    
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
    
    min_val_krw = float(settings.get('min_trde_val', 40.0)) * 100000000
    
    def process_code(row):
        code, name = str(row['Code']).zfill(6), row['Name']
        status = {"total": 1, "data": 0, "value": 0, "trend": 0, "rsi": 0, "vol": 0}
        try:
            # 2. 데이터 확보 (DB 우선 -> Live Fallback)
            df = None
            if data_manager:
                # [v19.0] 로컬 DB 활용 (고속) - target_date가 없으면 오늘 기준
                end_dt = target_date if target_date else datetime.now()
                end_s = end_dt.strftime('%Y-%m-%d')
                start_s = (end_dt - timedelta(days=300)).strftime('%Y-%m-%d')
                df = data_manager.get_daily_bars(code, start_s, end_s)
            
            if df is None or len(df) < 50:
                # [v19.1] 실시간 FDR 활용 (데이터 매니저가 없거나 DB 데이터 부족 시)
                start_s = (datetime.now() - timedelta(days=250)).strftime('%Y-%m-%d')
                try:
                    df = fdr.DataReader(code, start_s)
                except:
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
            
            # 4. 필터링 준비 (공용 함수를 위한 추가 필드 계산)
            df_valid['AvgVal5'] = df_valid['TrdeVal'].rolling(5).mean()
            
            # 거래량 분출 확인 (공용 로직과 일치)
            vol_thresh = float(settings.get('vol_ratio', 1.5))
            lookback = int(settings.get('vol_lookback', 10))
            df_valid['VolRatio'] = df_valid['volume'] / df_valid['volume'].shift(1)
            df_valid['VolBurst'] = (df_valid['VolRatio'] >= vol_thresh) & (df_valid['close'] > df_valid['close'].shift(1))
            df_valid['VolBurstActive'] = df_valid['VolBurst'].rolling(lookback).max().astype(bool)
            df_valid['OSC_Prev'] = df_valid['OSC'].shift(1)
            last_full = df_valid.iloc[-1]
            
            # 5. 전략 필터링 및 점수 산정 (공용 함수 사용)
            strat_res = get_strategy_score(last_full, settings)
            
            # 상세 상태 업데이트 (하위 호환성용)
            if last_full['AvgVal5'] >= min_val_krw: status["value"] = 1
            if abs(last_full['MA5'] - last_full['MA20']) / last_full['MA20'] <= float(settings.get('ma_convergence', 3.0)) / 100.0: status["trend"] = 1
            if float(settings.get('sel_rsi_min', 40.0)) <= last_full['RSI'] <= float(settings.get('sel_rsi_max', 70.0)): status["rsi"] = 1
            if last_full['VolBurstActive']: status["vol"] = 1
            
            if not strat_res["valid"]:
                return None, status
            
            total_score = strat_res["score"]
            pos_diff = strat_res["pos_diff"]
            
            res = {
                'Code': code, 'Name': name, 'Score': total_score,
                'Price': int(last_full['close']), 
                'Open': int(last_full['open']),
                'Diff': int(last_full['close'] - df_valid.iloc[-2]['close']),
                'Ratio': round((last_full['close'] / df_valid.iloc[-2]['close'] - 1) * 100, 2),
                'RSI': round(last_full['RSI'], 1), 'MA20Dist': round(pos_diff * 100, 2),
                'MA60Ratio': round((last_full['close'] / last_full['MA60']) * 100, 2),
                'MA20': float(last_full['MA20']), 'MA60': float(last_full['MA60']),
                'MACD': float(last_full['MACD']), 'MACD_Sig': float(last_full['MACD_Sig']), 'OSC': float(last_full['OSC']),
                'Volume': int(last_full['volume']), 'PrevVol': int(df_valid.iloc[-2]['volume']),
                'VolMax': int(df_valid['volume'].max()),
                'MABufScale': 1.0,  # 기본값
                'AvgTrdeVal': int(last_full['AvgVal5'] / 100000000) # [Fix] 억 단위 추가
            }
            return res, status
        except Exception:
            return None, status
        except Exception:
            return None, status

    # 10개 스레드로 병렬 분석
    with ThreadPoolExecutor(max_workers=10) as executor:
        raw_results = list(executor.map(process_code, [row for _, row in df_top.iterrows()]))
    
    selected = [r[0] for r in raw_results if r[0] is not None]
    
    # 통계 출력
    stats = {"total": 0, "data": 0, "value": 0, "trend": 0, "rsi": 0, "vol": 0}
    for _, s in raw_results:
        for k in stats: stats[k] += s.get(k, 0)
    
    logger_callback(f"[Stats] 분석: {stats['total']}, 대금: {stats['value']}, 수렴: {stats['trend']}, RSI: {stats['rsi']}, 분출: {stats['vol']}")
    
    df_result = pd.DataFrame(selected)
    if not df_result.empty:
        df_result = df_result.sort_values(by='Score', ascending=False)
        # [v18.9] 선정 결과 개수 제한 적용 (최대 N개)
        max_mon = int(settings.get('max_slots', 20))
        df_result = df_result.head(max_mon)
        
        # 캐시 저장 (실시간일 때만)
        if not target_date:
            df_result.to_csv("selected_targets.csv", index=False, encoding='utf-8-sig')
            
    logger_callback(f"[Done] 최종 {len(df_result)}개 종목 선정 완료. (상위 {len(df_result)}개 필터링됨)")
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
    df_all['VolBurst'] = (df_all['VolRatio'] >= float(settings.get('vol_ratio', 1.5))) & (df_all['close'] > df_all.groupby('code')['close'].shift(1))
    df_all['VolBurstActive'] = df_all.groupby('code')['VolBurst'].rolling(vol_lookback).max().reset_index(0, drop=True)
    df_all['OSC_Prev'] = df_all.groupby('code')['OSC'].shift(1)
    
    c1 = df_all['AvgVal5'] >= (min_억 * 100000000)
    # [v30.10] Optimized Dual-Track Selection Logic
    use_a = settings.get('use_sel_track_a', True)
    use_b = settings.get('use_sel_track_b', True)
    
    c2 = pd.Series(False, index=df_all.index)
    if use_a:
        c2 |= (df_all['MA_Diff'] <= conv)
    if use_b:
        c2 |= (df_all['MA20'] > df_all['MA60']) & (df_all['close'] > df_all['MA20'])
    c3 = (df_all['RSI'] >= float(settings.get('sel_rsi_min', 40.0))) & (df_all['RSI'] <= float(settings.get('sel_rsi_max', 70.0)))
    c4 = df_all['VolBurstActive'] == 1
    c5 = (df_all['OSC'] > 0)
    
    df_all['Valid'] = c1 & c2 & c3 & c4 & c5 & df_all['MA60'].notna()
    
    # Scoring (우상향 가산점 적용)
    df_all['s1'] = (df_all['RSI'] / 70 * 100).clip(upper=100)
    df_all['s2'] = ((1 - (df_all['MA_Diff'] / conv)) * 100).clip(lower=0)
    df_all['s3'] = (df_all['AvgVal5'] / 50000000000 * 100).clip(upper=100)
    df_all['s4'] = ((1 - (df_all['Pos_Diff'] / 0.03)) * 100).clip(lower=0)
    
    df_all['Score'] = (df_all['s2'] * 0.4) + (df_all['s1'] * 0.3) + (df_all['s3'] * 0.2) + (df_all['s4'] * 0.1)
    df_all.loc[df_all['OSC'] > df_all['OSC_Prev'], 'Score'] += 10.0
    
    # 6. 스케줄 생성
    logger_callback("  [Batch] 날짜별 상위 종목 추출 중...")
    schedule = {}
    max_s = int(settings.get('max_slots', 20))
    df_sim = df_all[df_all['time'] >= pd.to_datetime(start_date)].copy()
    
    # [v16.93] Fix: Explicitly define trading_days for the loop
    trading_days_list = df_sim['time'].dt.strftime('%Y-%m-%d').unique().tolist()
    
    for d_str in trading_days_list:
        # [v18.1] Lean 모드에서는 매일 유니버스를 다시 조회하지 않고 전체(union) 유니버스 사용
        if duration_days < 180:
            universe = all_codes 
        else:
            universe = get_monthly_universe(pd.to_datetime(d_str))
            
        day_df = df_sim[(df_sim['time'] == pd.to_datetime(d_str)) & (df_sim['code'].isin(universe)) & (df_sim['Valid'])]
        if not day_df.empty:
            schedule[d_str] = day_df.sort_values(by='Score', ascending=False).head(max_s)['code'].tolist()

    if not schedule:
        logger_callback("  [Warning] 조건에 맞는 종목이 한 상단도 추출되지 않았습니다. 필터 설정을 확인하세요.")
    else:
        logger_callback(f"[Batch] 완료. (총 {len(schedule)}일 추출됨)")
    return schedule
