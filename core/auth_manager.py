# -*- coding: utf-8 -*-
"""키움 REST 액세스 토큰 관리 (TTL 캐시 · 단일 비행 · 429 쿨다운)."""
from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

import requests

import config

# [v3.7] 환경변수는 메인 GUI(k_trader_gui.py)에서 USE_MOCK 설정에 따라 동적으로 설정됩니다.


class AuthManager:
    """
    키움 REST 액세스 토큰 관리.
    동일 프로세스 내 반복 호출 시 OAuth 재요청·로그 스팸을 막기 위해 TTL 캐시를 둡니다.
    OAuth는 직접 검증(return_code·token) + 단일 비행 + 429 쿨다운.
    """

    # 키움 토큰은 통상 24h 내외 — 만료 전 갱신 여유를 두고 23시간 캐시
    _TOKEN_TTL_SEC: float = 23 * 3600
    _OAUTH_429_COOLDOWN_SEC: float = 120.0
    _AUTH_FAIL_LOG_INTERVAL_SEC: float = 25.0

    def __init__(self, logger_callback=None) -> None:
        self.access_token: Optional[str] = None
        self.log = logger_callback or print
        self._token_lock = threading.Lock()
        self._token_issued_monotonic: float = 0.0
        self._oauth_cond = threading.Condition(self._token_lock)
        self._oauth_in_progress: bool = False
        self._oauth_429_until: float = 0.0
        self._last_auth_fail_log_at: float = 0.0
        # credentials=키 오류(재시도 무의미), rate_limit, network, other
        self._last_failure_kind: str = ""

    def get_access_token(self) -> Optional[str]:
        """현재 저장된 액세스 토큰 반환 (네트워크 없음). harvest·구 호출 호환."""
        return self.access_token

    def is_authenticated(self) -> bool:
        """유효 캐시 토큰 존재 여부 (TTL 포함)."""
        return self.has_cached_token()

    def has_cached_token(self) -> bool:
        """네트워크 없이 유효 캐시 토큰 존재 여부."""
        try:
            now_m = time.monotonic()
            with self._token_lock:
                return bool(
                    self.access_token
                    and (now_m - self._token_issued_monotonic) < self._TOKEN_TTL_SEC
                )
        except Exception:
            return False

    def is_fatal_auth_error(self) -> bool:
        """App Key/Secret 오류 등 — 재시도해도 성공하지 않는 경우."""
        try:
            return str(self._last_failure_kind or "") == "credentials"
        except Exception:
            return False

    def oauth_cooldown_remaining_sec(self) -> float:
        """429 쿨다운 남은 초."""
        try:
            with self._token_lock:
                return max(0.0, float(self._oauth_429_until) - time.monotonic())
        except Exception:
            return 0.0

    def invalidate_cached_token(self) -> None:
        """401·8005 등으로 토큰이 무효일 때 캐시를 비웁니다."""
        try:
            with self._token_lock:
                self.access_token = None
                self._token_issued_monotonic = 0.0
        except Exception:
            pass

    def _oauth_endpoint_and_keys(self) -> Tuple[str, str, str]:
        """(token_url, appkey, secretkey) — USE_MOCK 에 맞는 키 쌍."""
        use_mock = bool(getattr(config, "USE_MOCK", False))
        if use_mock:
            url = "https://mockapi.kiwoom.com/oauth2/token"
            key = str(getattr(config, "MOCK_APP_KEY", "") or "").strip()
            sec = str(getattr(config, "MOCK_APP_SECRET", "") or "").strip()
            if not key:
                key = str(getattr(config, "APP_KEY", "") or "").strip()
            if not sec:
                sec = str(getattr(config, "APP_SECRET", "") or "").strip()
        else:
            url = "https://api.kiwoom.com/oauth2/token"
            key = str(getattr(config, "APP_KEY", "") or "").strip()
            sec = str(getattr(config, "APP_SECRET", "") or "").strip()
        return url, key, sec

    def _set_429_cooldown(self) -> None:
        try:
            now_m = time.monotonic()
            with self._token_lock:
                self._oauth_429_until = max(
                    self._oauth_429_until,
                    now_m + self._OAUTH_429_COOLDOWN_SEC,
                )
            self._last_failure_kind = "rate_limit"
        except Exception:
            pass

    def _log_auth_fail_throttled(self, err: Exception, *, force_msg: str = "") -> None:
        """429·동시 실패 시 로그 스팸 방지."""
        try:
            now_m = time.monotonic()
            msg = force_msg or str(err or "")
            is_429 = "429" in msg or self._last_failure_kind == "rate_limit"
            with self._token_lock:
                if is_429:
                    self._oauth_429_until = max(
                        self._oauth_429_until,
                        now_m + self._OAUTH_429_COOLDOWN_SEC,
                    )
                    self._last_failure_kind = "rate_limit"
                if not force_msg and (
                    now_m - self._last_auth_fail_log_at
                ) < self._AUTH_FAIL_LOG_INTERVAL_SEC:
                    return
                self._last_auth_fail_log_at = now_m
            if is_429:
                self.log(
                    "⚠️ [Auth] 키움 OAuth 조회 제한(HTTP 429) — "
                    f"약 {int(self._OAUTH_429_COOLDOWN_SEC)}초 후 재시도합니다."
                )
            elif force_msg:
                self.log(force_msg)
            else:
                self.log(f"❌ [Auth] 인증 실패: {err}")
        except Exception:
            pass

    def _request_oauth_token(self) -> Optional[str]:
        """
        kiwoom_rest_api TokenManager 는 HTTP 200 + return_code≠0(8001 등)을
        성공으로 오인해 빈 토큰을 줄 수 있어 직접 검증합니다.
        """
        url, appkey, secretkey = self._oauth_endpoint_and_keys()
        use_mock = bool(getattr(config, "USE_MOCK", False))
        mode_str = "모의투자" if use_mock else "실전투자"

        if not appkey or not secretkey:
            self._last_failure_kind = "credentials"
            self._log_auth_fail_throttled(
                RuntimeError("empty key"),
                force_msg=(
                    f"❌ [Auth] {mode_str} App Key/Secret이 비어 있습니다. "
                    "config.py 의 MOCK_APP_KEY·MOCK_APP_SECRET(모의) 또는 "
                    "APP_KEY·APP_SECRET(실전)을 확인하세요."
                ),
            )
            return None

        try:
            res = requests.post(
                url,
                json={
                    "grant_type": "client_credentials",
                    "appkey": appkey,
                    "secretkey": secretkey,
                },
                headers={"Content-Type": "application/json;charset=UTF-8"},
                timeout=20,
            )
        except requests.RequestException as ex:
            self._last_failure_kind = "network"
            self._log_auth_fail_throttled(ex)
            return None

        if res.status_code == 429:
            self._set_429_cooldown()
            self._log_auth_fail_throttled(Exception("HTTP 429"))
            return None

        try:
            data = res.json() if res.text else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        rmsg = str(data.get("return_msg") or "").strip()
        try:
            rc_i = int(data.get("return_code", 0) or 0)
        except (TypeError, ValueError):
            rc_i = -1

        token = str(data.get("token") or "").strip()
        if token and rc_i in (0,):
            self._last_failure_kind = ""
            return token

        if (
            "8001" in rmsg
            or "8002" in rmsg
            or "App Key" in rmsg
            or "Secret Key" in rmsg
        ):
            self._last_failure_kind = "credentials"
            key_hint = (
                "MOCK_APP_KEY / MOCK_APP_SECRET"
                if use_mock
                else "APP_KEY / APP_SECRET"
            )
            self._log_auth_fail_throttled(
                RuntimeError(rmsg or "8001"),
                force_msg=(
                    f"❌ [Auth] {mode_str} API 키 검증 실패(8001). "
                    f"키움 OpenAPI+에서 **{mode_str}용** App Key·Secret을 다시 발급해 "
                    f"config.py 의 {key_hint} 에 정확히 넣어 주세요. "
                    f"(실전 키를 모의 서버에 쓰거나 그 반대면 실패합니다)"
                ),
            )
            return None

        self._last_failure_kind = "other"
        detail = rmsg or f"HTTP {res.status_code}"
        self._log_auth_fail_throttled(
            RuntimeError(detail),
            force_msg=f"❌ [Auth] OAuth 실패 — {detail} (return_code={rc_i})",
        )
        return None

    def get_auth_token(self) -> Optional[str]:
        try:
            now_m = time.monotonic()
            with self._oauth_cond:
                if (
                    self.access_token
                    and (now_m - self._token_issued_monotonic) < self._TOKEN_TTL_SEC
                ):
                    return self.access_token

                if now_m < self._oauth_429_until:
                    return None

                while self._oauth_in_progress:
                    self._oauth_cond.wait(timeout=45.0)
                    now_m = time.monotonic()
                    if (
                        self.access_token
                        and (now_m - self._token_issued_monotonic) < self._TOKEN_TTL_SEC
                    ):
                        return self.access_token
                    if now_m < self._oauth_429_until:
                        return None

                self._oauth_in_progress = True

            try:
                token = self._request_oauth_token()
                with self._oauth_cond:
                    if token:
                        self.access_token = token
                        self._token_issued_monotonic = time.monotonic()
                        self._last_failure_kind = ""
                        self.log("✅ [Auth] REST API 토큰 인증 성공")
                        return self.access_token
                    self.access_token = None
                    self._token_issued_monotonic = 0.0
            except Exception as e:
                self._log_auth_fail_throttled(e)
            finally:
                with self._oauth_cond:
                    self._oauth_in_progress = False
                    self._oauth_cond.notify_all()
        except Exception as e:
            self._log_auth_fail_throttled(e)
        return None

    def get_token(self) -> Optional[str]:
        """[COMPAT] kiwoom_rest_api / API Client 호환용 인터페이스"""
        return self.get_auth_token()
