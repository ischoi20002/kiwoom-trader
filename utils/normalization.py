def normalize_code(code):
    """[v12.5] Universal code normalization specifically for Korean Stock vs Index codes"""
    if code is None: return ""
    c = str(code).strip()
    # Stocks are strictly 6-digit. Indices are 3 or 4.
    if len(c) == 5:
        return c.zfill(6)
    return c
