# -*- coding: utf-8 -*-
"""
실전(실서버) 주문 전용 계좌 라이선스 검증.

- 모의투자(USE_MOCK=True): 검사하지 않음 (항상 허용)
- 실전 주문(place_order): license.key 서명·만료·계좌 해시 검사
- 하베스팅·시세조회·가상체결: 주문 API를 쓰지 않으므로 영향 없음

라이선스 발급: ``py -3 tools/issue_license.py --help``
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ed25519 공개키 (raw 32바이트, base64). tools/issue_license.py init-keys 가 갱신.
# 개인키는 tools/license_secrets/ 에만 두고 배포·커밋하지 않는다.
_EMBEDDED_PUBLIC_KEY_B64: str = (
    "Hbx7Osp4a3nbO4ZGAj3IqjspeSCqOUqvb8QTC/y4fjY="
)

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {
    "mtime": None,
    "path": None,
    "payload": None,
    "error": "",
}


def normalize_account_no(raw: Any) -> str:
    """계좌번호 정규화 — 숫자만 남김."""
    try:
        s = str(raw or "").strip()
    except Exception:
        return ""
    try:
        return "".join(ch for ch in s if ch.isdigit())
    except Exception:
        return ""


def hash_account_no(account_no: Any) -> str:
    """라이선스에 넣을 계좌 지문 (SHA-256 hex). 원문은 저장하지 않음."""
    norm = normalize_account_no(account_no)
    if not norm:
        return ""
    try:
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _project_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def license_file_candidates() -> List[Path]:
    """license.key 탐색 후보 (프로젝트 루트 → CWD)."""
    roots: List[Path] = []
    try:
        roots.append(_project_root())
    except Exception:
        pass
    try:
        cwd = Path.cwd()
        if cwd not in roots:
            roots.append(cwd)
    except Exception:
        pass
    out: List[Path] = []
    for r in roots:
        try:
            out.append(r / "license.key")
        except Exception:
            continue
    return out


def public_key_bytes() -> Optional[bytes]:
    """임베디드 공개키 또는 core/license_pubkey.b64 파일."""
    raw_b64 = str(_EMBEDDED_PUBLIC_KEY_B64 or "").strip()
    if raw_b64 and not raw_b64.startswith("REPLACE_"):
        try:
            key = base64.b64decode(raw_b64.encode("ascii"), validate=True)
            if len(key) == 32:
                return key
        except Exception:
            pass
    try:
        path = _project_root() / "core" / "license_pubkey.b64"
        if path.is_file():
            txt = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            key = base64.b64decode(txt.encode("ascii"), validate=True)
            if len(key) == 32:
                return key
    except Exception:
        pass
    return None


def _canonical_payload_bytes(payload: Dict[str, Any]) -> bytes:
    """서명 대상 — sig 제외, 키 정렬 JSON."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    try:
        return json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception as ex:
        raise ValueError(f"라이선스 payload 직렬화 실패: {ex}") from ex


def _verify_signature(payload: Dict[str, Any]) -> None:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as ex:
        raise RuntimeError(
            "cryptography 패키지가 필요합니다. "
            "pip install cryptography 후 다시 실행하세요."
        ) from ex

    pub_raw = public_key_bytes()
    if not pub_raw:
        raise RuntimeError(
            "라이선스 공개키가 없습니다. "
            "배포본 core/license_pubkey.b64 또는 임베디드 키를 확인하세요."
        )

    sig_b64 = str(payload.get("sig") or "").strip()
    if not sig_b64:
        raise ValueError("라이선스 서명이 없습니다.")
    try:
        sig = base64.b64decode(sig_b64.encode("ascii"), validate=True)
    except Exception as ex:
        raise ValueError(f"라이선스 서명 디코딩 실패: {ex}") from ex

    msg = _canonical_payload_bytes(payload)
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, msg)
    except Exception as ex:
        raise ValueError(f"라이선스 서명 검증 실패(위조·손상 가능): {ex}") from ex


def _parse_expires(raw: Any) -> Optional[date]:
    if raw is None or raw == "":
        return None
    try:
        s = str(raw).strip()[:10]
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception as ex:
        raise ValueError(f"만료일 형식 오류(YYYY-MM-DD): {raw!r}") from ex


