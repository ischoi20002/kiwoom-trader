# -*- coding: utf-8 -*-
"""텔레그램 Bot API 전송 유틸."""
from typing import Any, Dict, Optional, Tuple

import requests

import config


def _telegram_credentials() -> Tuple[str, str]:
    token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    cid = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    return token, cid


def send_telegram_message(
    message: str,
    *,
    parse_mode: Optional[str] = "HTML",
    log_func: Optional[Any] = None,
) -> bool:
    """
    텔레그램 Bot API로 메시지를 전송합니다.

    - DB 로직과 분리하기 위한 공용 유틸입니다.
    - Returns: 전송 성공(또는 서버 수신 후 응답만 지연) True, 실패 False

    주의
    ----
    ReadTimeout 은 요청이 이미 서버에 도달한 뒤 응답만 늦은 경우가 많아
    실제 수신과 함께 '실패'로 오탐이 났습니다. 이 경우 True 로 간주합니다.
    """
    log = log_func or print

    token, cid = _telegram_credentials()
    if not token or not cid or "발급" in token:
        try:
            log("[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 config.py 에 설정하세요.")
        except Exception:
            pass
        return False

    def _post(mode: Optional[str]) -> Tuple[Optional[requests.Response], Optional[Exception]]:
        payload: Dict[str, Any] = {"chat_id": cid, "text": message}
        if mode:
            payload["parse_mode"] = mode
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=(5, 15),  # (connect, read)
            )
            return res, None
        except Exception as ex:
            return None, ex

    def _body_ok(res: requests.Response) -> Tuple[bool, str]:
        """HTTP·JSON ok 판정. (성공여부, 실패사유)."""
        try:
            data = res.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            if bool(data.get("ok")):
                return True, ""
            desc = str(data.get("description") or "").strip()
            return False, desc or f"HTTP {res.status_code}"
        if res.status_code == 200:
            return True, ""
        return False, f"HTTP {res.status_code}: {(res.text or '')[:300]}"

    try:
        res, err = _post(parse_mode)
        if err is not None:
            # 연결 전 실패 vs 전송 후 응답 지연 구분
            if isinstance(err, requests.exceptions.ConnectTimeout):
                try:
                    log(f"[Telegram Exception] 연결 시간 초과: {err}")
                except Exception:
                    pass
                return False
            if isinstance(
                err,
                (requests.exceptions.ReadTimeout, requests.exceptions.Timeout),
            ):
                # 서버가 메시지를 받은 뒤 응답만 늦은 경우가 많음 → 오탐 방지
                return True
            try:
                log(f"[Telegram Exception] {err}")
            except Exception:
                pass
            return False

        assert res is not None
        ok, reason = _body_ok(res)
        if ok:
            return True

        # HTML 엔티티 오류 등 → plain 재시도 (수신 성공 가능)
        reason_l = reason.lower()
        parse_issue = bool(parse_mode) and (
            "parse" in reason_l
            or "entities" in reason_l
            or "can't find end of the entity" in reason_l
            or res.status_code == 400
        )
        if parse_issue:
            res2, err2 = _post(None)
            if err2 is not None:
                if isinstance(err2, requests.exceptions.ConnectTimeout):
                    try:
                        log(f"[Telegram Exception] plain 재시도 연결 시간 초과: {err2}")
                    except Exception:
                        pass
                    return False
                if isinstance(
                    err2,
                    (requests.exceptions.ReadTimeout, requests.exceptions.Timeout),
                ):
                    return True
                try:
                    log(f"[Telegram Exception] plain 재시도: {err2}")
                except Exception:
                    pass
                return False
            assert res2 is not None
            ok2, reason2 = _body_ok(res2)
            if ok2:
                return True
            try:
                log(f"[Telegram Error] {reason2 or reason}")
            except Exception:
                pass
            return False

        try:
            log(f"[Telegram Error] {reason}")
        except Exception:
            pass
        return False
    except Exception as e:
        try:
            log(f"[Telegram Exception] {e}")
        except Exception:
            pass
        return False
