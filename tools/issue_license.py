# -*- coding: utf-8 -*-
"""
실전 계좌 라이선스 발급 도구 (개발자 전용).

사용 예:
  # 1) 최초 1회: 키쌍 생성 (개인키는 절대 배포·커밋 금지)
  py -3 tools/issue_license.py init-keys

  # 2) 고객 실계좌번호로 license.key 발급
  py -3 tools/issue_license.py issue --account 1234567890 --licensee \"홍길동\" --expires 2027-12-31

  # 3) 검증
  py -3 tools/issue_license.py verify --account 1234567890 --file license.key

고객 PC에는 license.key 만 복사하고, config.py 에 LIVE_ACCOUNT_NO 를 동일 계좌로 넣습니다.
모의투자는 라이선스 없이 동작합니다.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECRETS_DIR = ROOT / "tools" / "license_secrets"
PRIVATE_KEY_PATH = SECRETS_DIR / "ed25519_private.b64"
PUBKEY_FILE = ROOT / "core" / "license_pubkey.b64"
GUARD_PATH = ROOT / "core" / "license_guard.py"


def _require_crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as ex:
        raise SystemExit(
            "cryptography 가 필요합니다: py -3 -m pip install cryptography"
        ) from ex
    return Ed25519PrivateKey, Ed25519PublicKey


def _canonical_bytes(payload: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _patch_embedded_pubkey(b64: str) -> None:
    """license_guard.py 의 _EMBEDDED_PUBLIC_KEY_B64 갱신."""
    text = GUARD_PATH.read_text(encoding="utf-8")
    pat = re.compile(
        r'(_EMBEDDED_PUBLIC_KEY_B64:\s*str\s*=\s*\(\s*")[^"]*("\s*\))',
        re.MULTILINE,
    )
    new_text, n = pat.subn(rf"\g<1>{b64}\2", text, count=1)
    if n != 1:
        # 여러 줄 형태
        pat2 = re.compile(
            r'(_EMBEDDED_PUBLIC_KEY_B64:\s*str\s*=\s*\(\s*\n\s*")[^"]*("\s*\n\s*\))',
            re.MULTILINE,
        )
        new_text, n = pat2.subn(rf"\g<1>{b64}\2", text, count=1)
    if n != 1:
        raise RuntimeError("license_guard.py 임베디드 공개키 패치 실패")
    GUARD_PATH.write_text(new_text, encoding="utf-8")


def cmd_init_keys(_: argparse.Namespace) -> int:
    Ed25519PrivateKey, _ = _require_crypto()
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_KEY_PATH.is_file():
        print(f"[건너뜀] 개인키 이미 존재: {PRIVATE_KEY_PATH}")
        priv_b64 = PRIVATE_KEY_PATH.read_text(encoding="utf-8").strip()
        priv = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(priv_b64.encode("ascii"))
        )
    else:
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes_raw()
        priv_b64 = base64.b64encode(raw).decode("ascii")
        PRIVATE_KEY_PATH.write_text(priv_b64 + "\n", encoding="utf-8")
        print(f"[생성] 개인키: {PRIVATE_KEY_PATH}")

    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii")
    PUBKEY_FILE.write_text(pub_b64 + "\n", encoding="utf-8")
    print(f"[저장] 공개키 파일: {PUBKEY_FILE}")
    _patch_embedded_pubkey(pub_b64)
    print(f"[갱신] 임베디드 공개키: {GUARD_PATH.name}")
    print()
    print("※ tools/license_secrets/ 는 Git에 올리지 마세요.")
    return 0


def _load_private_key():
    Ed25519PrivateKey, _ = _require_crypto()
    if not PRIVATE_KEY_PATH.is_file():
        raise SystemExit(
            f"개인키 없음: {PRIVATE_KEY_PATH}\n"
            "먼저: py -3 tools/issue_license.py init-keys"
        )
    raw = base64.b64decode(
        PRIVATE_KEY_PATH.read_text(encoding="utf-8").strip().encode("ascii")
    )
    return Ed25519PrivateKey.from_private_bytes(raw)


def cmd_issue(args: argparse.Namespace) -> int:
    from core.license_guard import hash_account_no, normalize_account_no

    accounts_raw: List[str] = list(args.account or [])
    if not accounts_raw:
        raise SystemExit("--account 계좌번호를 1개 이상 지정하세요.")

    digests: List[str] = []
    for a in accounts_raw:
        norm = normalize_account_no(a)
        if len(norm) < 8:
            raise SystemExit(f"계좌번호가 너무 짧습니다: {a!r}")
        digests.append(hash_account_no(norm))
        print(f"  계좌 …{norm[-4:]} → {digests[-1][:12]}…")

    expires: Optional[str] = None
    if args.expires:
        try:
            datetime.strptime(args.expires, "%Y-%m-%d")
        except ValueError as ex:
            raise SystemExit(f"--expires YYYY-MM-DD 형식: {ex}") from ex
        expires = args.expires
    elif args.days and int(args.days) > 0:
        expires = (date.today() + timedelta(days=int(args.days))).isoformat()

    payload: Dict[str, Any] = {
        "v": 1,
        "licensee": str(args.licensee or "").strip(),
        "accounts": digests,
        "issued_at": date.today().isoformat(),
    }
    if expires:
        payload["expires"] = expires

    priv = _load_private_key()
    sig = priv.sign(_canonical_bytes(payload))
    payload["sig"] = base64.b64encode(sig).decode("ascii")

    out = Path(args.out or (ROOT / "license.key"))
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[발급] {out}")
    if expires:
        print(f"  만료: {expires}")
    else:
        print("  만료: 없음")
    print("고객에게 license.key 를 전달하고 config.LIVE_ACCOUNT_NO 에 동일 계좌를 넣으세요.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from core import license_guard as lg

    path = Path(args.file) if args.file else None
    if path is not None:
        # 임시로 해당 파일만 보이게 cwd/루트 복사 대신 직접 로드
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            lg._verify_signature(data)  # noqa: SLF001 — 검증 도구
            print(f"[서명 OK] {path}")
            print(f"  licensee={data.get('licensee')!r} expires={data.get('expires')!r}")
            print(f"  accounts={len(data.get('accounts') or [])}개")
        except Exception as ex:
            print(f"[서명 실패] {ex}")
            return 1
        if args.account:
            ok, msg = lg.check_account_licensed(args.account)
            # check는 license.key 경로 탐색을 쓰므로, 인자 파일과 다를 수 있음
            # 동일 내용이면 accounts 직접 대조
            digest = lg.hash_account_no(args.account)
            allowed = {str(x).lower() for x in (data.get("accounts") or [])}
            if digest.lower() in allowed:
                exp = data.get("expires")
                if exp:
                    try:
                        if date.today() > datetime.strptime(str(exp)[:10], "%Y-%m-%d").date():
                            print(f"[계좌] 만료됨: {exp}")
                            return 1
                    except Exception as ex:
                        print(f"[계좌] 만료일 오류: {ex}")
                        return 1
                print(f"[계좌 OK] …{lg.normalize_account_no(args.account)[-4:]}")
                return 0
            print(f"[계좌 거부] {msg}")
            return 1
        return 0

    ok, msg = lg.check_account_licensed(args.account or lg.configured_live_account_no())
    print(("[OK] " if ok else "[거부] ") + msg)
    return 0 if ok else 1


def cmd_fingerprint(args: argparse.Namespace) -> int:
    from core.license_guard import hash_account_no, normalize_account_no

    n = normalize_account_no(args.account)
    print(n)
    print(hash_account_no(n))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="실전 계좌 라이선스 발급")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-keys", help="Ed25519 키쌍 생성")
    p_init.set_defaults(func=cmd_init_keys)

    p_issue = sub.add_parser("issue", help="license.key 발급")
    p_issue.add_argument(
        "--account",
        action="append",
        required=True,
        help="실계좌번호 (여러 번 지정 가능)",
    )
    p_issue.add_argument("--licensee", default="", help="고객 표시명")
    p_issue.add_argument("--expires", default="", help="만료일 YYYY-MM-DD")
    p_issue.add_argument("--days", type=int, default=0, help="오늘부터 N일 후 만료")
    p_issue.add_argument("--out", default="", help="출력 경로 (기본: ./license.key)")
    p_issue.set_defaults(func=cmd_issue)

    p_ver = sub.add_parser("verify", help="라이선스 검증")
    p_ver.add_argument("--account", default="", help="대조할 계좌번호")
    p_ver.add_argument("--file", default="", help="license.key 경로")
    p_ver.set_defaults(func=cmd_verify)

    p_fp = sub.add_parser("fingerprint", help="계좌 해시만 출력")
    p_fp.add_argument("--account", required=True)
    p_fp.set_defaults(func=cmd_fingerprint)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as ex:
        print(f"[오류] {ex}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
