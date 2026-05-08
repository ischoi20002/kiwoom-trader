# 변경 이력

`core/version.py`의 `APP_VERSION`과 맞춥니다.  
버전 숫자를 올릴 때 **같은 작업**에서 여기에 해당 버전 섹션을 추가합니다.

**날짜**: 괄호 안은 **그 변경이 저장소에 반영된 날**(버전을 올린 커밋의 날짜, 또는 실제 배포일)을 적습니다. 다른 항목과 줄을 맞추기 위해 임의로 바꾸거나, 대화 세션의 ‘오늘’만 보고 적지 않습니다.

---
정렬 규칙
----------
버전 섹션은 **항상 내림차순(최신 → 이전)** 으로 정렬하며, **최신 버전 블록이 최상단**에 오도록 유지한다.
즉, 새 버전이 추가되면 파일 상단 기준으로 **재정렬(삽입)** 한다.

---

## 0.6.45 (2026-05-08)

- 키움 REST `dmst_stex_tp`: 모의투자는 `KRX`, 실전투자는 `SOR`로 분기 (`OrderManager.dmst_stex_tp_for_orders`). 매수·매도·취소(kt10000/10001/10003) 및 체결내역 조회(kt00007)에 동일 적용 (`core/order_manager.py`).

## 0.6.44 (2026-05-08)

- 포지션관리 탭 주문·체결 목록: 미체결 후 취소 접수 성공 시 `order_history`를 `CANCELLED`로 갱신하고, 상태 열 표기를 `취소(미체결)`로 통일 (`core/order_manager.py`, `ui/tabs/virtual_tab.py`).

## 0.6.43 (2026-05-08)

- 실시간 감시에서 프리장/시간외 틱이 정규장 로직으로 섞이는 문제를 완화: 키움 WS 장운영구분(`0s`, fid `215`)을 별도 구독하고, 장코드가 `3`/`4`일 때만 `0B`/`0D` 틱을 감시·신호 경로로 통과하도록 필터링 (`ui/tabs/monitor_tab.py`).

## 0.6.42 (2026-05-08)

- 재시작 시 진행 중 15m 슬롯의 거래량·OHLC 부분 손실 보정 (5/8 09:15·09:30 슬롯 평균 V가 5/7 같은 시간대의 7%·1.7% 수준으로 떨어진 원인 해소).
  - `core/warmup_service.py`: 워밍업 검증 직전에 ka10080(`tic_scope='1'`)으로 1분봉 1페이지를 받아 _현재 진행 중 15m 슬롯_ `(slot_start, slot_end]` 구간의 1분봉을 합산해 OHLCV를 정확하게 채우는 `_maybe_recover_inprogress_slot_with_1m` 추가. 결과 봉 인덱스는 right-label(`slot_end`)로 기존 15m 규약과 일치. 1분봉 정규화 헬퍼 `_minute_api_raw_to_1m_ohlcv` 신설.
  - `core/stream_processor.py`: 워밍업이 V를 채운 진행 중 봉(`cum_v_start=0`)에 첫 0B가 들어올 때, `cum_v_start = max(0, volume - 현재V)` 로 잡아 다음 0B부터 V_new = (volume_new − volume) + V_warmup 가 되도록 수정 — 기존 fix는 첫 0B의 누계만 `cum_v_start` 로 잡아 두 번째 0B에서 워밍업 V가 _덮어쓰기_ 되어 사라지던 회귀를 제거. 일반 케이스(현재 V=0)에서는 종전 동작과 동일(`cum_v_start = volume`).
  - 신규 설정: `realtime_warmup_inprogress_slot_recover_with_1m`(기본 ON) — 1m 보강을 끄고 싶을 때 OFF.

## 0.6.41 (2026-05-08)

- 실시간 감시 「등락률」 열 표시를 ▲/▼ 형식으로 고정: 틱 정상 경로와 안전망·예외 폴백·강제 행 주입(`_inject_min_monitor_updates_from_monitor_info`)이 서로 다른 문자열(`info['ratio']`의 `+/−` 등)을 쓰며 깜빡이던 문제를 제거 (`core/signal_engine.py`의 `ratio_cell_display_for_monitor_tree`, `ui/tabs/monitor_tab.py`, `k_trader_gui.py`).

## 0.6.40 (2026-05-08)

- 실시간 `bars_data` 키 정규화·별칭 승격: 워밍업과 스트림 경로의 종목코드 문자열이 어긋날 때 빈 DF 스텁으로 덮어 봉수·신호가 초기화되던 문제 완화 (`utils/normalization.py`의 `canonical_stock_code`, 신규 `utils/bars_data_keys.py`, `core/stream_processor.py`, `core/signal_engine.py`).
- 종목코드 표기 정합: pandas·실수 문자열(`2380.0` 등)과 `monitor_info` 비표준 키가 있어도 틱·WS와 매칭되도록 보강 (`utils/normalization.py`, `core/signal_engine.py`, `core/trading_controller.py`, `ui/tabs/monitor_tab.py`).
- 실시간 신호 표시 지연 완화: 워밍업 직후 `monitor_info`에 S1 신호 캐시를 시드하고, 캐시만 있는 첫 틱에서는 무거운 풀 재계산을 건너뛰도록 조정 (`core/warmup_service.py`, `core/signal_engine.py`).

## 0.6.39 (2026-05-08)

- GUI 로그: 비메인 스레드에서 `self.log` 호출 시에도 메인 루프에 배출을 예약하도록 보강해, 기동 직후 REST 인증·계좌 동기화 등 백그라운드 메시지가 대시보드 로그 창에 누락되지 않도록 수정(`ui/gui_utils.py`).
- 매수/매도 발송 로그 정합: 지정가 주문의 실제 호가(매수 +1틱, 매도 −1틱)를 대시보드·세션 JSONL·텔레그램에 기준으로 쓰고, 참고 시세는 `ref_price`로 병기(`core/trading_controller.py`).

## 0.6.38 (2026-05-07)

- 실시간 15분봉 거래량 누락 정합성 강화 (BT 봉 대비 5~10% 수준만 잡히던 문제 해소): 0B(체결) 누적거래량(fid 13) 차분 누적이 슬롯 마지막 0B를 throttle/배치 타이밍으로 놓치는 문제를 정조준해 수정.
  - `core/signal_engine.py`: `_should_throttle_bar_update` 의 OHLCV 갱신 throttle 제거 — 매 0B마다 stream_processor 호출되도록 변경(봉 갱신은 dict 한 줄 갱신 수준으로 매우 가벼움).
  - `core/stream_processor.py`: 새 슬롯 진입 시점에 직전 봉 거래량을 _마지막 누적값(volume)_ 으로 한 번 더 보정(차분 정합성). 단 `cum_v_start>0` & 같은 날 봉일 때만 적용해 워밍업 봉을 큰 값으로 덮어쓰지 않도록 안전장치 추가. 같은 슬롯 내 워밍업 봉(`cum_v_start=0`)에 첫 0B가 들어올 때는 `cum_v_start` 만 갱신해 워밍업 V 보존.
  - `core/warmup_service.py`: `_apply_deep_history_to_bars_data` 가 `_finalize_bars_for_stream` 으로 `cum_v_start` 를 0으로 리셋하던 문제로, 장중 deep history backfill 시점에 진행 중 봉의 차분 누적 기준점이 0으로 풀리는 부작용 차단(같은 슬롯이면 직전 `cum_v_start` 보존).
- 실시간 패킷 정규화 보강(`ui/tabs/monitor_tab.py`): 0B fid 11(전일대비)·12(등락률) 토큰을 정규화하여 화살표(▲/▼)·대시(`-`/`—`)/None/콤마 등이 섞여도 엔진/UI 파싱이 일관되게 동작하도록 헬퍼 추가.
- 동적 백테 단일일(start_date==end_date) `ref_day` 보정(`dynamic_backtest.py`): 첫 거래일에 한해 `ref_day` 를 _직전 거래일_ 로 맞춰 RT 종목선정과 동일 universe 기준이 되도록 `prepare_dynamic_universe` 시작점을 확장.
- 봉 정합성 검증 진단 스크립트 추가:
  - `_diag_today_check.py`: 디버그 코드 의존 없이 그날 RT DB(`realtime_15m_mock.db.bars_15m_rt`) 적재 종목 _전체_ 를 자동으로 `trading_data.db.bars_15m` 과 비교(슬롯별 OHLCV 일치율·`v_ratio`·TOP-10 출력).
  - `_diag_042700_check.py`: 디버그 코드(0B periodic 로그)의 누적거래량 차분과 RT/TD 봉 거래량을 슬롯 단위로 대조해 stream_processor 차분 누적 정합성을 검증.

## 0.6.37 (2026-05-07)

- 실시간 감시 `신호상태` 고정 보정: 합치도(confluence) 플레이스홀더(`-`/`—`)일 때 워밍업 `✅ 15분봉·검증 완료` 문구가 `status_text`에 남지 않도록 `SignalEngine`에서 선제 `대기` 처리(`core/signal_engine.py`).

## 0.6.36 (2026-05-06)

- 부분체결 안전화: 매도 체결이 부분체결일 때 `PortfolioManager.on_fill(side="sell")`에서 보유 포지션을 제거하지 않고 잔여수량을 유지하도록 수정(SELL_PARTIAL 이벤트/qty 감소만 수행).

## 0.6.35 (2026-05-06)

- 체결 확정 전 포지션/보유 제거 방지: REST 주문 접수 직후 `reconcile_exchange_fill_after_order_accept()`가 즉시 `on_fill`을 호출하지 않도록 변경하고, `order_history(order_no)`에서 `FILLED/filled_qty>0` 확인이 될 때까지 폴링 후에만 `on_fill` 반영(`core/fill_reconciliation_service.py`).
- DB 조회/정합성: `data_manager_v2.get_order_history_row_by_order_no()`를 추가해 주문번호 단위 체결확정 확인을 지원(`data_manager_v2.py`).
- REST/WS 동시 경합 방지: REST 접수 후 체결확정 폴링 in-flight 중복 방지용 가드(`k_trader_gui.py`).

## 0.6.34 (2026-05-06)

