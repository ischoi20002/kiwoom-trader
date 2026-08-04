# -*- coding: utf-8 -*-
"""
GUI 없이 웹소켓(0B) 틱 → 1분 누적 후 공식 ka10080(1분)과 비교.

- 틱: **0B fid20 체결시각** 기준 시작 라벨 (09:14 = 09:14:00~09:14:59), KRX 거래시간만 누적
- 공식: ``cntr_tm`` 을 틱 open 라벨과 **동일 시각**으로 정합(실측)
- 비교는 WS 끊김·중간시작으로 깨진 분을 제외한 **온전한 1분**만 사용

사용 예:
  python tools/headless_tick_1m_vs_official.py --minutes 8 --codes 005930,000660,035420
  python tools/headless_tick_1m_vs_official.py --minutes 5 --live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _log(msg: str) -> None:
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def _fid_num(vals: Dict[Any, Any], fid: int, default: float = 0.0) -> float:
    v = vals.get(str(fid))
    if v is None or v == "":
        v = vals.get(fid)
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("-", "").strip())
    except Exception:
        return default


def _parse_0b(
    item_data: Dict[str, Any],
) -> Optional[Tuple[str, float, float, Optional[datetime]]]:
    """Returns (code, price, cum_volume, trade_time) or None."""
    try:
        from core.rt_tick_m1_accumulator import parse_0b_trade_datetime
        from core.tick_price_sanity import parse_ws_current_price
        from utils.normalization import canonical_stock_code
    except Exception:
        return None

    try:
        raw_pt = item_data.get("type", "0B")
        p_type = str(raw_pt if raw_pt is not None else "0B").strip().lower()
    except Exception:
        p_type = "0b"
    if p_type != "0b":
        return None

    raw_code = item_data.get(
        "item", item_data.get("stk_cd", item_data.get("item_code", ""))
    )
    code = canonical_stock_code(str(raw_code).strip().split("_")[0])
    if not code:
        return None

    vals = item_data.get("values", {}) or {}
    raw_fid10 = vals.get("10")
    if raw_fid10 is None:
        raw_fid10 = vals.get(10)
    px = parse_ws_current_price(raw_fid10)
    if px is None or int(px) <= 0:
        return None
    cum_v = float(_fid_num(vals, 13, 0.0))
    trade_dt = parse_0b_trade_datetime(vals, ref=datetime.now())
    return code, float(px), max(0.0, cum_v), trade_dt


def _cov_key(code: str, open_lab: pd.Timestamp) -> Tuple[str, str]:
    return (
        str(code).zfill(6),
        pd.Timestamp(open_lab).floor("min").strftime("%Y-%m-%d %H:%M:%S"),
    )


def _note_tick_coverage(
    coverage: Dict[Tuple[str, str], Dict[str, Any]],
    code: str,
    wall: datetime,
    *,
    gap_break_sec: float = 8.0,
) -> None:
    open_lab = pd.Timestamp(wall).floor("min")
    k = _cov_key(code, open_lab)
    sec = float(wall.second) + wall.microsecond / 1e6
    st = coverage.get(k)
    if st is None:
        coverage[k] = {
            "first_sec": sec,
            "last_sec": sec,
            "n": 1,
            "broken": False,
            "last_wall": wall,
            "max_gap_sec": 0.0,
        }
        return
    try:
        prev = st.get("last_wall")
        if prev is not None:
            gap = (wall - prev).total_seconds()
            if gap > float(st.get("max_gap_sec", 0) or 0):
                st["max_gap_sec"] = float(gap)
            # 분 안 긴 공백(재연결·수신 공백)만 불완전 — 짧은 Bye 재접속(~2s)은 허용
            if gap > float(gap_break_sec):
                st["broken"] = True
    except Exception:
        pass
    st["first_sec"] = min(float(st["first_sec"]), sec)
    st["last_sec"] = max(float(st["last_sec"]), sec)
    st["n"] = int(st["n"]) + 1
    st["last_wall"] = wall


def _mark_minute_broken(
    coverage: Dict[Tuple[str, str], Dict[str, Any]],
    codes: List[str],
    wall: datetime,
) -> None:
    """사용 안 함(호환). 공백은 ``_note_tick_coverage`` 의 gap 으로 판정."""
    return


def _is_complete_minute(
    st: Dict[str, Any],
    *,
    head_sec: float = 3.0,
    tail_sec: float = 55.0,
    min_ticks: int = 5,
) -> bool:
    if not st or bool(st.get("broken")):
        return False
    try:
        if int(st.get("n", 0)) < int(min_ticks):
            return False
        if float(st.get("first_sec", 99)) > float(head_sec):
            return False
        if float(st.get("last_sec", -1)) < float(tail_sec):
            return False
    except Exception:
        return False
    return True


async def _run_ws(
    *,
    token: str,
    codes: List[str],
    acc: Any,
    stop_at: datetime,
    use_mock: bool,
    coverage: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, int]:
    import websockets

    # GUI(monitor_tab)와 동일: 앱 레벨 PING 미응답 시 모의 서버가 ~40–50초 후 Bye 종료
    stats = {
        "ticks": 0,
        "real": 0,
        "login_ok": 0,
        "reconnects": 0,
        "pings": 0,
    }
    if use_mock:
        ws_url = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    else:
        ws_url = "wss://api.kiwoom.com:10000/api/dostk/websocket"

    while datetime.now() < stop_at:
        try:
            _log(f"WS connect {ws_url} codes={codes}")
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=20, close_timeout=5
            ) as ws:
                await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
                items = [c.zfill(6) for c in codes]
                reg = {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "market": "AL",
                    "data": [{"item": items, "type": ["0B"]}],
                }
                await ws.send(json.dumps(reg))
                _log(f"LOGIN+REG 전송 n={len(items)}")

                while datetime.now() < stop_at:
                    timeout = max(0.2, (stop_at - datetime.now()).total_seconds())
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=min(timeout, 2.0)
                        )
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        _log(f"recv 예외: {e!r}")
                        _mark_minute_broken(coverage, codes, datetime.now())
                        break
                    try:
                        packet = json.loads(raw)
                    except Exception:
                        continue
                    trnm = str(packet.get("trnm") or "")
                    if trnm == "LOGIN":
                        rc = packet.get("return_code")
                        _log(f"LOGIN 응답 return_code={rc}")
                        if rc == 0 or rc == "0":
                            stats["login_ok"] += 1
                        continue
                    # GUI와 동일: 수신 PING 패킷을 그대로 에코(세션 keepalive)
                    if trnm == "PING":
                        try:
                            await ws.send(json.dumps(packet))
                            stats["pings"] += 1
                            if stats["pings"] == 1 or stats["pings"] % 5 == 0:
                                _log(f"PING 에코 n={stats['pings']}")
                        except Exception as e:
                            _log(f"PING 에코 실패: {e!r}")
                            _mark_minute_broken(coverage, codes, datetime.now())
                            break
                        continue
                    if trnm != "REAL":
                        continue
                    stats["real"] += 1
                    payload = packet.get("data", [])
                    if not isinstance(payload, list):
                        payload = [payload]
                    wall = datetime.now()
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        parsed = _parse_0b(item)
                        if parsed is None:
                            continue
                        code, px, cum_v, trade_dt = parsed
                        bar_clock = trade_dt if isinstance(trade_dt, datetime) else wall
                        try:
                            acc.offer_tick(code, px, cum_v, now=bar_clock)
                            _note_tick_coverage(coverage, code, bar_clock)
                            stats["ticks"] += 1
                        except Exception:
                            pass
                    if stats["ticks"] and stats["ticks"] % 400 == 0:
                        _log(f"틱 누적 {stats['ticks']}건 …")
        except Exception as e:
            _log(f"WS 세션 예외: {e!r}")
            _mark_minute_broken(coverage, codes, datetime.now())
        if datetime.now() >= stop_at:
            break
        stats["reconnects"] += 1
        _log("WS 재연결 대기 1.5초…")
        try:
            await asyncio.sleep(1.5)
        except Exception:
            break

    return stats


def _fetch_official_1m(
    dm: Any, token: str, code: str, max_pages: int = 4
) -> pd.DataFrame:
    from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df

    merged: List[pd.DataFrame] = []
    nk = ""
    for _ in range(max(1, max_pages)):
        raw_list, nk_out = dm.fetch_kiwoom_minute_chart_page(
            code,
            token,
            nk,
            tic_scope="1",
            fetch_context="headless_tick_cmp",
        )
        if not raw_list:
            break
        chunk = ka10080_raw_rows_to_1m_df(raw_list)
        if chunk is not None and not getattr(chunk, "empty", True):
            c2 = chunk.rename(
                columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            merged.append(c2[["open", "high", "low", "close", "volume"]])
        nk = (nk_out or "").strip()
        if not nk:
            break
        time.sleep(0.12)
    if not merged:
        return pd.DataFrame()
    out = pd.concat(merged).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _row_at_open(off: pd.DataFrame, open_lab: pd.Timestamp) -> Optional[pd.Series]:
    """공식 1분: ``cntr_tm`` ≈ 틱 시작 라벨(동일 시각)."""
    if off is None or off.empty:
        return None
    want = pd.Timestamp(open_lab).floor("min").floor("s")
    if want in off.index:
        return off.loc[want]
    for ix in off.index:
        try:
            if pd.Timestamp(ix).floor("s") == want:
                return off.loc[ix]
        except Exception:
            continue
    return None


def _compare_session(
    codes: List[str],
    session_start: datetime,
    official: Dict[str, pd.DataFrame],
    db_path: Path,
    coverage: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    head_sec: float,
    tail_sec: float,
    min_ticks: int,
) -> None:
    import sqlite3

    from utils.krx_bars import is_krx_m1_closed_open_label

    if not db_path.is_file():
        _log(f"틱 DB 없음: {db_path}")
        return
    con = sqlite3.connect(str(db_path))
    try:
        ph = ",".join("?" * len(codes))
        df = pd.read_sql_query(
            f"SELECT code, datetime, open, high, low, close, volume "
            f"FROM bars_1m_tick_rt WHERE code IN ({ph}) AND datetime >= ? "
            f"ORDER BY code, datetime",
            con,
            params=[c.zfill(6) for c in codes]
            + [session_start.strftime("%Y-%m-%d %H:%M:%S")],
        )
    finally:
        con.close()
    if df.empty:
        _log("세션 중 seal된 틱 1분봉이 없습니다.")
        return
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.loc[df["datetime"].map(lambda t: bool(is_krx_m1_closed_open_label(t)))].copy()
    cur_open = pd.Timestamp(datetime.now()).floor("min")
    df = df.loc[df["datetime"] < cur_open].copy()
    if df.empty:
        _log("비교 가능한 완성 틱 1분봉이 없습니다(아직 분 미확정).")
        return

    keep_rows: List[Any] = []
    skip_n = 0
    for _, r in df.iterrows():
        code = str(r["code"])
        open_lab = pd.Timestamp(r["datetime"]).floor("min")
        st = coverage.get(_cov_key(code, open_lab), {})
        if _is_complete_minute(
            st, head_sec=head_sec, tail_sec=tail_sec, min_ticks=min_ticks
        ):
            keep_rows.append(r)
        else:
            skip_n += 1
            _log(
                f"  skip {code} {open_lab.strftime('%H:%M')} "
                f"(broken={st.get('broken')} first={st.get('first_sec')} "
                f"last={st.get('last_sec')} n={st.get('n')} "
                f"max_gap={st.get('max_gap_sec')})"
            )
    if not keep_rows:
        _log(f"온전한 1분 없음 (스킵 {skip_n}). 수집 시간을 늘리세요.")
        return

    df2 = pd.DataFrame(keep_rows)
    n_eq = n_diff = n_miss = 0
    n_ohlc_soft = 0
    abs_d: Dict[str, List[float]] = {"o": [], "h": [], "l": [], "c": [], "v": []}
    _log(f"=== 온전한 1분 비교 {len(df2)}봉 (스킵 {skip_n}) ===")
    for _, r in df2.iterrows():
        code = str(r["code"])
        open_lab = pd.Timestamp(r["datetime"]).floor("min")
        orow = _row_at_open(official.get(code, pd.DataFrame()), open_lab)
        lab = open_lab.strftime("%H:%M")
        if orow is None:
            n_miss += 1
            _log(
                f"  {code} {lab} MISS  "
                f"tick={r['open']:.0f}/{r['high']:.0f}/{r['low']:.0f}/{r['close']:.0f}/{r['volume']:.0f}"
            )
            continue
        to, th, tl, tc, tv = (
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r["volume"]),
        )
        oo, oh, ol, oc, ov = (
            float(orow["open"]),
            float(orow["high"]),
            float(orow["low"]),
            float(orow["close"]),
            float(orow["volume"]),
        )
        d_o, d_h, d_l, d_c, d_v = to - oo, th - oh, tl - ol, tc - oc, tv - ov
        abs_d["o"].append(abs(d_o))
        abs_d["h"].append(abs(d_h))
        abs_d["l"].append(abs(d_l))
        abs_d["c"].append(abs(d_c))
        abs_d["v"].append(abs(d_v))
        eq = d_o == 0 and d_h == 0 and d_l == 0 and d_c == 0
        soft = d_o == 0 and d_h == 0 and d_l == 0 and abs(d_c) <= 500
        if eq:
            n_eq += 1
            vol_note = "" if d_v == 0 else f" (VΔ={d_v:.0f})"
            _log(f"  {code} {lab} OHLC_EQ{vol_note}")
        elif soft:
            n_ohlc_soft += 1
            _log(
                f"  {code} {lab} OHLC~OK "
                f"dC={d_c:.0f} dV={d_v:.0f}  "
                f"tick={to:.0f}/{th:.0f}/{tl:.0f}/{tc:.0f}/{tv:.0f}  "
                f"off={oo:.0f}/{oh:.0f}/{ol:.0f}/{oc:.0f}/{ov:.0f}"
            )
        else:
            n_diff += 1
            _log(
                f"  {code} {lab} DIFF "
                f"tick={to:.0f}/{th:.0f}/{tl:.0f}/{tc:.0f}/{tv:.0f}  "
                f"off={oo:.0f}/{oh:.0f}/{ol:.0f}/{oc:.0f}/{ov:.0f}  "
                f"d={d_o:.0f}/{d_h:.0f}/{d_l:.0f}/{d_c:.0f}/{d_v:.0f}"
            )
    _log(
        f"요약 OHLC_EQ={n_eq} OHLC~OK(종가≤1틱)={n_ohlc_soft} "
        f"DIFF={n_diff} MISS={n_miss} complete={len(df2)}"
    )
    if abs_d["o"]:
        for k, name in (
            ("o", "시가"),
            ("h", "고가"),
            ("l", "저가"),
            ("c", "종가"),
            ("v", "거래량"),
        ):
            s = pd.Series(abs_d[k])
            _log(f"  |Δ{name}| med={s.median():.1f} max={s.max():.1f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--codes",
        default="005930,000660,035420",
        help="구독 종목(쉼표)",
    )
    ap.add_argument(
        "--minutes",
        type=float,
        default=8.0,
        help="수집 시간(분). 온전한 분 확보를 위해 6~10 권장",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="실전 API/WS (기본=모의)",
    )
    ap.add_argument("--max-pages", type=int, default=4)
    ap.add_argument(
        "--head-sec",
        type=float,
        default=3.0,
        help="온전한 분: 첫 틱이 이 초 이내",
    )
    ap.add_argument(
        "--tail-sec",
        type=float,
        default=55.0,
        help="온전한 분: 마지막 틱이 이 초 이후",
    )
    ap.add_argument(
        "--min-ticks",
        type=int,
        default=5,
        help="온전한 분: 최소 틱 수",
    )
    args = ap.parse_args()

    codes = [c.strip().zfill(6) for c in str(args.codes).split(",") if c.strip()]
    if not codes:
        _log("종목 없음")
        return 1

    import config
    from core.auth_manager import AuthManager
    from core.rt_tick_m1_accumulator import RtTickM1Accumulator
    from data_manager_v2 import DataManagerV2
    from utils.server_mode_paths import realtime_tick_1m_db_path

    use_mock = not bool(args.live)
    try:
        config.USE_MOCK = use_mock
        config.URL = config.MOCK_URL if use_mock else config.REAL_URL
    except Exception:
        pass

    am = AuthManager(logger_callback=_log)
    token = am.get_auth_token()
    if not token:
        _log("토큰 실패")
        return 2

    settings = {
        "realtime_tick_m1_accumulate_save_only": True,
        # 분 경계 늦은 체결 보정·DB flush 여유 (fid20 기준이어도 수신 지연 가능)
        "realtime_tick_m1_seal_defer_sec": 4.0,
    }
    # 다음 분 경계부터만 비교(시작 중간 분 제외)
    session_start = datetime.now().replace(second=0, microsecond=0) + timedelta(
        minutes=1
    )
    coverage: Dict[Tuple[str, str], Dict[str, Any]] = {}
    acc = RtTickM1Accumulator(settings=settings, logger_callback=_log)
    db_path = Path(realtime_tick_1m_db_path())
    _log(f"틱 DB={db_path} mock={use_mock}")
    _log(
        f"온전한 분 조건: first≤{args.head_sec}s last≥{args.tail_sec}s "
        f"min_ticks≥{args.min_ticks}, 비교 시작분≥{session_start.strftime('%H:%M')}"
    )

    now0 = datetime.now()
    stop_at = now0 + timedelta(minutes=max(2.0, float(args.minutes)))
    min_stop = session_start + timedelta(minutes=2, seconds=15)
    if stop_at < min_stop:
        stop_at = min_stop
    _log(f"수집 종료 예정 {stop_at.strftime('%H:%M:%S')}")

    try:
        stats = asyncio.run(
            _run_ws(
                token=str(token),
                codes=codes,
                acc=acc,
                stop_at=stop_at,
                use_mock=use_mock,
                coverage=coverage,
            )
        )
    except KeyboardInterrupt:
        _log("사용자 중단")
        stats = {"ticks": 0, "real": 0, "login_ok": 0, "reconnects": 0, "pings": 0}
    except Exception as e:
        _log(f"WS 루프 실패: {e!r}")
        stats = {"ticks": 0, "real": 0, "login_ok": 0, "reconnects": 0, "pings": 0}

    _log(
        f"WS 통계 ticks={stats.get('ticks')} real={stats.get('real')} "
        f"pings={stats.get('pings')} reconnects={stats.get('reconnects')}"
    )
    try:
        sealed = acc.maybe_seal_on_clock(datetime.now())
        _log(f"최종 seal 시도 sealed={sealed}")
        # seal_defer + 늦은 체결 amend 반영 대기
        time.sleep(5.0)
    except Exception as e:
        _log(f"seal 예외: {e!r}")

    dm = DataManagerV2(str(ROOT / "trading_data.db"))
    official: Dict[str, pd.DataFrame] = {}
    for code in codes:
        _log(f"공식 1분 조회 {code}")
        try:
            official[code] = _fetch_official_1m(
                dm, str(token), code, max_pages=int(args.max_pages)
            )
            _log(f"  → {len(official[code])} rows")
        except Exception as e:
            _log(f"  → 실패 {e!r}")
            official[code] = pd.DataFrame()
        time.sleep(0.3)

    _compare_session(
        codes,
        session_start,
        official,
        db_path,
        coverage,
        head_sec=float(args.head_sec),
        tail_sec=float(args.tail_sec),
        min_ticks=int(args.min_ticks),
    )

    try:
        acc.shutdown()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
