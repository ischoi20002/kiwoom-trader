# -*- coding: utf-8 -*-
import threading
import time
from datetime import date, datetime, timedelta, time as dtime
from typing import Any, Callable, Optional


def resolve_eod_snapshot_date(
    now: datetime,
    *,
    is_trading_day_fn: Optional[Callable[[date], bool]],
    previous_trading_day_fn: Optional[Callable[[date], Optional[date]]],
    end_of_day_due_fn: Callable[[date], datetime],
) -> Optional[date]:
    """
    EOD 스냅샷을 **지금** 저장해도 되는 영업일.

    장중(당일 종가+버퍼 전)에는 어제 파일을 현재 잔고로 덮지 않습니다.
    휴일에 과거 영업일을 오늘 잔고로 갱신하지도 않습니다.
    """
    try:
        today_d = now.date()
    except Exception:
        return None
    try:
        if callable(is_trading_day_fn) and bool(is_trading_day_fn(today_d)):
            try:
                due_today = end_of_day_due_fn(today_d)
            except Exception:
                return None
            if now < due_today:
                return None
            return today_d
        if not callable(previous_trading_day_fn):
            return None
        last = previous_trading_day_fn(today_d)
        if last is None:
            return None
        try:
            due_last = end_of_day_due_fn(last)
        except Exception:
            return None
        if now < due_last:
            return None
        return last
    except Exception:
        return None