- 주문 액션 비동기화 정리: 매수/매도 공통 주문 액션 워커로 통합하고 종목별 직렬화(inflight)·쿨다운 안전가드를 강화(`k_trader_gui.py`).
- UI 표시 정합: 전략유형(`StratType`) 실시간 깜빡임을 줄이기 위해 기본값으로 화면값 유지(설정 플래그 제공)하고, 강제 최소 갱신 경로에서도 보유 종목 `도달시 매도 기준가(AtrExit)`가 `-`로 덮이지 않도록 유지(`ui/tabs/monitor_tab.py`).
- 실시간 틱 처리 분리: 비메인 스레드 틱 수신 시 워커 사용을 우선해 메인스레드 점유와 간섭을 완화(`core/realtime_tick_dispatch_service.py`).

## 0.6.33 (2026-05-06)

- 실시간 감시 안정화: Tk 메인 스레드에서만 UI API(`after`/렌더) 호출하도록 경로를 정리하고, 틱 후속 액션 큐를 재스케줄링/워치독으로 보강해 전체 UI 멈춤(탭 전환 불가) 재현 가능성을 완화(`core/realtime_tick_dispatch_service.py`, `ui/gui_utils.py`, `k_trader_gui.py`).
- 실시간 갱신 성능: `pending_tree_updates` 렌더를 프레임 예산 기반으로 분할하고 강제 최소 갱신 경로를 라운드로빈/적체 임계치 기반으로 최적화해 화면 숫자 추종성과 응답성 균형을 개선(`core/signal_engine.py`, `ui/tabs/monitor_tab.py`, `k_trader_gui.py`).
- 실시간 감시 표시 정합: 행 태그(컬러코딩) 갱신을 설정 기반으로 복구하고, 강제 주입 경로에서도 전략유형 컬럼을 `sel_track` 기반으로 채워 `-` 표시 깜빡임을 완화(`ui/tabs/monitor_tab.py`, `k_trader_gui.py`).

## 0.6.32 (2026-05-06)

- 실시간 감시: `kodex200.csv`를 이름 표시 캐시로만 사용하도록 조정해, `watchlist_store.get_codes()`에 섞이던 문제로 인해 워밍업/WS 대상 종목 수가 과도하게 커지는 현상을 완화(`core/watchlist_store.py`, `k_trader_gui.py`).
- 실시간 감시 UI: `wl_only`(관심목록 전용) 계산에서 `virtual_positions` 종목을 제외해, 보유 및 대응 트리에서 보유 종목이 사라지는 우선순위 충돌을 추가 완화(`ui/tabs/monitor_tab.py`).

## 0.6.31 (2026-05-06)

- 실시간 감시: WS 수신 디버그 로그(과도 출력) 스팸을 줄이고, `LOGIN` 실패 원인(`return_msg/return_code`)을 `realtime_session_logs` JSONL로도 남겨 스크롤 없이 원인 추적 가능하도록 개선(`ui/tabs/monitor_tab.py`, `reporting/realtime_session_logger.py`).
- 실시간 감시/관심목록: `kodex200.csv`를 앱 시작 시 `watchlist_store`에 선적재하여, `KRX_ID/KRX_PW` 미설정 환경에서도 실시간 화면명 표시를 위해 `pykrx` 호출이 발생하는 문제를 완화(`k_trader_gui.py`, `core/watchlist_store.py`).

## 0.6.30 (2026-05-05)

- 실시간 감시/매매 체결 후 D+2 예수금 표시 안정화: 예수금상세(ka10001/kt00001) 파싱 시 `d1/d2` 선택 우선순위를 `*_samt`(현재 잔액/총액 성격)로 먼저 두고 `*_entra`(증분/추정 성격)를 후순위로 적용(`core/order_manager.py`).

## 0.6.29 (2026-05-04)

- 하베스팅 성능: 신규/증분 1분봉 적재 시 `bars_15m`/`bars_1d`를 전체 재빌드하지 않고 변경된 날짜 구간만 부분 리샘플/집계하도록 최적화(`harvest_service.py`, `utils/bar_resample.py`, `data_manager_v2.py`).

## 0.6.28 (2026-05-04)

- 실시간 감시: 보유 키 접미(`443060_1` 등)와 6자리 종목코드 정규화 정합 — 워밍업 병합·`monitor_info`·VP 가격 보정·스트림 구독 목록·가상보유 판별(`core/trading_controller.py`, `k_trader_gui.py`, `core/signal_engine.py`).
- Kiwoom WS `REG`: 종목 수가 많을 때 `data` 다중 대신 **청크별 `grp_no` 순차 전송**(`ui/tabs/monitor_tab.py`); 설정 `ws_reg_max_codes_per_chunk` 연동 유지(`core/defaults.py`, `settings.json`).
- 신호상태 열: 워밍업 문구를 `monitor_info.status_text`에 동기화, 틱 처리에서 합치도 플레이스홀더일 때 기존 문구 유지 및 표시값을 `status_text` 우선으로 통일(`ui/tabs/monitor_tab.py`, `core/signal_engine.py`).

## 0.6.27 (2026-05-03)

- 데이터 하베스팅에서 `sync_meta` 캐시/직전 마감 컷오프 보정으로 최신성 판정 비용 및 `SYNC` 중복을 줄이고, 1m→15m/1d 리샘플·일괄 집계는 실제 DB 범위 확장 시에만 수행하도록 최적화(`harvest_service.py`).

## 0.6.26 (2026-05-02)

- EOD 스냅샷 저장 직전의 계좌 동기화가 기동 직후 초기 동기화와 겹쳐 2회 실행되는 문제를 완화하기 위해 최근 동기화 시점 기준으로 중복 sync를 스킵(`core/eod_equity_snapshot_service.py`).

## 0.6.25 (2026-05-02)

- 텔레그램 매수/매도 체결 메시지를 `포지션관리탭 주문·체결 내역` 필드 순서로 재정의하고, 매수/매도는 `order_no` 기준 1회만 전송하도록 중앙화(`core/fill_reconciliation_service.py`, `data_manager_v2.py`).
- 실시간 감시 시작 시 계좌 요약 텔레그램 1회 전송, 주문 후 계좌 동기화 완료 직후 추가 1회 전송 로직 추가(`k_trader_gui.py`, `core/order_manager.py`).

## 0.6.24 (2026-05-02)

- 설정탭/종목선정에서 legacy(레거시) 분기(Track A/B 전환 토글 및 레거시 입력)를 제거하고, v2 기반 로직만 사용하도록 정리(`ui/tabs/settings_tab.py`, `strategy_selector.py`, `tools/settings_tab_ui_catalog.py`, `settings.json`).

## 0.6.23 (2026-05-02)

- 설정탭에서 동적백테 A/B 수 균형(신규 S1) 옵션을 제거하고, 관련 로직/최적화 타겟을 함께 삭제(`ui/tabs/settings_tab.py`, `dynamic_backtest.py`, `backtest_engine.py`, `core/signal_callbacks_service.py`, `parameter_optimizer.py`, `ui/tabs/backtest_tab.py`, `k_trader_gui.py`, `tools/settings_tab_ui_catalog.py`).

## 0.6.22 (2026-05-02)

- `k_trader_gui.py` 워커→메인 UI 브리지의 `warmup_stock_prep` 처리 로직을 `WarmupStockPrepUiService`로 서비스화하고, GUI 내 `_on_warmup_stock_prep()` 중복을 제거.

## 0.6.21 (2026-05-02)

- 운영용 포지션관리 탭 자산 추이 그래프 원천: `equity_snapshots_*` 스냅샷 디렉토리 저장/덮어쓰기 유틸(`utils/equity_snapshots_store.py`)과 운영/모의 분리 경로(`utils/server_mode_paths.py`) 추가.
- `k_trader_gui.py`에서 매일(15:30+buffer) EOD 총자산 스냅샷 자동 저장(동일 날짜 override) 및 복구 로직 추가.
- `ui/tabs/virtual_tab.py`에 1주일/1달/6개월/1년/커스텀 기간 선택 UI와 Plotly 자산 추이/비중(현금=D+2 포함) 차트 생성 연결.

## 0.6.20 (2026-05-01)

- 실시간 감시 ATR 기준가(`AtrExit`) 표시: `SL%` / `ATR SL` / `ATR TRL` / `FLASH` 후보 중 **도달시 매도 기준가로 환산된 최댓값 1개만** 라벨+금액으로 표시하도록 정리, 헤더도 `도달시 매도 기준가`로 변경 (`backtest_engine.py`, `ui/tabs/monitor_tab.py`).
- 실시간 감시 탭 Paned 세시(sash) 복원: 탭 전환 직후 높이 0 고정/오복원 판정 케이스를 보정하고, `보유 및 대응종목` 상단 영역은 약 5줄 높이로 유지하도록 클램프 강화 (`ui/tabs/monitor_tab.py`).

## 0.6.19 (2026-05-01)

- **실시간 감시 탭 상단 높이 유지(세션 내)**: 탭 전환 시 매번 강제 복원하던 동작을 막고, 최초 1회 복원 이후에는 사용자가 드래그한 보유/대응 종목 영역 높이를 유지하도록 조정 (`ui/tabs/monitor_tab.py`).

## 0.6.18 (2026-05-01)

- **금일 손익 기준자산 합성 안전화**: 직전 장마감가(`ka10001`)를 보유 종목 중 하나라도 조회하지 못하면 평단 대체를 금지하고 기준자산 합성을 중단(`None`)해 금일 손익을 `—`로 표시. 재시작/응답 편차 시 허위 손익(+/-) 노출을 차단 (`utils/eod_equity_snapshots.py`).

## 0.6.17 (2026-05-01)

