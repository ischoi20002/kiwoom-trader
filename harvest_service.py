import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import config
from data_manager_v2 import DataManagerV2
from utils.normalization import normalize_code
from kiwoom_rest_api.auth.token import TokenManager
from core.auth_manager import AuthManager
import sys
import io

# 콘솔 출력 인코딩 설정 (Windows CP949 등 대응)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', errors='replace')

class HarvestService:
    def __init__(self, target_list_path="kospi_top_500.csv"):
        # -----------------------------------------------------------------------
        # [v16.9.3] 하베스팅 실전 서버 강제 설정
        # -----------------------------------------------------------------------
        # 모의투자(config.USE_MOCK=True) 상태에서도 하베스팅은 항상 실전 서버를 사용해야
        # 합니다. 모의투자 서버는 일부 시장 데이터가 빈약해 DB 적재가 어긋날 수 있습니다.
        # 체결강도(VolPower)는 하베스팅에서 수집하지 않으며, 실시간 감시 등 별도 경로에서만 다룹니다.
        # 반드시 AuthManager()/DataManagerV2() 생성 전에 config를 변경해야 합니다.
        config.USE_MOCK = False
        config.URL = config.REAL_URL

        # [v3.8] DB 경로를 절대 경로로 고정 (GUI와 동일한 파일 사용 보장)
        db_path = os.path.join(os.getcwd(), "trading_data.db")
        self.db = DataManagerV2(db_path)

        # -----------------------------------------------------------------------
        # [v16.9.4] 토큰 관리자: kiwoom_rest_api.TokenManager → AuthManager 교체
        # -----------------------------------------------------------------------
        # 【문제】 kiwoom_rest_api 패키지의 TokenManager는 내부적으로
        #   kiwoom_rest_api/config.py 의 모듈 수준 변수를 사용합니다:
        #     API_KEY = os.environ.get("KIWOOM_API_KEY", "")  ← import 시점에 1회 고정
        #   따라서 이후에 os.environ 을 수정하거나 kiwoom_config.API_KEY 를 재할당해도
        #   이미 ""(빈 문자열)로 굳어진 상태라 토큰 발급이 항상 실패합니다.
        # 【해결】 프로젝트 자체의 AuthManager(core/auth_manager.py)를 사용합니다.
        #   AuthManager.get_auth_token()은 매 호출 시점에 config.APP_KEY /
        #   config.APP_SECRET 을 직접 읽으므로, 위에서 config.USE_MOCK=False 로
        #   설정한 실전 키가 정확히 반영됩니다.
        self.token_manager = AuthManager()

        self.target_list_path = target_list_path
        self.running = True
        # 성능 최적화: sync_meta를 run()에서 한 번에 적재해서
        # is_data_complete()에서 종목당 DB 왕복(3~4회)을 제거합니다.
        self._sync_meta_cache: dict[str, dict[str, str]] = {}
        # 성능 최적화: 1분봉 상한(cap)도 run()에서 1회만 계산합니다.
        self._cap_dt_once: Optional[str] = None
        # 성능/정확성: 캘린더 대신 DB에서 '직전 마감' 기준을 구합니다.
        # (휴장일/특수일 때문에 previous_trading_day가 어긋나는 경우 대비)
        self._prev_close_dt_once: Optional[datetime] = None

        self.log(f"🔍 [Info] 하베스팅 모드: 실전투자 서버 (데이터 무결성 우선)")

        os.environ['KIWOOM_API_KEY'] = config.APP_KEY
        os.environ['KIWOOM_API_SECRET'] = config.APP_SECRET

        try:
            from kiwoom_rest_api import config as kiwoom_config
            kiwoom_config.API_KEY = config.APP_KEY
            kiwoom_config.API_SECRET = config.APP_SECRET
            kiwoom_config.BASE_URL = config.REAL_URL
        except:
            pass

    def log(self, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {msg}", flush=True)

    def _fetch_minute_data(self, code, token, next_key=""):
        url = f"{config.URL}/api/dostk/chart"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "cont-yn": "Y" if next_key else "N",
            "next-key": next_key,
            "api-id": "ka10080"
        }
        body = {
            "stk_cd": code,
            "tic_scope": "1", # 1분봉
            "upd_stkpc_tp": "1"
        }
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                data = res.json()
                raw_list = data.get('stk_min_pole_chart_qry', data.get('output', data.get('data', [])))
                n_key = res.headers.get('next-key', "")
                return raw_list, n_key
            else:
                self.log(f"⚠️ API 에러 ({code}): {res.status_code} | Msg: {res.text[:100]}")
                return None, ""
        except Exception as e:
            self.log(f"❌ 요청 중 예외 ({code}): {e}")
            return None, ""

    def _parse_to_bars(self, raw_list):
        """API 원본 데이터를 정형화된 리스트로 변환"""
        if not raw_list: return []
        parsed = []
        for item in raw_list:
            # [v7.8] 지수용 키(idx_prc, cntg_tm 등)와 주식용 키 대응 강화
            cl = item.get('idx_prc', item.get('cur_prc', item.get('stck_prpr', '0')))
            op = item.get('idx_oprc', item.get('open_pric', item.get('stck_oprc', '0')))
            hg = item.get('idx_hgpr', item.get('high_pric', item.get('stck_hgpr', '0')))
            lw = item.get('idx_lwpr', item.get('low_pric', item.get('stck_lwpr', '0')))
            tm = str(item.get('cntg_tm', item.get('cntr_tm', item.get('stck_cntg_hour', '')))).strip()
            vl = item.get('trde_qty', item.get('cntg_vol', '0'))
            
            if not tm or len(tm) < 12: continue
            
            # YYYYMMDDHHMMSS -> YYYY-MM-DD HH:MM:SS
            dt_fmt = f"{tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
            
            def clean(v): return int(str(v).replace('+', '').replace('-', '').strip())
            
            parsed.append({
                'time': dt_fmt,
                'open': clean(op),
                'high': clean(hg),
                'low': clean(lw),
                'close': clean(cl),
                'volume': clean(vl)
            })
        return parsed

    def harvest_code(self, code: str, token: str) -> bool:
        """한 종목의 부족한 데이터를 모두 수집 (v6.6: 증분 즉시 저장 모델)"""
        if self.is_data_complete(code):
            self.log(f"[SKIP] {code}: 이미 모든 데이터가 충분하고 구멍이 없습니다.")
            return False

        initial_last_sync = self.db.get_last_sync_time(code)
        last_sync = initial_last_sync  # For logging/legacy checks
        earliest_sync = self.db.get_earliest_sync_time(code)
        # 장중 상한(cap)보다 최신 데이터는 DB에 저장하지 않으므로,
        # API가 "다음 페이지 없음"을 반환했을 때도 last_sync가 cap 이상이면 COMPLETE로 마킹합니다.
        try:
            from reporting.krx_trading_calendar import minute_harvest_datetime_cap_iso

            cap_dt_once = minute_harvest_datetime_cap_iso()
        except Exception:
            cap_dt_once = None
        self.log(
            f"[SYNC] {code} 시작 — 키움 1분봉(ka10080) 수집 → bars_1m 저장, 이후 15분 리샘플로 bars_15m 갱신 "
            f"(DB 1m 범위: {earliest_sync or '미상'} ~ {initial_last_sync or '없음'})"
        )
        
        next_key = ""
        total_fetched = 0
        last_raw_top: str = ""
        last_raw_bottom: str = ""

        while self.running:
            raw_data, next_key = self._fetch_minute_data(code, token, next_key)
            if not raw_data:
                break

            bars = self._parse_to_bars(raw_data)
            if not bars:
                break

            raw_top = bars[0]["time"]
            raw_bottom = bars[-1]["time"]
            last_raw_top = raw_top
            last_raw_bottom = raw_bottom

            filtered = (
                [b for b in bars if not cap_dt_once or b["time"] <= cap_dt_once]
                if cap_dt_once
                else bars
            )

            # [v16.9] [Fixed] 이미 최신 데이터가 있더라도, 과거 데이터가 부족(COMPLETE 아님)하면 중단하지 않고 과거로 계속 내려갑니다.
            if total_fetched == 0 and initial_last_sync and raw_top <= initial_last_sync:
                status = self.db.get_sync_status(code)
                if status == "COMPLETE":
                    self.log(f"   >> {code}: 이미 최신이며 과거 데이터(COMPLETE)까지 충분합니다.")
                    total_fetched = 0
                    break
                else:
                    self.log(f"   - {code}: 최신 데이터는 중복되지만, 과거 데이터(Backfill)를 위해 더 깊이 탐색합니다.")

            if filtered:
                # [v16.9] 즉시 DB 저장 후 마지막 동기화 시간 갱신 (Incremental Sync 정확도 향상)
                df_chunk = pd.DataFrame(filtered)
                self.db.save_bars(code, df_chunk)
                last_sync = self.db.get_last_sync_time(code)  # DB에서 실시간으로 다시 읽어옴
                total_fetched += len(filtered)

            # 페이지 경계·조기 종료 판단은 원본 청크 하단(키움 역행 진행도)
            current_bottom_time = raw_bottom
            
            # [v16.9.7] 조기 종료 기준 분기:
            # - COMPLETE: 증분만 — 수집 시작 시점의 DB 최신(initial_last_sync) 이전까지 역행하면 이후 페이지는
            #   대부분 이미 보유분(INSERT OR IGNORE)이라 API 낭비. last_sync 기준으로 멈춤.
            # - 비COMPLETE: 과거 백필 — DB 맨 앞(earliest)까지 내려가야 하므로 earliest 와 비교.
            status = self.db.get_sync_status(code)
            if status == "COMPLETE":
                if initial_last_sync and current_bottom_time <= initial_last_sync:
                    self.log(
                        f"   - {code}: 증분 완료 — 청크 하단 {current_bottom_time} ≤ 기존 최신 {initial_last_sync}. "
                        f"조기 종료(중복 구간 이하 페이지 생략)."
                    )
                    break
            else:
                earliest_db = self.db.get_earliest_sync_time(code)
                if earliest_db and current_bottom_time <= earliest_db:
                    self.log(
                        f"   - {code}: 백필 구간 연결 — {current_bottom_time} ≤ DB 최초 {earliest_db}. 조기 종료 (Overlapped)."
                    )
                    break
            
            self.log(
                f"   - {code}: 1분봉 {total_fetched}개 누적 저장(bars_1m) … 최근 저장 시각 {current_bottom_time}"
            )
            
            if not next_key:
                # [v6.8] API 바닥 — 장중 필터로 마지막 청크가 전부 스킵된 경우 COMPLETE 오표 방지
                try:
                    if cap_dt_once is None or bool(filtered) or (
                        cap_dt_once and last_sync and str(last_sync) >= str(cap_dt_once)
                    ):
                        self.db.set_sync_status(code, "COMPLETE")
                except Exception:
                    pass
                break
            
            time.sleep(0.3)
            
        # total_fetched는 "API가 준 청크 중 필터 통과 건수"라 중복(INSERT OR IGNORE)일 수 있습니다.
        # 실제로 1m 범위가 확장되었을 때만 15m 리샘플/1d 집계를 수행해 시간을 줄입니다.
        try:
            new_last_sync = self.db.get_last_sync_time(code)
            new_earliest_sync = self.db.get_earliest_sync_time(code)
        except Exception:
            new_last_sync = initial_last_sync
            new_earliest_sync = earliest_sync

        inserted_new_range = (new_last_sync != initial_last_sync) or (
            new_earliest_sync != earliest_sync
        )

        if inserted_new_range and total_fetched > 0:
            try:
                _nk = normalize_code(code)
            except Exception:
                _nk = str(code).strip()
            self._codes_1m_updated.add(_nk)

            self.log(f"   - {code}: 1분봉 적재 완료 → bars_15m 리샘플(1m→15m) 갱신 중...")
            self.db.refresh_resampled_data(code)
            self.log(
                f"[OK] {code} 처리 완료: 신규 1분봉 {total_fetched}개(bars_1m) + 15분봉 테이블 리빌드(bars_15m)"
            )
            # 1분봉 → 일봉(bars_1d) 집계: 동적 백테·DB 일봉 선정 경로용
            try:
                if self.db.rebuild_daily_bars_from_1m(code):
                    self.log(f"   - {code}: bars_1d(일봉) 1m 집계 반영 완료")
                    self._codes_1d_rebuilt_inline.add(_nk)
            except Exception as e:
                try:
                    self.log(f"   ⚠️ {code}: bars_1d 집계 실패(무시하고 계속): {e}")
                except Exception:
                    pass
        else:
            if last_sync and last_raw_top and last_raw_top <= last_sync:
                self.log(f"[OK] {code} 처리 완료: 이미 최신 상태입니다.")
            else:
                if not inserted_new_range:
                    self.log(f"[OK] {code} 처리 완료: 가져왔지만 DB 범위 확장 없음(중복/캡 스킵).")
                else:
                    self.log(f"[OK] {code} 처리 완료: 서버에서 가져온 데이터가 없습니다.")

        # API 응답은 받았지만(혹은 첫 페이지에서) 실제로 저장된 1m 데이터가 0건이면
        # 장기간 미갱신 종목은 다음 런에서 불필요하게 다시 SYNC를 돌리지 않도록 상태를 보강합니다.
        # (거래정지/상장폐지/데이터 공백 등으로 새 데이터가 장기간 없을 때 반복 호출 방지)
        if total_fetched == 0:
            try:
                if last_sync:
                    last_dt = datetime.strptime(str(last_sync), "%Y-%m-%d %H:%M:%S")
                    days_old = (datetime.now() - last_dt).days
                    if days_old >= 14:
                        # 2주 이상 미갱신인데도 계속 SYNC가 뜨는 종목은 사실상 업데이트 대상이 아닐 가능성이 큼.
                        # 다만 '일시적 거래정지'도 있을 수 있으니, NO_DATA를 영구 스킵이 아니라
                        # TTL(재시도 기간)형으로 저장합니다.
                        # - status 값 예: NO_DATA|2026-05-03
                        mark_dt = datetime.now().strftime("%Y-%m-%d")
                        try:
                            self.db.set_sync_status(code, f"NO_DATA|{mark_dt}")
                        except Exception:
                            pass
            except Exception:
                pass
        return bool(inserted_new_range and total_fetched > 0)

    def _preload_sync_meta_cache(self, codes: list[str]) -> None:
        """run() 시작 시 sync_meta를 한 번에 적재해 is_data_complete() 성능을 개선합니다."""
        if not codes:
            self._sync_meta_cache = {}
            return
        try:
            norm_codes = [normalize_code(c) for c in codes]
            out: dict[str, dict[str, str]] = {}
            # SQLite 변수 개수 제한을 고려해 청크 처리
            chunk_size = 500
            for i in range(0, len(norm_codes), chunk_size):
                sub = norm_codes[i : i + chunk_size]
                placeholders = ",".join(["?"] * len(sub))
                sql = (
                    "SELECT code, last_sync_dt, earliest_sync_dt, status "
                    f"FROM sync_meta WHERE code IN ({placeholders})"
                )
                with self.db._get_connection() as conn:
                    rows = conn.execute(sql, sub).fetchall()
                for r in rows:
                    code, last_sync_dt, earliest_sync_dt, status = r
                    out[str(code)] = {
                        "last_sync_dt": str(last_sync_dt) if last_sync_dt else "",
                        "earliest_sync_dt": str(earliest_sync_dt) if earliest_sync_dt else "",
                        "status": str(status) if status else "",
                    }
            self._sync_meta_cache = out
        except Exception:
            # 캐시 실패 시 기존 DB 조회 로직으로 폴백하도록
            self._sync_meta_cache = {}

    def is_data_complete(self, code: str) -> bool:
        """데이터의 충분성 및 구멍 유무를 판단 (v6.8: 종목별 한계 및 전역 경계 기준)"""
        try:
            # 1. DB의 수집 범위 및 상태 확인 (캐시 사용)
            meta = self._sync_meta_cache.get(normalize_code(code))
            if meta:
                last_sync = meta.get("last_sync_dt") or None
                earliest_sync = meta.get("earliest_sync_dt") or None
                status = meta.get("status") or None
            else:
                # 캐시가 없는 경우에만 DB 왕복
                last_sync = self.db.get_last_sync_time(code)
                earliest_sync = self.db.get_earliest_sync_time(code)
                status = self.db.get_sync_status(code)  # (DataManagerV2에서 로드)
            
            if not last_sync:
                return False

            now = datetime.now()

            # 문자열 last_sync_dt 파싱은 아래 판정들에서 반복되므로 여기서 1회만 합니다.
            try:
                last_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return False
            
            # [v16.9] 'COMPLETE' 상태이면 역사적으로 완료된 것으로 간주
            # 'NO_DATA'는 장기간 API가 유효한 1m 데이터를 주지 않는 종목 스킵용입니다.
            # 단, 일시적 거래정지 가능성을 고려해 TTL(재시도 기간) 후 다시 SYNC를 허용합니다.
            now_dt = datetime.now()
            if isinstance(status, str) and status.startswith("NO_DATA|"):
                # TTL: NO_DATA 마킹 이후 N일이 지나면 재시도(=완결 아님)합니다.
                # (너무 짧게 잡으면 SYNC가 다시 많이 뜨고, 너무 길면 재개 반영이 느립니다.)
                retry_after_days = 5
                try:
                    mark_str = status.split("|", 1)[1]
                    mark_dt = datetime.strptime(mark_str, "%Y-%m-%d")
                    if (now_dt - mark_dt).days < retry_after_days:
                        return True
                except Exception:
                    # 파싱 실패 시 안전하게 폴백(스킵하지 않음)
                    return False

            if status == "NO_DATA":
                return True

            is_historically_complete = (status == "COMPLETE")

            # 장 개시 전(09:00 이전)에는 오늘 당장 필요한 범위가 "직전 마감까지"이므로,
            # sync_meta.status가 COMPLETE로 아직 반영되지 않았더라도(last_sync_dt 기준으로) 스킵이 가능합니다.
            # 특히 휴장/특수일에 status 플래그가 느리게 맞춰지는 종목에서 SYNC가 남는 문제를 완화합니다.
            if now.hour < 9:
                if self._prev_close_dt_once is not None:
                    if last_dt >= self._prev_close_dt_once:
                        return True
                else:
                    # 폴백: 캘린더 이전 거래일(15:30) 기준
                    try:
                        from reporting.krx_trading_calendar import previous_trading_day

                        prev_d = previous_trading_day(now.date())
                        if prev_d is not None:
                            prev_close = datetime(
                                prev_d.year, prev_d.month, prev_d.day, 15, 30, 0
                            )
                            if last_dt >= prev_close:
                                return True
                    except Exception:
                        pass

            if not is_historically_complete:
                return False  # 과거 데이터가 더 필요함 (바닥까지 가야함)

            # [v16.9.8] 장중(1분 상한 모드): 직전 거래일 장마감(cap)까지 이미 있고 당일 분봉은 아직 안 넣어도 되는 경우 → API 호출 생략
            try:
                cap = self._cap_dt_once
                if cap and last_sync >= cap and last_sync[:10] < now.strftime("%Y-%m-%d"):
                    return True
            except Exception:
                pass
            
            # [Fix] 오늘 장 개시 전(09:00 이전)이라면, 어제 장마감 데이터까지만 있어도 완료로 간주
            if now.hour < 9:
                # 위에서 '직전 마감 시각'을 기준으로 이미 스킵 가능했던 케이스가 여기로 오지 않습니다.
                # 여기까지 왔다면 sync_meta.status가 COMPLETE이거나, 이전 시각 판정에 실패한 케이스입니다.
                # 최후 폴백(캘린더 어제 15:30): 데이터 불일치 완화 목적.
                yesterday_limit = (now - timedelta(days=1)).replace(
                    hour=15, minute=30, second=0, microsecond=0
                )
                if last_dt >= yesterday_limit:
                    return True
            
            if last_sync[:10] < now.strftime("%Y-%m-%d"):
                return False 
                
            return True 
        except Exception as e:
            return False

    def run(self):
        """메인 수집 루프 (v18.0.0: KODEX 200 + KOSPI 500 통합 유니버스)"""
        # 0. 공공데이터포털 KRX 상장종목(금융위 API) — 인증키가 있으면 최신 KOSPI 전체를 우선 사용
        codes_kospi_official: list = []
        svc_key = str(getattr(config, "DATA_GO_KR_SERVICE_KEY", "") or "").strip()
        if svc_key:
            try:
                from utils.krx_public_listing import (
                    describe_delisting_policy,
                    fetch_kospi_official_dataframe,
                )

                self.log(f"ℹ️ [유니버스] {describe_delisting_policy()}")
                df_k = fetch_kospi_official_dataframe(svc_key)
                st = self.db.merge_official_krx_listing(df_k)
                bas_hint = str(df_k["basDt"].iloc[0]) if len(df_k) and "basDt" in df_k.columns else "-"
                self.log(
                    f"📋 [KRX공식] 기준일(basDt)={bas_hint} | 스냅샷 {st['snapshot_rows']}종목 | "
                    f"DB ACTIVE {st['active_count']} | 이번에 EXITED 전환 {st['newly_exited_count']}건 (차트 DB는 유지)"
                )
                codes_kospi_official = (
                    df_k["Code"].astype(str).str.strip().str.zfill(6).tolist()
                    if len(df_k)
                    else []
                )
            except Exception as e:
                self.log(f"[WARN] KRX 공식 목록 실패 — FDR/CSV 폴백: {e}")

        # 1. KOSPI 주식 유니버스
        #    - 공공데이터 성공 시: 공식 KOSPI 전체
        #    - 그 외: FDR StockListing('KOSPI') 전체(코드 끝 0 휴리스틱, 시총순 상한 대량)
        #    - FDR 실패 시에만 kospi_top_500.csv 등 기존 CSV
        codes_stocks: list = []
        if codes_kospi_official:
            codes_stocks = list(codes_kospi_official)
            self.log(f"📊 [유니버스] 공공데이터 KOSPI {len(codes_stocks)}종목")
        else:
            try:
                # count>200 이면 kodex200.csv 경로를 건너뛰고 FDR 전체에 가깝게 로드
                df_fdr = self.db.get_kospi_list(count=100000)
                if df_fdr is not None and not df_fdr.empty:
                    codes_stocks = [
                        str(c).strip().zfill(6)
                        for c in df_fdr["Code"].astype(str).tolist()
                        if c
                    ]
                    self.log(
                        f"📊 [유니버스] 공공데이터 미적용 → FDR KOSPI {len(codes_stocks)}종목 "
                        f"(DATA_GO_KR_SERVICE_KEY 미설정 시 이 경로가 사용됩니다)"
                    )
            except Exception as e:
                self.log(f"[WARN] FDR KOSPI 유니버스 로드 실패: {e}")
            if not codes_stocks and os.path.exists(self.target_list_path):
                try:
                    df_500 = pd.read_csv(
                        self.target_list_path, dtype={"Code": str}, encoding="utf-8-sig"
                    )
                    codes_stocks = [
                        str(c).strip().zfill(6)
                        for c in df_500["Code"].tolist()
                        if c
                    ]
                    self.log(
                        f"📊 [유니버스] CSV 폴백 {self.target_list_path} ({len(codes_stocks)}종목)"
                    )
                except Exception as e:
                    self.log(f"[WARN] {self.target_list_path} 로드 실패: {e}")

        # 2. KODEX 200: 소규모 CSV 유니버스(~500)일 때만 보강. 전체 유니버스면 중복이므로 생략
        codes_k200: list = []
        if len(codes_stocks) < 600:
            try:
                df_k200 = self.db.get_kospi_list(200)
                if df_k200 is not None and not df_k200.empty:
                    codes_k200 = (
                        df_k200["Code"].astype(str).str.strip().str.zfill(6).tolist()
                    )
                    self.log(
                        f"📊 [유니버스] KODEX 200 보강 {len(codes_k200)}종목 (소규모 목록과 합집합)"
                    )
            except Exception as e:
                self.log(f"[WARN] KODEX 200 보강 로드 실패: {e}")

        # 3. 합집합 (주식 + [선택] K200). 시장지수·VIX는 수집하지 않음.
        all_codes = []
        seen = set()

        for c in codes_stocks + codes_k200:
            if c not in seen:
                all_codes.append(c)
                seen.add(c)

        _k200_note = f" + K200보강 {len(codes_k200)}" if codes_k200 else ""
        self.log(
            f"🚀 [START] 하베스팅 유니버스 총 {len(all_codes)}개 종목 "
            f"(주식 {len(codes_stocks)}{_k200_note}, 지수/VIX 제외)"
        )
        
        token = self.token_manager.get_auth_token()
        if not token:
            self.log("[ERROR] 토큰을 가져올 수 없습니다. 종료합니다.")
            return

        try:
            from reporting.krx_trading_calendar import minute_harvest_datetime_cap_iso

            _cap0 = minute_harvest_datetime_cap_iso()
            if _cap0:
                self._cap_dt_once = _cap0
                self.log(
                    f"⏸ [장중] 1분봉은 '{_cap0}' 이하만 DB 저장합니다(당일 장중 봉 제외). "
                    f"장 마감 후 실행 시 당일 분봉까지 반영됩니다."
                )
        except Exception:
            pass

        # now.hour < 9(장 개시 전)에서 '직전 마감 시각' 컷오프를 캘린더가 아니라
        # DB의 기준 종목(예: 첫 번째 코드)에서 실측으로 잡습니다.
        # 이렇게 하면 휴장일/특수일로 인해 이전 거래일 판정이 어긋나는 문제를 제거합니다.
        try:
            ref_code = all_codes[0] if all_codes else None
            if ref_code:
                now_dt = datetime.now()
                cutoff_dt = datetime(
                    now_dt.year, now_dt.month, now_dt.day, 9, 0, 0
                ).strftime("%Y-%m-%d %H:%M:%S")
                with self.db._get_connection() as conn:
                    row = conn.execute(
                        "SELECT MAX(datetime) FROM bars_1m WHERE code = ? AND datetime < ?",
                        (normalize_code(ref_code), cutoff_dt),
                    ).fetchone()
                raw_max = row[0] if row else None
                if raw_max:
                    self._prev_close_dt_once = datetime.strptime(
                        str(raw_max), "%Y-%m-%d %H:%M:%S"
                    )
                else:
                    with self.db._get_connection() as conn:
                        row2 = conn.execute(
                            "SELECT MAX(datetime) FROM bars_1d WHERE code = ? AND datetime < ?",
                            (normalize_code(ref_code), cutoff_dt[:10]),
                        ).fetchone()
                    raw_max2 = row2[0] if row2 else None
                    if raw_max2:
                        # bars_1d의 datetime은 YYYY-MM-DD
                        self._prev_close_dt_once = datetime.strptime(
                            str(raw_max2)[:10] + " 15:30:00", "%Y-%m-%d %H:%M:%S"
                        )
        except Exception:
            self._prev_close_dt_once = None

        # 종목별 sync_meta를 한 번에 적재해 is_data_complete()의 DB 왕복을 제거합니다.
        self._preload_sync_meta_cache(all_codes)

        # 이번 런에서 1m가 실제로 쌓인 종목 / harvest_code 안에서 이미 1d까지 재집계한 종목
        self._codes_1m_updated: set[str] = set()
        self._codes_1d_rebuilt_inline: set[str] = set()

        for idx, code in enumerate(all_codes):
            if not self.running: break
            
            self.log(f"[{idx+1}/{len(all_codes)}] 종목 분석 중 ({code})...")
            harvested = False
            try:
                # [v7.0] 수집 전 무결성/입수 완료 여부 판단하여 스킵
                if self.is_data_complete(code):
                    self.log(f"   >> {code}: 이미 모든 구간이 동기화되어 있습니다. 건너뜁니다.")
                    continue

                harvested = self.harvest_code(code, token)
            except Exception as e:
                self.log(f"[ERROR] {code} 처리 중 치명적 오류: {e}")
            
            # 종목 간 대기 (서버 부하 분산)
            if harvested:
                time.sleep(0.5)
            else:
                # 이미 최신이거나(보통 continue로 여기까지 오지 않지만) DB 범위 확장 없음이면 대기 최소화
                time.sleep(0.02)

        # 1m 대비 bars_1d 누락·인라인 집계 실패분만 보강 (매 런 전 유니버스 전체 리빌드는 생략)
        self.log("📅 [1m→1d] 주식 유니버스 bars_1d 조건부 보강(누락·이번 런 1m 미반영분)…")
        n_1d_ok = 0
        n_1d_skip = 0
        for j, code in enumerate(all_codes):
            if not self.running:
                break
            try:
                ncode = normalize_code(code)
            except Exception:
                ncode = str(code).strip()
            try:
                behind = self.db.is_daily_bars_behind_1m(code)
            except Exception:
                behind = True
            need = behind or (
                ncode in self._codes_1m_updated
                and ncode not in self._codes_1d_rebuilt_inline
            )
            if not need:
                n_1d_skip += 1
                continue
            try:
                if self.db.rebuild_daily_bars_from_1m(code):
                    n_1d_ok += 1
            except Exception as e:
                if j < 5:
                    self.log(f"   ⚠️ [1m→1d] {code}: {e}")
        self.log(
            f"   [1m→1d] 집계 반영 종목: {n_1d_ok}, 스킵(이미 최신): {n_1d_skip}"
        )

        # 대량 적재 후 플래너 통계 갱신 (조회 지연 완화에 유리, 락은 짧음)
        self.log("📈 DB ANALYZE 실행 중… (쿼리 플래너 통계 갱신)")
        try:
            if self.db.run_analyze():
                self.log("✅ ANALYZE 완료")
            else:
                self.log("⚠️ ANALYZE 실패 — 콘솔 로그를 확인하세요.")
        except Exception as e:
            self.log(f"⚠️ ANALYZE 예외: {e}")

        self.log("[FINISH] 모든 수집 작업이 종료되었습니다.")

if __name__ == "__main__":
    service = HarvestService()
    try:
        service.run()
        print("\n" + "=" * 50)
        print("✅ 모든 작업이 완료되었습니다. (콘솔은 자동으로 닫힙니다)")
    except KeyboardInterrupt:
        service.running = False
        print("\n🛑 사용자에 의해 중지되었습니다.")
    except Exception as e:
        print(f"\n❌ 치명적 오류 발생: {e}")
