import re

_NUMERIC_CODE_STR = re.compile(r"^[+-]?\d+(\.\d+)?$")


def normalize_code(code):
    """[v12.5] Universal code normalization specifically for Korean Stock vs Index codes"""
    if code is None: return ""
    c = str(code).strip()
    # Stocks are strictly 6-digit. Indices are 3 or 4.
    if c == 'KS11': return '001'
    if c == 'KQ11': return '101'
    if len(c) == 5:
        return c.zfill(6)
    return c


def canonical_stock_code(code: object) -> str:
    """
    실시간 감시·bars_data 공통 6자리 종목코드.
    보유키 접미(``443060_1``)·앞자리 0 누락(``5930``) 등을 ``005930`` 형태로 맞춥니다.
    pandas/Excel 에서 종목코드가 int·float·``2380.0`` 문자열로 온 경우도 처리합니다.
    """
    try:
        if code is None:
            return ""
        # bool 은 int 의 서브클래스이므로 반드시 먼저 분기
        if isinstance(code, bool):
            return ""
        if isinstance(code, int):
            iv = abs(int(code))
            return str(iv).zfill(6)[-6:]
        if isinstance(code, float):
            try:
                if code != code or code in (float("inf"), float("-inf")):
                    return ""
                iv = abs(int(round(code)))
                sc = str(iv).zfill(6)
                return sc[-6:] if len(sc) > 6 else sc
            except Exception:
                pass
        s = str(code).strip()
        if not s:
            return ""
        base = s.split("_")[0].strip()
        # KRX 우선주 등 영문 포함 코드(예: 0126Z0) — 숫자만 추출하면 001260(남광토건)과 충돌
        if any(ch.isalpha() for ch in base):
            alnum = "".join(ch for ch in base if ch.isalnum()).upper()
            if len(alnum) == 6:
                return alnum
            bu = base.upper()
            if len(bu) == 6:
                return bu
        # ``2380.0`` / ``002380`` 처럼 순수 숫자 표기 → 자릿수 복원 후 6자리
        try:
            if _NUMERIC_CODE_STR.match(base):
                fv = float(base)
                iv = abs(int(round(fv)))
                if iv <= 999999:
                    return str(iv).zfill(6)
        except Exception:
            pass
        digits = "".join(ch for ch in base if ch.isdigit())
        if not digits:
            return base
        if len(digits) <= 6:
            return digits.zfill(6)
        return digits[-6:]
    except Exception:
        return ""

def get_tick_size(price):
    """한국 주식시장 표준 호가 단위 (KOSPI/KOSDAQ 통합, 가격대별 단위)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 1
    if p < 2000:
        return 1
    if p < 5000:
        return 5
    if p < 20000:
        return 10
    if p < 50000:
        return 50
    if p < 200000:
        return 100
    if p < 500000:
        return 500
    return 1000