- **일일결산·가상 종합리포트 15분봉**: `trading_data.db` 의 `bars_15m`·`bars_1m` 리샘플 우선, 없을 때만 키움 REST 폴백. 가상 리포트는 REST 일자 간 대기를 실제 REST 폴백 시에만 수행 (`data_manager_v2.py`, `reporting/daily_settlement_report.py`, `reporting/virtual_report.py`).
- **전략검증 탭**: 노트북 라벨 **전략검증**, 순서를 전략 설정 다음·맨 끝으로 조정. **종합리포트·일일결산**을 대시보드에서 이전하고 **시세 수집 · 리포트 생성** 구역에서 **최신주가데이타수집**과 나란히 `Action.TButton` 스타일로 배치 (`k_trader_gui.py`, `ui/tabs/backtest_tab.py`, `ui/tabs/dashboard_tab.py`, `ui/tabs/virtual_tab.py`).
- **대시보드 기동 UX**: 첫 REST 인증 대기 중 **인디터미네이트 프로그레스바**·단계 문구, 완료 후 짧은 뒤 영역 숨김 (`k_trader_gui.py`, `ui/tabs/dashboard_tab.py`).
- **동기화 로그 간소화**: 계좌 요약에서 종목별 `└ 보유:` 나열 제거, 기동 후 장문의 ka85 후속 처리 설명 로그 제거. 사용하지 않는 `log_boot_summary` 인자 제거 (`core/order_manager.py`, `core/portfolio_manager.py`, `k_trader_gui.py`).

## 0.6.16 (2026-05-01)

- **실시간 감시 탭 상단 높이 고정**: 보유/대응 종목 영역이 재시작 후 1줄로 축소되는 이슈를 피하기 위해 상단 패널을 **약 5종목 표시 높이**로 강제 복원하도록 변경 (`ui/tabs/monitor_tab.py`).
- **세시 복원 안전화**: rowheight 기반 목표 픽셀 계산 + 최소/최대 클램프로 창 크기 변화 시에도 상단 패널 붕괴를 방지 (`ui/tabs/monitor_tab.py`).

## 0.6.15 (2026-05-01)

- **실시간 감시 탭 상·하단 분할 기억**: 보유/대응 종목과 감시 목록 사이 세시(sash) 이동 시 `monitor_column_widths.json` 의 ``sash0_rel`` 에 저장되도록 **Paned `<Configure>` 로 세시 픽셀 변화 감지**, 복원 중 오탐 방지 플래그, Paned 직접 ``ButtonRelease`` 시 동기 추가 (`ui/tabs/monitor_tab.py`).
- **보유 트리 최소 높이**: 상단 Treeview 의 ``height=3`` 고정 제거 — 패널이 1~2줄만 보이던 현상 완화 (`ui/tabs/monitor_tab.py`).

## 0.6.14 (2026-05-01)

- **금일 손익 분모·분자 정합**: 직전 장마감 합성과 당일 비교 시 모두 **D+2 예수 + 보유 순매각 합** 만 사용. ``last_tot_evlu_amt`` ±2.5% 치환은 **표시용 총자산**에만 두고, 금일 손익에는 ``skip_broker_te_reconcile=True`` 로 치환을 끄어 시세 변동이 없을 때 허위 손익(수수료·정의 불일치 꼴) 방지 (`ui/tabs/virtual_tab.py`, `utils/eod_equity_snapshots.py`, `reporting/daily_settlement_report.py`).
- **직전 장마감 합성 보유 집계**: ``virtual_positions`` 를 포지션 탭과 같이 **6자리 종목코드 기준 첫 항목만** 합산(이중 키 방지) (`utils/eod_equity_snapshots.py`).

## 0.6.13 (2026-05-01)

- **금일 손익·기준 총자산 합성**: 직전 장마감가 기준 주식 평가를 **액면(가격×수량)** 이 아니라 포지션 탭과 동일한 **순매각 합**( ``invested`` + ``unrealized_at_price_net_of_sell_fee`` )으로 계산. 합성 총자산은 ``VirtualTab._compute_virtual_tab_total_assets`` 와 동일 규칙 적용 — 휴장일 등 시세 변동이 없을 때 수수료 정의 불일치로 인한 허위 손익 완화 (`utils/eod_equity_snapshots.py`).
- **휴장일 금일 손익 0 고정** 제거: KRX 비거래일에도 **합성 기준 대비 실제 차이**를 표시 (`utils/eod_equity_snapshots.py`, `reporting/daily_settlement_report.py`).

## 0.6.12 (2026-05-01)

- **금일 손익**: 로컬 EOD JSON **읽기/자동 기록을 폐기**. 기준 총자산은 **매번** ``fetch_stock_prev_close_ka10001`` × 보유 + D+2 예수 **합성**만 사용 (`utils/eod_equity_snapshots.py`, `ui/tabs/virtual_tab.py`, `reporting/daily_settlement_report.py`). ``maybe_record_eod_after_session``·일일 리포트 EOD ``write`` 는 무시/제거.
- **계좌 「미실현 손익」**(구 미실현(보유)): **순매각(매도 수수료 차감) − 평단×수량** 합계·손익률 분모 Σ(평단×수량) — 매입수수료는 매입 원가(cost)에서 제외. 상단 「누적 수익」보유 분도 동일 정의 (`utils/fee_rates.py`, `ui/tabs/virtual_tab.py`).

## 0.6.11 (2026-05-01)

- **포지션 탭·계좌 「주식 평가금」**: 전량 매도 가정 **순매각**(설정 매도 수수료율 차감) 합계 — `Σ invested + Σ 미실현(매도 차감)`으로 표시. 잔고 API `holdings_stocks_mv_api`/행 `evlu_amt`만으로 덮어쓰지 않음 (`ui/tabs/virtual_tab.py`).
- **보유 평가액 표시 규칙**: 잔고 스냅 경로에서 **현재가×수량**이 살 있으면 `ka_ev`(평가금액)보다 우선 — 필드 정의 혼선 시 액면 시가 표시 안정화 (`ui/tabs/virtual_tab.py`).
- **총자산 계산용 주식분**: 표시와 동일하게 로컬 순매각 합만 사용(잔고 평가합으로 대체하지 않음) (`ui/tabs/virtual_tab.py`).
- **ka10085 평가 합산**: 행별 **현재가×수량**을 `evlu_amt`보다 우선해 합산(내부 `stocks_market_value`), 매입수수료·필드 정의 차 최소화 (`core/order_manager.py`).

## 0.6.10 (2026-05-01)

- **보유 현재가**: 잔고 스냅(`use_ka`)일 때도 **`_monitor_live_price`(호가·stkinfo·스텁)** 를 **`ka_cur_prc`보다 우선** — 전일 단가만 온 스냅이 현재가 열을 덮지 않도록 (`ui/tabs/virtual_tab.py`).
- **참고가 시드**: `virtual_position_reference_price_for_monitor` 에서 **ka10004(호가) → stkinfo** 순 호출 (역순 시 잘못된 전일값이 `last_price`에 들어가던 문제) (`core/trading_controller.py`).
- **stkinfo**: 응답에서 **`output` 블록을 루트보다 먼저** 파싱, **호가 TR** 은 부호 있는 `cur_prc` 를 **절댓값**으로 인식 (`core/order_manager.py`).
- **스텁 재시드**: 인증 직후 **실시간 `price`가 없으면** `last_price`를 다시 채워 잘못 시드가 남지 않게 (`k_trader_gui.py`).
- **진단 출력**: `📌 [직전장마감]` 콘솔·GUI 로그·info 로깅 제거 (필요 시 로거 debug만) (`core/trading_controller.py`, `core/order_manager.py`).

## 0.6.9 (2026-05-01)

- **보유·스텁 현재가**: `ka_cur_prc`·`평가금/수량`이 **평단과 동일(≈0.5원 이내)** 이면 실시간 시세가 아닌 복제로 보고 무시, REST 전일·호가로 시드 (`core/trading_controller.py`).
- **포지션 탭**: `vp_price_stub` 도 `last_price` 가 평단과 같고 틱이 없으면 **현재가로 쓰지 않음** — 호가·전일 폴백 후 평단 (`ui/tabs/virtual_tab.py`). `last_price` vs 평단 비교를 **0.501원** 허용으로 통일.

## 0.6.8 (2026-05-01)

- **전일종가(ka10001)**: `stkinfo` 응답에서 **`output`/`output1` 우선**, 필드는 **`prdy_clpr` 등 전일 전용 → `pred_close_pric` 순**. 루트·타 블록 깊이 탐색이 잘못된 노드(기준가·구버전 전일)를 먼저 잡아 **하루 밀림**이 나던 문제를 완화 (`core/order_manager.py`). 광역 탐색에서 `base_pric`·`bss_cls_pric` 제외.

## 0.6.7 (2026-05-01)

- **전일 종가 REST**: 주식 기본정보 `ka10001`을 **`POST /api/dostk/stkinfo`** 로 호출하도록 수정. `mrkcond`(호가) URL 혼용 시 HTTP 200이어도 전일가 필드가 비던 문제를 제거 (`core/order_manager.py`).
- **보유 스텁 시세**: `vp_price_stub` 인 경우에도 `monitor_info['last_price']`에 시드한 참고가를 **현재가 표시에 반영** (`ui/tabs/virtual_tab.py`).
- **REST 폴백**: 호가(ka10004) 실패 시 **전일 종가( stkinfo )** 로 보조 (`ui/tabs/virtual_tab.py`).
- **기동 시 참고가**: 인증 성공 **약 0.4초 후** 토큰 준비 타이밍을 두고 보유 스텁 `last_price`를 재시드, `app`·`TradingController`의 `monitor_info` 동기화 (`k_trader_gui.py`).
- **참고가 우선순위**: 계좌 스냅 외에 REST **전일 종가 → 현재가** 순 폴백 (`core/trading_controller.py`).

## 0.6.6 (2026-05-01)

- **체결 후 동기화**: 1·2차 전체 동기화에 더해 **약 22초 뒤 3차** 예약 — 증권사 D+2·예수금상세 반영 지연 흡수 (`k_trader_gui.py`).
- **포지션 탭·수수료**: 반영 ON 시 매입은 `invested`(매수 수수료 포함), 평가·미실현은 매도 수수료 차감 순매각 기준으로 정합 (`utils/fee_rates.py`, `ui/tabs/virtual_tab.py`, `reporting/virtual_report.py`).
- **총자산**: D+2와 동일 출처인 **`holdings_stocks_mv_api`**(보유 액면 평가 합)를 주식 평가·총자산 합산에 우선 — 계좌 정합 (`ui/tabs/virtual_tab.py`).

## 0.6.5 (2026-04-30)

