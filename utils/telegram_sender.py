# -*- coding: utf-8 -*-
from typing import Any, Dict, Optional

import requests

import config


def send_telegram_message(
    message: str,
    *,
    parse_mode: Optional[str] = "HTML",
    log_func: Optional[Any] = None,
) -> bool:
    """
    텔레그램 Bot API로 메시지를 전송합니다.

    - DB 로직과 분리하기 위한 공용 유틸입니다.
    - Returns: 전송 성공 True, 실패 False
    """
    log = log_func or print

    token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
    cid = getattr(config, "TELEGRAM_CHAT_ID", "") or ""
    if not token or not cid or "발급" in token:
        try:
            log("[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 config.py 에 설정하세요.")
        except Exception:
            pass
        return False

    try:
        payload: Dict[str, Any] = {"chat_id": cid, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        res = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        if res.status_code != 200:
            try:
                log(f"[Telegram Error] HTTP {res.status_code}: {res.text[:500]}")
            except Exception:
                pass
            return False
        return True
    except Exception as e:
        try:
            log(f"[Telegram Exception] {e}")
        except Exception:
            pass
        return False

