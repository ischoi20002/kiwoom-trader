# -*- coding: utf-8 -*-
"""
실전 NXT 거래가능 종목 캐시.

- 키움 ``ka10100`` 응답 ``nxtEnable == 'Y'`` 기준
- 당일 감시 종목·보유 종목을 아침/감시시작 시 일괄 갱신
- 15:30·15:15 실전 주문 경로에서 NXT 불가 시 next_open/동시호가 폴백에 사용
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set


def _norm_code(code: Any) -> str:
    try:
        from utils.normalization import canonical_stock_code

        return str(canonical_stock_code(code) or code).zfill(6)
    except Exception:
        try:
            return str(code or "").split("_")[0].zfill(6)
        except Exception:
            return ""


def _cache_path() -> str:
    try:
        from utils.server_mode_paths import equity_snapshots_root_dir

        root = equity_snapshots_root_dir()
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, "_nxt_eligibility.json")
    except Exception:
        return os.path.join(os.getcwd(), "_nxt_eligibility.json")


class NxtEligibilityService:
    """종목별 NXT 거래가능 여부 캐시·갱신."""

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._lock = threading.RLock()
        self._day: str = ""
        self._enabled: Set[str] = set()
        self._disabled: Set[str] = set()
        self._updated_at: Optional[str] = None
        self._refresh_inflight: bool = False
        self._load_disk()

    def _safe_log(self, msg: str) -> None:
        try:
            self.log(msg)
        except Exception:
            pass

    def _today_key(self, now: Optional[datetime] = None) -> str:
        try:
            t = now if now is not None else datetime.now()
            return t.date().isoformat()
        except Exception:
            return date.today().isoformat()

    def _roll_day_if_needed(self, now: Optional[datetime] = None) -> None:
        day = self._today_key(now)
        with self._lock:
            if self._day == day:
                return
            self._day = day
            self._enabled = set()
            self._disabled = set()
            self._updated_at = None

    def _load_disk(self) -> None:
        try:
            path = _cache_path()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            day = str(data.get("day") or "")
            today = self._today_key()
            if day != today:
                return
            en = data.get("enabled") or []
            dis = data.get("disabled") or []
            with self._lock:
                self._day = day
                self._enabled = {
                    _norm_code(c) for c in en if _norm_code(c)
                }
                self._disabled = {
                    _norm_code(c) for c in dis if _norm_code(c)
                }
                self._updated_at = data.get("updated_at")
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            with self._lock:
                payload = {
                    "day": self._day or self._today_key(),
                    "updated_at": self._updated_at
                    or datetime.now().isoformat(timespec="seconds"),
                    "enabled": sorted(self._enabled),
                    "disabled": sorted(self._disabled),
                }
            path = _cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass

    def known_count(self) -> int:
        with self._lock:
            return len(self._enabled) + len(self._disabled)

    def is_nxt_tradable(
        self, code: Any, *, fetch_if_unknown: bool = True
    ) -> bool:
        """
        NXT 거래가능 여부.

        미조회·실패 시 False(안전: 15:30 next_open 폴백).
        모의서버에서는 호출해도 의미가 없고, 상위가 live 가드를 합니다.
        """
        nk = _norm_code(code)
        if len(nk) != 6:
            return False
        self._roll_day_if_needed()
        with self._lock:
            if nk in self._enabled:
                return True
            if nk in self._disabled:
                return False
        if not fetch_if_unknown:
            return False
        try:
            ok = self._fetch_one(nk)
            return bool(ok)
        except Exception:
            return False

    def _auth_token(self) -> Optional[str]:
        try:
            am = getattr(self.app, "auth_manager", None)
            if am is None:
                return None
            tok = am.get_auth_token()
            return str(tok) if tok else None
        except Exception:
            return None

    def _fetch_one(self, nk: str) -> bool:
        """단건 ka10100. 성공 시 캐시 반영 후 True/False, 실패 시 False."""
        token = self._auth_token()
        if not token:
            return False
        dm = getattr(self.app, "data_manager", None)
        if dm is None or not hasattr(dm, "fetch_nxt_status"):
            return False
        try:
            enabled = bool(dm.fetch_nxt_status(nk, token))
        except Exception:
            enabled = False
        with self._lock:
            if enabled:
                self._enabled.add(nk)
                self._disabled.discard(nk)
            else:
                self._disabled.add(nk)
                self._enabled.discard(nk)
            self._updated_at = datetime.now().isoformat(timespec="seconds")
        try:
            self._persist()
        except Exception:
            pass
        return enabled

    def collect_codes_for_refresh(self) -> List[str]:
        """감시·보유 종목 합집합."""
        codes: Set[str] = set()
        try:
            for c in getattr(self.app, "monitor_codes", None) or []:
                nk = _norm_code(c)
                if len(nk) == 6:
                    codes.add(nk)
        except Exception:
            pass
        try:
            tc = getattr(self.app, "trading_controller", None)
            for c in getattr(tc, "monitor_codes", None) or []:
                nk = _norm_code(c)
                if len(nk) == 6:
                    codes.add(nk)
        except Exception:
            pass
        try:
            pf = getattr(self.app, "portfolio", None)
            vp = getattr(pf, "virtual_positions", None) or {}
            for c in vp.keys():
                nk = _norm_code(c)
                if len(nk) == 6:
                    codes.add(nk)
        except Exception:
            pass
        return sorted(codes)

    def refresh_codes(
        self,
        codes: Optional[Iterable[Any]] = None,
        *,
        force: bool = False,
        pause_sec: float = 0.35,
        background: bool = False,
    ) -> None:
        """
        종목 목록 NXT 가능 여부 일괄 갱신.

        ``force=False`` 이고 당일 이미 동일 집합이 채워져 있으면 스킵하지 않고
        미지의 종목만 조회합니다. ``force=True`` 이면 전달 종목 전부 재조회.
        """
        if background:
            try:
                threading.Thread(
                    target=lambda: self.refresh_codes(
                        codes, force=force, pause_sec=pause_sec, background=False
                    ),
                    name="NxtEligibilityRefresh",
                    daemon=True,
                ).start()
            except Exception:
                pass
            return

        try:
            import config as _cfg

            if bool(getattr(_cfg, "USE_MOCK", False)):
                return
        except Exception:
            pass

        with self._lock:
            if self._refresh_inflight:
                return
            self._refresh_inflight = True

        try:
            self._roll_day_if_needed()
            raw = list(codes) if codes is not None else self.collect_codes_for_refresh()
            targets: List[str] = []
            for c in raw:
                nk = _norm_code(c)
                if len(nk) != 6:
                    continue
                if force:
                    targets.append(nk)
                    continue
                with self._lock:
                    known = nk in self._enabled or nk in self._disabled
                if not known:
                    targets.append(nk)
            # 중복 제거·정렬
            targets = sorted(set(targets))
            if not targets:
                return

            token = self._auth_token()
            if not token:
                self._safe_log(
                    "ℹ️ [NXT] 거래가능 종목 갱신 생략 — REST 토큰 없음"
                )
                return

            dm = getattr(self.app, "data_manager", None)
            if dm is None or not hasattr(dm, "fetch_nxt_status"):
                self._safe_log(
                    "⚠️ [NXT] fetch_nxt_status 없음 — 갱신 불가"
                )
                return

            self._safe_log(
                f"📌 [NXT] 거래가능 종목 조회 시작 "
                f"({len(targets)}종목, force={bool(force)})"
            )
            ok_n = 0
            no_n = 0
            err_n = 0
            for i, nk in enumerate(targets):
                try:
                    enabled = bool(dm.fetch_nxt_status(nk, token))
                    with self._lock:
                        if enabled:
                            self._enabled.add(nk)
                            self._disabled.discard(nk)
                            ok_n += 1
                        else:
                            self._disabled.add(nk)
                            self._enabled.discard(nk)
                            no_n += 1
                        self._updated_at = datetime.now().isoformat(
                            timespec="seconds"
                        )
                except Exception:
                    err_n += 1
                    with self._lock:
                        # 실패 시 불가 쪽으로 두지 않고 미지로 남겨 재시도 가능
                        self._enabled.discard(nk)
                        self._disabled.discard(nk)
                if pause_sec > 0 and i + 1 < len(targets):
                    try:
                        time.sleep(float(pause_sec))
                    except Exception:
                        pass

            try:
                self._persist()
            except Exception:
                pass
            with self._lock:
                en = len(self._enabled)
                dis = len(self._disabled)
            self._safe_log(
                f"✅ [NXT] 거래가능 종목 갱신 완료 — "
                f"가능 {ok_n} / 불가 {no_n} / 오류 {err_n} "
                f"(캐시 가능={en}, 불가={dis})"
            )
        finally:
            with self._lock:
                self._refresh_inflight = False

    def refresh_morning(self, *, force: bool = True) -> None:
        """장전·감시 시작용: 감시+보유 종목 강제 재조회(백그라운드)."""
        try:
            codes = self.collect_codes_for_refresh()
            self.refresh_codes(codes, force=force, background=True)
        except Exception as e:
            self._safe_log(f"⚠️ [NXT] 아침 갱신 기동 실패: {e}")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "day": self._day,
                "updated_at": self._updated_at,
                "enabled": sorted(self._enabled),
                "disabled": sorted(self._disabled),
            }