- **실시간 봉 정합성(0D 차단)**: 호가(0D) 이벤트가 15분봉(OHLCV) 업데이트 경로를 타지 않도록 분리. 체결(0B)만 봉 갱신을 수행하도록 `_orderbook_only` 플래그를 도입해 `SignalEngine`에서 봉 업데이트를 건너뛰도록 수정 (`ui/tabs/monitor_tab.py`, `core/signal_engine.py`).
- **진단 스크립트 추가**: 실시간 15분 DB와 백테스트 DB의 슬롯별 OHLCV·신호 차이를 비교하는 점검 스크립트 추가 (`_diag_compare_rt15m_vs_td.py`, `_diag_rt_bt_mismatch.py`).

## 0.6.4 (2026-04-28)

- **실시간 Flash·고점(`max_p`)**: `_peak_high_from_bars_df`가 **매수(·추매) 시각 이후에 시작된 15분봉**의 High만 반영. 진입 전 같은 봉의 고가가 Flash/표시에 섞이지 않도록 (`backtest_engine.py`).
- **S1 중복 매수 방지**: 동일 종목 재매수 최소 간격을 설정 `s1_buy_signal_cooldown_sec`로 분리, 기본 **1800초(30분)**. 관심목록 매수 알림 간격도 동일 (`core/signal_engine.py`, `core/defaults.py`, `settings.json`, `ui/tabs/settings_tab.py`, `tools/settings_tab_ui_catalog.py`).

## 0.6.3 (2026-04-28)

- **기본값**: `settings.json`(설정 탭 저장분)과 `tools/sync_defaults_from_settings.py`로 `core/defaults.py`의 `DEFAULT_SETTINGS` 동기화. 수수료·백테 기간·모의 계좌 등 `skip` 키는 **이전 defaults.py** 값을 유지하고, 없을 때만 `settings.json`으로 채움.
- **도구**: `sync_defaults_from_settings.py`가 `skip` 키를 빠뜨리던 문제 수정(`ast.literal_eval`로 기존 dict 보존).

## 0.6.2 (2026-04-27)

- **Track B 선정**: `sel_b_require_close_ge_ma5`(종가≥MA5)를 B v2 비활성·일괄 레거시·`dual_track_selection_metrics` 레거시 경로에도 동일 적용 (`strategy_selector.py`).
- **백테스트 탭**: 파라미터 최적화 시작 시 설정 탭 자동 `save_settings` 제거 — 동적 백테와 같이 **「설정 저장 및 즉시 적용」**으로 확정된 값만 엔진에 반영 (`ui/tabs/backtest_tab.py`).
- **데이터·하베스트·동적 백테**: 시장지수/VIX 바 정리·거래일 보강, 일봉 대비 1분 갱신 진단 등 (`data_manager_v2.py`, `harvest_service.py`, `dynamic_backtest.py`).
- **기타**: `settings.json`, `monitor_column_widths.json`, `virtual_db_mock.json` 반영.

## 0.6.1 (2026-04-27)

- **예수금·D+1/D+2 UI**: 계좌 동기화 후 `refresh_portfolio_light` 경로에서 상단 라벨이 0으로 남던 문제 — `_update_summary_after_tick`에서 `portfolio`의 예수·D+n을 반영 (`ui/tabs/virtual_tab.py`).
- **예수금상세 REST**: `ka10001`/`kt00001` 응답 본문을 `output`/`output1` 리스트 등에서 안정적으로 추출, 필드 후보 확장 (`core/order_manager.py`).
- **대시보드 동기화**: `refresh_cash_deposit=True`·`wait_for_lock=True` 명시 (`k_trader_gui.py`).
- **체결 후 계좌 동기화**: 단일 2.5초 지연 대신 디바운스 + 1차(약 4.2s)·2차(약 7s) 추격, `wait_for_lock`으로 락 스킵 완화 (`k_trader_gui.py`).
- **로그 스로틀**: D+2 한도 축소 안내·「1주도 살 수 없음」Skip 메시지를 동일 조건에서 3분에 1회로 제한 (`core/portfolio_manager.py`, `core/trading_controller.py`). `session_logger` 기록은 유지.
- **기타**: `monitor_column_widths.json` 반영.

## 0.6.0 (2026-04-27)

- **최적화(베이지안)**: Optuna import 실패 시 원인·설치 안내 로그, trial 종료 상태별 결과 행 수집, `n_jobs=1` 직렬화로 전역 캐시·스레드 경합 완화, 목적값 NaN/Inf 방지 (`parameter_optimizer.py`).
- **계좌 동기화**: 자동 **주기**(약 30초) 제거. 자동은 **기동 후 인증 성공 시 1회**만; 수동 동기화·체결 후 경로는 유지 (`k_trader_gui.py`, `ui/tabs/virtual_tab.py`).
- **전략·선정·설정·백테스트**: `strategy_selector.py`, `ui/tabs/settings_tab.py`, `core/defaults.py`, `strategy/signals.py`, `backtest_engine.py`, `tools/settings_tab_ui_catalog.py`, `settings.json`, `monitor_column_widths.json` 반영.
- **도구**: `tools/analyze_backtest_holdings.py` 추가.

## 0.5.0 (2026-04-26)

- **Track A 일봉 선정 v2**: 단기 상대강도(교차섹션 분위)·고가대비 되돌림·이평 근접·ATR%·갭 등 동일 스택으로 `prepare_dynamic_universe`·`select_monitoring_targets` 정합 (`strategy_selector.py`, `core/defaults.py`).
- **설정 탭**: Track A v2 키·레거시 구분, 감시 선정 프레임(최소 점수·개수 상한·순위 밴드·폴백 토글) (`ui/tabs/settings_tab.py`, `tools/settings_tab_ui_catalog.py`).
- **감시 선정 N 비의존**: `sel_track_min_score_a`/`b`로 점수 임계 후보, `max_slots_*`는 상한만. 임계 미달 시 **기본 감시 없음**(`sel_track_min_score_fallback_topn` 기본 OFF). 옵션 `max_slots_score_rel_pad`·`max_slots_band_extra_cap`는 임계 비활성 시 순위 경계 완화 (`strategy_selector.py`).
- **진단**: `tools/diagnose_selection_forward_returns.py` 스케줄 생성이 동일 `_df_top_by_score_slot_band` 규칙 사용.
- **기타**: 듀얼트랙 `Valid_B`용 OSC·MA60 보조 변수 보강 등 (`strategy_selector.py`).

## 0.4.5 (2026-04-26)

- **모니터 코드 집합 SSOT**: 감시 선정·관심목록 전용(wl_only) 판별을 `core/monitor_code_sets.py` 한곳에서 정규화·차집합 처리. `ui/tabs/monitor_tab.py`, `core/signal_engine.py`, `core/watchlist_store.codes_report_exclusion_set` 가 동일 규칙을 사용한다.

## 0.4.4 (2026-04-26)

- **실시간 감시·보유 트리**: 감시 선정 종목이 관심목록에도 있을 때 「관심목록 전용 숨김」 로직이 전체 관심목록 기준으로 동작해 보유·감시 트리에서 빠지거나 틱 갱신이 막히던 문제 수정. **관심목록만**(감시 미선정)에 한해 숨김·스킵하도록 `wl_only` 집합 사용, `signal_engine` 트리 큐잉도 동일 기준 (`ui/tabs/monitor_tab.py`, `core/signal_engine.py`).

## 0.4.3 (2026-04-26)

- **감시·보유 전략유형(AB 선정)**: 선정 `sel_track` 이 AB인 종목이 실제 S1 매수(TYPE_A/B)로 진입한 뒤에도 「전략 A+B」로 남던 문제 수정. `load_all` 시 `sig_type` 복원, `infer_sig_type_for_ui`·주문 이력(`get_latest_buy_reason_with_sig_type`) 보강, 매수 직후 `monitor_info` 의 `sel_track` 을 A/B 로 좁힘 (`core/portfolio_manager.py`, `data_manager_v2.py`, `ui/tabs/monitor_tab.py`, `core/signal_engine.py`, `core/trading_controller.py`).
- **기타**: `monitor_column_widths.json` 반영.

## 0.4.2 (2026-04-26)

- **포지션 탭 계좌 요약**: 실시간 감시 중 30초 주기는 **보유(ka10085) 정합만** 하고 예수금상세 TR는 생략. **예수금·D+1/D+2**는 매매 체결 후 전체 동기화(및 수동 동기화)에서만 REST 갱신. **주식평가금·총자산**은 보유 행 합계·틱 갱신과 일치하도록 정리 (`k_trader_gui.py`, `core/order_manager.py`, `core/portfolio_manager.py`, `ui/tabs/virtual_tab.py`).
- **kt00007 체결 일시**: `YYYYMMDDHHMMSS`(14자리) 및 행의 `cntr_dt`·`ord_dt` 등을 반영해 `order_history`의 표시 일시·`fill_row_key`가 어긋나던 문제 수정 (`core/order_manager.py`).
- **바·백테스트·설정**: `utils/bar_resample.py`, `backtest_engine.py`, `dynamic_backtest.py`, `core/defaults.py`, `settings.json`, `monitor_column_widths.json` 반영.
- **도구**: `tools/refresh_all_bars_15m_krx.py`, `tools/verify_1m_15m_alignment.py`, `utils/krx_bars.py` 추가.

## 0.4.1 (2026-04-25)

- **고정밀 백테(보수적 체결가)**: 1분 경로에서 **매도·EXIT 판가·체결 = 1분 저가**, **매수 체결 = 1분 고가** (`dynamic_backtest.py`, `backtest_engine.py` / `SimpleBacktester` 1분 시뮬). 지표 `simulate_intrabar`는 기존처럼 종가·OHLC 흐름.
- **시장 지표·시장 레짐 전면 비사용**: `use_market_filter`·MSI/VIX·Type A/B 레짐 phase·관련 UI·`MarketDataService`·레짐 모듈·일부 루트 plot 스크립트 제거. 전략·백테·최적화·리포트·대시보드/상단 UI 정리, `settings.json` 키·`core/signal_engine` 구문 수정 등.
- **기타**: `analyze_hynix` 시장 데이터 제거, `plot_with_vix`/`plot_kospi_*`(MSI) 루트 스크립트 삭제.

## 0.4.0 (2026-04-25)

