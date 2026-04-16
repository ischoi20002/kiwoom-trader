# 변경 이력

`core/version.py`의 `APP_VERSION`과 맞춥니다.  
버전 숫자를 올릴 때 **같은 작업**에서 여기에 해당 버전 섹션을 추가합니다.

---

## 0.3.27 (2026-04-15)

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
