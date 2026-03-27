def normalize_code(code):
    """[v12.5] Universal code normalization specifically for Korean Stock vs Index codes"""
    if code is None: return ""
    c = str(code).strip()
    # Stocks are strictly 6-digit. Indices are 3 or 4.
    if len(c) == 5:
        return c.zfill(6)
    return c

def get_tick_size(price):
    """[v3.8] 한국 주식시장 표준 호가 단위 (KOSPI/KOSDAQ 통합 기준, 2023~)"""
    p = float(price)
    if p < 2000: return 1
    if p < 5000: return 5
    if p < 10000: return 10
    if p < 50000: return 50
    if p < 100000: return 100
    if p < 500000: return 500
    return 1000