- **동적 유니버스·KODEX200**: KRX/pykrx 의존 제거, `kodex200.csv`·`core/kodex200_universe` 정리. `diagnose_dynamic_universe_selection_by_trading_day`로 일봉·`Valid_B`·스케줄 진단. `prepare_dynamic_universe` 지표·스코어 추출에 `_score_dynamic_universe_daily_ohlcv` 사용.
- **하베스팅 → 일봉**: `bars_1m`을 일(캘린더) 단위로 집계해 `bars_1d`에 반영(`utils/bar_resample`, `DataManagerV2.rebuild_daily_bars_from_1m`, `harvest_service` 종료 시 일괄 보강). `update_historical_data`에도 동일.
- **리포트·기본값**: `reporting/trade_report` 종목 누적 수익과 요약·수수료 기준 맞춤, `core/defaults`·선정·전략 관련 주석 정리.

## 0.3.64 (2026-04-19)

- **일일 결산 요약**: 보유 시세를 포지션 탭과 동일하게 해석(`VirtualTab._resolve_active_holdings_prices` 재사용·폴백). **금일 실현**은 청산일(`exit_date` / `exit_datetime`) 기준. **금일 손익**은 전일 HTML이 아니라 **`eod_equity_snapshots_*.json`** 에 저장된 직전 영업일 EOD 총자산 대비(`reporting/daily_settlement_report.py`, `utils/eod_equity_snapshots.py`).
- **EOD 스냅샷**: 모의/실전별 JSON 경로(`utils/server_mode_paths.eod_equity_snapshots_path`). 장 마감+버퍼 이후 포지션 탭 갱신 시 자동 기록, 일일 결산 생성 시에도 동일 조건에서 해당 영업일 키로 저장.
- **포지션 관리 상단**: **미실현(보유)** 와 **금일 손익(전일 EOD)** 를 분리 표시(`ui/tabs/virtual_tab.py`).

## 0.3.63 (2026-04-18)

- **S1 ATR 트레일 표시·청산**: Type A·B 공통으로 **S1**에서 트랙별「ATR 분석 주기」(`use_atr_exit_a` / `use_atr_exit_b`)가 OFF인데도 `use_atr_trailing_stop_*` 기본값 True 때문에 감시·리포트에 **TRL** 이 나오던 현상 수정. 마스터 OFF 시 S1 트레일 비활성, SL만 ATR 토글이 켜진 경우 `SL …` 만 표시 (`backtest_engine.py` — `compute_atr_exit_display_text`, `evaluate_position`, TP 연기 분기).

## 0.3.62 (2026-04-18)

- **감시 트리·가상보유 스텁 현재가**: 선정 CSV에 없는 종목만 감시에 올릴 때(`vp_price_stub`) `last_price` 가 항상 0이던 문제 — 계좌 동기화 스냅(`ka_cur_prc`, 없으면 `ka_evlu_amt`/수량)으로 참고가를 채움. 보유 탭과 동일한 우선순위이며 평단은 넣지 않음 (`core/trading_controller.py`, `k_trader_gui.py`).

## 0.3.61 (2026-04-18)

- **롤백(실시간 감시 UI)**: 0.3.59~0.3.60 에서 넣었던 `monitor_tab.py`·`k_trader_gui.py` 변경(현재가 0 보완·`monitor_info` 참조·ka10004 스캔 등)이 감시 중단·트리뷰 소실 등 부작용을 일으켜, 해당 두 파일을 **0.3.58 직후(워밍업 수정만 반영된 상태)** 로 되돌림. 포지션 탭 현재가는 기존처럼 계좌 스냅·REST 폴백 경로를 사용.

## 0.3.58 (2026-04-18)

- **실시간 워밍업·캘린더 깊이 판정**: `가장 오래된 봉 <= now−N일` 단일 시각 비교는 롤링 보존 직후 봉이 컷오프보다 늦게 찍히는 경우가 많아 **항상 미충족**으로 나올 수 있음 → (오늘−최초 봉 날짜) 일수 ≥ (보존 N일 − 슬랙)으로 판정. 설정 `realtime_warmup_depth_calendar_slack_days`(기본 21) (`core/warmup_service.py`, `core/defaults.py`).

## 0.3.57 (2026-04-18)

- **실시간 워밍업 API**: `캘린더·깊이` 연속조회(`deep_fetch`)는 **과거 N일 미충족(`need_depth`)** 일 때만 사용. 장중 **당일 갭**만 있을 때는 얕은 연속조회로 분리 — 1년치는 이미 있는데도 ka10080이 깊이 모드로 반복되던 현상 완화 (`core/warmup_service.py`).

## 0.3.56 (2026-04-18)

- **실시간 워밍업 깊이 판정**: (1) RT15m DB 보존 구간 `MIN(datetime)` 이 캘린더 컷오프 이전이거나, (2) ka10080 연속조회에서 **next-key 없이 끝난 종목**(브로커·종목 이력상 과거 한계)은 `symbol_meta_rt`에 기록해 동일·더 짧은 보존 일수로 재적재하지 않음. 페이지네이션 루프 내 깊이 조기 종료는 누적 `merged` 기준으로 판정(`core/warmup_service.py`, `core/realtime_monitor_bars_store.py`).

## 0.3.55 (2026-04-18)

- **실시간 워밍업(RT15m DB)**: `ORDER BY datetime DESC LIMIT` 으로만 메모리에 올린 프레임의 `index.min()` 으로는 DB 에 쌓인 **실제 과거 깊이**를 알 수 없어, 재시작 후에도 `과거 N일 미충족`으로 오판하며 ka10080 캘린더·깊이 조회가 반복되던 문제 수정 — RT15m SQLite 에서 `MIN(datetime)` 으로 깊이 판정 (`core/warmup_service.py`, `core/realtime_monitor_bars_store.py`).

## 0.3.54 (2026-04-18)

- **데이터 하베스팅(GUI)**: 시작 안내를 `messagebox` 대신 **2초 후 자동 닫힘** 비모달 창으로 표시(`ui/tabs/backtest_tab.py`).
- **데이터 하베스팅(콘솔)**: 수집 완료·중단·오류 후 **Enter 대기 제거** — 프로세스가 바로 종료되어 콘솔 창이 닫힘(`harvest_service.py`).

## 0.3.53 (2026-04-18)

- **계좌 동기화와 메인 스레드**: `sync_with_actual_account`(REST·정합·저장)를 **데몬 스레드**에서 실행해 기동·감시 주기·체결 후 재동기화·동기화 버튼이 **UI를 수 초~10초 막지 않도록** 변경. 동시에 두 번 실행되면 꼬일 수 있어 **락으로 직렬화**(이미 동기화 중이면 해당 호출은 생략)(`k_trader_gui.py`).

## 0.3.52 (2026-04-18)

- **UI 반응성**: 포지션 탭 전체 그리기 등 무거운 갱신을 `after(1)`로 한 틱 미루어 이벤트 루프가 탭 클릭 등을 처리할 여지를 둠. 갱신 중에는 하단 **「처리 중: …」** 라벨과 **모래시계 커서**로 표시(`k_trader_gui.py`). `refresh_virtual_tab_full` 동일 패턴.

## 0.3.51 (2026-04-18)

- **기동 로그**: 『계좌 동기화』(ka85) **이후**에 이어지는 작업(로컬 정합·포지션 탭 갱신·kt00007 백그라운드 보강 등)을 **첫 동기화 완료 직후** 요약으로 안내. 인증 직후 긴 요약은 짧은 한 줄로 정리(`k_trader_gui.py`, `core/portfolio_manager.py`).

## 0.3.50 (2026-04-18)

- **기동 로그**: REST 인증 성공 직후 실행되는 백그라운드 작업을 **요약 블록 한 번**으로 안내(`k_trader_gui.py`). 루프마다 로그를 늘리지 않음.

## 0.3.49 (2026-04-18)

- **실시간 감시 탭 세로 분할(sash)**: 기동 직후 다른 탭에 있으면 Paned 높이가 잡히기 전에 복원되던 문제 완화 — **「실시간 감시」탭으로 전환할 때** sash 복원을 다시 예약(`k_trader_gui.py`). 복원 로직에 높이 보완·클램프 강화(`ui/tabs/monitor_tab.py`).
- **종료 시 터미널 출력**: 감시 레이아웃 저장 안내를 `print`로 중복 찍던 부분 제거 — 앱 로그 경로만 유지(`ui/tabs/monitor_tab.py`).

## 0.3.48 (2026-04-18)

- **포지션 관리 · 수수료반영 토글**: 합계 카드 옆 슬라이드 스위치 — **미반영/반영**에 따라 매입·평가·손익·수익율 및 종목 행(매입·평가·손익·수익율) 정의 일치. **1회 스캔 캐시**로 중복 계산 축소, 토글 클릭 시 **슬라이더는 즉시** 그리고 합계·트리는 **`after(0)`** 로 지연. 고아 행 정리·`Treeview` 순서 안정화·표시 안전 처리(`ui/tabs/virtual_tab.py`).

## 0.3.47 (2026-04-18)

- **포지션 관리 · 실시간 보유**: 보유 **전체 합계**(매입·평가·평가손익·수익율)를 **수수료 미반영**(평단×수량·시장 평가)으로 집계, 종목 트리 **위** 컴팩트 카드로 표시(`ui/tabs/virtual_tab.py`). 제목·부제 제거·여백 축소로 높이 정리, 손익/수익율 색상.
- **컬럼 폭**: `monitor_column_widths.json` 저장 반영.

## 0.3.46 (2026-04-18)

- **KODEX200 유니버스**: `kodex200_universe_priority`에 **`csv_only`** 추가 — `kodex200.csv`만 사용하고 pykrx·원격 URL·FDR 폴백 없음. 기본값을 **`csv_only`**로 변경(`core/kodex200_universe.py`, `core/defaults.py`, `settings.json`).
- **기동 로그**: KOSPI200 유니버스 로드 문구에서 「편입종목 확인」 표현 제거(`k_trader_gui.py`).
- **문서**: `get_kospi_list` 독스트링을 설정 기반 설명으로 정리(`data_manager_v2.py`).

## 0.3.45 (2026-04-17)

