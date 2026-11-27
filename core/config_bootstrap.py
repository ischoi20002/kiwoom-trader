# -*- coding: utf-8 -*-
"""
신규 설치·설정 마법사용 config.py 부트스트랩.

- config.py 없으면 example 에서 생성
- 필수 API 키 충족 여부 판별
- 마법사 입력값을 config.py 로 안전하게 기록
"""
from __future__ import annotations

import ast
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


_ROOT_CANDIDATES = (Path(r"c:\k-trader"), Path(r"c:\vibe"))


def project_root() -> Path:
    """운영/개발 루트 (config.py 가 있을 곳 우선, 없으면 이 파일 기준)."""
    here = Path(__file__).resolve().parents[1]
    for cand in _ROOT_CANDIDATES:
        try:
            if (cand / "k_trader_gui.py").is_file():
                return cand
        except Exception:
            continue
    return here


def config_path(root: Optional[Path] = None) -> Path:
    return (Path(root) if root is not None else project_root()) / "config.py"


def example_path(root: Optional[Path] = None) -> Path:
    return (Path(root) if root is not None else project_root()) / "config.example.py"


def license_key_path(root: Optional[Path] = None) -> Path:
    return (Path(root) if root is not None else project_root()) / "license.key"


@dataclass
class ConfigFormValues:
    """설정 마법사 폼 값."""

    use_mock: bool = True
    app_key: str = ""
    app_secret: str = ""
    mock_app_key: str = ""
    mock_app_secret: str = ""
    live_account_no: str = ""
    allow_real_order: bool = False
    allow_mock_order: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    krx_id: str = ""
    krx_pw: str = ""
    data_go_kr_service_key: str = ""
    license_src: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def ensure_config_file_exists(root: Optional[Path] = None) -> Path:
    """
    config.py 가 없으면 config.example.py 를 복사한다.

    Returns
    -------
    Path
        config.py 경로
    """
    root_p = Path(root) if root is not None else project_root()
    cfg = config_path(root_p)
    if cfg.is_file():
        return cfg
    ex = example_path(root_p)
    try:
        if ex.is_file():
            shutil.copy2(ex, cfg)
            return cfg
    except Exception as ex_err:
        raise RuntimeError(f"config.example.py 복사 실패: {ex_err}") from ex_err
    # example 도 없으면 최소 스텁
    try:
        cfg.write_text(
            (
                "# auto-generated stub — 설정 마법사에서 값을 채우세요\n"
                "import os\n"
                'DATA_GO_KR_SERVICE_KEY = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()\n'
                'APP_KEY = ""\n'
                'APP_SECRET = ""\n'
                'REAL_URL = "https://api.kiwoom.com"\n'
                'MOCK_URL = "https://mockapi.kiwoom.com"\n'
                "USE_MOCK = True\n"
                "URL = MOCK_URL if USE_MOCK else REAL_URL\n"
                "KA10080_GLOBAL_MIN_INTERVAL_SEC = 1.05\n"
                'MOCK_APP_KEY = ""\n'
                'MOCK_APP_SECRET = ""\n'
                "ALLOW_MOCK_ORDER = True\n"
                "ALLOW_REAL_ORDER = False\n"
                'LIVE_ACCOUNT_NO = ""\n'
                'TELEGRAM_BOT_TOKEN = ""\n'
                'TELEGRAM_CHAT_ID = ""\n'
                'KRX_ID = ""\n'
                'KRX_PW = ""\n'
            ),
            encoding="utf-8",
            newline="\n",
        )
    except Exception as ex_err:
        raise RuntimeError(f"config.py 스텁 생성 실패: {ex_err}") from ex_err
    return cfg


def _read_assign_literals(text: str) -> Dict[str, Any]:
    """간단 할당문에서 리터럴 값만 추출 (실패 시 빈 dict)."""
    out: Dict[str, Any] = {}
    try:
        tree = ast.parse(text)
    except Exception:
        return out
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        try:
            out[name] = ast.literal_eval(node.value)
        except Exception:
            continue
    return out


