# -*- coding: utf-8 -*-
"""감시 탭 non-UI 헬퍼 — 텔레그램 분할·셀 포맷·열 폭 기본값 (Tk 비의존)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.signal_monitor_display import format_monitor_vs_prev_cell
from core.supply_display import format_bid_ask_monitor_display

# 텔레그램 단일 메시지 최대 길이(일반 텍스트 기준 여유)
TELEGRAM_MAX_CHARS: int = 4000

# 감시 트리 컬럼 기본 폭(px). 사용자 조절값·세로 분할 비율은 monitor_column_widths.json
DEFAULT_MONITOR_COL_WIDTHS: Dict[str, int] = {
    "Score": 40,
    "Code": 56,
    "Name": 88,
    "StratType": 52,
    "Price": 64,
    "AtrExit": 138,
    "VsPrev": 50,
    "Ratio": 62,
    "Status": 188,
    "VolPower": 52,
    "BidAsk": 118,
    "Volume": 72,
}
MONITOR_COL_MIN_WIDTH: Dict[str, int] = {
    "Score": 32,
    "Code": 48,
    "Name": 52,
    "StratType": 44,
    "Price": 52,
    "AtrExit": 72,
    "VsPrev": 40,
    "Ratio": 52,
    "Status": 80,
    "VolPower": 44,
    "BidAsk": 72,
    "Volume": 56,
}


def selection_telegram_chunks(
    tag_stripped: str, total: int, lines: List[str], extra_note: str = ""
) -> List[str]:
    """종목 선정 알림 본문을 텔레그램 길이 제한에 맞게 분할(일반 텍스트)."""
    header = (
        f"🎯 종목 선정 완료 ({tag_stripped})\n"
        f"총 {total}개 감시 등록\n"
    )
    if extra_note.strip():
        header += extra_note.strip() + "\n"
    header += "\n"
    chunks: List[str] = []
    buf = header
    for line in lines:
        add = line + "\n"
        if len(buf) + len(add) > TELEGRAM_MAX_CHARS:
            chunks.append(buf)
            buf = f"(목록 계속)\n{add}"
        else:
            buf += add
    if buf.strip():
        chunks.append(buf)
    return chunks


def monitor_fmt_vs_prev(info: Dict[str, Any]) -> str:
    """전일 종가 대비 등락폭(원)."""
    return format_monitor_vs_prev_cell(info)


def monitor_fmt_bid_ask(info: Dict[str, Any]) -> str:
    """매도잔량 / 매수잔량 및 매도÷매수 비율."""
    try:
        return format_bid_ask_monitor_display(
            info.get("total_ask"), info.get("total_bid")
        )
    except Exception:
        return "— / — (—)"


def monitor_ratio_direction(ratio_val: object) -> str:
    """등락률 표시값 → up / down / stable (줄무늬 행 태그용). ▲/▼ 접두와 +/- 숫자 모두 허용."""
    try:
        raw = str(ratio_val).strip()
        if not raw:
            return "stable"
        if raw.startswith(("▲", "△")):
            return "up"
        if raw.startswith(("▼", "▽")):
            return "down"
        s = raw.replace("%", "").replace(",", "").strip()
        s = s.lstrip("+")
        try:
            v = float(s)
        except ValueError:
            if "▼" in raw or "▽" in raw:
                return "down"
            if "▲" in raw or "△" in raw:
                return "up"
            return "stable"
        if v > 0:
            return "up"
        if v < 0:
            return "down"
    except (TypeError, ValueError):
        pass
    return "stable"


def ws_fid_raw(vals: Any, fid: int) -> Tuple[bool, Any]:
    """0B payload에서 fid 키(int/str) 존재 여부와 원본값."""
    try:
        if not isinstance(vals, dict):
            return False, None
        if fid in vals:
            return True, vals.get(fid)
        sk = str(fid)
        if sk in vals:
            return True, vals.get(sk)
    except Exception:
        pass
    return False, None


def normalize_ws_num_token(v: object) -> Optional[str]:
    """
    websocket fid 토큰을 파싱 가능한 문자열로 정규화한다.

    - '▲'/'▼' → +/−
    - 미수신·플레이스홀더(빈문자/'—'/None 등)는 None
      → 호출측에서 직전 유효값을 유지해 보합색 깜빡임을 막는다.
    - 브로커가 보낸 실제 보합 '0'은 '0'으로 반환한다.
    """
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s in ("-", "—", "–", "None", "null", "nan", "NaN"):
            return None
        if "▲" in s or "△" in s:
            s = s.replace("▲", "+").replace("△", "+")
        if "▼" in s or "▽" in s:
            s = s.replace("▼", "-").replace("▽", "-")
        s = s.replace(",", "").strip()
        if not s:
            return None
        return s
    except Exception:
        return None


def apply_ws_prev_close_fields(info: Dict[str, Any], vals: Any) -> None:
    """
    fid 11(diff)·12(ratio)가 키로 존재하고 유효할 때만 info를 갱신한다.

    키가 없거나 플레이스홀더면 직전 값을 덮어쓰지 않는다.
    (가격만 있는 부분 패킷이 전일대비를 '0'으로 지워 행 색이 보합으로 깜빡이던 문제 방지)
    """
    try:
        if not isinstance(info, dict):
            return
        has_d, raw_d = ws_fid_raw(vals, 11)
        if has_d:
            nd = normalize_ws_num_token(raw_d)
            if nd is not None:
                info["diff"] = nd
        has_r, raw_r = ws_fid_raw(vals, 12)
        if has_r:
            nr = normalize_ws_num_token(raw_r)
            if nr is not None:
                info["ratio"] = nr
    except Exception:
        pass


def stripe_direction_from_tags(tags: Any) -> str:
    """기존 행 태그에서 등락 방향(up/down/stable)만 추출."""
    try:
        for t in tags or ():
            s = str(t)
            if "up" in s:
                return "up"
            if "down" in s:
                return "down"
    except Exception:
        pass
    return "stable"


_ROW_DIRECTIONS: Tuple[str, ...] = ("up", "down", "stable")


def row_direction_from_tags(tags: Any) -> Optional[str]:
    """
    행 태그(``up_even``·``active_down_odd`` 등)에서 등락 방향만 정확히 추출한다.

    방향 태그가 없으면 None. (``warmup_ok_row`` 처럼 'up' 부분문자열을 포함한 태그를 오인하지 않음)
    """
    try:
        for t in tags or ():
            parts = str(t).split("_")
            if parts and parts[0] == "active":
                parts = parts[1:]
            if (
                len(parts) == 2
                and parts[0] in _ROW_DIRECTIONS
                and parts[1] in ("even", "odd")
            ):
                return parts[0]
    except Exception:
        pass
    return None


def _parse_signed_number(v: object) -> Optional[float]:
    """전일대비·등락률 토큰 → 부호 있는 float. 미수신·파싱 불가면 None."""
    try:
        s = normalize_ws_num_token(v)
        if s is None:
            return None
        s = s.replace("%", "").strip()
        if not s:
            return None
        x = float(s)
        if x != x:  # NaN
            return None
        return x
    except (TypeError, ValueError):
        return None


def monitor_direction_from_info(info: Any) -> Optional[str]:
    """
    monitor_info 의 전일대비(diff, fid 11)·등락률(ratio, fid 12)로 행 방향을 판정한다.

    - diff 부호 우선, diff 가 0/없음이면 ratio 부호
    - 수신값이 모두 0 이면 실제 보합 → ``"stable"``
    - 둘 다 없거나 파싱 불가면 None (호출측이 직전 색을 유지해야 함)
    """
    try:
        if not isinstance(info, dict):
            return None
        d = _parse_signed_number(info.get("diff"))
        r = _parse_signed_number(info.get("ratio"))
        for v in (d, r):
            if v is None:
                continue
            if v > 0:
                return "up"
            if v < 0:
                return "down"
        if d is not None or r is not None:
            return "stable"
    except Exception:
        pass
    return None


def resolve_monitor_row_direction(
    info: Any,
    pending_dir: Optional[str],
    current_dir: Optional[str],
) -> str:
    """
    감시 행의 최종 등락 방향.

    우선순위: 렌더 시점 최신 전일대비 → 큐 적재 시 계산된 방향(up/down) → 기존 행 색.
    판단 근거가 없을 때 보합(흰색)으로 떨어뜨리지 않기 위한 규칙이다.
    """
    d = monitor_direction_from_info(info)
    if d is not None:
        return d
    if pending_dir in ("up", "down"):
        return str(pending_dir)
    if current_dir in _ROW_DIRECTIONS:
        return str(current_dir)
    return "stable"


def parse_monitor_sash_rel(raw: Dict[str, Any]) -> Optional[float]:
    """저장된 상·하단 분할 비율(첫 번째 sash 위치 / Paned 높이)."""
    try:
        if not isinstance(raw, dict):
            return None
        v = raw.get("sash0_rel")
        if v is None:
            return None
        x = float(v)
        if x != x:  # NaN
            return None
        return max(0.06, min(0.90, x))
    except (TypeError, ValueError):
        return None


def format_selection_result_log_lines(df: Any) -> List[str]:
    """선정 결과 DataFrame → 로그 출력용 문자열 목록."""
    d = df.copy()
    d.columns = [str(c).lower() for c in d.columns]
    lines: List[str] = [
        "\n" + "=" * 102,
        f"🎯 [종목 선정 결과] {len(d)}개 → 실시간 감시 대상으로 바로 등록합니다.",
        "-" * 102,
        (
            f"{'#':>3} | {'코드':8} | {'종목명':18} | {'가점':>6} | {'트랙':^5} | "
            f"{'현재가':>10} | {'등락%':>7} | {'RSI':>5} | {'5일대금(억)':>10}"
        ),
        "-" * 102,
    ]
    for i, (_, row) in enumerate(d.iterrows(), 1):
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name", "Unknown"))[:18]
        try:
            sc = float(row.get("score", 0))
        except (TypeError, ValueError):
            sc = 0.0
        st = str(row.get("seltrack", "")).strip().upper() or "-"
        try:
            price = int(float(row.get("price", 0)))
        except (TypeError, ValueError):
            price = 0
        try:
            ratio = float(row.get("ratio", 0))
        except (TypeError, ValueError):
            ratio = 0.0
        try:
            rsi = float(row.get("rsi", 0))
        except (TypeError, ValueError):
            rsi = 0.0
        try:
            tv = int(float(row.get("avgtrdeval", 0)))
        except (TypeError, ValueError):
            tv = 0
        lines.append(
            f"{i:3d} | {code} | {name:18} | {sc:6.1f} | {st:^5} | "
            f"{price:10,d} | {ratio:7.2f} | {rsi:5.1f} | {tv:10d}"
        )
    lines.append("=" * 102 + "\n")
    return lines


# 구 monitor_tab private 이름 호환
_selection_telegram_chunks = selection_telegram_chunks
_monitor_fmt_vs_prev = monitor_fmt_vs_prev
_monitor_fmt_bid_ask = monitor_fmt_bid_ask
_monitor_ratio_direction = monitor_ratio_direction
_stripe_direction_from_tags = stripe_direction_from_tags
_parse_monitor_sash_rel = parse_monitor_sash_rel
_format_selection_result_log_lines = format_selection_result_log_lines
_DEFAULT_MONITOR_COL_WIDTHS = DEFAULT_MONITOR_COL_WIDTHS
_MONITOR_COL_MIN_WIDTH = MONITOR_COL_MIN_WIDTH

__all__ = [
    "TELEGRAM_MAX_CHARS",
    "DEFAULT_MONITOR_COL_WIDTHS",
    "MONITOR_COL_MIN_WIDTH",
    "selection_telegram_chunks",
    "monitor_fmt_vs_prev",
    "monitor_fmt_bid_ask",
    "monitor_ratio_direction",
    "ws_fid_raw",
    "normalize_ws_num_token",
    "apply_ws_prev_close_fields",
    "stripe_direction_from_tags",
    "row_direction_from_tags",
    "monitor_direction_from_info",
    "resolve_monitor_row_direction",
    "parse_monitor_sash_rel",
    "format_selection_result_log_lines",
]