- **포지션 관리 탭 레이아웃**: 기동 직후 다른 탭에 있으면 Paned 높이가 잡히기 전에 sash만 복원되는 문제 완화 — **「포지션」탭으로 전환할 때** 세로 분할(sash) 복원을 다시 예약 (`k_trader_gui.py`).
- **포지션 탭 세로 분할 저장**: `<<PanedWindowMoved>>` 직후 좌표가 밀리는 경우·**Windows에서 가상 이벤트 누락**을 보완하기 위해 짧은 지연 후 비율을 읽어 `virtual_portfolio_ui.json`에 반영. 세시에서 마우스 업 시 `identify`로 판별(Windows는 정수 인덱스) (`ui/tabs/virtual_tab.py`).
- **저장 비율 없음**: 첫 실행 등 `sash0_rel`이 없을 때 기본 비율 **0.42**로 복원 (`ui/tabs/virtual_tab.py`).

## 0.3.44 (2026-04-17)

- **웹소켓 로그인 실패(8005 등)**: `LOGIN` 거부 시 REST 토큰 **TTL 캐시 무효화** 후 재발급·재접속 — 동일 무효 토큰으로만 무한 재시도하던 현상 완화 (`ui/tabs/monitor_tab.py`). 실패 로그 **스로틀**, 짧은 대기 후 재시도.

## 0.3.43 (2026-04-17)

- **실시간 감시 UI**: 워밍업 `prep_ok` 시 상단 상태를 「데이터 준비 중」으로 덮어 **감시 중** 문구가 사라지던 현상 수정 (`k_trader_gui.py`).
- **웹소켓 구독 목록**: `_active_stream_codes`만 쓰면 워밍업이 일부만 반영될 때 **한 종목만 REG** 되던 문제 수정 — 감시·관심·보유 합집합에 워밍업 코드 **병합** (`k_trader_gui.py` `stream_subscription_codes`). 구독 목록이 비었을 때 **감시를 끄지 않고** 재시도, `selected_targets.csv` 폴백.
- **실시간 패킷**: 호가(0D)만 오고 체결가가 없을 때 `price==0`으로 틱이 전부 무시되던 경로 완화 (`ui/tabs/monitor_tab.py`).
- **워밍업**: 지표 계산은 꼬리 봉만 (`realtime_warmup_indicator_calc_max_rows`, `core/warmup_service.py`). 메인 워밍업은 **항상** 최소·당일만 — 캘린더 깊이(1년)는 **백그라운드 큐만** (`realtime_warmup_quick_then_deep_background` 분기 제거).
- **포지션 탭**: 세로 Paned **sash 복원** 안전화(높이 미확정·클램프), 보유 트리 **래퍼 프레임**으로 확장 고정 (`ui/tabs/virtual_tab.py`).
- **설정**: `realtime_warmup_indicator_calc_max_rows` (`core/defaults.py`, `settings.json`).
- **동일 환경**: 회사 PC는 `SETUP_COMPANY_PC.md`대로 클론 후 `pip install -r requirements.txt` 또는 동일 재현 시 `requirements-lock.txt`(저장소에 동봉).

## 0.3.42 (2026-04-17)

- **실시간 워밍업·RT15m DB 보충(ka10080)**: 기본 **`tic_scope` 15분봉**만 연속조회하고, 1분→15분 리샘플을 하지 않음. 설정 `realtime_warmup_ka10080_tic_scope`(`core/defaults.py`, `settings.json`). `data_manager_v2.fetch_kiwoom_minute_chart_page`에 `tic_scope` 인자 추가 (`core/warmup_service.py`).
- **일일결산 15분 차트(REST)**: `fetch_15m_bars_for_trading_day_rest_only`도 **ka10080 15분봉**만 받고 1분 리샘플 제거 (`data_manager_v2.py`).
- **감시 탭 신호상태**: 백그라운드 깊이 보강 완료 시 `prep_ok` 를 다시 보내 **15분봉·검증 완료**로 덮어쓰던 현상 제거 (`core/warmup_service.py`).
- **배포·재현**: 회사 PC 등 동일 환경용 **`requirements-lock.txt`**(Python 3.12 `pip freeze`), **`SETUP_COMPANY_PC.md`**, **`requirements.txt`** 보강(pykrx·FDR·optuna 등).
- **`.gitignore`**: 대용량 DB 명시(`realtime_15m*.db`, `KTrader.db`, `trading_state_*.db` 등). `trading_data.db`는 기존과 같이 제외.
- **KODEX200 유니버스**(`core/kodex200_universe.py`): KRX PDF 직접 호출·HTTP 재시도·`kodex200_constituents_url` 원격 CSV 등(이전 세션 반영분 포함).

## 0.3.41 (2026-04-17)

- **포지션 관리 탭 UI**: 주문·체결 **날짜 네비**를 필터와 **한 줄**에 두고, 구분 세그먼트와 같은 **컴팩트** 스타일(◀·날짜·▶·오늘)로 정리 (`ui/tabs/virtual_tab.py`).
- **주문 로그 날짜 네비**: 당일에 주문 이력이 없어 DB 캘린더에 없을 때도 **다음(▶)** 으로 **오늘(빈 목록)** 에 도달하도록 수정 (`_order_log_next_date_with_orders`).

## 0.3.40 (2026-04-17)

- **포지션 관리 탭 UI**: 주문·체결 내역 **구분(전체·매수·매도)** 필터를 콤보박스 대신 **세그먼트 컨트롤** 스타일로 표시 (`ui/tabs/virtual_tab.py`).

## 0.3.39 (2026-04-16)

- **실시간 감시 탭 UI**: 상단(보유)·하단(감시목록) **세로 분할 비율**(`sash0_rel`) 저장·복원. `monitor_column_widths.json`을 `{"columns":{…},"sash0_rel":…}` 형식으로 확장하고, 기존 평면 컬럼 dict 도 그대로 읽음 (`ui/tabs/monitor_tab.py`).
- **포지션 관리 탭 UI**: 보유 트리·주문·체결 트리 **컬럼 폭** 및 보유/주문 영역 **세로 분할** 저장·복원. `virtual_portfolio_ui.json` (`settings.json`과 동일 폴더) (`ui/tabs/virtual_tab.py`).

## 0.3.38 (2026-04-16)

- **KODEX200 유니버스 자동 반영**: `core/kodex200_universe.py` — 기본 `kodex200_universe_priority=etf_api_first` 시 pykrx KODEX200 ETF(069500) 구성을 최우선하고, 실패 시 `kodex200.csv`·FDR 순. `local_csv_first` 로 기존(수동 CSV 우선) 동작 유지. 선택 `kodex200_export_csv_on_etf_success` 로 ETF 성공 시 `kodex200.csv` 자동 갱신. `data_manager_v2.get_kospi_list`, `strategy_selector` 실시간 선정 경로 연동 (`core/defaults.py`, `settings.json`, `k_trader_gui.py`).

## 0.3.37 (2026-04-16)

- **워밍업 UI(신호상태)**: 백그라운드 깊이(ka10080) 조회가 메인 `run()` 종료 후 `is_running` 때문에 조기 종료되고, `prep_progress`만 남아 `(1/41) 분봉 API 1/500…` 에 멈추던 문제 수정. 깊이 보강은 `respect_warmup_lifecycle=False` 로 끝까지 조회, 트리 진행 표시는 `suppress_prep_tree_progress=True` 로 생략, 완료 시 `prep_ok` 로 신호상태 복구. ka10080 진행 알림은 **실제 조회 종목 코드**로 행을 갱신하고 `(i/N)` 은 메인 루프와 같은 종목일 때만 맞춤 (`core/warmup_service.py`).

## 0.3.36 (2026-04-16)

- **실시간 스트림 재등록 로그**: 증분 워밍업으로 구독 시그니처가 매번 달라도, `REG` 전송은 유지하되 대시보드 문구는 **최대 약 25초에 1회**·종목 수 포함으로 스로틀 (`ui/tabs/monitor_tab.py`).
- **워밍업 API**: `is_running` 해제로 빠지는 **정상 연속조회 중단**은 로그 생략(종목별 스팸 제거). ka10080 진행 로그는 **120페이지 단위**만 출력, 1페이지 단독 로그 제거 (`core/warmup_service.py`).

## 0.3.35 (2026-04-16)

- **워밍업 로그**: `run()` 시작 안내를 한 줄로 통합, 종목별 1차 성공 로그 제거 후 메인 루프 종료 시 요약 한 줄만 출력. ka10080 연속조회 진행 로그 간격 확대·소량 페이지 요약 생략, API 15m 보충·RT15mDB 영속화·깊이보강(백그라운드) 성공 로그 생략, 1m→15m 리샘플 안내는 run당 최대 3회 (`core/warmup_service.py`).

## 0.3.34 (2026-04-16)

- **실시간 감시 웹소켓**: `REG` 재전송 요청이 같은 구독 종목 집합이면 전송·`[시스템] 실시간 스트림 재등록` 로그를 생략해 대시보드 로그 스팸을 줄임. 소켓 연결 시 시그니처 초기화, 로그인 직후 최초 `REG` 시 시그니처 기록 (`ui/tabs/monitor_tab.py`).

## 0.3.33 (2026-04-16)

- **실시간 감시 탭 UI**: 컨트롤 줄 순서를 `상태 | 실시간 감시 시작·중단 | 일일 감시목록 선정`으로 변경. 상태 문구 길이가 바뀔 때 액션 버튼이 옆으로 밀리던 현상 완화 (`ui/tabs/monitor_tab.py`).

## 0.3.32 (2026-04-16)

- **실시간 Smart Buy (Type B)**: 호가 `total_ask`/`total_bid`로 계산한 매도/매수 잔량비를 `info['ask_bid_ratio']`에 넣지 않아, Smart Buy ON 시 `info.get(..., 1.0)` 폴백이 **항상** `smart_buy_ba_ratio`(예: 1.5) 미만으로 처리되던 버그 수정 (`core/signal_engine.py`).

## 0.3.31 (2026-04-15)