def load_config_form_values(root: Optional[Path] = None) -> ConfigFormValues:
    """기존 config.py 에서 폼 값을 읽는다 (없으면 기본값)."""
    root_p = Path(root) if root is not None else project_root()
    ensure_config_file_exists(root_p)
    cfg = config_path(root_p)
    vals = ConfigFormValues()
    try:
        text = cfg.read_text(encoding="utf-8")
        lit = _read_assign_literals(text)
        vals.use_mock = bool(lit.get("USE_MOCK", True))
        vals.app_key = str(lit.get("APP_KEY", "") or "")
        vals.app_secret = str(lit.get("APP_SECRET", "") or "")
        vals.mock_app_key = str(lit.get("MOCK_APP_KEY", "") or "")
        vals.mock_app_secret = str(lit.get("MOCK_APP_SECRET", "") or "")
        vals.live_account_no = str(lit.get("LIVE_ACCOUNT_NO", "") or "")
        vals.allow_real_order = bool(lit.get("ALLOW_REAL_ORDER", False))
        vals.allow_mock_order = bool(lit.get("ALLOW_MOCK_ORDER", True))
        vals.telegram_bot_token = str(lit.get("TELEGRAM_BOT_TOKEN", "") or "")
        vals.telegram_chat_id = str(lit.get("TELEGRAM_CHAT_ID", "") or "")
        vals.krx_id = str(lit.get("KRX_ID", "") or "")
        vals.krx_pw = str(lit.get("KRX_PW", "") or "")
        vals.data_go_kr_service_key = str(lit.get("DATA_GO_KR_SERVICE_KEY", "") or "")
    except Exception:
        pass
    return vals


def is_config_ready(root: Optional[Path] = None) -> bool:
    """
    본 GUI 기동에 필요한 최소 설정이 채워졌는지.

    - 모의: MOCK_APP_KEY·MOCK_APP_SECRET 또는 APP_KEY·APP_SECRET
    - 실전: APP_KEY·APP_SECRET + LIVE_ACCOUNT_NO + license.key 존재
    """
    root_p = Path(root) if root is not None else project_root()
    cfg = config_path(root_p)
    if not cfg.is_file():
        return False
    try:
        vals = load_config_form_values(root_p)
    except Exception:
        return False
    if vals.use_mock:
        mock_ok = bool(vals.mock_app_key.strip() and vals.mock_app_secret.strip())
        real_ok = bool(vals.app_key.strip() and vals.app_secret.strip())
        return mock_ok or real_ok
    if not (vals.app_key.strip() and vals.app_secret.strip()):
        return False
    if not vals.live_account_no.strip():
        return False
    try:
        if not license_key_path(root_p).is_file():
            return False
    except Exception:
        return False
    return True


def validate_form_values(vals: ConfigFormValues, root: Optional[Path] = None) -> Optional[str]:
    """검증 실패 시 한국어 오류 메시지, 성공 시 None."""
    root_p = Path(root) if root is not None else project_root()
    if vals.use_mock:
        if not (
            (vals.mock_app_key.strip() and vals.mock_app_secret.strip())
            or (vals.app_key.strip() and vals.app_secret.strip())
        ):
            return "모의투자: MOCK_APP_KEY/SECRET 또는 APP_KEY/SECRET 을 입력하세요."
        return None
    if not vals.app_key.strip() or not vals.app_secret.strip():
        return "실전: APP_KEY / APP_SECRET 을 입력하세요."
    if not vals.live_account_no.strip():
        return "실전: LIVE_ACCOUNT_NO(계좌번호)를 입력하세요."
    lic = license_key_path(root_p)
    src = (vals.license_src or "").strip()
    if not lic.is_file() and not src:
        return "실전: license.key 파일을 선택하세요."
    if src:
        try:
            if not Path(src).is_file():
                return f"license.key 원본을 찾을 수 없습니다: {src}"
        except Exception as ex:
            return f"license.key 경로 확인 실패: {ex}"
    return None