class EodEquitySnapshotService:
    """
    EOD(일일) Equity Snapshot 스케줄러/저장 서비스.

    - Tk 메인 스레드 예약(after): `tick` 단에서 수행
    - 실제 저장 작업(계좌 동기화 + 파일 upsert): background thread에서 수행
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

    def start(self) -> None:
        """스냅샷 스케줄러를 시작(중복 예약 방지 포함)."""
        try:
            # 이미 예약이 있으면 중복 예약 방지
            if getattr(self.app, "_equity_snapshot_after_id", None) is not None:
                try:
                    root = getattr(self.app, "root", None)
                    if root is not None:
                        root.after_cancel(self.app._equity_snapshot_after_id)
                except Exception:
                    pass
                self.app._equity_snapshot_after_id = None
        except Exception:
            pass

        self.app._equity_snapshot_cancelled = False
        self._schedule_tick()

    def _schedule_tick(self) -> None:
        """30초마다 스냅샷 저장 여부를 체크하는 after 예약."""
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                return
            self.app._equity_snapshot_after_id = root.after(
                30000, self.tick
            )
        except Exception:
            self.app._equity_snapshot_after_id = None

    def tick(self) -> None:
        """
        30초마다 체크:
        - 오늘이 거래일이면 snap_date=today
        - 휴장일이면 snap_date=previous_trading_day
        - 현재 시각이 15:30 + buffer 이후면 스냅샷 저장 job 스폰
        """
        try:
            if getattr(self.app, "_equity_snapshot_cancelled", False):
                return

            now = datetime.now()
            buffer_minutes = 10
            try:
                buffer_minutes = int(
                    float(
                        getattr(self.app, "settings", {}).get(
                            "daily_report_session_buffer_minutes", 10
                        )
                    )
                )
            except Exception:
                buffer_minutes = 10

            # 거래일 판정은 KRX 캘린더(폴백 포함) 사용
            try:
                from reporting.krx_trading_calendar import (
                    is_trading_day,
                    previous_trading_day,
                )
            except Exception:
                is_trading_day = None
                previous_trading_day = None

            today_d = now.date()

            def _end_of_day_due(day_d: date) -> datetime:
                try:
                    from utils.krx_session_hours import get_session_hours

                    return get_session_hours(day_d).close_datetime() + timedelta(
                        minutes=max(0, int(buffer_minutes))
                    )
                except Exception:
                    return datetime.combine(
                        day_d, dtime(15, 30, 0)
                    ) + timedelta(minutes=max(0, int(buffer_minutes)))

            snap_date = resolve_eod_snapshot_date(
                now,
                is_trading_day_fn=is_trading_day if callable(is_trading_day) else None,
                previous_trading_day_fn=(
                    previous_trading_day if callable(previous_trading_day) else None
                ),
                end_of_day_due_fn=_end_of_day_due,
            )
            if snap_date is None:
                return
            # 과거 영업일 파일이 이미 있으면 장중 현재값으로 덮지 않음
            if snap_date < today_d:
                try:
                    from utils.equity_snapshots_store import read_equity_snapshot

                    existing = read_equity_snapshot(snap_date)
                except Exception:
                    existing = None
                if existing is not None:
                    try:
                        self.app._equity_snapshot_last_saved_date = str(snap_date)[:10]
                    except Exception:
                        pass
                    return
                try:
                    self.log(
                        f"ℹ️ [EOD Snapshot] {snap_date} 파일 없음 — "
                        "장중 현재 잔고로 과거일을 채우지 않음"
                    )
                except Exception:
                    pass
                try:
                    self.app._equity_snapshot_last_saved_date = str(snap_date)[:10]
                except Exception:
                    pass
                return

            snap_date_str = str(snap_date)[:10]

            # 같은 날짜는 성공 저장된 것만 1회 스킵
            if getattr(self.app, "_equity_snapshot_last_saved_date", None) == snap_date_str:
                return
            if getattr(self.app, "_equity_snapshot_pending_date", None) == snap_date_str:
                return
            if getattr(self.app, "_equity_snapshot_job_in_progress", False):
                return

            self.app._equity_snapshot_pending_date = snap_date_str
            self.app._equity_snapshot_job_in_progress = True

            threading.Thread(
                target=self._job_save_eod_equity_snapshot,
                args=(snap_date_str,),
                daemon=True,
            ).start()
        except Exception:
            # 체크 실패는 다음 tick에서 재시도
            pass
        finally:
            self._schedule_tick()

    def _job_save_eod_equity_snapshot(self, snap_date_str: str) -> None:
        """
        백그라운드 작업:
        - (가능하면) 계좌 동기화 1회 force(refresh_cash_deposit=True)
        - 포트폴리오 total_assets(=d2_deposit + holdings_stocks_mv_api) 스냅샷 저장
        """
        saved_ok = False
        try:
            if getattr(self.app, "_equity_snapshot_cancelled", False):
                return

            # 스냅샷 직전 계좌 동기화(최소 1회)로 D+2/평가를 맞춥니다.
            try:
                # 기동 직후 이미 계좌 동기화를 1회 수행했는데,
                # 서비스가 시작된 뒤 첫 tick(약 30초)이 due로 판정되면
                # 스냅샷 저장 직전에 sync를 한번 더 호출하게 됩니다.
                # 이중 호출을 줄이기 위해 최근 sync가 충분히 가까우면 생략합니다.
                last_sync_ts = float(
                    getattr(getattr(self.app, "portfolio", None), "last_sync_time", 0.0)
                    or 0.0
                )
                now_ts = time.time()
                if last_sync_ts <= 0 or (now_ts - last_sync_ts) >= 60.0:
                    self.app.portfolio.sync_with_actual_account(
                        silent=True,
                        force=True,
                        refresh_cash_deposit=True,
                    )
                else:
                    # 최근 동기화 직후라서 중복 REST 호출/로그를 피합니다.
                    pass
            except Exception as sync_ex:
                try:
                    self.log(f"⚠️ [EOD Snapshot] 동기화 실패: {sync_ex}")
                except Exception:
                    pass

            d2 = 0.0
            stock_eval = 0.0
            d1 = 0.0
            total_equity = 0.0
            with self.app.portfolio.lock:
                d2 = float(getattr(self.app.portfolio, "d2_deposit", 0) or 0)
                d1 = float(getattr(self.app.portfolio, "d1_deposit", 0) or 0)
                stock_eval = float(
                    getattr(self.app.portfolio, "holdings_stocks_mv_api", 0.0) or 0.0
                )
                total_equity = float(
                    getattr(self.app.portfolio, "total_equity", d2 + stock_eval) or 0.0
                )

            total_assets = d2 + stock_eval

            record = {
                "date": snap_date_str,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "d2_deposit": d2,
                "d1_deposit": d1,
                "stock_eval": stock_eval,
                "total_assets": total_assets,
                "total_equity": total_equity,
            }

            from utils.equity_snapshots_store import upsert_equity_snapshot

            path = upsert_equity_snapshot(snap_date_str, record)
            if path:
                saved_ok = True
        except Exception as e:
            try:
                self.log(f"❌ [EOD Snapshot] 저장 실패({snap_date_str}): {e}")
            except Exception:
                pass
        finally:
            try:
                self.app._equity_snapshot_job_in_progress = False
                self.app._equity_snapshot_pending_date = None
                if saved_ok:
                    self.app._equity_snapshot_last_saved_date = snap_date_str
            except Exception:
                pass