- **포트폴리오 `invested`**: 브로커 동기화·가상 신규 시 체결금액에 **매수 수수료**를 더해 S1 on_fill과 맞춤. S2 추매 시 BUY 이벤트 합산 우선, 없으면 기존 `invested`에 추매 다리(가격×수량+수수료) 가산 (`core/portfolio_manager.py`).
- **미실현 손익 공통화**: `utils/fee_rates.unrealized_at_price_net_of_sell_fee` — 매도 수수료 차감 순입금 − `invested`. 가상 탭·가상 내러티브 리포트·일일 정산 요약에서 동일 식 사용 (`utils/fee_rates.py`, `ui/tabs/virtual_tab.py`, `reporting/virtual_report.py`, `reporting/daily_settlement_report.py`).
- **설정**: 모의 매도 수수료 `sell_fee_mock` **0.5 → 0.55** (`settings.json`).

## 0.3.30 (2026-04-16)

- **설정·동작**: `use_auto_virtual`(가상 매매 자동화) 토글 제거 — 설정 탭·`defaults`·`settings.json`·리포트 매핑·`trading_controller` 가드 삭제. 실시간 신호에 따른 매수·전략 액션은 항상 진행 (`ui/tabs/settings_tab.py`, `core/defaults.py`, `core/trading_controller.py`, `reporting/trade_report.py`, `tools/settings_tab_ui_catalog.py`).

## 0.3.29 (2026-04-16)

- **하베스팅 UI**: 대시보드 데이터 수집 타일 제거. **최신주가데이타수집** 버튼을 백테스팅 탭 상단 `시세 데이터` 구역으로 이전(설명 문구 없음). 기간·최적화·성과 검증은 `grid` 균등 3열·`기간 · 최적화 · 성과 검증` 프레임으로 정리 (`ui/tabs/backtest_tab.py`, `ui/tabs/dashboard_tab.py`).

## 0.3.28 (2026-04-16)

- **실시간 감시 탭 UI**: 종목 일일 선정을 대시보드에서 이전 — 버튼명 **일일 감시목록 선정**, 오른쪽 묶음 순서(선정 → 감시 시작 → 상태), 두 액션 버튼 동일 `width` (`ui/tabs/monitor_tab.py`, `ui/tabs/dashboard_tab.py`). KODEX 로드 후 활성화·원격 선정은 `monitor_tab` 경로 (`k_trader_gui.py`, `core/remote_commander.py`).

## 0.3.27 (2026-04-16)

- **계좌 REST 주기 동기화**: KRX 장중/장외 구분·보유만으로 타이머 유지 로직을 제거하고, **실시간 감시가 켜져 있을 때만** 약 30초마다 동기화. 감시 중이 아니면 30초 루프 없음 (`k_trader_gui.py`).

## 0.3.26 (2026-04-16)

- **trading_data.db 용량**: PK(`code`,`datetime`)와 동일한 보조 인덱스(`idx_bars_1m_code_dt` 등)가 데이터·키를 이중 저장 — `data_manager_v2` 부트 시 `DROP INDEX IF EXISTS`로 정리, 신규에는 동일 인덱스 미생성. 수동 점검: `python tools/audit_trading_db_size.py`, 인덱스만 드롭: `tools/drop_redundant_candle_indexes.sql`. 파일 크기 축소는 `VACUUM`(시간 소요).

## 0.3.25 (2026-04-16)

- **워밍업 분리**: `realtime_warmup_quick_then_deep_background`(기본 True)일 때 메인 워밍업은 최소 봉·당일 갭만 빠르게 채우고, 캘린더·깊이(1년) ka10080 보강은 데몬 스레드 큐에서 종목별 순차 실행. `bars_data` 갱신은 `RLock`으로 스트림 틱과 동기화 (`k_trader_gui.py`, `core/stream_processor.py`, `core/warmup_service.py`).

## 0.3.24 (2026-04-16)

- **워밍업 UI**: ka10080 연속조회(특히 캘린더·깊이 모드) 중에도 `prep_progress`로 페이지 진행 표시·로그(15·50페이지 단위). 연속조회 루프에서 `is_running` 확인으로 중단 가능 (`core/warmup_service.py`, `ui/tabs/monitor_tab.py`, `k_trader_gui.py`).

## 0.3.23 (2026-04-16)

- **실시간 최소 봉수**: `min_15m_bars_for_signal`만이 아니라 설정 탭 ATR 주기 `atr_period_a`·`atr_period_b`(폴백 `atr_period`)와의 **최댓값**을 `effective_min_15m_bars_for_monitoring()`으로 통일 — 신호 엔진·워밍업 검증 공통 (`core/defaults.py`, `core/signal_engine.py`, `core/warmup_service.py`).

## 0.3.22 (2026-04-16)

- **워밍업 과거 1년(캘린더) 확보**: `realtime_warmup_fetch_calendar_history`(기본 True)일 때 ka10080 를 신호 최소봉만으로 조기 종료하지 않고, 가장 오래된 15분봉이 `realtime_15m_calendar_retention_days` 이전이 될 때까지 연속조회. 상한 `realtime_warmup_ka10080_max_pages_deep`(기본 500). 키움 응답 한도로 1년 미만일 수 있음 (`core/warmup_service.py`, `core/defaults.py`).

## 0.3.21 (2026-04-16)

- **실시간 15분 DB 캘린더 롤링**: `realtime_15m_*.db`는 `realtime_15m_calendar_retention_days`(기본 365) 이전 봉을 종목별 DELETE. 워밍업 배치·확정봉 저장 후 동일 트랜잭션에서 정리. 조회는 `datetime >= cutoff` 필터. `realtime_warmup_15m_query_limit` 기본 7000으로 조정(1년 분량 상한). (`core/realtime_monitor_bars_store.py`, `k_trader_gui.py`, `core/defaults.py`)

## 0.3.20 (2026-04-16)

- **실시간 15분 DB 영속화**: 워밍업 검증 통과 직후 종목별 병합 OHLCV를 `realtime_15m_*.db`에 배치 저장(`bulk_upsert_ohlcv_index_frame`). 저장 상한은 `realtime_warmup_15m_query_limit`과 동일. 설정 `realtime_warmup_persist_to_rt15m_db`(기본 True)로 끌 수 있음 (`core/realtime_monitor_bars_store.py`, `core/warmup_service.py`, `core/defaults.py`).

## 0.3.19 (2026-04-16)

- **실시간 감시 전용 15분 DB**: 하베스팅 `trading_data.db`와 분리된 `realtime_15m_mock.db` / `realtime_15m_live.db`에 확정 15분봉 적재 (`core/realtime_monitor_bars_store.py`, `core/stream_processor.py` 콜백, `k_trader_gui.py`).
- **워밍업**: `realtime_warmup_ohlcv_source`에 `realtime_15m_db_then_api`(별칭 `rt_15m_db`, `monitor_15m_then_api`) — 실시간 DB 우선 후 API 백필 (`core/warmup_service.py`). 기본·`settings.json` 예시를 해당 모드로 정렬 (`core/defaults.py`).
- **문서**: `docs/realtime_monitor_bar_and_db_notes.md` 구현 섹션 보강.

## 0.3.18 (2026-04-16)

- **포지션·체결**: 매도 `exit_reason`을 주문 로그(`EXIT_*`)와 동일 문자열로 저장·체결 시 우선 반영 (`core/trading_controller.py`, `core/portfolio_manager.py` `on_fill`).
- **매매 종료 누락 방지**: 로컬 보유 없이 매도 체결만 온 경우 `order_history` 기반 `record_orphan_sell_close` 보강 (`data_manager_v2.py`, `k_trader_gui.py`).
- **근본 보강**: 실매도 접수 성공 직후 포지션 전체를 주문번호로 `pending_exit_snapshots`에 저장·`virtual_db` 영속화, 체결 시 스냅샷으로 종료 확정; `on_fill(..., order_no=)` 전달 (`core/portfolio_manager.py`, `k_trader_gui.py`, `core/trading_controller.py`). 락을 `RLock`으로 변경.
- **비상 매도**: `sell_all`에서 접수 번호가 있으면 동일 스냅샷 등록 (`core/portfolio_manager.py`).

## 0.3.17 (2026-04-15)

- **UI**: 동기화·종합리포트·일일결산·관심목록·서버 전환·모드 배지를 **대시보드** `운영 · 계좌` 영역으로 이전. 포지션 관리 탭 상단에는 안내 문구만 표시 (`ui/tabs/dashboard_tab.py`, `ui/tabs/virtual_tab.py`). 종합리포트 생성 중 버튼 비활성화는 대시보드·기존 로직 겸용.

## 0.3.16 (2026-04-15)

- Plotly `go.Candlestick` 호환: `increasing_fill_color` 등 폐기 속성 제거 → `increasing`/`decreasing` dict(`line`, `fillcolor`)로 지정 (`reporting/daily_settlement_report.py` — 종합·일일 결산 15분 차트 공통)

## 0.3.15 (2026-04-15)

- 종합(가상) 리포트 생성 시 15분 차트용 REST를 **종목·거래일 단위로 한 번만** 호출하도록 동일 리포트 내 `rest_day_cache` 공유 (`reporting/virtual_report.py`). 캐시 히트 시 일자 간 `sleep(0.12)` 생략.

## 0.3.14 (2026-04-15)

- 종합(가상) 리포트 **백테스트 분석 통계**: 실행·신호와 전략·스테이지 실현을 **Type A/B × S1/S2 네 칸 + 합계** 한 표로 통합하고, 청산·세션·스마트 시도 등은 **전역·세션 요약** 표로 분리 (`reporting/trade_report.py`)

## 0.3.13 (2026-04-15)

- 백테스트·가상 종합 리포트 「실행·신호 요약」표에서 **신호 후보 대비 완료 라운드 비중** 행 제거(단위 불일치로 해석이 어려움) (`reporting/trade_report.py`)

## 0.3.12 (2026-04-15)

- 종합(가상) 리포트 **스마트 진입 거절(수급)**: 실시간 감시에서 Smart Buy 미통과 시 세션 이벤트 `smart_buy_blocked`를 남기고(15분봉·종목당 1회), 가상 이력 일자 구간의 `session_events.jsonl`에서 건수를 합산해 표시 (`core/signal_engine.py`, `reporting/trade_report.py`, `reporting/virtual_report.py`)

## 0.3.11 (2026-04-15)

