import os
import time
import json
import requests
import pandas as pd
from datetime import datetime
from typing import Optional
import config
from data_manager_v2 import DataManagerV2
from utils.normalization import normalize_code
from kiwoom_rest_api.auth.token import TokenManager
from core.auth_manager import AuthManager
from data_manager.minute_adjust_refetch import (
    detect_minute_overlap_mismatch,
    refetch_and_replace_code,
)
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

        # [v3.8] DB 경로를 프로젝트 루트 절대 경로로 고정 (GUI·cwd와 무관하게 동일 파일)
        self.db = DataManagerV2(DataManagerV2.DEFAULT_DB_PATH)

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
        # 분봉 수정주가 변경 감지·재수신 사용 여부 (settings ``harvest_minute_adj_refetch``, 최초 사용 시 로드)
        self._minute_adj_enabled: Optional[bool] = None
        # 성능 최적화: sync_meta를 run()에서 한 번에 적재해서
        # is_data_complete()에서 종목당 DB 왕복(3~4회)을 제거합니다.
        self._sync_meta_cache: dict[str, dict[str, str]] = {}
        # 성능 최적화: 1분봉 상한(cap)도 run()에서 1회만 계산합니다.
        self._cap_dt_once: Optional[str] = None
        # KRX 캘린더 기준 '반드시 포함되어야 할 마지막 1분봉'(애프터 19:59 / 정규 15:30)
        # — run()에서 1회 계산
        self._required_close_dt_once: Optional[datetime] = None
        # 애프터 진행 중이라 시작 자체를 거부한 경우 허용 시각
        self.blocked_until: Optional[datetime] = None

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

    def _harvest_project_root(self) -> str:
        """GUI와 동일한 프로젝트 루트(trading_data.db 위치)."""
        try:
            return os.path.dirname(os.path.abspath(DataManagerV2.DEFAULT_DB_PATH))
        except Exception:
            return os.getcwd()

    def _load_harvest_settings(self) -> dict:
        """하베스팅 유니버스용 settings.json (실패 시 빈 dict)."""
        try:
            path = os.path.join(self._harvest_project_root(), "settings.json")
            if not os.path.isfile(path):
                path = "settings.json"
            if not os.path.isfile(path):
                return {}
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            try:
                self.log(f"[WARN] settings.json 로드 실패: {e}")
            except Exception:
                pass
            return {}

    def _fetch_minute_data(self, code, token, next_key="", *, tic_scope: str = "1"):
        """
        키움 ka10080 분봉 1페이지 조회.
        tic_scope: \"1\"=1분봉, \"15\"=15분봉(서버 공식봉).
        """
        scope = str(tic_scope or "1").strip() or "1"
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
            "tic_scope": scope,
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
                self.log(
                    f"⚠️ API 에러 ({code}, tic={scope}): {res.status_code} | Msg: {res.text[:100]}"
                )
                return None, ""
        except Exception as e:
            self.log(f"❌ 요청 중 예외 ({code}, tic={scope}): {e}")
            return None, ""

    def _sync_bars_15m_from_api(
        self,
        code: str,
        token: str,
        *,
        start_day: str = "",
        end_day: str = "",
        cap_dt: Optional[str] = None,
        max_pages: int = 400,
    ) -> int:
        """
        ka10080(tic_scope=15) 직접 조회 → ``bars_15m`` 저장(INSERT OR REPLACE).

        ``start_day``(YYYY-MM-DD)가 있으면 그 날짜 이전으로 내려가면 조기 종료.
        ``end_day``는 상한 필터(해당일 23:59:59 초과 봉 제외). 없으면 cap만 적용.
        반환: 저장 시도 행 수 합계.
        """
        day_lo = str(start_day or "").strip()[:10]
        day_hi = str(end_day or "").strip()[:10]
        floor_ts = f"{day_lo} 00:00:00" if day_lo else ""
        ceil_ts = f"{day_hi} 23:59:59" if day_hi else ""
        if cap_dt:
            try:
                if ceil_ts:
                    ceil_ts = min(str(ceil_ts), str(cap_dt))
                else:
                    ceil_ts = str(cap_dt)
            except Exception:
                pass

        # 백필 하한: start_day 없으면 기존 1m earliest(있으면)까지만 내려감
        if not floor_ts:
            try:
                earliest_1m = self.db.get_earliest_sync_time(code)
                if earliest_1m:
                    floor_ts = f"{str(earliest_1m)[:10]} 00:00:00"
            except Exception:
                pass

        next_key = ""
        total_saved = 0
        pages = 0
        try:
            max_p = max(1, int(max_pages))
        except Exception:
            max_p = 400

        while self.running and pages < max_p:
            raw_data, next_key = self._fetch_minute_data(
                code, token, next_key, tic_scope="15"
            )
            pages += 1
            if not raw_data:
                break
            bars = self._parse_to_bars(raw_data)
            if not bars:
                break

            raw_bottom = bars[-1]["time"]
            filtered: list = []
            for b in bars:
                t = b.get("time") or ""
                if ceil_ts and t > ceil_ts:
                    continue
                if floor_ts and t < floor_ts:
                    continue
                filtered.append(b)

            if filtered:
                try:
                    df_chunk = pd.DataFrame(filtered)
                    n = int(self.db.save_bars_15m(code, df_chunk) or 0)
                    total_saved += n
                except Exception as e:
                    self.log(f"   ⚠️ {code}: bars_15m 저장 실패: {e}")

            # 페이지 하단이 목표 하한보다 이전이면 더 이상 불필요
            try:
                if floor_ts and raw_bottom and str(raw_bottom) < str(floor_ts):
                    break
            except Exception:
                pass

            if not next_key:
                break
            try:
                time.sleep(0.3)
            except Exception:
                pass

        return int(total_saved)

    def _minute_adj_refetch_if_changed(
        self,
        code: str,
        token: str,
        bars: list,
        upto: str,
        cap_dt: Optional[str],
    ) -> Optional[int]:
        """
        이번 페이지 중 DB 와 겹치는 1분봉 종가가 바뀌었으면(키움 소급 수정주가) 해당 종목 분봉을
        API 바닥까지 다시 받아 교체한다. 교체 성공 시 저장한 1분봉 수, 아니면 None(기존 증분 계속).
        """
        try:
            if self._minute_adj_enabled is None:
                self._minute_adj_enabled = bool(
                    self._load_harvest_settings().get("harvest_minute_adj_refetch", True)
                )
            if not self._minute_adj_enabled:
                return None
            ncode = normalize_code(code)
            mm = detect_minute_overlap_mismatch(self.db.db_path, "bars_1m", ncode, bars, upto)
            if not mm:
                return None
            self.log(
                f"   🔁 {code}: 기존 1분봉과 키움 값 불일치 {mm['n_mismatch']}/{mm['n_compared']}봉 "
                f"(오차 중앙값 {mm['median_rel'] * 100:.2f}%) → 수정주가 이벤트로 보고 분봉 전체 재수신"
            )
            rec = refetch_and_replace_code(
                self._fetch_minute_data,
                self._parse_to_bars,
                self.db.db_path,
                ncode,
                token,
                cap=cap_dt,
                should_continue=lambda: self.running,
            )
            r1 = rec.get("bars_1m", {}) or {}
            r15 = rec.get("bars_15m", {}) or {}
            self.log(
                f"   🔁 {code}: 재수신 결과 1m={r1.get('status')} (변경 {r1.get('changed', '-')}/{r1.get('n', '-')}, "
                f"{str(r1.get('from', ''))[:10]}~) | 15m={r15.get('status')} (변경 {r15.get('changed', '-')})"
            )
            if r1.get("status") == "replaced":
                return int(r1.get("n") or 0)
            return None
        except Exception as e:
            try:
                self.log(f"   ⚠️ {code}: 분봉 수정주가 재수신 실패(기존 증분 계속): {e}")
            except Exception:
                pass
            return None

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
            f"[SYNC] {code} 시작 — 키움 1분봉(ka10080 tic=1)→bars_1m, "
            f"이후 15분봉(ka10080 tic=15) 직접조회→bars_15m "
            f"(DB 1m 범위: {earliest_sync or '미상'} ~ {initial_last_sync or '없음'})"
        )
        
        next_key = ""
        total_fetched = 0
        last_raw_top: str = ""
        last_raw_bottom: str = ""
        adj_checked = False

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

            # 처음으로 기존 DB 구간과 겹치는 페이지에서 1회: 키움 소급 수정주가 감지 시 전체 재수신
            if not adj_checked and initial_last_sync and raw_bottom <= initial_last_sync:
                adj_checked = True
                n_refetched = self._minute_adj_refetch_if_changed(
                    code, token, bars, initial_last_sync, cap_dt_once
                )
                if n_refetched:
                    total_fetched = n_refetched
                    break

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
            # - 비COMPLETE: 과거 백필 — API next-key 가 없을 때까지 역행한다.
            #   저장 직후 DB earliest 와 비교하면 신규 종목이 1페이지(~900봉) 만에
            #   '겹침'으로 오판되어 백필이 끊긴다.
            status = self.db.get_sync_status(code)
            if status == "COMPLETE":
                if initial_last_sync and current_bottom_time <= initial_last_sync:
                    self.log(
                        f"   - {code}: 증분 완료 — 청크 하단 {current_bottom_time} ≤ 기존 최신 {initial_last_sync}. "
                        f"조기 종료(중복 구간 이하 페이지 생략)."
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
        # 실제로 1m 범위가 확장되었을 때만 공식 15분봉 직접조회·FDR 일봉 반영을 수행해 시간을 줄입니다.
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

            # 변경된 구간만 공식 15분봉으로 덮어씀(대개는 '최신 1~2일')
            start_src = (
                new_earliest_sync
                if new_earliest_sync and new_earliest_sync != earliest_sync
                else initial_last_sync
            )
            end_src = new_last_sync or initial_last_sync
            try:
                start_day = str(start_src)[:10]
                end_day = str(end_src)[:10]
            except Exception:
                start_day = ""
                end_day = ""

            n15 = 0
            try:
                if start_day and end_day:
                    self.log(
                        f"   - {code}: 1분봉 적재 완료 → bars_15m 직접조회(ka10080 tic=15) "
                        f"[{start_day}~{end_day}] 갱신 중..."
                    )
                    n15 = self._sync_bars_15m_from_api(
                        code,
                        token,
                        start_day=start_day,
                        end_day=end_day,
                        cap_dt=cap_dt_once,
                    )
                else:
                    self.log(
                        f"   - {code}: 1분봉 적재 완료 → bars_15m 직접조회(ka10080 tic=15) "
                        f"전체 구간(1m earliest~) 갱신 중..."
                    )
                    n15 = self._sync_bars_15m_from_api(
                        code,
                        token,
                        start_day="",
                        end_day=str(end_src)[:10] if end_src else "",
                        cap_dt=cap_dt_once,
                    )
            except Exception as e:
                self.log(f"   ⚠️ {code}: bars_15m 직접조회 실패: {e}")
            self.log(
                f"[OK] {code} 처리 완료: 신규 1분봉 {total_fetched}개(bars_1m) + "
                f"공식 15분봉 {n15}개 반영(bars_15m)"
            )
            # FDR → bars_1d: 동적 백테·감시 선정과 동일 일봉 소스
            try:
                ok = self.db.sync_daily_bars_from_fdr_to_required_eod(code)
                if ok:
                    self.log(f"   - {code}: bars_1d(FDR) 최신 EOD 반영 완료")
                    self._codes_1d_fdr_synced.add(_nk)
            except Exception as e:
                try:
                    self.log(f"   ⚠️ {code}: bars_1d(FDR) 반영 실패(무시하고 계속): {e}")
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
        """데이터의 충분성 및 구멍 유무를 판단 (KRX 분봉 완결 시각: 애프터 19:59 / 그 외 15:30)."""
        try:
            # 1. DB의 수집 범위 및 상태 확인 (캐시 사용)
            meta = self._sync_meta_cache.get(normalize_code(code))
            if meta:
                last_sync = meta.get("last_sync_dt") or None
                status = meta.get("status") or None
            else:
                # 캐시가 없는 경우에만 DB 왕복
                last_sync = self.db.get_last_sync_time(code)
                status = self.db.get_sync_status(code)

            if not last_sync:
                return False

            now = datetime.now()

            try:
                last_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return False

            # [v16.9] 'NO_DATA'는 장기간 API가 유효한 1m 데이터를 주지 않는 종목 스킵용
            if isinstance(status, str) and status.startswith("NO_DATA|"):
                retry_after_days = 5
                try:
                    mark_str = status.split("|", 1)[1]
                    mark_dt = datetime.strptime(mark_str, "%Y-%m-%d")
                    if (now - mark_dt).days < retry_after_days:
                        return True
                except Exception:
                    return False

            if status == "NO_DATA":
                return True

            # 과거 백필 미완(COMPLETE 아님)이면 무조건 수집 대상
            if status != "COMPLETE":
                return False

            # 1페이지(~900봉, 약 2거래일)만 받고 COMPLETE 된 종목은 백필 미완으로 재수집
            try:
                earliest = None
                if meta:
                    earliest = meta.get("earliest_sync_dt") or None
                if not earliest:
                    earliest = self.db.get_earliest_sync_time(code)
                if earliest:
                    earliest_dt = datetime.strptime(
                        str(earliest)[:19], "%Y-%m-%d %H:%M:%S"
                    )
                    if (last_dt - earliest_dt).days < 14:
                        return False
            except Exception:
                pass

            # COMPLETE: last_sync 가 KRX 기준 '마지막 완결 1분봉'까지 도달했는지 확인
            required_close = self._required_close_dt_once
            if required_close is None:
                try:
                    from reporting.krx_trading_calendar import (
                        required_eod_bar_datetime_asof_local,
                    )

                    required_close = required_eod_bar_datetime_asof_local(now)
                except Exception:
                    required_close = None

            if required_close is not None:
                if last_dt >= required_close:
                    return True
                return False

            # 캘린더 폴백 실패 시: 날짜만 비교(구버전 호환)
            if last_sync[:10] < now.strftime("%Y-%m-%d"):
                return False

            return True
        except Exception:
            return False

    def _harvest_blocked_now(self) -> Optional[datetime]:
        """애프터 진행 구간(정규장 마감~애프터 종료+버퍼)이면 허용 시각, 아니면 None."""
        try:
            from reporting.krx_trading_calendar import harvest_blocked_until_local

            return harvest_blocked_until_local()
        except Exception as e:
            self.log(f"⚠️ [하베스팅] 애프터 차단 판정 실패(진행): {e}")
            return None

    def _stop_if_blocked(self, stage: str) -> bool:
        """실행 중 애프터 진행 구간에 들어가면 중단(미완 분봉·일봉 저장 방지)."""
        until = self._harvest_blocked_now()
        if until is None:
            return False
        self.running = False
        self.log(
            f"⛔ [하베스팅] 애프터장 진행 중이라 {stage}에서 중단합니다. "
            f"{until.strftime('%H:%M')} 이후 다시 실행하세요."
        )
        return True

    def run(self):
        """메인 수집 루프 (KOSPI 전종목)."""
        _blocked_until = self._harvest_blocked_now()
        if _blocked_until is not None:
            self.blocked_until = _blocked_until
            self.log(
                f"⛔ [하베스팅] 애프터장 진행 중(정규장 마감~20:00)에는 수집하지 않습니다. "
                f"{_blocked_until.strftime('%H:%M')} 이후 다시 실행하세요."
            )
            return
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

        # 3. 합집합 (코스피 + [선택] K200). 시장지수·VIX는 수집하지 않음.
        all_codes = []
        seen = set()

        for c in codes_stocks + codes_k200:
            if c not in seen:
                all_codes.append(c)
                seen.add(c)

        _k200_note = f" + K200보강 {len(codes_k200)}" if codes_k200 else ""
        self.log(
            f"🚀 [START] 하베스팅 유니버스 총 {len(all_codes)}개 종목 "
            f"(코스피 {len(codes_stocks)}{_k200_note}, 지수/VIX 제외)"
        )
        
        token = self.token_manager.get_auth_token()
        if not token:
            self.log("[ERROR] 토큰을 가져올 수 없습니다. 종료합니다.")
            return

        try:
            from reporting.krx_trading_calendar import (
                minute_harvest_datetime_cap_iso,
                required_eod_bar_datetime_asof_local,
            )

            _cap0 = minute_harvest_datetime_cap_iso()
            if _cap0:
                self._cap_dt_once = _cap0
                self.log(
                    f"⏸ [장중] 1분·15분봉은 '{_cap0}' 이하만 DB 저장합니다(당일 장중 봉 제외). "
                    f"장 마감 후 실행 시 당일 분봉까지 반영됩니다."
                )
            req_close = required_eod_bar_datetime_asof_local()
            if req_close:
                self._required_close_dt_once = req_close
                self.log(
                    f"📅 [완결 기준] KRX 분봉 마지막 완결 1분봉: "
                    f"{req_close.strftime('%Y-%m-%d %H:%M:%S')}"
                    + (
                        " (애프터마켓 포함)"
                        if req_close.hour >= 16
                        else " (정규장)"
                    )
                )
        except Exception:
            pass

        # 종목별 sync_meta를 한 번에 적재해 is_data_complete()의 DB 왕복을 제거합니다.
        self._preload_sync_meta_cache(all_codes)

        # 이번 런에서 1m가 실제로 쌓인 종목 / harvest_code 안에서 FDR 1d까지 반영한 종목
        self._codes_1m_updated: set[str] = set()
        self._codes_1d_fdr_synced: set[str] = set()

        for idx, code in enumerate(all_codes):
            if not self.running: break
            if self._stop_if_blocked("분봉 수집"):
                break
            
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

        if self.running:
            self._stop_if_blocked("FDR 일봉 보강")

        # FDR → bars_1d: 1m 적재 여부와 무관하게 EOD 기준 일봉 보강
        self.log("📅 [FDR→1d] 유니버스 bars_1d 조건부 보강(마지막 완결 거래일 기준)…")
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
                behind = self.db.is_daily_bars_behind_required_eod(code)
            except Exception:
                behind = True
            need = behind or (
                ncode in self._codes_1m_updated
                and ncode not in self._codes_1d_fdr_synced
            )
            if not need:
                n_1d_skip += 1
                continue
            try:
                if self.db.sync_daily_bars_from_fdr_to_required_eod(code):
                    n_1d_ok += 1
            except Exception as e:
                if j < 5:
                    self.log(f"   ⚠️ [FDR→1d] {code}: {e}")
        self.log(
            f"   [FDR→1d] 반영 종목: {n_1d_ok}, 스킵(이미 최신): {n_1d_skip}"
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
        if service.blocked_until is not None:
            print("⛔ 애프터장 진행 중이라 수집하지 않았습니다. (10초 후 콘솔이 닫힙니다)")
            time.sleep(10)
        else:
            print("✅ 모든 작업이 완료되었습니다. (콘솔은 자동으로 닫힙니다)")
    except KeyboardInterrupt:
        service.running = False
        print("\n🛑 사용자에 의해 중지되었습니다.")
    except Exception as e:
        print(f"\n❌ 치명적 오류 발생: {e}")