_ASSIGN_RE_CACHE: Dict[str, re.Pattern[str]] = {}


def _assign_pattern(name: str) -> re.Pattern[str]:
    if name not in _ASSIGN_RE_CACHE:
        _ASSIGN_RE_CACHE[name] = re.compile(
            rf"^({re.escape(name)}\s*=\s*).*$",
            re.MULTILINE,
        )
    return _ASSIGN_RE_CACHE[name]


def _replace_assign(text: str, name: str, value_src: str) -> str:
    """NAME = ... 줄을 value_src 로 교체. 없으면 파일 끝에 추가."""
    pat = _assign_pattern(name)
    if pat.search(text):
        return pat.sub(rf"\g<1>{value_src}", text, count=1)
    return text.rstrip() + f"\n{name} = {value_src}\n"


def write_config_py(vals: ConfigFormValues, root: Optional[Path] = None) -> Path:
    """
    폼 값을 config.py 에 기록한다.

    example 을 기반으로 치환하며, 문자열은 ``repr`` 로 이스케이프한다.
    """
    root_p = Path(root) if root is not None else project_root()
    ensure_config_file_exists(root_p)
    cfg = config_path(root_p)
    try:
        text = cfg.read_text(encoding="utf-8")
    except Exception as ex:
        raise RuntimeError(f"config.py 읽기 실패: {ex}") from ex

    # DATA_GO_KR 은 환경변수 식 유지가 이상적이나, 사용자가 직접 넣으면 리터럴로
    replacements: Mapping[str, str] = {
        "APP_KEY": repr(vals.app_key.strip()),
        "APP_SECRET": repr(vals.app_secret.strip()),
        "USE_MOCK": "True" if vals.use_mock else "False",
        "MOCK_APP_KEY": repr(vals.mock_app_key.strip()),
        "MOCK_APP_SECRET": repr(vals.mock_app_secret.strip()),
        "ALLOW_MOCK_ORDER": "True" if vals.allow_mock_order else "False",
        "ALLOW_REAL_ORDER": "True" if vals.allow_real_order else "False",
        "LIVE_ACCOUNT_NO": repr(vals.live_account_no.strip()),
        "TELEGRAM_BOT_TOKEN": repr(vals.telegram_bot_token.strip()),
        "TELEGRAM_CHAT_ID": repr(vals.telegram_chat_id.strip()),
        "KRX_ID": repr(vals.krx_id.strip()),
        "KRX_PW": repr(vals.krx_pw.strip()),
    }
    for name, value_src in replacements.items():
        text = _replace_assign(text, name, value_src)

    # URL 줄이 있으면 USE_MOCK 에 맞게 유지 (example 기본식)
    if "URL =" in text and "MOCK_URL if USE_MOCK" not in text:
        text = _replace_assign(text, "URL", "MOCK_URL if USE_MOCK else REAL_URL")

    if vals.data_go_kr_service_key.strip():
        # 환경변수 식을 리터럴로 덮지 않고, 별도 할당이 이미 있으면 교체
        if re.search(r"^DATA_GO_KR_SERVICE_KEY\s*=", text, re.MULTILINE):
            # os.environ.get(...) 형태면 리터럴로 교체
            text = re.sub(
                r"^DATA_GO_KR_SERVICE_KEY\s*=\s*.*$",
                f"DATA_GO_KR_SERVICE_KEY = {repr(vals.data_go_kr_service_key.strip())}",
                text,
                count=1,
                flags=re.MULTILINE,
            )

    tmp = cfg.with_suffix(".py.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(cfg)
    except Exception as ex:
        try:
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass
        raise RuntimeError(f"config.py 저장 실패: {ex}") from ex

    # license.key 복사
    src = (vals.license_src or "").strip()
    if src:
        try:
            shutil.copy2(src, license_key_path(root_p))
        except Exception as ex:
            raise RuntimeError(f"license.key 복사 실패: {ex}") from ex

    return cfg