- 일일 결산·종합 리포트 **15분 Plotly 차트**: 한국 시장 관례에 맞춰 **상승 봉·상승 거래량 막대 = 빨강(`#BF616A`)**, **하락 = 파랑(`#5E81AC`, MA선과 구분)** (`reporting/daily_settlement_report.py`; 종합 리포트는 동일 `_build_plotly_15m_section` 사용)

## 0.3.10 (2026-04-15)

- 일일 결산 HTML 생성: `_build_enriched_order_rows` 호출 시 누락되던 **`excl` 정의** 보강(기본 빈 집합·선택 `app.daily_report_exclude_codes`) — `NameError`로 배치 생성이 막히던 문제 수정 (`reporting/daily_settlement_report.py`)
- 계좌 동기화: **`SYNCED` 유령 제거** 시 “직전 실매도” 판정을 **분 단위**(`sync_ghost_suppress_minutes`, 레거시 시간 키는 상한 변환)로 좁혀 과도한 억제 완화 (`core/portfolio_manager.py`, `core/defaults.py`, `settings.json`)
- 체결 동기화: REST 선처리 시 주문번호를 **`on_fill` 성공 후에만** `_exchange_fill_order_ids`에 넣도록 변경 — 매도 스킵 뒤 CHE가 `on_fill`을 못 타 **매매종료내역 누락**될 수 있던 순서 버그 수정 (`k_trader_gui.py`); `on_fill`은 스킵 시 `False` 반환 (`core/portfolio_manager.py`)
- **도구**: `order_history` → `virtual_history` 복구 스크립트 추가 (`tools/recover_missing_history_from_orders.py`)

## 0.3.9 (2026-04-15)

- 실시간 감시 S1 Smart Buy: **`use_smart_buy_a` 기본 `False`**(키 누락 시에도 Type A SB 비활성), **`sel_track`(A/B/AB)과 동일한 SB 스위치** 적용 — 전략 A 표시인데 Type B 수급 필터만 타던 불일치·「수급 대기」 오표시 완화 (`core/signal_engine.py`)
- 백테스트: Type A Smart Buy 캐시 **`use_sb_a` 기본 `False`**로 `defaults`와 정합 (`backtest_engine.py`)

## 0.3.8 (2026-04-15)

- 포지션 관리·종합 리포트·일일 결산: **관심목록으로 종목을 숨기거나 집계에서 제외하지 않음** — 관심은 신호 테스트 등 전용, 계좌 데이터는 전부 반영 (`virtual_tab.py`, `daily_settlement_report.py`, `watchlist_store.py` 설명 정리)

## 0.3.7 (2026-04-15)

- 실시간 감시: **트리 UI 갱신**만 `after` 주기 완화 (`k_trader_gui.py`의 `REALTIME_TREE_UI_REFRESH_MS`, 기본 280ms). 틱·지표·신호 처리 경로는 0.3.6과 동일 — 발열·CPU는 **표시 루프만** 줄여 관찰

## 0.3.6 (2026-04-13)

- 실시간 감시 탭: 청산으로 보유 트리에서 빠진 종목은 **감시 선정(`monitor_codes`)에 있을 때만** 실시간 감시 목록에 표시; 선정 외 보유만 하던 종목은 감시 목록으로 자동 이동하지 않음 (`monitor_tab.py`)

## 0.3.5 (2026-04-13)

- 실시간 감시 탭 **보유 및 대응 종목** / **실시간 감시 목록**: **거래대금** 컬럼 제거 (`monitor_tab.py`, `signal_engine.py` 트리 갱신 값 정합)

## 0.3.4 (2026-04-13)

- 실시간 감시 탭 **보유 및 대응 종목** / **실시간 감시 목록**: 컬럼 **헤더는 항상 가운데**; **전략유형·신호상태** 셀 **가운데**, **현재가**부터 오른쪽 수치·호가 셀 **오른쪽** 정렬 (`monitor_tab.py`)

## 0.3.3 (2026-04-13)

- **증권사 체결내역 REST (`kt00007`)**: 계좌 동기화 후·주문 로그 날짜 변경 시 백그라운드로 조회해 `order_history`에 병합(실전/모의 state DB 각각, `order_source`·`fill_row_key`로 중복 방지)
- 설정 **`exchange_fill_sync_lookback_days`** (기본 7일): 동기화 시 병합할 과거 일수

## 0.3.2 (2026-04-13)

- 포지션 관리 탭 **매매 종료 내역** 사유: 진입 TYPE 대신 **`exit_reason`(매도·정리 사유)** 및 청산 이벤트에서 보강 표시
- **주문 로그** 상태: `FILLED` → 표시 **`성공`**

## 0.3.1 (2026-04-13)

- Smart Exit **가격 하락 임계** (`smart_exit_price_threshold`): 고점 대비 **하락 %(양수)** 로 통일, 음수는 절댓값과 동일 처리 (`TradingStrategy.evaluate_position` 등)

## 0.3.0 (2026-04-13)

- 포지션 관리 탭 **요약 지표 재정의**: `backtest_initial_capital` 제거 — **보유주식 손익(미청산)** 은 미실현 합만, **누적 수익(종료+보유)** 은 매매종료 손익 합 + 보유 미실현 합, %는 **총자산** 분모
- 보유 **미실현**: 현재가 전량 매도 가정 시 **매도 수수료** 반영(`invested` 원가); 트리 손익금과 상단 합계·틱 요약이 **동일 스냅샷**으로 일치
- 실시간 보유 / 매매종료 / 주문 로그 Treeview **열 정렬**(진입일시·가격·수익률·VP·사유 등)

## 0.2.10 (2026-04-13)

- 가상 매매 **종합 리포트** 생성: **진행률 표시**(단계별 메시지 + 프로그레스바), **백그라운드 스레드** 처리로 UI 멈춤 완화, 생성 중 **종합리포트** 버튼 비활성·중복 실행 안내
- `generate_virtual_narrative_report(..., progress_callback=…)` — 통계·종목별 차트·저장 단계에서 `(done, total, msg)` 콜백

## 0.2.9 (2026-04-13)

- 가상 매매 **종합 리포트** 15분 차트: **진입일~청산일(미청산 시 오늘)** 거래일 15분봉을 이어 붙여 표시
- 동일 차트: **연속 장중** x축(봉 순서)으로 야간·휴장 빈 구간 제거, 호버·눈금에 실제 일시 표시 (`compress_session_gaps`)
- 일일 결산 15분 Plotly: `max_display_rows`·구간 라벨·제목 보강(가상 리포트와 공용)

## 0.2.8 (2026-04-14)

- 일일 결산 HTML: **세션 이벤트 로그(JSONL 복사본)** 섹션 제거(리포트에서 JSONL 미포함·생성 시 세션 이벤트 미조회)

## 0.2.7 (2026-04-14)

- 포지션(가상) 탭 **주문 로그** 날짜 네비: 주문이 없는 날짜는 건너뛰기, 가장 과거/가장 최근(오늘 이하) 주문일에서는 **이전** / **다음** 비활성 (`get_distinct_order_calendar_dates`)

## 0.2.6 (2026-04-14)

- 실시간 감시: 전략유형 **전략 A/B** 문구 정리, 신호상태에서 **「대기」** 접두 제거
- 포지션(가상) 탭 실시간 보유: **평가금** 열 양수 앞 `+` 제거

## 0.2.5 (2026-04-14)

- 실시간 감시 **신호상태**: 보유 종목은 S1 합치도 대신 **S2 추매 합치도**(`추매(A|B) N/M`, 2차 진입 완료 시 `추매(S2) 완료`) 표시; 미보유 시 타점 문구가 트리에 반영되도록 순서 정리

## 0.2.4 (2026-04-14)

- 실시간 감시: 감시 트리 `sel_track=B` 인데 포지션에 TYPE_B가 비어 있으면 ATR 기준가는 B로 그려지나 청산만 A 트랙으로 잡히던 불일치 수정 (`evaluate_position`에 `sel_track` 전달·표시와 동일 보정)
- 실시간: 보유 시 봉 수 부족해도 청산 평가 시도, `evaluate_position`에서 지표 ATR NaN 시 `bars_df` 기반 ATR 보강 (`_curr_atr_resolved`)

## 0.2.3 (2026-04-14)

- 포지션(가상) 탭 상단 요약: **금일 손익** 라벨을 실제 의미에 맞게 **보유주식 손익**으로 변경

## 0.2.2 (2026-04-14)

- 포지션(가상) 탭 **실시간 보유 현황**: Canvas·라벨 그리드 대신 Treeview로 표시 (스크롤·줄무늬·손익 태그와 동일 패턴)

## 0.2.1 (2026-04-14)

- 예수금: 출금가능금액(pymn) 기준 표시, 주문가능·D+2와 분리; `get_withdrawable_deposit()` 추가
- S1 매수: `invest_amt < 1주 가격`일 때 `max(1,…)`로 한도 초과 매수하던 문제 제거 (D+2 미수 방지)
- 실시간 감시 ATR 기준가: NaN·`ATR미사용` 보강(ADX 실패 시에도 ATR 계산, TR 근사 폴백, Type B·sel_track·워밍업 시계열 연동)
- 기타: `strategy/indicators` ADX/ATR 분리, 포지션 동기화 시 `sel_track=B`이면 `sig_type` TYPE_B 등

## 0.2.0 (2026-04-13)

- 가상 매매 종합 리포트: 미청산 보유 종목을 현재가 기준으로 평가, 매도 시 수수료를 반영한 순입금 추정을 종목별·상단 요약에 표시
- 코어·UI·리포팅·수수료/경로 유틸 등 누적 개선 (실시간 감시, 가상 탭, 일일 결산·거래 리포트 등)

## 0.1.1

- `CHANGELOG.md` 신설: 이후 버전 갱신 시 수정 요약을 누적 기록
- `core/version.py` 관례에「버전 변경 시 본 파일 반영」문구 보강

## 0.1.0

- 앱 표시 버전을 `0.N.M` 체계로 정리 (`core/version.py`의 `APP_VERSION`, 메인 창 제목 연동)
- 실전 반영 전 선행 `0`·`N`/`M` 증가 규칙을 `core/version.py` 주석으로 명시
