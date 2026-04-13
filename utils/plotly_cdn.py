# -*- coding: utf-8 -*-
"""
Plotly HTML 리포트용: `pio.to_html(..., include_plotlyjs=False)` 를 쓸 때
<head>에 넣는 plotly.js CDN은 **로컬 plotly 패키지가 번들하는 plotly.js 버전**과
일치해야 한다. 고정 URL(예: 2.24.1)과 plotly 6.x(3.x JS)가 어긋나면
브라우저에서 축·캔들이 비정상(대각선 등)으로 그려질 수 있다.
"""
from __future__ import annotations

import html


def plotly_cdn_script_tag() -> str:
    """
    설치된 plotly에 맞는 CDN `plotly-*.min.js` 스크립트 태그 1개를 반환한다.

    Returns:
        ``<script src="https://cdn.plot.ly/plotly-X.Y.Z.min.js" ...></script>``
    """
    url = "https://cdn.plot.ly/plotly-2.24.1.min.js"
    try:
        from plotly.io._html import plotly_cdn_url

        u = plotly_cdn_url()
        if u and isinstance(u, str) and u.startswith("http"):
            url = u
    except Exception:
        pass
    return (
        f'<script src="{html.escape(url, quote=True)}" '
        'crossorigin="anonymous"></script>'
    )