def _load_license_file(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as ex:
        raise RuntimeError(f"license.key 읽기 실패: {ex}") from ex
    try:
        data = json.loads(text)
    except Exception as ex:
        raise ValueError(f"license.key JSON 파싱 실패: {ex}") from ex
    if not isinstance(data, dict):
        raise ValueError("license.key 최상위는 객체여야 합니다.")
    if int(data.get("v") or 0) != 1:
        raise ValueError(f"지원하지 않는 라이선스 버전: {data.get('v')!r}")
    _verify_signature(data)
    return data


def _resolve_license_path() -> Optional[Path]:
    for p in license_file_candidates():
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def load_verified_license(*, force_reload: bool = False) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    서명 검증된 라이선스 payload 반환.

    Returns:
        (payload 또는 None, 오류/안내 메시지)
    """
    path = _resolve_license_path()
    if path is None:
        return None, "license.key 파일이 없습니다."

    try:
        mtime = path.stat().st_mtime
    except Exception as ex:
        return None, f"license.key 상태 확인 실패: {ex}"

    with _CACHE_LOCK:
        try:
            if (
                not force_reload
                and _CACHE.get("path") == str(path)
                and _CACHE.get("mtime") == mtime
                and _CACHE.get("payload") is not None
            ):
                return dict(_CACHE["payload"]), ""
            if (
                not force_reload
                and _CACHE.get("path") == str(path)
                and _CACHE.get("mtime") == mtime
                and _CACHE.get("error")
            ):
                return None, str(_CACHE["error"])
        except Exception:
            pass

    try:
        payload = _load_license_file(path)
        with _CACHE_LOCK:
            _CACHE["path"] = str(path)
            _CACHE["mtime"] = mtime
            _CACHE["payload"] = dict(payload)
            _CACHE["error"] = ""
        return dict(payload), ""
    except Exception as ex:
        msg = str(ex) or "라이선스 검증 실패"
        with _CACHE_LOCK:
            _CACHE["path"] = str(path)
            _CACHE["mtime"] = mtime
            _CACHE["payload"] = None
            _CACHE["error"] = msg
        return None, msg


def _is_production_build() -> bool:
    """운영 배포본이면 True — 우회 스위치 무시."""
    try:
        from core.build_flavor import is_production_build

        return bool(is_production_build())
    except Exception:
        return False


def license_enforcement_enabled() -> bool:
    """
    실전 라이선스 검사 on/off.

    - 운영(production) 빌드: 항상 True (config 우회 불가)
    - 개발(dev) 빌드만: config.LICENSE_ENFORCE_REAL=False 허용
    """
    try:
        if _is_production_build():
            return True
    except Exception:
        pass
    try:
        import config as _cfg

        return bool(getattr(_cfg, "LICENSE_ENFORCE_REAL", True))
    except Exception:
        return True


def configured_live_account_no() -> str:
    """config.LIVE_ACCOUNT_NO — 실전 주문에 등록할 계좌."""
    try:
        import config as _cfg

        return normalize_account_no(getattr(_cfg, "LIVE_ACCOUNT_NO", ""))
    except Exception:
        return ""


def check_account_licensed(
    account_no: Any,
    *,
    today: Optional[date] = None,
) -> Tuple[bool, str]:
    """
    계좌번호가 서명된 라이선스에 포함되고 만료되지 않았는지 확인.
    """
    acc = normalize_account_no(account_no)
    if not acc:
        return False, "실전 계좌번호(LIVE_ACCOUNT_NO)가 비어 있습니다."

    payload, err = load_verified_license()
    if payload is None:
        return False, err or "라이선스를 불러올 수 없습니다."

    try:
        exp = _parse_expires(payload.get("expires"))
    except Exception as ex:
        return False, str(ex)
    if exp is not None:
        d = today or date.today()
        if d > exp:
            return False, f"라이선스가 만료되었습니다 (expires={exp.isoformat()})."

    try:
        allowed = payload.get("accounts") or []
        if not isinstance(allowed, list) or not allowed:
            return False, "라이선스에 허용 계좌가 없습니다."
        digest = hash_account_no(acc)
        allowed_norm = {str(x).strip().lower() for x in allowed if str(x).strip()}
        if digest.lower() not in allowed_norm:
            return False, "이 계좌는 라이선스에 등록되어 있지 않습니다."
    except Exception as ex:
        return False, f"계좌 대조 실패: {ex}"

    try:
        licensee = str(payload.get("licensee") or "").strip()
    except Exception:
        licensee = ""
    suffix = f" ({licensee})" if licensee else ""
    return True, f"라이선스 확인됨{suffix}"


def ensure_real_trading_allowed(
    account_no: Optional[Any] = None,
) -> Tuple[bool, str]:
    """
    실전 주문 직전 게이트.

    - 운영 빌드: 항상 라이선스 필수
    - 개발 빌드만 LICENSE_ENFORCE_REAL=False 시 통과
    - 그 외 → LIVE_ACCOUNT_NO(또는 인자)가 라이선스에 있어야 함
    """
    try:
        if not license_enforcement_enabled():
            return True, "라이선스 검사 비활성(개발 전용 LICENSE_ENFORCE_REAL=False)"
    except Exception:
        pass

    acc = normalize_account_no(account_no) if account_no is not None else configured_live_account_no()
    return check_account_licensed(acc)


def describe_license_status() -> str:
    """UI/로그용 한 줄 상태."""
    try:
        if not license_enforcement_enabled():
            return "실전 라이선스 검사 꺼짐"
    except Exception:
        pass
    acc = configured_live_account_no()
    if not acc:
        return "실전 계좌 미설정 (config.LIVE_ACCOUNT_NO)"
    ok, msg = check_account_licensed(acc)
    return msg if ok else f"실전 주문 불가: {msg}"
