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

## 0.18.68 (2026-08-26)

- **포지션관리 보유 패널 높이**: 실시간 감시 보유/대응과 같이 보유 종목 수(최소 3줄)에 맞춰 상단 sash 높이를 자동 조정 (`virtual_tab`).
- **감시탭 줄무늬**: 보유/대응 트리 스트라이프를 화면 행 순서로 재적용 (`monitor_tab`, 0.18.67).

## 0.18.67 (2026-08-26)

- **감시탭 줄무늬 수정**: 보유/대응 트리 가로 스트라이프를 캐시 길이 대신 화면 행 순서로 적용해 2줄씩 같은 색이 나오던 문제 수정 (`monitor_tab`).

## 0.18.66 (2026-08-26)

- **말분 공식1분 조회 로그**: 신호평가용 슬롯 말분 ka10080 성공/실패를 세션(`m1_last_official_coverage`)·GUI에 기록 (`rt_watchlist_m1_api_poller`, `realtime_m1_fallback`).

## 0.18.65 (2026-08-26)

- **헤드 catchup 재시도**: 종목별 `minute_not_ready` 시 고정 12초가 아니라 헤드 말분 마감 후 ~55초(벽시계)까지 재시도. 한도 초과 시에도 확보된 차트분은 store에 반영 (`rt_watchlist_m1_api_poller`).

## 0.18.64 (2026-08-26)

- **로그 정리**: `[M1Poll] 틱말분 완성15 평가 실패` 집계 로그 미출력 (동작 유지).

## 0.18.63 (2026-08-26)

- **텔레그램 오탐 수정**: 전송 후 ReadTimeout을 실패로 오인하던 문제 완화, HTML 파싱 실패 시 plain 재시도, 체결 알림 HTML 이스케이프. GUI의 '미설정' 오탐 문구 정리 (`telegram_sender`, `fill_reconciliation`, `k_trader_gui`).

## 0.18.62 (2026-08-26)

- **로그 정리**: 매도 이중 체결(이미 청산됨) 시 Portfolio/체결 중복 안내 로그를 제거해 조용히 스킵.

## 0.18.61 (2026-08-26)

- **로그 문구**: 매도 이중 체결 확인 시 `매도 체결 무시` 경고 톤을 `중복 확인·이미 청산 반영` 안내로 변경 (`portfolio_manager`).

## 0.18.60 (2026-08-26)

- **로그 정리**: `[M1Poll] 틱말분 보조 예약(공식 우선)` 예약 안내 로그 제거 (동작은 유지).

## 0.18.59 (2026-08-26)

- **재시작 catchup 전 체결·미체결 가드**: 직전 완성슬롯 재평가 전에 계좌 동기화·당일 kt00007 체결 병합·미체결 매수(S1 pending)/매도(exit_pending) 복구를 수행해 중복 주문을 줄임 (`restart_pre_catchup`, `warmup_stream_prep_service`).

## 0.18.58 (2026-08-26)

- **공식 15분 정렬교체 제거**: 슬롯 완성 후 ka10080(15) REST로 bars를 덮어쓰던 경로를 비활성. GUI 스케줄 미연결·`schedule` no-op. 신호는 M1 합성·누락채움만 사용 (`k_trader_gui`, `rt_closed_15m_official_replace`).

## 0.18.57 (2026-08-26)

- **평가 전 누락 15분 채움**: 말분 공식·틱 보조 완성15 평가 직전에 닫힌 슬롯 누락·스텁을 공식1분 합성으로 채운 뒤 평가한다. 말분 조회 시 직전 슬롯용 1분 catchup도 함께 반영 (`rt_watchlist_m1_api_poller`).

## 0.18.56 (2026-08-26)

- **말분 평가 공식 우선**: `:00` 완성15는 공식 1분 REST로 평가하고, 틱 말분은 실패·타임아웃 보조로만 사용. 공식 조회는 quiet에 막히지 않으며 분산은 헤드와 같이 `n×1.1s`로 조임 (`rt_watchlist_m1_api_poller`, `ka10080_spread`).

## 0.18.55 (2026-08-26)

- **누락 15분봉 보조 채움**: 헤드 1분 catchup 직후, 이미 닫힌 최근 슬롯의 누락·스텁 15분을 공식 1분 합성으로 삽입/교정한다. 공식 ka10080(15) 교체와 병행하는 보조 경로 (`realtime_official_bars_sync`, `rt_watchlist_m1_api_poller`).

## 0.18.54 (2026-08-26)

- **헤드 catchup 분산 단축**: `:13` 마감~`:00` 평가(~1분) 안에 끝나도록 헤드 전용 spread(`n×1.1s`, 상한 50s, 레거시 40s 바닥 미적용)와 우선순위 순 균등 간격을 적용 (`ka10080_spread`, `rt_watchlist_m1_api_poller`).

## 0.18.53 (2026-08-26)

- **헤드 1분 catchup 커버리지 로그**: 슬롯 헤드 soft catchup 후 기대 분 수·차트 분·store 공식 분·누락 시각을 `m1_head_catchup_coverage`로 세션 JSONL에 남김. 불완전하면 UI 경고 (`rt_watchlist_m1_api_poller`, `realtime_m1_fallback`).

## 0.18.52 (2026-08-26)

- **보유/대응 패널 잘림**: 상단 높이 계산이 LabelFrame·헤딩·가로스크롤을 빼먹어 종목이 늘면 세로 스크롤이 생겼다. chrome 실측·적용 성공 후에만 스킵·부족 시 한두 줄 보정으로 보유 행이 보이도록 sash/minsize를 맞춤 (`monitor_tab`).

## 0.18.51 (2026-08-25)

- **선정 lookback을 실시간과 동일 280일로 정렬**: 동적 BT는 평가일 D마다 `[D-280, D]` 슬라이딩 창에서 일봉 지표를 다시 계산하고, 실시간 FDR/DB/키움 일봉도 동일 `bt_universe_indicator_lookback_days`(기본 280)을 쓴다. end 고정 800일·FDR 폴백 계단(280→120)을 제거해 BT 시작일과 무관하게 같은 평가일의 유니버스가 맞도록 함 (`strategy_selector`, `settings`/`defaults`).

## 0.18.50 (2026-08-25)

- **동일 평가일 유니버스가 BT 기간에 따라 달라짐**: 일봉 RSI/MACD EWM 조회를 `start_date-120`에 묶어 OSC 부호·Valid가 바뀌고(예: 103140 vs 457190) 당일만/장기 구간 결과가 갈렸다. `end_date` 기준 고정 lookback(`bt_universe_indicator_lookback_days`)으로 지표·transition delta를 기간 불변 처리 (`strategy_selector`).

## 0.18.49 (2026-08-25)

- **공식15 정렬 차이 로그**: 틱 합성봉과 OHLCV가 달라 실제 덮어쓸 때만 종목·슬롯 시각을 로그한다 (`k_trader_gui`).

## 0.18.48 (2026-08-25)

- **워밍업 진행중 슬롯 1분봉 보충 제거**: ka10080(1분) 선적재·진행중 15분 합산이 head/말분 공식 catchup·틱 경로와 중복되어 재시작만 늦췄다. 워밍업은 15분봉 경로만 유지하고 M1은 감시 시작 후 catchup에 맡긴다 (`warmup_service`).

## 0.18.47 (2026-08-25)

- **정렬 메모리 반영이 종목당 수 초**: ka10080 1페이지의 과거 봉을 메모리에 없어도 행마다 SQLite INSERT+purge 해서 반영이 수분 걸렸다. heal은 최근 4봉, 메모리에 있는 봉만 반영, DB는 종목당 1트랜잭션, 반영 소요 요약 로그 (`realtime_monitor_bars_store`, `rt_closed_15m_official_replace`, `k_trader_gui`).

## 0.18.46 (2026-08-25)

- **정렬 배치 완료 로그 지연**: 26종×최대 8페이지·재시도 15초로 ka10080 조회가 수분 걸려 시작만 찍히고 완료가 안 보였다. 정렬 배치는 1페이지·재시도 1회, 5종마다 진행 로그, 고아 pending 강제 정리 (`rt_closed_15m_official_replace`).

## 0.18.45 (2026-08-25)

- **부분체결 수량 미반영**: 취소 접수 시 `filled_qty`를 0으로 덮어쓰고, 접수 행만 보면 kt00007 부분체결 합산을 놓쳤다. 취소 시 누적 체결을 보존·PARTIAL 표기, 체결확인은 합산 수량으로 보강, DB는 체결수량을 줄이지 않음 (`order_manager`, `fill_reconciliation_service`, `data_manager_v2`, `virtual_tab`).

## 0.18.44 (2026-08-25)

- **터치호가 취소 프로브가 너무 빠름**: kt00007 미반영 2회(~90초)만에 취소 API를 넣어 호가 대기 주문이 조기 취소됐다. 접수 후 최소 3분(`touch_pending_cancel_probe_min_sec`)이 지나야 취소 프로브를 한다 (`limit_order_watch_service`).

## 0.18.43 (2026-08-25)

- **장중 재시작 후 신호평가 누락**: 워밍업 catch-up이 형성 중 슬롯만 봐서(예: 13:48→13:45봉) 이미 끝난 직전 완성봉(13:30) 평가를 건너뛰었다. 당일 직전 완성 15분 슬롯을 워밍업 OHLCV로 즉시 평가한다 (`rt_watchlist_m1_api_poller`, `krx_bars`).

## 0.18.42 (2026-08-25)

- **실시간 보유/대응 패널 높이**: 고정 5줄 대신 보유 종목 수에 맞춰 sash 높이를 바꾸고, 3개 이하면 최소 3줄 높이를 유지합니다 (`monitor_tab`).

## 0.18.41 (2026-08-25)

- **공식 15분 정렬 1분 간격**: 정렬 모드에서 종목마다 quiet/평가큐 양보(최대 45초)와 스레드가 겹쳐 반영 로그가 1분씩 벌어졌다. 슬롯당 배치 1워커로 고정 지연 후 전 종목을 짧은 REST 간격으로 조회·한 번에 OHLCV/MA 반영 (`rt_closed_15m_official_replace`).

## 0.18.40 (2026-08-25)

- **완성15 평가·주문 지연**: 이전 슬롯 공식 정렬의 전체 지표 재계산이 GIL을 잡아 다음 슬롯 틱말분 평가가 1~2분 밀렸다. 정렬 모드에서는 재계산 생략, 평가 quiet를 종목 수에 맞게 연장, 공식 반영은 평가 종료까지 양보, 평가 큐는 합치도 세분 우선 (`closed_15m_fast_path`, `rt_closed_15m_official_replace`, `rt_watchlist_m1_api_poller`).

## 0.18.39 (2026-08-25)

- **모바일 보유 현재가 미표시**: API에 `last_price`가 있는데 보유 목록 UI가 손익만 그렸다. 현재가·평단을 표시하고, 모니터 시세가 없으면 ka10085 `ka_cur_prc`를 쓰도록 보강 (`mobile_web/app.js`, `status_snapshot`).

## 0.18.38 (2026-08-25)

- **공식 15분 정렬이 다음 슬롯 평가를 막던 문제**: 종목마다 전체 지표를 동기 재계산하며 전역 락을 잡아, 26종이 1분 간격으로 붙고 다음 봉 틱말분 평가가 밀렸다. OHLCV·MA만 먼저 반영하고 지표는 1워커 후순위, 완료 로그는 반영 후로 (`k_trader_gui`, `closed_15m_fast_path`, `rt_closed_15m_official_replace`).

## 0.18.37 (2026-08-25)

- **active_position 저장 실패**: Flash 고점 보존 때 `_save_active_position`이 자기 자신에게 `max_p`를 넘겨 TypeError가 났다. DataManager UPSERT로 고점을 넘기도록 수정 (`portfolio_manager`).

## 0.18.36 (2026-08-25)

- **09:30봉 마감 후 평가·주문 지연**: `:00` 틱과 타임아웃이 같은 종목을 이중 제출하고, FIFO 2워커가 보유 청산을 미보유 S1 뒤에 수 분 세워 둠. (종목, 슬롯) 원자 점유, 보유 전용 평가 워커, 타임아웃은 보유 우선 (`rt_watchlist_m1_api_poller`, `closed_15m_fast_path`).

## 0.18.35 (2026-08-21)

- **개장 매도 유예(RT/BT 정합)**: 15분봉 라벨로 유예를 세면 BT가 실시간보다 한 봉 늦게 풀림. 봉 완성 시각(라벨+15분)으로 판정 (`backtest_engine`, `dynamic_backtest`).
- **개장 유예 45분**: Type A/B `gap_grace_mins` 30→45. 완성시각 기준으로 예전 라벨 30분(첫 GAP 09:30봉)과 같은 타이밍이며, 2024-06~2026-08 BT에서 수익이 더 좋음.
- **설정 복구**: `type_balance_a_pct` 오염값(0.215)을 65.0으로 복구. 백테 매도수수료 0.215% 유지.

## 0.18.34 (2026-08-21)

- **실전 15:30/15:15 NXT 불가 종목 폴백**: `ka10100`(`nxtEnable`)으로 종목별 NXT 가능 여부를 아침·감시 시작 시 갱신. 가능 종목은 기존 NXT 1호가+SOR, 불가 종목은 모의와 같이 15:30→익일 09:00 예약·15:15→동시호가 시장가 (`nxt_eligibility_service`, `signal_engine`).
- **기동 시 포지션 복구 로그 중복 제거**: GUI `load_all` 1회만 수행, 인증 후 재호출 생략 (`portfolio_manager`, `rest_auth_retry_service`).
- **금일손익 장전 기준 로그 정리**: 계좌 동기화 전에는 캡처하지 않고, 동일·미세 변동 로그 생략·의미 있는 변경만 설정/재산정으로 표시 (`session_open_day_pnl`, `virtual_tab`).

## 0.18.33 (2026-08-20)

- **장시작 기준 총자산이 재시작마다 바뀌던 문제**: 장중 기동이 어제 EOD 스냅샷을 지금 잔고로 덮어 금일손익 분모가 흔들림. 장중에는 어제 파일을 갱신하지 않고, 당일 고정 기준은 `_day_pnl_baseline.json`에 남겨 재시작 후에도 유지 (`eod_equity_snapshot_service`, `session_open_day_pnl`).

## 0.18.32 (2026-08-20)

- **공식 15분 교체 UI 멈춤**: 슬롯 마감 ~2분 후(09:47·10:02) 전 종목 교체를 Tk `after(0)`에 올려 pandas 롤링·지표 재계산이 메인을 막음. 메모리 반영은 워커에서 한 종목씩 직렬 수행 (`rt_closed_15m_official_replace`, `rt_ui_responsiveness`, `k_trader_gui`).

## 0.18.31 (2026-08-20)

- **15분봉 마감 UI 멈춤 재발 방지**: 09:15 등 슬롯 마감 때 평가·롤 워커 8개가 pandas로 GIL을 점유하면 Tk 메인이 1분 가까이 after를 못 돌림. 워커는 설정과 무관하게 최대 2, 평가/롤 후 짧게 양보. UI 콜백·틱 큐는 한 틱 시간 예산을 넘기지 않음 (`rt_ui_responsiveness`, `closed_15m_fast_path`, `realtime_official_bars_sync`, `k_trader_gui`).

## 0.18.30 (2026-08-19)

- **같은 슬롯 S1 동시주문**: 백테스트가 15분봉 안에서 트랙 A를 먼저 사면 보유 동수가 바뀌어 트랙 B 동시신호가 TYPE_B로 뒤집히던 문제. 슬롯 시작 보유로 타입·동수를 판정한다 (`dynamic_backtest`). 실시간 봉 마감 평가와 맞춤.

## 0.18.29 (2026-08-19)

- **워밍업 신호상태 멈춤**: GS처럼 보유 종목이 `15분봉 API 1/40페이지`에 남는 문제. 워밍업은 끝났는데 `prep_ok`가 다음 종목 이벤트를 한 칸에 덮어 쓰여 화면만 진행 중으로 보였음. 완료/실패는 큐로 남기고, 장중 틱이 없어도 완료 문구가 유지되게 수정 (`warmup_stock_prep_ui_service`).

## 0.18.28 (2026-08-19)

- **공식 15분 이전 슬롯 정합**: ka10080 정렬교체가 성공할 때 같은 페이지의 이전 완성봉도 값이 다르면 덮어씀. 직전 슬롯 502 등으로 틱봉이 남아도 다음 조회에서 MA60을 맞춤. 형성 중 봉·이미 끝난 평가는 그대로 (`rt_closed_15m_official_replace`, `k_trader_gui`).

## 0.18.27 (2026-08-19)

- **터치호가 첫 체결확인 60초**: 40초 첫 `kt00007` 조회도 미반영이어서, 접수·정정 직후 첫 확인을 `touch_quote_first_confirm_sec` 기본 60초로 늘림. 미반영 재시도는 기존 30초 유지.

## 0.18.26 (2026-08-19)

- **터치호가 첫 체결확인 40초**: 접수 직후 30초 `kt00007` 조회는 거의 미반영이고, 다음 30초(총 60초)에야 체결이 잡히던 문제를 완화. 첫 확인만 `touch_quote_first_confirm_sec`(기본 40초), 미반영 재시도·정정 후 확인은 기존 `touch_quote_order_poll_sec`(30초) 유지 (`limit_order_watch_service`).

## 0.18.25 (2026-08-18)

- **휴장일 가짜 15분봉 제거**: 대체공휴일(예: 2026-08-17 광복절 대체)에 직전 종가·거래량 0 봉이 들어가 MA60이 백테스트와 어긋나 매수가 안 나던 문제. 슬롯 롤이 거래일만 건너뛰고, RT 15분 DB에서 비거래일 봉을 삭제 (`krx_bars`, `krx_trading_calendar`, `realtime_monitor_bars_store`).

## 0.18.24 (2026-08-18)

- **부분 틱 말분 평가 금지**: 장중 재시작으로 말분 틱이 중간부터면 그 틱으로 완성15를 평가하지 않음. 공식 말분 1분이 들어온 뒤 공식 1분만으로 합성·평가 (`realtime_official_bars_sync`, `rt_watchlist_m1_api_poller`).

## 0.18.23 (2026-08-18)

- **재시작 후 틱전용 15분봉 금지**: 장중 재시작 직후 M1 스토어에 틱만 있을 때 완성 15분을 틱만으로 합성·평가하지 않음. 워밍업 ka10080(1분)을 스토어에 선적재하고, 슬롯 앞구간에 공식 1분이 없으면 catchup까지 평가 보류 (`warmup_service`, `realtime_official_bars_sync`).

## 0.18.22 (2026-08-18)

- **전략유형 Type A가 A+B로 고정되던 문제**: 호텔신라·카카오페이처럼 Type A가 맞는데 첫 틱 A+B가 남던 표시를, A/B 확정 후 A+B로 되돌리지 않고 A+B→A/B만 허용하도록 수정 (`monitor_tab`).

## 0.18.21 (2026-08-18)

- **전략유형 A↔A+B 깜빡임**: 미체결·AB 선정 종목까지 매 틱 갱신되며 호텔신라·카카오페이처럼 전략 A와 A+B가 오가던 문제. 실제 보유(수량>0)이고 값이 전략 A/B일 때만 갱신 (`monitor_tab`, `trading_controller`).

## 0.18.20 (2026-08-18)

- **보유 전략유형 Type B 오표시**: 감시 시절 '전략 A'가 고정되거나, 동기화(SYNCED) 시 오늘 트랙 A가 매수 이력을 덮어 신호상태만 Type B로 보이던 문제 수정. 보유 행은 실제 체결 유형으로 갱신하고, 해당 종목 매수 이력을 선정 트랙보다 우선 (`monitor_tab`, `portfolio_manager`, `data_manager_v2`).

## 0.18.19 (2026-08-15)

- **볼린저 과열 필터(기본 OFF)**: 15분 종가 Z=(Close−MA20)/σ. Type A +2σ 차단·전 타입 +3σ 차단을 설정으로 켜 비교 가능 (`use_bb_block_a_2s`, `use_bb_block_all_3s`).

## 0.18.18 (2026-08-15)

- **종목시뮬 변동성 돌파**: 단일 종목 시뮬에서 기존 Type A/B 외에 래리 윌리엄스식 변동성 돌파(시가+전일레인지×K, 당일 종가 청산)를 선택 가능. K(0.01~2.0)와 고정밀(1분 최초 터치) 지원.

## 0.18.17 (2026-08-15)

- **장전 자동 감시 시작**: 앱이 켜져 있으면 `--auto-start` 없이도 거래일마다 **개장 25분 전**에 실시간 감시를 자동 시작. 수능·신년 등 특수 개장일도 세션 시각 기준 (`realtime_monitor_auto_start_minutes_before_open`).

## 0.18.16 (2026-08-14)

- **15:30 S2·청산도 S1과 동일 허용**: `next_open`/허용 플래그 시 고정밀·bt_hp EOD 스킵 해제. RT 틱·완성봉 held 경로에서 모의=익일예약·실전=NXT 분기 공통화 (`bt_hp_intrabar_unified`, `backtest_engine`, `signal_engine`, `signals.settings_allow_1530_signal_eval`).

## 0.18.15 (2026-08-14)

- **RT 15:30 S1 신호 허용**: `next_open_1530_enabled`(모의 익일예약) 또는 실전 NXT일 때 종가 단일가 슬롯 신호를 마스크하지 않음. BT와 동일하게 신호는 허용하고 체결만 예약/SOR로 분기 (`signals`, `signal_engine`).
- 15:30 슬롯 판별을 특수개장일 `close` 라벨 기준으로 정합 (`is_krx_last_15m_slot`).

## 0.18.14 (2026-08-14)

- **레짐 30일 이력 표 본문 미표시**: 전역 Treeview `foreground=""`(감시 탭 태그용) 때문에 태그 없는 이력 행 글자가 안 보이던 문제 — `RegimeHistory.Treeview` 전용 스타일·레짐 색 태그 적용 (`regime_history_dialog`).

## 0.18.13 (2026-08-14)

- **15:15 공식 정렬교체 지연 수정**: `continuous_end`(15:20) 이후 형성 슬롯을 15:30으로 두어 15:15 봉이 일반 슬롯처럼 완성 직후 롤·정렬교체(~+270s) 되게 함. 이전에는 15:30 롤까지 예약이 밀려 +860s 근처로 찍힘 (`krx_bars`).

## 0.18.12 (2026-08-14)

- **터치호가 주문 지연 완화**: 슬롯마다 전 종목 ka10004 prefetch를 제거하고, 매수·청산 확정 종목만 호가 1회 동기 조회. 주문 경로는 캐시 우선(`prefer_cache`)·TTL 15초 (`signal_engine`, `order_execution_service`, `defaults`).
- **ka10004 대기**: 전역 유량 대기를 락 밖에서 sleep 하도록 수정해 대기 직렬화가 덜 꼬이게 함 (`order_manager`).

## 0.18.11 (2026-08-14)

- **감시 시작 UI 프리징**: Transition 게이트 FDR(~200종 일봉) 조회를 메인 스레드에서 워밍업 백그라운드로 이동. 선정 직후(~15분) 레짐 결과는 재조회 생략 (`trading_controller`, `warmup_stream_prep_service`, `monitor_tab`).
- **주문·체결 날짜 자동 오늘**: 밤새 켜 두면 조회일이 어제로 남는 문제 — follow 모드에서 자정·체결 갱신 시 「오늘」로 맞춤. ◀/▶ 과거 조회 시 follow 해제 (`virtual_tab`).
- **체결 후 주문 트리 누락 완화**: 보유 구조 시그니처가 같아도 주문·체결 트리를 갱신. 체결 확정 경로에서도 주문 트리 갱신 요청 (`k_trader_gui`, `fill_confirmation_service`).

## 0.18.10 (2026-08-13)

- **Flash 고점 종가 통일(옵션3)**: 기본 `use_flash_peak_bar_high=false` — Flash/Smart Exit 고점을 종가·평가가만 누적. 봉 High 시드는 토글 ON일 때만 (`backtest_engine`, `dynamic_backtest`, `signal_engine`).
- **ATR 트레일 고점 분리**: `use_atr_peak_bar_high` — ATR 무장·트레일만 High, Flash는 종가 유지 가능. 기본 OFF (`defaults`, `settings_tab`).
- 저정밀 BT가 Flash 고점에 High를 안 넣어 RT와 어긋나던 경로 정리. 2026-01~08 비교에서 종가 고점이 High 대비 우위.

## 0.18.9 (2026-08-13)

- **ka10080 유량=1 정합**: 전역 최소 간격을 0.5→**1.05초**로 상향(클램프 하한 0.8). 429 직후 쿨다운·재시도 대기 강화 (`config`, `defaults`, `data_manager_v2`, `k_trader_gui`).
- **ka10080 통계 로그 스팸 제거**: UI 요약은 신규 FAIL 증가 시에만 출력. JSONL에는 구간 델타(`ok_delta`/`fail_delta`) 기록 (`ka10080_fetch_diag`).
- 설정 UI에 「ka10080 전역 간격」 항목 추가 (`settings_tab`).

## 0.18.8 (2026-08-13)

- **금일손익 N/A 고착 해소**: 장중·장마감 후 감시 시작도 N/A 고정 대신 직전 거래일 EOD 스냅→직전종가 합성 폴백. 기존 N/A 세션 자동 복구 (`session_open_day_pnl`, `virtual_tab`).
- **모바일 자산추이 복구**: `load_equity_snapshots_range`가 `date`와 파일명 문자열을 비교하다 TypeError로 빈 points를 반환하던 버그 수정. mock/live 후보 폴더 병합 (`equity_snapshots_store`, `mobile_web`).

## 0.18.7 (2026-08-12)

- **15:30→next_open 누락 방지**: 완성15 S1/held 평가를 봉·최소길이 준비 뒤에만 done 마킹. 조기 점유로 재시도가 막히던 버그 수정. 지표 실패 시 unmark (`signal_engine`).
- **15:30 진단 로그**: `auction_1530_scheduled`·`auction_1530_eval_final_fail`·`signal_s1_closed_15m_eval`·`next_open_buy_queued` 를 session JSONL에 기록 (`rt_watchlist_m1_api_poller`, `next_open_pending_action_service`).
- **틱 주경로·API miss 폴백**: 매분 틱 1분 seal, 합성15 시 공식 결측분을 틱으로 채움. head ka10080은 soft catchup(실패해도 평가 유지). 설정 토글 추가 (`rt_tick_m1_store`, `realtime_official_bars_sync`, `rt_watchlist_m1_api_poller`, `settings_tab`).

## 0.18.6 (2026-08-12)

- **ka10004 유량 대응**: 호가 조회 전역 직렬화(≈1.05s)·429 재시도(최대 4회). 청산 터치호가 실패 시 참고가 폴백으로 매도 생략 방지 (`order_manager`, `order_execution_service`).

## 0.18.5 (2026-08-12)

- **신호·주문 우선**: 말분 공식1분(`ka10080`)이 조용 구간을 우회하던 경로 제거. 틱말분/15:30 평가·`submit_eval`·터치호가 조회 시 공식 REST 양보. 조용 구간 기본 8초 (`closed_15m_fast_path`, `rt_watchlist_m1_api_poller`, `order_execution_service`).

## 0.18.4 (2026-08-11)

- **15:30 단일가 전 종목 평가 보장**: 틱/공식1분 미준비 시 재시도·최종 스윕·무거래 평봉 폴백. 미평가 종목만 15:31 공식1분으로 구조. 중간 합성 대기는 실패 집계에서 제외 (`rt_watchlist_m1_api_poller`).
- **모바일 API 로그 정리**: 원격 소켓 강제 끊김(`ConnectionResetError` 등) traceback이 터미널 로그와 섞이지 않도록 억제 (`status_api`).

## 0.18.3 (2026-08-10)

- **금일손익 「—」 고착 수정**: 자정에 날짜가 바뀌며 기준 상태가 리셋된 뒤, 감시가 켜져 있어도 `on_monitoring_started`가 다시 안 불려 `started_at`이 비는 문제. 날짜 롤오버 시 감시 중이면 금일손익 세션 재무장. 장마감 후 기준 없음 문구 명확화 (`session_open_day_pnl`).

## 0.18.2 (2026-08-10)

- **ka10080 실패 진단 보강**: `empty_response`·HTTP 429/기타 실패 시 `return_code`·`return_msg`·`body_bytes`·본문 키/미리보기를 session JSONL·대시보드에 기록 (`data_manager_v2`, `ka10080_fetch_diag`). 모의 API 빈응답 vs 초당 한도를 구분하기 위함.

## 0.18.1 (2026-08-09)

- **모바일 접속 PIN 간소화**: 긴 URL-safe 토큰 대신 **숫자 8자리** 생성. 로그인 숫자 키패드·공백 입력 허용.
- **설정 링크**: 대시보드「설정 링크」=`공개URL/#t=PIN` 복사 → 폰에서 붙여넣기만으로 로그인 (`mobile_web`).
- **운영 배포**: `deploy_production.py`가 `mobile_web/` 정적 파일을 함께 복사.

## 0.18.0 (2026-08-09)

- **모바일 읽기 전용 현황**: PC 로컬 HTTP(S) API + `mobile_web` PWA. 잔고·보유·감시·당일 체결·자산 추이 조회만 (주문 없음).
- **포트포워딩 전제**: `0.0.0.0` 바인딩, Bearer 토큰 로그인, 선택적 TLS. 설정·대시보드 URL 안내, `docs/mobile_status_portforward.md`.

## 0.17.4 (2026-08-09)

- **유니버스 갱신 주기 분기 고정**: `kospi_universe_refresh=quarterly` (설정·기본값). 백테·실시간 모두 달력 분기 첫 영업일 marcap as-of.
- **실시간 유니버스**: 월별 고정을 제거하고 설정 주기를 따름. 월별→분기 전환 시 as-of가 다르면 CSV 재동기화.
- **백테 레짐 기간 불변**: Lean 단일 유니버스 제거, 당일 유니버스 기준 Valid/RS·교차변동성 재동기화 (BT 기간에 따라 같은 날 레짐이 달라지던 문제).

## 0.17.3 (2026-08-07)

- **실시간 월별 유니버스 전월 고착 수정**: 당월 전환 시 `marcap` 연도 캐시가 오래되면 8월인데 as-of 7월 CSV로 폴백되던 문제. 실시간 갱신 전 연도 캐시 신선도 확인, 당월 as-of 불일치 캐시 폐기·경고 로그 추가.

## 0.17.2 (2026-08-07)

- **백테스트 자산추이 그래프 뒤집힘 수정**: 일 마감 equity를 `YYYY-MM-DD`만 기록해 시작점(`… 08:59:00`)보다 문자열/자정 정렬에서 앞에 오던 문제. 마감 시각(SessionHours close)을 포함해 기록하고, 리포트는 Timestamp 정렬 + 날짜-only 구 데이터는 장마감으로 보정.

## 0.17.1 (2026-08-06)

- **백테스트 SessionHours 반영**: `dynamic_backtest` / `backtest_engine`의 개장·종가·동시호가·익일 시가·1분봉 조회·갭유예가 일자별 `open`/`close`를 사용(신년·수능 등).
- **개장 슬롯 판별**: `is_krx_first_15m_slot` 추가. BT 시초 조기패스(`bt_open_auction_pass_enabled`)도 고정 09:00이 아닌 당일 개장 슬롯 기준.

## 0.17.0 (2026-08-06)

- **KRX 특수 개장시간 전체 대응**: 일자별 `SessionHours`(개장·동시호가·종가)를 단일 소스로 도입. 신년 첫 거래일·수능일 자동 규칙 + `krx_special_sessions.json` 오버라이드.
- **봉 격자·타이머·세션 판정**: `krx_bars` / NextOpen / 종가 동시호가 체결확인 / EOD·하베스트 / 금일손익이 당일 `open`·`close` 기준으로 동작. 미등록일은 기본 `09:00~15:30`.

## 0.16.17 (2026-08-05)

- **실전 NXT/SOR 마감 직후 주문**: `USE_MOCK=False` 일 때 15:15 슬롯(~15:20)은 동시호가 시장가 대신 NXT 1호가 터치+SOR, 15:30 슬롯은 익일09:00 예약 대신 NXT 즉시 주문. 모의 서버는 기존(15:15 시장가·15:30 next_open) 유지.
- **ka10004 NXT 호가**: `OrderManager.fetch_best_bid_ask_rest(..., venue="NXT")` — `{code}_NX` 매수1/매도1 조회.

## 0.16.16 (2026-08-05)

- **15:30 단일가 = 틱말분과 동일 경로**: 벽시계 15:30에 프로브 → 체결 틱 확인 즉시 완성15 평가 → (설정 시) 익일 09:00 주문예약. 무거래는 `realtime_auction_1530_tick_wait_sec`(기본 5초) 타임아웃 후 공식 1분 폴백.
- **15:30 평가 세션 가드**: next_open 활성 시 15:30 슬롯 평가는 당일 15:40까지 허용. 세션 체크를 done 마킹보다 먼저 수행해 마킹만 되고 예약이 스킵되는 경우를 방지.
- **15:31 공식 last 중복 생략**: 당일 15:30 틱경로를 이미 기동했으면 15:31 wake의 공식 말분 라운드를 건너뜀.

## 0.16.15 (2026-08-05)

- **동시호가·익일예약 상태 로그**: 15:15 동시호가 시장가(플래그·발송), 15:30→익일09:00 주문 예약/만료, 09:00 직전 제출 시도·성공·실패를 UI 로그로 명시.
- **15:15 동시호가 체결확인 시점**: 접수(~15:20) 직후가 아니라 **15:30:00부터** kt00007 폴링(30초 간격, 15:40까지). 15:20~15:30에는 조회하지 않음.

## 0.16.14 (2026-08-05)

- **감시 시작 UI 멈춤 완화**: 시작 버튼 경로의 감시목록 트리 전량 재구성을 다음 틱으로 미룸. 워밍업 `prep_progress` UI는 병합·0.35초 스로틀. STARTED 직후 트리 강제 렌더/inject 유예(약 2.5초).

## 0.16.13 (2026-08-05)

- **감시 합치도 표시 부하 완화**: 틱 경로 풀계산에 `realtime_s1_full_calc_min_interval_sec`를 실제로 적용(기존엔 설정만 읽고 미사용). 기본 1→5초, UI enqueue 0.2→0.5초. 주문 평가는 완성15 경로 그대로.

## 0.16.12 (2026-08-05)

- **M1 head 반영 로그 생략**: 슬롯 head 일괄 조회의 종목별 `head 반영 N분` UI 로그 제거(동작 유지).
- **정렬교체 로그**: 2.5초 디바운스 부분집계 제거. 슬롯에 예약한 종목이 **모두 끝난 뒤** `정렬교체 완료 N종목` 한 줄만 출력.

## 0.16.11 (2026-08-05)

- **공식 15분 정렬교체 스킵**: 전일 슬롯(예: 전일 15:30) 및 당일 첫 15분(09:00) 완성 전(`wall < 09:15`)에는 정렬교체를 하지 않음. 장시작 API·로그 부하 제거.
- **정렬교체 로그 집계**: 종목별 반복 대신 슬롯당 `정렬교체 완료 N종목` 한 줄로 요약.
- **M1 평봉 채움 로그 생략**: 무거래 평봉 채움은 동작만 유지하고 UI 로그는 남기지 않음.
- **M1/틱말분 완성15 평가**: 합치도(`official_replace_eval_eligible`)로 미보유를 스킵하지 않음. 감시 전 종목 평가(신호 없으면 매수 안 함은 그대로).
- **틱말분/완성15 평가 로그 집계**: 종목별 반복 대신 슬롯당 `평가 완료 n=N` 한 줄로 요약.

## 0.16.10 (2026-08-05)

- **M1 슬롯 일괄 조회**: 매분 ka10080 전종목 폴링 제거. 15분 슬롯 앞구간(보통 14봉, `15:15`는 4봉) 마감 시 1페이지로 head만 `RtTickM1Store` 반영.
- **말분**: 틱 누적 모드(기본)는 기존 seal→합성 평가. 공식 말분 모드·`15:30`만 말분 마감 후 1회 조회·평가. 이후 공식 15분 교체는 기존 정렬 경로.

## 0.16.9 (2026-08-04)

- **전략최적화 종목 풀**: 대표 10종 고정 대신 동적백테 선정 스케줄(A/B) 합집합을 Optuna 대상으로 사용(실패 시 기존 폴백).
- **최적화 ○/● 확대**: 설정 탭 `add_param_row` 수치·체크박스를 최적화 대상으로 자동 등록. 명시 탐색공간이 없으면 현재값 주변(또는 True/False) 폴백.
- **누락 수치 보완**: `s2_b_ma5_gap`, `ma_convergence`, ATR_SL 진입 유예(`atr_sl_entry_grace_*`) 및 S2-B 익절/손절/Flash 탐색공간 정합.

## 0.16.8 (2026-08-04)

- **설정 UI 복구**: `use_type_balance_gate` / `type_balance_a_pct` 를 설정 탭 「자금 · 사이징」에 다시 노출.
- **운영 배포 묶음**: 0.16.5~0.16.7(15:15 동시호가, 15:30 익일 시초 이월, B_SOFT_CAP) GitHub·운영(`c:\k-trader`) 반영.

## 0.16.7 (2026-08-04)

- **B_SOFT_CAP 채택**: Type B 과다 노출로 약해진 2024 수익을 완화. `max_slots_b=10`, `transition_soft_slots_b=4`, `use_type_balance_gate=true`, `type_balance_a_pct=65`. 스모크 기준 2024 +3.7%→+12.2%, 전체 +180%→+215%.

## 0.16.6 (2026-08-04)

- **15:30 익일 시초 이월 옵션**: `15:30` 완성봉의 `S1/S2/EXIT` 신호를 즉시 체결하지 않고 익일 장 시작 직전 동시호가 지정가 주문으로 이월하는 공통 옵션 `next_open_1530_enabled` 추가. 지정가는 15:30 평가 기준가격 그대로 사용하고, 체결가는 시초가에 맡긴다.
- **BT/RT 공통 reason 태그**: 익일 시초 이월 경로는 `NEXT_OPEN_0900|src=15:30` 태그를 사용해 거래 추적·검증이 가능하도록 통일.
- **RT 상태 복구**: 익일 시초 이월 대기 액션을 JSON으로 저장·복구하고, 앱 시작 후 자동 스케줄러로 제출하도록 연결.

## 0.16.5 (2026-08-04)

- **15:15 동시호가 시장가→15:30 종가**: 15:15 완성봉은 S1·S2·청산을 평가하되 체결은 당일 15:30 Close(단일가). 기록 시각 15:30, reason `AUCTION_MKT|src=15:15`. 15:30 슬롯 평가는 계속 스킵.
- **BT**: Dynamic/Simple/HP 인트라바에서 15:15 연속분 체결 금지, 15:30 Close 1회 체결(없으면 스킵).
- **RT**: 15:15 경로는 시장가(03), 터치 정정·미체결 취소 없음, 라이브가 게이트 스킵, kt00007 폴링은 15:40까지.

## 0.16.4 (2026-08-04)

- **EOD 슬롯 BT 청산 스킵 정합**: `15:15`/`15:30`에서 백테스트·동적·통일 인트라바도 청산까지 생략(RT와 동일). `max_p`만 갱신.

## 0.16.3 (2026-08-04)

- **EOD 슬롯 신호평가 스킵**: `15:15`/`15:30` 완성봉은 S1·S2·완성봉 청산 콜백을 하지 않음(동시호가·단일가에서 터치지정가 체결 불가). 봉 갱신·DB는 유지. BT/동적도 S1·S2 동일 스킵.
- **틱→1분 검증 DB 저장 OFF**: `realtime_tick_m1_accumulate_save_only` 기본 false(신호 경로는 인메모리 `RtTickM1Store`).
- **marcap parquet 안내**: fastparquet/pyarrow·32bit 환경 오류 메시지 보강(`.venv`/`run_k_trader.bat` 유도).

## 0.16.2 (2026-08-04)

- **공식14+틱1 합성 평가**: 슬롯 앞 구간은 공식 1분, 말분은 0B fid20 체결시각 틱 1분으로 15분 합성 후 평가.
- **`:00` 틱 즉시 평가**: 다음 분 첫 체결 시 말분 seal·평가. 무거래는 `realtime_15m_tick_tail_wait_sec`(기본 1.5s) 타임아웃.
- **설정**: 틱 말분 합성 ON / 공식 말분 1분 평가 OFF. M1 API 폴러는 UI 비노출(항상 수집용 ON).
- **검증용**: PING 에코·fid20 누적 헤드리스 비교(`tools/headless_tick_1m_vs_official.py`). DEFAULT_SETTINGS를 settings와 동기화.

## 0.16.1 (2026-08-03)

- **설정 UI 정리**: 불필요해진 `공식15분 교체 후 평가`·대기/폴링/합치도/슬롯감지 UI 제거.
- **정렬용 공식15 교체**: M1 평가 모드에서 슬롯 완성 후 ~2분 고정 지연 + 소수 회 조회. 조용 구간 양보로 M1 프로브/평가 REST 비간섭.
- **DEFAULT_SETTINGS 동기화**: 현재 `settings.json` 저장값을 코드 디폴트로 반영(수수료·BT기간·모의계좌 등 환경키 제외).

## 0.16.0 (2026-08-03)

- **공식 1분 기반 RT 평가 복원**: M1 API 폴러·`RtTickM1Store`·1분 합성 15분으로 완성봉 평가. 공식 15분 대기 평가는 기본 OFF.
- **마지막 1분 우선 평가**: 슬롯 꼬리 1분 완성 시 보유·고 met_ratio 우선 조회·`submit_eval`. 라이브 확인 게이트 유지.
- **프로브 개선**: 상위 후보 복수 종목으로 시장 API 준비만 짧게 확인(기본 2.5초). 한 종목 무거래에 전체 대기하지 않음. 종목별 not_ready 재폴링·무거래 평봉.
- **검증용 틱→1분 누적**: `RtTickM1Accumulator` + 전용 DB(`realtime_1m_tick_*.db`). 비차단 큐·전용 워커·분 경계 직후 seal defer로 신호/프로브와 격리.

## 0.15.25 (2026-08-03)

- **BT 다음1분 종가 게이트 제거**: 저정밀 백테 S1·S2·EXIT에서 15분 판정 후 다음 1분 종가 확인·체결 로직 삭제. 15분봉 종가로 즉시 체결. RT 라이브가 게이트는 유지.

## 0.15.24 (2026-08-03)

- **15분 라벨=키움 공식(좌측)**: 1m→15m 합성을 `[T, T+15)` 시작 라벨로 통일. `09:00` 봉 포함, `15:15`=연속 잔여, `15:30`=단일가. RT 슬롯 롤·공식교체 대기·워밍업 진행중 보강도 동일 규약.

## 0.15.23 (2026-08-03)

- **1분→15분 합성(타임스탬프 창)**: 개수 고정이 아니라 구간으로 집계. 일반 슬롯 `[T-15, T)`, 09:15는 `09:00~09:14`, 마지막 15:30은 `15:15~15:19` + `15:30` 단일가. `utils/krx_bars.py` 및 BT/워밍업/리포트 경로 정합.
- **기동 Tk 크래시 수정**: 인증 워커 등 백그라운드에서 `root.after`/위젯을 직접 호출하던 경로를 `run_on_ui_thread` 큐로 메인 루프에 위임. `refresh_portfolio_light: main thread is not in main loop` 후 종료 현상 완화.

## 0.15.22 (2026-08-03)

- **확인 게이트 S1·S2·EXIT 공통**: 완성 15분 판정 후 주문 직전 확인가 게이트를 S2·청산까지 확장. RT=라이브가, BT 저정밀=다음 1분 종가(동일 chase/fade/MA20). 통과 시 확인가로 체결, 실패 시 스킵.
- **공유 모듈**: `strategy/s1_confirm_gate.py` — RT·Simple/Dynamic BT가 동일 규칙 사용.
- **설정 UI**: 라이브/BT 게이트 문구를 S1·S2·EXIT 공통으로 정리.

## 0.15.21 (2026-08-03)

- **저정밀 전용 단순화**: `RtTickM1Store`·M1 API 폴러·틱→15분 누적·마지막1분 틱 보정 제거. 완성봉 권위는 공식 15분 교체만.
- **S1 라이브가 주문 게이트**: 완성봉으로 신호 판정 후, 주문 직전 라이브가 추격(0.8%)/이탈(1.0%)·MA20 정합 확인. 실패 시 쿨다운 미소모.
- **공식 15분 대기**: 슬롯 후 폴링 기본 75초·간격 1.5초(실측 ~50초 지연 반영).
- **매도 체결 후 잔고 잔상**: exit_pending·포스트필 동기화로 보유 고스트 완화(`reconcile_exit_pending_max_seconds`).

## 0.15.20 (2026-08-03)

- **선정 0건 CSV 잔존 수정**: 오늘 선정 0(HARD 등)인데 이전 일자 `selected_targets.csv`가 남아 18종 등이 다시 올라가던 문제 수정. 0건 시 CSV 비움·보유만 `_start_held_positions_only`(옛 CSV 미사용).
- **선정 대기 재클릭**: 진행 중 재시도 시 옛 CSV로 시작하지 않고 보유만 즉시 감시.

## 0.15.19 (2026-08-02)

- **감시 시작**: 선정 0건(또는 선정 실패)이어도 **보유 포지션이 있으면** 보유 청산·S2 감시로 실시간 감시 시작. HARD(붕괴)는 S1·S2만 차단, EXIT 유지.
- **marcap parquet**: `pyarrow` DLL 차단 시 `fastparquet` 폴백. `requirements.txt`에 fastparquet 추가.

## 0.15.18 (2026-08-02)

- **연속감시**: 장마감(~16:00) 선정 실패 시 다음 거래일 장전(기본 08:50)에 **강제 재선정** 후 감시 시작. 실패 시 EOD 전까지 주기 재시도.

## 0.15.17 (2026-08-02)

- **틱→15분 전체 누적(검증용)**: `RtTick15mAccumulator` — 0B 틱으로 슬롯 전체 OHLCV 누적 후 **전용 DB**(`realtime_15m_tick_*.db`)에만 저장. 신호·주문·`bars_data` 미사용.
- **매매 경로 격리**: 디스패치는 `offer_tick`(큐 put만), 누적·seal·SQLite는 전용 스레드. 공식 15분 슬롯 watcher와 분리.
- 설정: `realtime_tick_15m_accumulate_save_only`(기본 ON). 설정 탭 토글 추가.

## 0.15.16 (2026-08-01)

- **레짐 표시 일자**: 헤더·「30일」이력 모두 **적용일(다음 영업일)** 기준. 판정은 완료 거래일(`data_as_of`) 유지.
- **HARD 상승일 억제**(실험·기본 OFF): `hard_suppress_on_index_up` / `hard_suppress_to_off_only`. FULL+OOS에서 미채택 → 기본 비활성.

## 0.15.15 (2026-07-31)

- **하베스팅 15분봉**: `bars_15m`을 1m→15m 리샘플 대신 `ka10080(tic_scope=15)` 공식봉 직접 조회로 저장(RT·BT 봉 소스 정합).
- **실시간 완성 15분**: M1 API 폴러 기본 OFF, 공식 15분 교체 후 S1/보유평가. 신호근접·보유 우선 조회·백필.
- **BT 유니버스 워밍업**: 단일일 백테도 `prepare` 선정창을 ref_day 이전 20영업일까지 확장 — 짧은 창으로 001120 등이 끼어들던 괴리 수정(`bt_universe_schedule_warmup_trading_days`).

## 0.15.14 (2026-07-30)

- **기본값 동기화**: `s2_profit_limit` / `s2_b_profit_limit` **1.5 → 1.0**. 백테 기간 기본 `2024-06-03`~`2026-07-27`.
- prefer_track 재튜닝 Phase3에서 채택. GUI FULL 재현 확인(총수익 약 +202%, MDD 약 −10.9%).

## 0.15.13 (2026-07-30)

- **동시신호 초안**: `confluence` 제거, `prefer_track`(선정 트랙 일치) 추가. 구 `confluence` 설정은 `prefer_a`로 읽음.
- **설정 UI**: 실험용 토글 제거(트랙 B만 동시신호 허용 · TYPE_A 차단 · 전면 차단). 기본은 OFF 유지.
- 동시신호 A만/전면 차단·트랙 B 허용 FULL+OOS 비교 → OOS 이점 없음 또는 MDD 악화 → **현행(prefer_a + 동수 B) 유지**.

## 0.15.12 (2026-07-30)

- **트랙 불일치 가시화**: S1 매수 스킵 시 `signal_buy_skipped(track_signal_mismatch)` 세션 로그. 감시 표시는 `타점(A) · 트랙B 제외` 형태.
- **동시신호 타입 확정**: `same_bar_sig_prefer_sel_track`(트랙으로 바로 확정), `s1_simul_balance_tie_mode`(always/empty_only/when_held/off). 기본은 현행(`prefer` OFF · `always`).
- FULL+OOS: 트랙 우선·타이브레이크 완화는 OOS 개선 없음 → **기본값 유지**. `empty_only`는 OOS 동일·FULL −3.7%p.
- **설정 UI**: 동시신호 확정을 ①초안 → ②동수면 B로 덮기 → ③트랙으로 바로 확정 순서로 표기.

## 0.15.11 (2026-07-29)

- **레짐 표시**: 실시간·이력은 **완료 거래일** 기준(`completed_trading_day_asof_local`). 장중 미완성 당일 일봉으로 HARD↔OFF가 뒤집히지 않음.
- **레짐 이력 30일**: 표시 일자를 로컬 `bars_1d`가 아니라 FDR·KRX 일자로 — DB 하베스트 지연으로 마지막 날이 밀리던 문제 수정.
- **Tiered 응답 UI/키 제거**: TRANSITION 시 S1/S2 차단 토글이 그대로 적용. (슬롯 축소는 soft slots 유지)
- 응답축(슬롯·차단·무시) FULL+OOS 스윕 → **현행 soft8·차단없음 유지**.

## 0.15.10 (2026-07-29)

- **설정 UI**: 유니버스 표시명 `KOSPI200` → `KOSPI`. KODEX200 선택 시 상위 N(150/200/250/300) 비활성·흐리게.
- **설정 UI**: 「감시 선정: 최소점수·개수상한·순위밴드」 블록 제거 (로직 키는 유지, 기본값으로 사실상 비활성).
- **DD 연동 리스크 제거**: 백테에서도 미체감(트리거 미도달)이었고 실전 미연동 → UI·로직·설정 키 전부 삭제.

## 0.15.9 (2026-07-28)

- **유니버스 갱신 주기**: 백테에서 monthly/weekly/biweekly/daily/bimonthly/quarterly 비교 가능. 스윕 결과 **monthly 유지** (FULL·OOS 모두 최선).
- **실시간도 monthly 고정**: `kospi_universe.csv`를 당월 첫 영업일 marcap as-of로 갱신(월 중 일별 재갱신 없음). 실시간 전용 갱신주기 설정 없음.
- 비교 스크립트: `tools/compare_k200_univ_refresh.py`.

## 0.15.8 (2026-07-28)

- **기본 설정 튜닝 반영 (K200 백테 1~5차)**: 유니버스 `kospi_top_n=200`, Type B 갭다운 `gap_down_limit_b=-2.5`. S2·selection·DD sizing·trail은 현행 유지가 최적으로 확인됨.
- 디폴트(`core/defaults.py`)도 동일하게 동기화.
- 비교 스크립트: `tools/compare_kospi_univ_ladder.py`, `compare_k200_*_tuning.py`.

## 0.15.7 (2026-07-28)

- **실시간 KOSPI 유니버스 일일 갱신**: 감시목록 선정·`get_kospi_list` 시 marcap 최신 거래일 as-of로 `kospi_universe.csv` 자동 갱신(같은 as-of면 스킵). 올해 parquet 캐시 3일 TTL 재다운로드.
- `kospi_universe.csv`에 `asof_date` 컬럼 저장.

## 0.15.6 (2026-07-28)

- **백테 KOSPI 유니버스 PIT(과거 시점 시총)**: FinanceData marcap 연도별 parquet → 월 첫 영업일 as-of 시총 상위 N. FDR `StockListing` 현재 시총 스냅샷 대신 사용.
- 로컬 캐시: `data/marcap/`, `data/universe_marcap_monthly/`. 실시간 `kospi_universe.csv` 갱신도 marcap 최신 as-of.
- 백테 경로에서 FDR로 `kospi_universe.csv` 자동 재생성 차단 (`allow_fdr_generate=False`).
- 의존성: `pyarrow` (marcap parquet 읽기).

## 0.15.5 (2026-07-28)

- **공장 초기화 버그 수정**: 디폴트를 메모리에만 넣고 `reset_ui_tabs`가 기존 `settings.json`을 다시 읽어 덮어쓰던 문제. 이제 `DEFAULT_SETTINGS`를 저장한 뒤 UI를 재구성한다.
- **설정 자동저장 덮어쓰기 수정**: 백테스트/최적화 시작 시 `_final_save_settings`가 설정 탭 UI를 무시하고 메모리만 저장해, 이전에 저장한 값(예: 전환 게이트 ON)을 OFF로 덮어쓰던 문제. 저장 전 UI→메모리 동기화.

## 0.15.4 (2026-07-28)

- 설정 탭 포지션 사이징: **금액(원) 단위 지정 제거** — 1·2단계 비중은 총자산 대비 **%만** 지원. 실전·백테·리포트 경로도 % 고정.

## 0.15.3 (2026-07-28)

- **디폴트 설정**을 현재 `settings.json`과 동기화: 유니버스 `KOSPI200`·상위 `150`, `use_transition_gate=True` (`tools/sync_defaults_from_settings.py`).

## 0.15.2 (2026-07-28)

- 포지션 관리 상단 **「누적 수익(종료+보유)」** 표시 제거.

## 0.15.1 (2026-07-28)

포지션 관리 「금일 손익」 정의를 장시작 총자산 대비로 변경.

- **금일 손익** = 현재 총자산 − **시초가 반영 전** 총자산 (`utils/session_open_day_pnl.py`).
- 장전 감시 중 총자산을 갱신하다 **09:00에 고정**(당일 시초가가 기준에 섞이지 않음).
- 장전 스냅을 못 잡으면 보유 **직전 종가** 합성 총자산으로 폴백.
- **장중·장마감 후** 감시 시작 시 `N/A` 표시.
- 라벨: 「금일 손익 (장시작 기준)」.

## 0.15.0 (2026-07-28)

실시간 감시·연속 스케줄 통합 및 일일 선정 자동 연동.

- **UI**: 「연속 감시」 버튼 제거 — 실시간 감시 시작 시 EOD/장전 스케줄 자동 연동.
- **스케줄**: 장마감 선정 **16:00**(버퍼 30분), 감시 자동 시작 **08:50** (`realtime_monitor_auto_start_hhmm`).
- **EOD**: 16:00에 중단→일일 선정만 수행(즉시 재시작 없음) → 다음날/당일 08:50 시작.
- **신선도**: 재시작·감시 시작 시 `selected_targets.csv` 만료면 일일 선정 후 시작 (`selection_freshness`).
- **0건 가드**: 선정 결과 0종목이면 감시 시작 건너뜀.

## 0.14.0 (2026-07-28)

실시간 15분봉 마감 직후 신호 평가·주문 지연 최소화.

- **슬롯 감지**: 벽시계 경계 exact wake + 경계 근처 고주기 폴링 (`seconds_until_next_krx_15m_boundary`).
- **병렬 롤·락 분리**: 종목 병렬 슬롯 롤, compose/콜백을 `_bars_data_lock` 밖에서 수행.
- **평가 워커풀·비동기 DB**: `Closed15mEvalPool` / `Closed15mDbUpsertQueue` (`core/closed_15m_fast_path.py`).
- **주문 패스트패스**: Tk 메인 큐 우회 → `OrderActionDispatcher`(closed-15m 쿨다운 완화·워커 확대).
- **touch 호가**: ka10004 캐시 + 지표 계산과 병렬 prefetch.
- **공식 REST 후순위**: 슬롯 확정 후 `realtime_closed_15m_defer_official_sec`(기본 4초) 동안 M1 폴링·형성봉 sync·15분 교체 연기.

## 0.13.3 (2026-07-27)

레짐 계산 FDR 전환, 모의 예수금 파싱, 운영 배포 보조.

- **레짐 FDR**: 실시간·30일 이력 레짐을 `bars_1d` DB 대신 FDR 일봉 병렬 조회로 계산 (`_fetch_daily_ohlcv_batch_fdr`, `_compute_transition_regime_map_fdr`).
- **예수금상세(kt00001)**: 모의 SDK 우선·루트/output 병합 파싱·`cont-yn` 헤더 — D+1/D+2 파싱 실패 경고 완화.
- **운영 배포**: `tools/deploy_production.py`, `install_k_trader.bat`, `requirements.txt` (kiwoom-rest-api, websockets).

## 0.13.2 (2026-07-27)

GUI 헤더 B 추세 레짐 표시 및 최근 영업일 이력.

- **헤더 중앙 레짐 표시**: `RegimeDisplayService` — 기동 시·장마감+30분 후 갱신, 유지/약화/붕괴 색상 구분.
- **「30일」 이력 버튼**: 최근 ~22영업일 일별 레짐 막대 타임라인 + 표 (`ui/regime_history_dialog.py`).
- **`compute_transition_regime_history`**: `prepare_dynamic_universe`와 동일 분류로 이력 일괄 산출.

## 0.13.1 (2026-07-27)

브레드스 레짐 제거, TRANS_S8_NS2 전환 게이트 반영.

- **레짐 전환 게이트**: Valid B-share 급락 + 변동성 확인 → TRANSITION(슬롯 축소) / HARD(chop·S1·S2 차단). `strategy_selector`·`dynamic_backtest`·실시간 선정 공통.
- **TRANS_S8_NS2 디폴트**: soft 슬롯 8/8, TRANSITION 시 S2 유지(`transition_block_s2_on_soft` OFF), chop HARD 임계 사전 설정.
- **GUI 설정 탭**: 브레드스 게이트 UI 제거 → 「레짐 전환 게이트」LabelFrame 및 파라미터 토글.
- **런타임**: `transition_runtime_block_s1/s2` — 실시간 S1 매수·S2 추매 차단 (`signal_callbacks_service`, `strategy_engine`).
- **제거**: 브레드스/chop/stress 독립 게이트 및 `breadth_runtime_sig_priority` 동시신호 우선순위.

## 0.13.0 (2026-07-27)

실시간 S1 주문·15분 슬롯 롤 타이밍 수정, 선정 Score 모드 확장.

- **S1 주문 파이프라인**: 15분 확정 평가 후 `last_detailed_sig`·메인 액션 폴러 깨우기. 캐시 폴백·`signal_buy_skipped`/`strategy_action_skipped` JSONL.
- **15분 슬롯 롤**: 워처 1초·감시 전 종목·큐 적체 시에도 동기 롤. 틱 경로 `try_roll_15m_slot_on_tick` (`realtime_15m_slot_watch_sec`).
- **`sel_score_mode`**: `legacy` / `fwd_aligned_v1` / `s1_pnl_aligned_v1` / `b_s1_pnl_aligned_v1` — S1 손익 정렬 선정 스코어.
- **`enforce_track_signal_match`**: Track·Sig_Type 교차 거절 토글(기본 ON). `TradingStrategy.track_signal_match_allows` 공용화.
- 비교·진단 스크립트: `tools/compare_score_modes*.py`, `compare_s1_pnl_rank.py`, `diagnose_type_*_exit_risk.py` 등.

## 0.12.0 (2026-07-27)

실시간 S1/S2/청산을 저정밀(완성 15분) 백테와 정합.

- **`realtime_s1_eval_on_local_15m_close`(기본 ON)**: 로컬 15분 슬롯 확정 시 S1·S2·청산 평가. 틱 주기 EXIT/STAGE2 주문 생략. 공식 15분 REST 교체는 위생용(신호 비차단).
- **`realtime_15m_last_minute_from_ticks`(기본 ON)**: 슬롯 앞 구간은 공식 1분, **마지막 1분만 틱** 합성 — 슬롯 직후 지연·누락 완화.
- 인트라바/close-now S1 미러 기본 OFF. 설정 탭 「③ Stage 1」 토글·현재 `settings.json`→`defaults` 동기화.
- S1/S2 비중 그리드 비교 스크립트(`tools/compare_s1_s2_sizing.py`) — 현행 20%/25%가 총수익 최적.

## 0.11.0 (2026-07-26)

트레일 B 완화·DD 연동 사이징 반영, 쿨다운/갭/연속SL 비교 검증.

- **디폴트**: `atr_multiplier_trail_b` 5→**6**(A는 5 유지). `use_dd_sizing` ON · 임계 15% · 금액×0.5 · 슬롯 유지.
- **DD 사이징**: 고점 대비 낙폭 시 S1/S2 금액(및 선택적 슬롯) 축소 — `dynamic_backtest` + 설정 탭 「① 자금」.
- **연속 ATR_SL 쿨다운**: 조회기간 내 2회 이상이면 연장 가능(`atr_sl_streak_*`). 비교 결과 이득 없어 기본 OFF(0).
- **검증**: 쿨다운×갭·연속SL·트레일·DD 비교 스크립트(`tools/compare_*.py`). 갭/연속SL은 현행 유지가 우위, 트레일 B=6·DD15×0.5는 수익 개선.

## 0.10.0 (2026-07-26)

ATR_SL 재진입 쿨다운·브레드스 레짐 슬롯·전략 디폴트 동기화.

- **`atr_sl_reentry_cooldown_days`(기본 3)**: ATR_SL 청산 후 동일 종목 S1 신규매수를 N일(달력일) 차단. 동적 백테·실시간 `SignalEngine`·설정 탭 「⑥ 청산」에 반영.
- **ATR_SL 진입 유예**: `atr_sl_entry_grace_hours` / `atr_sl_entry_grace_mult` / `atr_sl_entry_grace_same_day` — 진입 직후 조기 ATR_SL 완화(`backtest_engine.evaluate_position`).
- **브레드스 레짐 정책**: UP/NEUTRAL별 A·B 슬롯·신호 우선순위(`prefer_b`/`confluence`), DOWN 차단. `strategy_selector.resolve_breadth_regime_policy` → 실시간 선정·`prepare_dynamic_universe`·동적 백테 `same_bar` 연동. 설정 UI 확장.
- **디폴트**: 현재 `settings.json`(Type B·A SL×2.8·TP 6%·브레드스 OFF·재진입 3일 등)을 `core/defaults.py`에 동기화.

## 0.9.6 (2026-07-24)

고정밀(RT정합 OFF) S1: 15분 Signal은 속도 게이트만, 매수는 close-now.

- **의도 복구**: 완성 15분 `Signal=False`면 1분 루프 생략(속도). `Signal=True`여도 완성봉 Signal을 분에 정적 복사하지 않고 `evaluate_bt_hp_intrabar_unified`(close-now)로 재판정.
- **구현**: 고정밀·RT정합 모두 `_process_intra_bar_rt_parity` 사용. 차이는 대상 집합만(보유·Signal 후보 vs 전 감시). 고속 하이브리드는 RT정합 ON일 때만.
- **효과**: 구형 `1M_SIG+0m` 조기매수(미래참조) 제거. 예: 현대차 2024-06-18 09:30봉 Track B는 close-now TYPE_B 시점(`+9m`)에 체결.

## 0.9.5 (2026-07-24)

성과 검증 백테에 저정밀(15분) 모드 UI 복구.

- **`bt_high_precision`**(기본 true): OFF면 1분 intrabar 없이 15분봉만 매매·청산(기존 엔진 `high_precision=False` 경로).
- **`ui/tabs/backtest_tab.py`**: 「고정밀(1분)」 ON/OFF 토글. OFF 시 RT정합·고속 토글 비활성. 「상시 적용」 안내 문구 제거.
- 상태 표시: `저정밀` / `고정밀` / `RT정합(고속)`.

## 0.9.4 (2026-07-24)

RT정합 매수 0건 버그 — close-now Ready 오판 및 OSC 고속필터 과차단.

- **원인1**: `evaluate_close_now_s1_at_m1_idx`가 `get_buy_signals_detailed` 결과에서 Ready를 읽었으나 해당 API는 Ready 열을 반환하지 않음 → Ready 항상 False → `can_execute` 전면 실패(완성봉 Signal=True여도 매수 0).
- **원인2**: 고속 하이브리드 `bt_rt_parity_fast_osc_abs_max=8`이 가격 스케일 OSC(수백~수천)를 거의 전부 스킵.
- **수정**: Ready는 `_apply_ready_column_to_df`가 붙인 `work["Ready"]`를 사용. OSC 절대값 게이트 기본 비활성(0). RSI 밴드 사전필터만 유지.
- **참고**: 기존 고정밀 `1M_SIG+0m`은 완성 15분 Signal을 슬롯 첫 분에 정적 복사하는 구조라 RT close-now와 건수가 다를 수 있음(미래참조성).

## 0.9.3 (2026-07-24)

RT정합 백테 고속 하이브리드 — close-now 병목 완화(실시간 미러 무영향).

- **원인**: RT정합은 전 종목×매 1분 `get_buy_signals_detailed`(close-now)가 지배적 비용.
- **`bt_rt_parity_fast`(기본 true)**: 백테 RT정합에서만 S1 close-now 전에 넓은 RSI/OSC 사전필터 + 미체결 시 RSI/OSC 변화량 스로틀. `evaluate_close_now_s1_at_m1_idx`에 `precomputed_sim`으로 지표 중복 계산 제거.
- **`ui/tabs/backtest_tab.py`**: 「고속 하이브리드」 체크(RT정합 ON일 때만 활성). 해제 시 완전 정합(느림). RT정합·고속 옵션은 다크 테마에서 구분이 쉬운 **초록 ON / 회색 OFF 토글 버튼**으로 표시.
- 실시간 `SignalEngine`은 `close_now_fast_skip=False` 유지.

## 0.9.2 (2026-07-24)

동적 백테에 RT 정합(전종목 1분) 선택 옵션 추가 — 기존 고정밀과 속도·체결 비교용.

- **`bt_rt_parity_intrabar`**(기본 false): ON이면 감시 종목 전부를 매 15분 슬롯마다 `evaluate_bt_hp_intrabar_unified`로 평가(실시간 미러와 동일). OFF면 기존처럼 보유·15분 Signal 종목만 `_process_intra_bar_dynamic`.
- **`dynamic_backtest.py`**: `_process_intra_bar_rt_parity` 신설, `last_run_elapsed_sec`·모드 로그.
- **`ui/tabs/backtest_tab.py`**: 「RT정합(전종목1분)」 체크박스, 완료 상태에 모드·소요 초 표시.
- Bayesian 최적화 경로는 변경 없음(느린 RT정합 미적용).

## 0.9.1 (2026-07-24)

M1 폴러를 정규장 연속매매 1분 격자에서만 동작하도록 복구.

- **원인**: 장전(예: 07:54)에도 `RtWatchlistM1ApiPoller`가 존재하지 않는 1분봉을 ka10080으로 조회 → 모의 API `empty_response`(HTTP 200 빈 바디) 1건 후, 실패 통계 요약이 종일 반복 표시. CHANGELOG상 장전·장후 생략 가드가 있었으나 `_wake_loop`에 세션 가드가 빠져 있었음.
- **`core/rt_watchlist_m1_api_poller.py`**: `is_krx_m1_closed_open_label`(평일 09:00~15:19)로 `_wake_loop`·프로브·종목 조회를 게이트. 장전·장후·주말 분은 REST 자체를 생략.

## 0.9.0 (2026-07-23)

감시종목 선정 품질 개선 — 시장 브레드스 게이트 신설 및 선정 필터 복구.

- **원인**: 실시간 감시 승률이 매우 낮았고(모의 기준 16% 수준·손절의 45%가 진입 15분 내 발생), 근본 원인은 (1) 약세장에서도 롱 진입을 무차별 허용(시장 레짐 필터 부재), (2) Track B 상대강도 필터 사실상 비활성(`sel_b_rs_rank_min=0.0`)·RSI 상한 밴드/캡 미적용(`buy_rsi_max_a/b=100`)·유동성/거래량 폭발 기준 과완화였음. FDR(실시간) vs DB(백테스트) 선정 겹침률은 100%로 데이터 소스 불일치는 아님을 확인.
- **`strategy_selector.py`**: 지수 재도입 없이 유니버스 자체의 MA20 상회 비율로 시장 폭(브레드스)을 산출하는 경량 게이트 신설.
  - `compute_market_breadth()` / `classify_breadth_regime()` 공용 헬퍼 추가(실시간·백테스트 대칭).
  - `_apply_breadth_gate_to_df()` — DOWN 레짐 시 진입 차단, NEUTRAL 시 감시 슬롯 축소·Track B 선택적 차단.
  - `select_monitoring_targets()`(실시간)·`prepare_dynamic_universe()`(백테스트 일별 스케줄)에 게이트 적용.
- **`settings.json`·`core/defaults.py`**: 브레드스 게이트 키 신설(`use_market_breadth_gate`, `breadth_down_pct=0.30`, `breadth_up_pct`, `breadth_neutral_block_track_b`, `breadth_neutral_slot_scale`). 선정 필터 복구/강화(`sel_b_rs_rank_min` 0.0→0.45, `buy_rsi_max_a` 72, `buy_rsi_max_b` 80, `sel_b_rsi_max` 78, `sel_a/b_require_sel_rsi_band` true, `min_trde_val` 30→60, `vol_ratio` 1.15→1.5, `vol_lookback` 3→5).
- **`ui/tabs/settings_tab.py`**: 설정 탭에 「시장 브레드스 게이트」 섹션 추가 — 게이트 on/off·Track B 차단·`breadth_down_pct`/`breadth_up_pct`/`breadth_neutral_slot_scale` 입력 위젯.

## 0.8.78 (2026-07-10)

KB금융 등 Stage 2(추매) 중복 주문 방지.

- **원인**: 모의투자 RC4033(미체결 없음=전량체결) 시 매수는 `confirm_after_no_open_qty` 에서 즉시 신뢰 반영이 없고 kt00007 지연만 재시도 → `on_fill` 미호출·`stage` 1 유지. ka10085 잔고 동기화는 수량만 갱신. `events=[]` DB 복구 포지션은 S2 체결도 S1 리셋 분기로 빠질 수 있음. 7/9·7/10 KB금융 S2 주문(0147053·0014890)이 이 경로로 stage 미갱신 후 재추매.
- **`core/execution/fill_confirmation_service.py`**: RC4033·no_open_qty 시 **매수도** `apply_trusted_fill_on_no_open_qty` 즉시 반영(매도와 동일).
- **`core/portfolio_manager.py`**: `position_effective_stage`·S2 reason 판별·`on_fill` S2 전용 분기(이벤트 없음·잔고 선반영·중복 체결 방지)·S2 체결 시 `entry_n`/pending 해제·DB 로드 시 `entry_n` 동기화.
- **`core/execution/order_execution_service.py`**: `STAGE2` 집행 전 stage≥2·미체결 대기 차단.
- **`core/signal_engine.py`**, **`core/strategy_engine.py`**: 실효 stage 참조 통일.

## 0.8.77 (2026-07-10)

실시간 감시 UI 멈춤(틱·렌더 경로) 구조적 완화.

- **`core/signal_engine.py`**: 틱마다 `calculate_indicators` 로 ATR 표시 계산하던 경로 제거 → 백그라운드 캐시만. 종목별 UI enqueue 스로틀(0.2초). 중복 force-min enqueue 제거. S1 진단 로그 전역 버스트 완화.
- **`core/realtime_tick_dispatch_service.py`**: 워커 배치당 최대 6틱·GIL 양보로 메인 Tk 이벤트 루프 기아 완화.
- **`k_trader_gui.py`**: 렌더 체인 가동 중에도 `_monitor_tree_dirty` 로 갱신 유실 방지. inject 강제 갱신 2.5초 스로틀.
- **`core/defaults.py`**: 틱 flush·ATR 표시·inject·UI enqueue 기본값 조정.

## 0.8.76 (2026-07-09)

DB 복구 보유 포지션의 실시간 청산 미발송 수정.

- **원인**: 재시작·재인증으로 `active_positions` DB에서 복구된 보유 포지션은 `events=[]`(확정 매수 이벤트 없음)이라 `_mirror_held_exit_blocked_pre_confirm` 이 인트라바 미러 청산을 전면 차단. 미러가 `{"action": "HOLD"}` 만 반환하고 레거시 틱 청산으로도 넘어가지 않아, 화면상 ATR SL 이탈(예: 한국전력 35,350 < SL 37,553)에도 매도 신호 0건.
- **`core/signal_engine.py`**:
  - `_mirror_held_exit_blocked_pre_confirm` — `events` 없어도 진입 `reason`(`TYPE_A`/`TYPE_B`/`S1_`/`S2_`) + 진입시각이 있으면 청산 허용(`_position_has_strategy_entry_reason` 신설).
  - 미러 청산 차단 시 `{"action": "HOLD"}` 대신 `None` 반환 → 레거시 틱 청산 폴백 경로 복원.
  - 미러가 `HOLD` 를 반환해도 틱 기반 `evaluate_position_scoped(exit_only)` 로 1회 재검사.
  - `realtime_eval_position_on_m1_revision_only=True` 경로에서도 M1 정체 시 15초(`realtime_eval_position_min_interval_sec`)마다 틱 재평가.

## 0.8.75 (2026-07-09)

- **`core/signal_engine.py`**: 공식 M1 평가 경로(`_official_sync_eval`)에서는 살아 있는 WS `price` 를 덮어쓰지 않음. 트리 「현재가」는 `monitor_display_price()` 로 표시.
- **`core/realtime_official_bars_sync.py`**: M1·슬롯 롤 후 신호 재평가 시 `_official_sync_eval=True` 전달. `_resolve_monitor_price` 는 `price` → `last_price` 순.
- **`ui/tabs/monitor_tab.py`**: WS 0B 수신 시 `last_price` 도 함께 갱신.

## 0.8.74 (2026-07-08)

보유 청산·S2 평가 주기를 M1 리비전(S2)과 통일.

- **변경**: 15초 ``exit_due`` 청산 주기 폐기(인트라바 미러 ON 시). 완료 1분봉이 ``RtTickM1Store`` 에 반영될 때만 청산·S2 동시 평가(Stage 1·2 공통).
- **`core/signal_engine.py`**: ``realtime_eval_position_on_m1_revision_only``(기본 True), ``skip_exit`` 분기 제거. M1 없을 때만 ``realtime_eval_position_min_interval_sec`` 시간 폴백.
- **0.8.73 틱 폴백**: M1 평가 시점에 forming 분 ATR SL 보완 유지.
- **`backtest_engine.py`**: ATR 표시 ``compute_atr_exit_display_text`` 에 ``_cached_bars_peak_high`` 적용 — 청산·표시 공통 peak 캐시로 봉 스캔 부하 완화.

## 0.8.73 (2026-07-08)

실시간 ATR SL 미매도 수정 — 인트라바 미러 청산에 틱가 폴백.

- **원인**: 감시 화면 ATR SL은 **실시간 틱가**로 표시하지만, ``realtime_s1_mirror_dynamic_intrabar`` 청산은 **완료 M1 저가**만 검사. forming 분에 37,025원까지 내려와도 직전 완료 분 저가가 37,555원 위면 HOLD → 매도 누락.
- **`core/signal_engine.py`**: M1 미러가 EXIT 없이 HOLD일 때 동일 포지션·지표로 **틱가 1회 청산 폴백** 추가.

## 0.8.72 (2026-07-08)

시장중단 15분 슬롯 M1 정적 프로브 오탐 완화.

- **원인(12:06:43)**: 12:00 슬롯 처리 지연 중 일부 종목 M1 창에 1~2개 분봉만 들어온 상태에서도 거래량 합계가 작으면 “서킷브레이커·시장중단”으로 오탐 가능.
- **`core/krx_market_halt_slots.py`**: 15분 창 내 최소 M1 분봉 수(기본 8개) 미만 종목은 quiet 판정 대상에서 제외하고, quiet 비율 분모도 충분한 커버리지 종목만 사용.
- **`core/defaults.py`**: ``realtime_market_halt_m1_min_bars_per_code`` / ``realtime_market_halt_m1_scale_volume_by_bars`` 기본값 추가.
- **UI 로그 문구**: M1 프로브 사유를 “M1 거래 정적(자동 추정, 오탐 가능)”으로 완화.

## 0.8.71 (2026-07-08)

15분 슬롯 경계 UI 멈춤 수정 — 메인 after(0) 폭주 제거.

- **원인(12:00:04)**: ``Rt15mSlotBoundaryWatcher`` 가 27종목 슬롯 전환 시 ``maybe_roll`` + ``after(0)`` ×27 을 메인에 쌓아 ``ui_render`` 정지.
- **`core/realtime_official_bars_sync.py`**: ``peek_15m_slot_roll_needed`` + ``Rt15mOfficialSyncWorker`` 단일 워커 큐(종목 중복 제거).
- **`ui/tabs/monitor_tab.py`**: pending 백로그 시 프레임 예산·처리량 상향, 태그 갱신 생략.
- **`core/signal_engine.py`**: ``slot_changed`` 진단 전역 스로틀 2.5초.

## 0.8.70 (2026-07-08)

실시간 UI 멈춤 추가 완화 — Treeview.index 제거·렌더 체인 복구.

- **원인(11:49 재시작)**: 0.8.69 적용 후에도 감시 시작 직후 ``slot_changed`` 진단·``Treeview.index()``·inject 큐 폭주로 두 번째 ``ui_render``(pending 7)에서 메인 스레드 고착.
- **`ui/tabs/monitor_tab.py`**: ``index(iid)`` 제거(줄무늬 캐시), 프레임 예산·``update_idletasks`` 강화.
- **`k_trader_gui.py`**: pending 많을 때 inject 생략, ``finally``에서 렌더 체인 재예약, 워치독 0.8초.
- **`core/signal_engine.py`**: ``slot_changed`` 진단 전역 스로틀 1.5초.
- **`ui/tabs/virtual_tab.py`**: 보유 트리 ``reorder`` 5초 스로틀.

## 0.8.69 (2026-07-08)

실시간 UI 멈춤 근본 완화 — 전일 시장중단 오탐·진단 폭주·렌더 점유 상한.

- **원인(2026-07-08 09:00)**: 장 시작 슬롯 롤업 시 **전일 15:30** 을 M1 미수집 상태로 시장중단 오탐 → 27종목 S1 억제·halt 재조회 + CJ대한통운 ``signal_but_no_buy`` 진단이 **틱마다** 기록 → 09:02:03 이후 ``ui_render`` 체인 정지(2시간+).
- **`core/krx_market_halt_slots.py`**: 당일 슬롯만 M1 프로브·S1 억제; M1 행 없음=미수집 처리; 감시 시작 시 전일 halt 레지스트리 정리.
- **`core/signal_engine.py`**: ``signal_but_no_buy`` 진단 스로틀(기본 30초·전역 0.75초).
- **`ui/tabs/monitor_tab.py`**: 렌더 프레임 시간 예산(기본 45ms) 초과 시 나머지 항목 이월.
- **`core/trading_controller.py`**: 첫 스트림 준비 시 ``purge_stale_market_halt_slots``.

## 0.8.68 (2026-07-07)

D+2 예수금 부족 시 매수 주문 차단 — 미체결 매수 예약 반영.

- **원인**: 15:07 팬오션 매수 직후 ``d2_deposit`` 이 동기화 전 갱신되지 않아, 15:08 한국전력 4주(≈15.3만원)가 가용 D+2(≈3,700원)를 초과 발송 → D+2 마이너스.
- **`core/portfolio_manager.py`**: 미체결 S1/S2 매수 주문별 D+2 현금 **로컬 예약**·가용 D+2 산출.
- **`core/execution/order_execution_service.py`**: 최종 주수 산출 시 예약 차감·1주 불가 시 주문 생략.
- **`core/execution/limit_order_watch_service.py`**: 체결/취소/타임아웃 시 예약 해제.

## 0.8.67 (2026-07-07)

실시간 UI 멈춤 완화 — 슬롯 경계 신호평가를 워커로 위임.

- **원인**: ``schedule_official_sync_and_signal_eval`` 이 메인 스레드 ``after(0)`` 에서 ``on_tick_received(defer=False)`` 를 호출 → 15분 슬롯 전환 시 26종목×지표 풀계산이 Tk 이벤트 루프를 장시간 점유(세션 로그 15:08:51 이후 정지).
- **`core/realtime_official_bars_sync.py`**: ``tick_dispatch_service`` 경로로 위임(워커 스레드).
- **`k_trader_gui.py`**: 공식 15m 반영 시 ``recalculate_indicators`` 를 락 밖·백그라운드 스레드로 이동. UI 렌더 ``_ui_render_in_progress`` 고착 시 강제 재무장.

## 0.8.66 (2026-07-07)

시장중단 15분 슬롯 — 지연 공식봉(ka10080) 재조회로 플레이스홀더 복구.

- **배경**: 서킷브레이커 직후 14:00 슬롯은 즉시 ``row_not_found`` 이나, CB 해제 후에는 API에 공식 15분봉이 생길 수 있음. M1 역산 없이 **지연 재조회**로 ``bars_data``·지표를 복구.
- **`core/krx_market_halt_slots.py`**: 등록 시 3분 후 1차·이후 슬롯 경계마다(최소 10분 간격) 감시 종목 전체 ``force_halt_retry`` 스케줄. 3종목 이상(또는 거래량>0 1종목) 확보 시 ``market_halt_15m_recovered`` 기록·S1 억제 해제.
- **`core/rt_closed_15m_official_replace.py`**: ``force_halt_retry`` 경로 — 시장중단 스킵·짧은 실패 쿨다운만 적용.
- **`core/realtime_official_bars_sync.py`**: ``Rt15mSlotBoundaryWatcher`` 에서 미복구 슬롯 재조회 트리거.

## 0.8.65 (2026-07-07)

서킷브레이커·시장중단 15분 슬롯 감지 — 공식 15m 보정·S1 평가 억제.

- **배경**: 서킷브레이커 발동 구간(예: 2026-07-07 14:00 슬롯)에 ka10080(15분) 공식봉이 없어 전 종목 ``row_not_found`` → 26종목 연속 REST 재조회·실패 로그·S1 오판 유발.
- **`core/krx_market_halt_slots.py`** (신규): M1 거래 정적 프로브·공식 15m 다종목 미발견 누적 시 **시장중단 슬롯** 등록, ``market_halt_15m_slot`` JSONL 기록.
- **`core/rt_closed_15m_official_replace.py`**: 시장중단 슬롯은 공식 REST 스킵·``row_not_found`` 실패 로그 생략·종목별 재시도 쿨다운 미적용.
- **`core/realtime_official_bars_sync.py`**, **`k_trader_gui.py`**, **`core/signal_engine.py`**: 해당 슬롯 S1 신호평가 억제(confluence ``시장중단(15m)``).

## 0.8.64 (2026-07-07)

실시간 UI 멈춤 완화 — S1 진단 로깅 I/O 부하 감소.

- **원인**: v0.8.63 ``s1_watch_eval_diag`` 가 종목당 60초마다 **동기 JSONL 쓰기**(세션 로그 전역 락) → 당일 2,300건+ 누적·틱 폭주 시 UI 응답 지연/멈춤. 14:00 슬롯 ``rt15m_official`` 26종목 연속 조회(``row_not_found``)도 API 부하 가중.
- **`core/signal_engine.py`**: 진단 기록을 15분 슬롯 변경 시 + 기본 300초 주기로 축소, 전역 0.25초 버스트 제한, **데몬 스레드 비동기 기록**으로 틱 경로 블로킹 제거. ``signal_but_no_buy`` 즉시 기록은 유지.

## 0.8.63 (2026-07-07)

실시간 S1 진단 로깅 대상 수정 — 일일 감시 종목에 기록되도록.

- **버그**: v0.8.62 ``s1_watch_eval_diag`` 가 ``watchlist_store``(관심목록 파일)에만 기록되어, 관심목록 0건인 날(일일 선정 23종목만 감시)에는 진단 로그가 전혀 쌓이지 않음.
- **`core/signal_engine.py`**: 진단 대상을 ``monitor_info`` 등록·비보유·비관심전용(``wl_only`` 제외) 종목으로 변경.

## 0.8.62 (2026-07-06)

실시간 S1 신호 진단 로깅 추가 (백테스트 대비 미진입 종목 원인 특정용).

- **배경**: 2026-07-06 동적 백테스트는 352820(하이브)을 09:30봉 TYPE_A로 매수(→11:40 ATR_SL)했으나 실시간은 종일 미진입. 352820은 백테스트 유니버스(schedule_a rank3)·실시간 감시목록(`selected_targets.csv`) 양쪽에 정상 포함됐고, 신호 로직(`get_buy_signals_detailed`)도 동일하여 유니버스·신호 로직 차이가 아님이 확인됨. 남은 원인(장초반 라이브 15분봉 ≠ DB 확정봉, 또는 워밍업 `bars_ready` 미충족)은 실시간이 종목별 신호 평가를 로그에 남기지 않아 사후 특정이 불가했음.
- **`core/signal_engine.py`**: `_log_s1_watch_diag` 추가 — 감시·비보유·비관심전용 종목의 S1 평가 상태(`bars_ready`·봉수·`signal_on`·`sig_type`·`is_buy`·`met_ratio`·`confluence`)를 `s1_watch_eval_diag`로 기록. 신호=ON인데 미매수면 즉시, 그 외에는 `realtime_s1_watch_diag_interval_sec`(기본 60초) 주기. 신호 판단 로직은 불변(관측 전용).

## 0.8.61 (2026-07-03)

실시간 S1 진입 — 지연(stale) 신호가 오탐 진입 차단 (백테스트 대비 과진입 방지).

- **배경**: 2026-07-03 동적 백테스트 vs 실시간 대조 결과, 실시간만 105560(KB금융)을 매수(→ATR_SL 손절)했으나 백테스트는 매수하지 않음. 원인은 실시간 신호 평가가(`signal_hint_price`)가 실제 시장가보다 약 12분 지연(168,300 vs 신선 매도1호가 171,100, 괴리 1.66%)되어 "눌림A"가 오탐된 것.
- **`core/execution/order_execution_service.py`**: `_s1_stale_signal_gap_block` 추가 — touch 매수 시 신선 매도1호가(`fresh_ask`)와 신호가의 괴리율이 `realtime_s1_stale_signal_max_gap_pct`(기본 1.5%)를 넘으면 진입 생략(`buy_skipped`/`stale_signal_price_gap` 로그). 당일 정상 진입(0.10%·1.15%·1.48%)은 통과, 문제의 105560(1.66%)만 차단.

## 0.8.60 (2026-07-03)

인트라바 ATR SL — 진입 전 1분봉 오판 방지.

- **`core/bt_hp_intrabar_unified.py`**: `resolve_position_entry_ts` 추가 — BUY 이벤트 시각이 `entry_time`보다 늦으면 이벤트 우선(SYNCED 장전 시각 잔존 대비). 청산 미러는 1분봉 **봉 종료 시각(open+1분) ≤ 진입 시각** 인 분만 제외.
- **`core/portfolio_manager.py`**: SYNCED·미확정 S1 체결 확정 시 `entry_time`을 체결 시각으로 갱신(장전 동기화 시각 잔존 방지).
- **`core/signal_engine.py`**: 진입 시각 없으면 인트라바 청산 미러 차단. `ATR_SL` 시 `m1_bar`·`entry_time`을 strategy_action 로그에 포함.
- **`core/execution/order_execution_service.py`**: `strategy_action` JSONL에 `m1_bar`·`entry_time` 필드 전달.

## 0.8.59 (2026-07-03)

정상 M1 폴러 동작 시 UI 로그 최소화.

- **`core/rt_watchlist_m1_api_poller.py`**: ka10080 성공 후 catch-up 반영 건수(`ℹ️ [M1Poll] … 차트 catch-up N분`) UI 출력 제거. 실패·파싱 오류 등 `⚠️` 로그만 유지.

## 0.8.58 (2026-07-03)

ka10080 M1 catch-up 반복 반영 방지.

- **`core/realtime_m1_fallback.py`**: `RtTickM1Store`가 보관하는 최근 N분 구간만 catch-up 대상으로 제한. ka10080 1페이지 전체(약 900분)를 매 분 다시 반영해 `차트 catch-up 900분` 로그가 반복되던 문제를 차단.
- 기존 store에 같은 OHLCV가 이미 있으면 재반영하지 않아, 정상 장중에는 새로 닫힌 1분만 반영되도록 조정.

## 0.8.57 (2026-07-03)

ka10080 M1 폴러 — 시계 가드 대신 **차트 응답 기반** 판정·라운드 프로브.

- **`utils/krx_bars.py`**: `m1_bar_end_ts_for_closed_open`, `chart_covers_m1_closed_minute` — ka10080 차트 `max(cntr_tm)` 이 목표 분 종료 시각 미만이면 ``minute_not_ready``(장전·개장 직후).
- **`core/rt_watchlist_m1_api_poller.py`**: 매 분 대표 종목 1건 **프로브** 후 라운드 스케줄/생략. ``minute_not_ready`` 는 UI·진단 스팸 없이 조용히 생략, 차트는 도달했는데 행만 없을 때만 ``row_not_found`` 기록. 0.8.56 시계 가드(`is_krx_m1_closed_open_label`) 제거.

## 0.8.56 (2026-07-03)

ka10080 M1 폴러 — 장전·장후 `row_not_found` 누적 방지.

- **`utils/krx_bars.py`**: 닫힌 1분봉 시작 라벨이 KRX 연속매매 격자(평일 09:00~15:19)에 속하는지 판정하는 `is_krx_m1_closed_open_label` 추가.
- **`core/rt_watchlist_m1_api_poller.py`**: `_wake_loop`·타이머 작업 양쪽에서 장전·장후·15:20 이후 동시호가 분을 생략해, 존재하지 않는 분봉에 대한 ka10080 조회와 `row_not_found` 로그 폭증을 차단.

## 0.8.55 (2026-07-03)

인트라바 S1 — 같은 M1 ``지금 닫히면?`` 신호 재판정 (정적 Signal/Sig_Type 복사 제거).

- **``core/bt_hp_signal_bridge.py``**: ``evaluate_close_now_s1_at_m1_idx`` — forming M1 OHLCV + intrabar RSI/MACD 로 마지막 15m 행 ``get_buy_signals_detailed`` 재실행.
- **``evaluate_bt_hp_intrabar_unified``**: close-now ON 시 M1마다 재판정, ``df_with_ind`` 전달 시 재사용(백테는 ``full_15m`` 에서 1회 생성·캐시).
- **``SignalEngine``**: forming-only S1 매수 직전 ``_confirm_s1_with_close_now`` — 불일치 시 ``intrabar_close_now_blocked`` JSONL.
- 설정: ``realtime_intrabar_close_now_signals`` (기본 true).

## 0.8.54 (2026-07-03)

ka10080(1분·15분) 조회 실패 빈도·원인 분석용 진단 로그.

- **`core/ka10080_fetch_diag.py`**: 실패/성공 누적, `fail_by_reason`·`fail_by_context`·종목별 Top, UI 90s 스로틀, 5분 JSONL 요약.
- **`data_manager_v2.fetch_kiwoom_minute_chart_page`**: HTTP 429/502·빈 응답·네트워크 오류 시 `record_fail`, 성공 시 `record_ok`, `last_ka10080_fetch_meta` 저장.
- **M1 폴러·15m 확정·워밍업·BT-HP 브리지**: `fetch_context` 전달, `row_not_found`·`no_token`·`merged_empty` 등 의미론적 실패 기록.
- **session JSONL**: `ka10080_fetch_fail`(건당), `ka10080_fetch_stats`(주기 요약).
- 설정: `realtime_ka10080_fail_log_ui_throttle_sec`, `realtime_ka10080_fail_summary_interval_sec`.

## 0.8.53 (2026-07-03)

ka10080 M1·15분 REST 분산 — 감시 종목 수(3~40+)에 따라 spread 자동 스케일.

- **M1**: ``spread = clamp(N × 2.5s, 12, 55)`` — 3종목 12s, 16종목 40s, 40종목 55s(cap).
- **15분 확정**: ``spread = clamp(N × 22.5s, 90, 840)`` — 16종목 360s, 40종목 840s(14분, 슬롯 내).
- ``core/ka10080_spread.py`` — M1 폴러·15분 스케줄러 공통.

## 0.8.52 (2026-07-03)

ka10080(1분·15분) REST 경쟁 완화 — M1 분산 확대·15분 확정 조회 M1 이후 분산 스케줄.

- **M1 폴러 분산**: `realtime_watchlist_m1_api_spread_sec` 20→**40**초(매 분 0~40s 구간에 1분봉 조회).
- **15분 확정 REST 분리**: 슬롯 직후 2.5s 일괄 조회 대신 `realtime_closed_15m_official_replace_stagger` ON — ``m1_spread+guard(52s)`` 이후 **360초**에 종목별 해시 분산(15분 슬롯 내 완료, M1과 ka10080 경쟁 최소화).
- 설정: `realtime_m1_ka10080_guard_sec`, `realtime_closed_15m_official_replace_min_delay_sec`, `realtime_closed_15m_official_replace_spread_sec` (`core/rt_closed_15m_official_replace.py`, `core/defaults.py`).

## 0.8.51 (2026-06-25)

한미약품(128940) 등 — 매수 직후 동일 호가 매도·오판 손절(진입 이전 M1 저가) 수정.

- **오판 STOP_LOSS**: 인트라바 미러가 15분 슬롯 **전체** M1을 재생하면서, 13:59 진입(444,000) 직후 13:47 분봉 저가(423,500)로 SL -4.5% 판정 → 실제 시세 445k+ 인데 매도 발송. `evaluate_bt_hp_intrabar_unified` 에 진입 시각 이전 분봉 청산·max_p 갱신 스킵 추가.
- **매수 미체결 중 매도**: S1 터치 매수 kt00007 미확정 상태에서 청산 미러가 동작. `s1_buy_pending`·SYNCED 선반영·확정 BUY 이벤트 없으면 보유 청산 미러 보류(`core/signal_engine.py`).

## 0.8.50 (2026-06-25)

2026-07-02 등 BT 6종목 매매 vs 실시간 1종목만 체결된 불일치 — M1(ka10080) 실패 시 인트라바 미러가 무력화되던 근본 원인 수정.

- **근본 원인**: `realtime_s1_mirror_dynamic_intrabar` ON 시 S1 매수는 `evaluate_bt_hp_intrabar_unified` 경로인데, 이는 **공식 1분봉(RtTickM1Store)** 이 필수. `RtWatchlistM1ApiPoller`가 ka10080 502 등으로 실패하면 `return`만 하고 M1·신호 평가 모두 스킵 → 오전 BT 매수 6건이 RT에 0건. 128940만 오후 M1이 쌓인 뒤 체결된 것과 일치.
- **M1 폴러 강화**: API 실패 시에도 `schedule_official_sync_and_signal_eval` 호출(`realtime_m1_poller_eval_on_fetch_fail`). 성공 시 ka10080 응답 차트 전체 catch-up(`realtime_m1_poller_apply_chart_catchup`). 실패·장후에는 `bars_1m` DB 폴백(`realtime_intrabar_m1_db_fallback`). 캐시 히트 시에도 평가 재트리거(`realtime_m1_poller_eval_on_cache_hit`).
- **인트라바 미러 M1 폴백**: `_intrabar_mirror_get_full15m_and_m1`이 store만 보던 것을 `load_intrabar_m1_window`(store+DB 병합)로 변경(`core/realtime_m1_fallback.py`, `core/signal_engine.py`, `core/rt_watchlist_m1_api_poller.py`).
- **검증**: `replay_realtime_signal_engine_day.py --day 2026-07-02 --drive db_1m_closes --dynamic-bt` → 동적 BT 6건(007070·030200·051900·081660·097950·139480)과 엔진 S1 매수 경로 정합 확인.

## 0.8.49 (2026-07-01)

우선주 코드(0126Z0)가 남광토건(001260)으로 오인되어 감시·매매된 치명적 버그 수정 + KODEX200 유니버스 갱신.

- **우선주 코드 정규화 수정(핵심)**: `canonical_stock_code("0126Z0")`가 숫자만 추출·zfill 하며 `001260`(남광토건)으로 충돌 → 삼성에피스홀딩스(~46만) 봉으로 S1 신호가 나가고 남광토건(~8천) 틱·주문으로 체결·즉시 손절. 영문 포함 6자리 KRX 코드(예: `0126Z0`)는 alnum 6자리 그대로 유지(`utils/normalization.py`).
- **KODEX200 유니버스 CSV 갱신**: `KODEX_200_20260629.xls` 기준으로 `kodex200.csv`·`kodex_200.csv` 동기화(편입·편출 반영).

## 0.8.48 (2026-06-25)

동적 백테스트 매매 0건일 때 UI가 완료되지 않고 멈춘 것처럼 보이던 문제 수정.

- **백테스트 UI 완료 처리**: `trades`가 비어 있어도 시뮬레이션은 정상 종료되는데, 기존에는 상태가 `[날짜] 시계열 모의투자 중...`에 고정되고 리포트·안내가 없었다. 매매 0건 시 「백테스트 완료 (매매 0건)」 표시 및 안내 팝업 추가, `trades is None`(중단)과 구분(`ui/tabs/backtest_tab.py`).

## 0.8.47 (2026-06-24)

BT-RT 불일치 2차 원인(실시간 신호 평가가 WS 틱 가격에 묶임) 수정 + WS 끊김 안정화·진단.

- **M1 폴러가 신호 평가를 구동하도록 수정(핵심)**: WS 0B 틱이 끊기면 `monitor_info` 틱 가격이 0이 되어, M1 API 폴러(ka10080)가 공식 1분봉을 채워도 `schedule_official_sync_and_signal_eval` 의 `on_tick_received` 트리거가 `px>0` 조건에 막혀 신호 평가가 통째로 스킵됐다(BT는 매매 발생, RT는 0건). 폴러가 방금 가져온 공식 종가/거래량을 평가 트리거에 직접 전달하고, 트리거 측은 틱 가격이 0이면 공식 M1 동기화된 마지막 봉 Close 로 폴백하도록 수정 → WS가 끊겨도 매 분 공식 봉으로 BT와 동일 시점에 신호 평가(`core/rt_watchlist_m1_api_poller.py`, `core/realtime_official_bars_sync.py`).
- **검증**: `db_1m_closes` 리플레이로 동일 엔진이 120110 매수 신호 1건 재현(동적 BT와 일치) → 로직 동일·라이브 데이터 구동 결함임을 확인.
- **WS 끊김 안정화**: `ping_timeout` 10→20초, `close_timeout=5` 추가(순간 점유·느린 pong 으로 정상 연결이 'no close frame'으로 끊기는 빈도 감소). 수신 에러 후 즉시 재접속하던 것을 2~8초 점증 백오프로 완화(`ui/tabs/monitor_tab.py`).
- **WS 끊김 진단 로깅**: 끊길 때 예외종류·종료코드·종료사유·마지막 수신 후 경과초·누적 끊김 횟수를 콘솔과 세션 JSONL(`ws_disconnect`)에 기록 → 원인(서버 유휴 드롭 vs ping 타임아웃) 특정 가능.

## 0.8.46 (2026-06-23)

BT-RT 불일치(당일 백테 3종목 매매 vs 실시간 0건) 원인 규명·수정.

- **실시간 틱 전량 차단 수정(1차 원인)**: WS 장운영구분(fid215)이 `'s'`/`'u'`/`'2'` 등 신뢰할 수 없는 값으로 와도 "명시적 비정규장"으로 확정해 정규장 시간 내내 0B/0D 체결 틱을 전량 스킵 → 신호 엔진에 단 한 틱도 도달하지 못해 매매 0건. PC 시각 기준 KRX 정규장(평일 09:00~15:30)이면 fid215와 무관하게 틱을 통과시키고, 비숫자 fid215는 명시적 코드로 확정하지 않도록 수정(`ui/tabs/monitor_tab.py`).
- **ka10080 429 완화(2차 정합성)**: 22종목 동시 폴링 시 글로벌 간격 0.35s가 M1 분산(8s)과 충돌해 429 상시 발생. 글로벌 간격 0.5s, M1 분산 20s, 백오프 0.6s로 조정(`settings.json`, `core/defaults.py`).
- **리플레이 도구 버그픽스**: `DynamicBacktester.run()`이 bool 이 아닌 매매내역 DataFrame 을 반환하는데 `if not ok:`로 평가해 발생하던 `truth value of a DataFrame is ambiguous` 예외 교정(`tools/replay_realtime_signal_engine_day.py`).
- **검증**: `db_1m_closes` 리플레이로 동일 신호 엔진이 005830·138930·316140 매수 신호를 재현 → 로직은 BT와 동일, 실거래 0건은 데이터 기아가 원인임을 확인.

## 0.8.45 (2026-06-22)

전체 코드 재점검(청산·주문/체결·실시간/UI·동기화)으로 발견한 버그 13건 수정.

- **UI 멈춤(메인스레드 동기 REST)**: 보유 현재가 폴백(ka10004/ka10001, 종목당 8~16초)과 금일손익 분모(보유 종목별 ka10001)를 **백그라운드 워커**로 전환하고 캐시만 사용(`allow_rest=False`). EOD 리포트 경로는 동기 정확도 유지(`ui/tabs/virtual_tab.py`, `utils/eod_equity_snapshots.py`).
- **터치호가 pending 무한 루프**: kt00007 미반영이 지속되면 `exit_pending`(재매도 불가)·`s1_buy_pending`(재매수 불가)가 영구 잠금되던 경로에 누적 상한(`touch_pending_confirm_max`, 기본 20) 추가 → 초과 시 미체결 타임아웃 흡수(`core/execution/limit_order_watch_service.py`).
- **좀비 타이머**: `_schedule_touch_timer` 재예약 전 기존 `after_id` 취소 — 동일 주문 폴링 워커 중복 실행 방지.
- **`save_db` 원자적 저장**: `self.lock` 안 스냅샷 직렬화 + `tmp→os.replace` 교체로 동시 변경/쓰기 중 종료 시 손상 방지(`core/portfolio_manager.py`).
- **reconcile 이중 종료내역**: 매도 진행 중(`exit_pending`+pending 스냅샷) 종목의 reconcile 제거 보류 — `SYNC_EXIT`와 실체결 SELL 이중 기록·손익 왜곡 방지.
- **재인증 `load_all` 휘발필드 보존**: DB 재로드 시 `exit_pending`·`pending_exit_ord_no`·`max_p`·`events`를 메모리에서 보존(이중 매도 방지).
- **계좌 동기화 동시 실행 가드**: `sync_with_actual_account` 비차단 재진입 래퍼로 reconcile/save_db/streak interleave 방지.
- **RT 미러 청산 정합**: 현재 슬롯 M1 고점만 누적해 유실되던 당일 고점을 `full_15m`로 복원 시드(`max_p`) — Flash/ATR 트레일 무장 BT-RT 정합. 동기화 포지션의 `sel_track` 트랙 힌트 전달로 B트랙 A키 오적용 방지(`core/signal_engine.py`, `core/bt_hp_intrabar_unified.py`).
- **매수 부분체결 가드**: 전량 체결 시에만 watch/`s1_buy_pending` 해제 — 잔량 호가 잔존 중 이중 매수 방지(`core/execution/fill_confirmation_service.py`).
- **기타**: `_get_touch_confirm_poll_ms` 미정의(AttributeError) 교정, `OrderActionDispatcher` 워커 예외 로깅, `vp_stub` 레거시 `is_active`/`clear` 일관화(`utils/monitor_vp_stub.py`).
- **설정**: `touch_pending_confirm_max`(기본 20) 신규. `vp_price_stub_grace_sec`·`realtime_gap_down_tick_confirm`·`realtime_gap_down_entry_grace_sec`·`realtime_monitor_atr_ir_cache_min_sec`를 `settings.json`에 명시(`core/defaults.py`, `settings.json`).

## 0.8.44 (2026-06-19)

- **KRX 15분봉 슬롯 정합**: 장외·야간(19:15, 00:00 등) 가짜 슬롯 전진 제거 — `is_krx_15m_slot_end`·`next_krx_15m_slot_end`(15:30→익일 09:15) 적용, `[RT15m-Official] 행 미발견` 로그 스팸 완화.

## 0.8.43 (2026-06-16)

- **연속 감시**: 감시 탭 「연속 감시 시작」— 장마감(15:30+buffer) 후 자동 **감시 중단 → 일일 선정 → 워밍업 포함 재시작**. 기존 「실시간 감시 시작」은 1회 감시만(자동 전환 없음).
- **일일 선정 콜백**: `select_monitoring_targets(on_complete=…)` — EOD 사이클·자동화 연동용.

## 0.8.42 (2026-06-16)

- **실시간 S1 ↔ 동적 고정밀 백테 정합**: `Signal ∧ Ready`(봉 인덱스 ≥ 19) 통일, 워밍업·캐시 경로에서도 `evaluate_bt_hp_intrabar_unified` 인트라바 미러 실행, 미러 ON 시 15분 슬롯 프리시그널 게이트 비활성화(장전 False 고정 방지).
- **RT15m-Official 로그 억제**: `행 미발견`은 마지막 재시도에만 출력, 실패 키에 지수 백오프 쿨다운(최대 15분).
- **계좌 조회 8005**: 토큰 무효 시 캐시 삭제·재인증 시도.

## 0.8.41 (2026-06-16)

- 인증 재시도/쿨다운 개선: OAuth 응답의 `return_code`·`token`을 직접 검증하고, `8001`(AppKey/Secret 불일치) 발생 시 재시도를 중단하며 메시지를 명확화.
- 429 대응: 단일 비행 + OAuth 429 쿨다운으로 기동 시 과다 인증 호출과 로그 스팸 완화.
- 동기화 정합: 보유 0종목이 확인되면(수동 `force=True`) 로컬 포지션을 즉시 정합합니다.

## 0.8.40 (2026-06-10)

- **체결·주문 로그 정리**: SYNCED(ka10085 선반영) 포지션을 S1 체결 확정으로 처리(S2 오표시·수량 합산 방지). `[체결/REST]`·`[Portfolio]` 중복 제거, Buy Signal→주문 접수 순서 정리, 터치호가 pending/확정·체결후 동기화 로그 간소화.
- **워밍업 강제 API 갱신**: `realtime_warmup_force_api_refresh`(기본 True) — 감시 시작 시 DB 검증 통과해도 종목마다 ka10080 15분봉 REST 1회 조회.

## 0.8.39 (2026-06-05)

- **터치호가 옵션 정리**: `touch_fill_first_poll_sec` 설정 키 제거(혼란 방지). 터치 confirm 간격은 `touch_quote_order_poll_sec` 기준으로 일원화.
- **터치호가 세트 동작**: 체결 확인 → 미체결이면 곧바로 정정 주문을 한 세트로 묶어 `touch_quote_order_poll_sec` 뒤에 진행.

## 0.8.38 (2026-06-05)

- **터치호가 감시 주기**: 접수·정정 후 `touch_quote_order_poll_sec` 기준으로 **체결 확인**을 하고, 미체결이면 즉시 정정(이후 다시 동일 주기로 루프).
- **EXIT 터치 pending**: pending 중 **취소 API 미호출** — 미체결 매도 유지·kt00007 재확인만; terminal(체결 없음) 시 `exit_pending` 해제·재매도 복구 스케줄.
- **매도 잠금 복구**: `cancel_stale_open_sell_orders_for_code`·`sum_open_sell_lock_qty_for_code` — ka10075 미체결 매도 정리, 800033 시 잠금 해제 후 재시도; `exit_pending` 만료 해제·매도 수량 보정.
- **ka10080 429**: 분봉 REST 한도 로그 스로틀·전역 쿨다운(`data_manager_v2`).

## 0.8.37 (2026-06-04)

- **RC4033 즉시 on_fill**: kt00007 대기 없이 `apply_trusted_fill_on_no_open_qty` — 매도는 증권사 미체결없음 확인 직후 힌트가로 포지션 반영.
- **취소 API RC4033** 시 `OrderManager`에서도 동일 경로 트리거.
- **EXIT pending 1회** 후 취소조회(`touch_exit_pending_probe_after`), 이후 체결 확인은 `touch_quote_order_poll_sec` 기준.

## 0.8.36 (2026-06-02)

- **RC4033 즉시 체결확정**: pending 4회·24초 후 동기화에 의존하던 흐름 개선 — `confirm_after_no_open_qty`(kt00007 연속 재시도 + 잔고 기반 매도 확정), pending 2회 후 취소조회 조기 탐지, boost 간격 1.5초.
- **터치 poll**: `try_confirm_fill_once` 항상 `force_kt00007_sync`, 첫 확인 간격 기본 2.5초.

## 0.8.35 (2026-06-02)

- **체결 확인 증분 반영(Phase 5)**: `FillConfirmationService` — CHE·REST 경로가 주문번호별 `applied_qty` delta만 `on_fill`. 부분체결 2건째 CHE가 스킵되던 문제 수정.
- **체결 상태 DB(Phase 2)**: `order_fill_state` 테이블 — `order_qty`/`exchange_filled_qty`/`applied_qty` 추적.
- **kt00007 per-order sync(Phase 3)**: `fr_ord_no` 필터·전역 스로틀 bypass — 터치 poll `try_confirm_fill_once` 경로.
- **post-fill sync(Phase 6)**: 미확정 주문 존재 시 `post_fill_sync_skip_while_pending` 으로 계좌동기화 생략.

## 0.8.34 (2026-06-02)

- **주문 로직 독립 분리**: 매수/매도 집행·미체결 감시·체결 동기화·액션 큐를 `core/execution/` 패키지로 모음. `OrderExecutionService`(S1/S2/EXIT), `OrderActionDispatcher`(워커 큐), 기존 fill/limit/post-fill 서비스 이동. `TradingController`·`k_trader_gui`는 thin delegate 유지 — 이후 전면 개편 대비.

## 0.8.33 (2026-06-02)

- **감시 트리 신호상태 고정**: `monitor_tab.py`에 `monitor_signal_status_display_text` import 누락으로 선정·갱신 실패하던 문제 수정. 워밍업·`대기` 문구가 합치도를 가리지 않도록 플레이스홀더 확장(`core/monitor_signal_status_display.py`).
- **체결·동기화 직후 UI 멈춤 완화**: kt00007 병합 스로틀·boost 폴링 축소, 체결 후 계좌 동기화 1/2차 디바운스, 포지션 탭 GUI·주문로그 debounce(`core/fill_reconciliation_service.py`, `core/post_fill_account_sync_service.py`, `core/portfolio_manager.py`, `k_trader_gui.py`, `core/defaults.py`).
- **동기화 직후 경량 UI 갱신**: silent 계좌 동기화·늦은 매도 체결 시 전체 리빌드 대신 `request_virtual_tab_sync_light_refresh`(보유·요약·주문로그만). 동기화로 이미 반영된 중복 매도는 종료내역 보강·UI 리빌드 생략(`core/portfolio_manager.py`, `core/fill_reconciliation_service.py`, `core/limit_order_watch_service.py`).

## 0.8.32 (2026-05-31)

- **bars_1d 소스 FDR 통일**: 하베스팅·`update_historical_data`·`update_daily_data`가 1m 집계 대신 FDR 일봉을 `bars_1d`에 반영. 감시 선정(`api_only`)·동적 백테(`prepare_dynamic_universe`)와 동일 일봉 정의(`data_manager_v2.py`, `harvest_service.py`).
- **FDR 일봉 API**: `sync_daily_bars_from_fdr*`, `is_daily_bars_behind_required_eod`, `fetch_fdr_daily_dataframe` (`data_manager_v2.py`).
- **과거 bars_1d 백필 도구**: `tools/backfill_bars_1d_from_fdr.py` — 유니버스×구간 FDR → `bars_1d` 일괄 교체.
- **감시 api_only EOD 앵커**: `target_date` 미지정 시 `required_eod_bar_date_asof_local()` 15:30 기준(`strategy_selector.py`).
- **설정**: `monitor_pre_selection_bars_1d_source` 기본값 `fdr` (`core/defaults.py`, `settings.json`).
- **감시·백테 선정 비교**: 「백테스팅 선정목록」 버튼으로 `selected_targets.csv` vs DB `bars_1d` 선정 대조(`ui/tabs/monitor_tab.py`, `core/selection_compare.py`).

## 0.8.31 (2026-05-31)

- **하베스팅 DB 완결성 판정 수정**: 장 개시 전 완결 여부를 기준 종목 DB `last_sync`(오래된 값)로 판단하던 문제를 KRX 캘린더 `required_eod_bar_datetime_asof_local()` 기준으로 교체해, 최신 거래일 15:30 미만 데이터가 잘못 스킵되지 않도록 함(`harvest_service.py`, `reporting/krx_trading_calendar.py`).
- **하베스팅 DB 경로 고정**: `os.getcwd()` 대신 `DataManagerV2.DEFAULT_DB_PATH`를 사용해 GUI와 동일한 `trading_data.db`를 참조(`harvest_service.py`).

## 0.8.29 (2026-05-20)

- **터치호가 amend 유예(추가 방어)**: `try_confirm_fill_once`가 `pending`이면 정정(amend)을 보내지 않고 다음 확인만 예약(ghost/RC4033·RC4042 연쇄 완화)(`core/limit_order_watch_service.py`).
- **보유종목 등록 누락(SYNCED 스킵 완화)**: ka10085 동기화에서 최근 SELL 종료(잔고 유령 방지) 스킵이 과도하게 적용되어 체결은 됐는데 VP에 안 올라오는 케이스를 완화(서버 보유 qty>0 또는 SELL 이후 새 BUY가 있으면 허용) (`core/portfolio_manager.py`).
- **매수 접수(SUCCESS) 승격 안정화**: ka10085 동기화 직후 `patch_buy_filled_price_after_reconcile()`가 여러 접수(SUCCESS) 행 중 일부를 잘못 스킵해 `filled_qty=0`이 남는 문제를 완화(재료 후보 선택 강화)(`data_manager_v2.py`).
- **WS 장운영구분 플레이스홀더 정규화**: `0s.fid215`가 `o/q/r` 같은 문자열 플레이스홀더로 들어오는 경우를 placeholder로 취급해 정규장 OFF 확정으로 인한 틱 차단을 완화(`ui/tabs/monitor_tab.py`).

## 0.8.30 (2026-05-20)

- **GUI 프리징 완화(체결 동기화 복구 경로)**: `FillReconciliationService`의 late recover에서 `sync_with_actual_account(force=True)`를 Tk 메인스레드에 바로 태우지 않고 백그라운드 스레드로 처리해 화면 멈춤을 줄임.
- **체결확인 pending 원인 디버깅**: `try_confirm_fill_once()`의 row 스냅샷과 `applied=False` 실패를 로깅해 “체결은 됐는데 pending으로 남는” 원인을 DB(kt00007 병합/row 확정) vs 로컬 적용(Portfolio.on_fill) 중 어디에서 발생하는지 분리함(`core/fill_reconciliation_service.py`).

## 0.8.28 (2026-05-20)

- **터치호가 pending→정정 연쇄 완화**: `try_confirm_fill_once`가 `pending`을 반환하는 동안은 즉시 `kt10002` 정정을 보내지 않고, pending이 1회 연속일 때는 amend를 유예하여 RC4033/RC4042 연쇄를 줄입니다(`core/limit_order_watch_service.py`).

## 0.8.27 (2026-05-19)

- **터치호가 체결확인 레이턴시 완화**: `exchange_fill_confirm_retry_interval_sec` 기본값을 `5초`로 상향해(설정·기본값·폴링 첫 확인) DB/체결 반영 지연 레이스로 인한 불필요한 정정을 줄임(`settings.json`, `core/defaults.py`, `core/limit_order_watch_service.py`, `core/fill_reconciliation_service.py`).
- **RC4042 안정화 진단/재시도**: `RC4042(정정가능수량 초과)` 발생 시 `filled_qty/qty/rem` 기준을 로그로 남기고, 해당 케이스에서는 정정 시도 횟수 소진(카운트) 대신 체결 동기화(boos_polling)로 전환해 동일 실패 연쇄를 완화(`core/limit_order_watch_service.py`).

## 0.8.26 (2026-05-19)

- **터치호가 정정 루프 오탐 방지**: 주문 로그 기준 전량 체결(예: `filled_qty`/`result`가 이미 FILLED 또는 잔량 0)로 판정되는 경우, `kt10002` 정정 타이머를 즉시 terminal 처리하여 불필요한 정정 반복을 줄임(`core/limit_order_watch_service.py`, 터치 정정 경로).

## 0.8.25 (2026-05-19)

- **실시간감시 '가상보유 미선정' 현재가 갱신 보강**: `SignalEngine` pending enqueue가 누락되는 경우를 대비해 `실시간감시탭` 최소 강제 enqueue 대상에 `portfolio.virtual_positions`(보유) 종목을 포함시켜 `보유 및 대응종목`의 틱 갱신 누락을 완화(`k_trader_gui.py`).

## 0.8.24 (2026-05-19)

- **터치호가 정정 루프 중단**: `RC4033(정정/취소할 수량이 없음)`(=전량 체결/남은 수량 0) 정정 거부 시 터치호가 정정 타이머를 terminal로 처리해 동일 실패 반복을 방지(`core/limit_order_watch_service.py`).

## 0.8.23 (2026-05-19)

- **실시간 감시 UI 멈춤 완화**: 보유행 ATR 표시를 위한 `calculate_indicators()` 호출을 종목별 IR 마지막 행 캐시로 재사용(기본 8초 최소 주기)해 주문 직후 계산부하 폭증으로 인한 프리징을 완화(`ui/tabs/monitor_tab.py`).

## 0.8.22 (2026-05-19)

- **실시간 감시 UI 정합**: 보유 직후/동일 시그니처 조기 return 경로에서도 `보유 및 대응종목` 누락을 복구(reconcile)하고, `wl_only`인데도 실제 보유면 삭제/스킵하지 않도록 방어(`k_trader_gui.py`, `ui/tabs/monitor_tab.py`).
- **보유 스텁 '현재가' 시드**: 장 시작 전 `vp_price_stub` 표시용 `last_price`를 평단으로 고정하지 않고 `bars_data` 마지막 `Close`로 시드(전일종가처럼 표시)해 혼동을 줄임(`core/trading_controller.py`).

## 0.8.21 (2026-05-18)

- **터치호가 전용·체결 경로 단일화**: 실시간 REST는 항상 터치 지정가. 접수 직후 **2초×8 공통 폴링 제거**, `LimitOrderWatchService` 폴링에서 **kt00007 1회 확인 → 호가 정정** 순(`core/trading_controller.py`, `core/order_manager.py`, `core/limit_order_watch_service.py`, `core/fill_reconciliation_service.py`).

## 0.8.20 (2026-05-18)

- **보유 직후 UI 멈춤 완화**: 금일손익 분모(종목별 직전 종가 REST) **5분 캐시**, 보유 스텁 생성 시 **동기 REST 제거**, 포지션 탭 **주문 트리 지연 렌더**, 스텁 REST 보강은 **백그라운드**(`utils/eod_equity_snapshots.py`, `core/trading_controller.py`, `k_trader_gui.py`, `ui/tabs/virtual_tab.py`).
- **터치호가 정정**: `touch_quote_order_poll` 의 호가 조회·정정을 **메인 스레드 밖**에서 실행해 Tk 교착 완화(`core/limit_order_watch_service.py`).
- **감시·포지션 탭**: 렌더 중 **stale 재무장 중복 예약** 방지, 틱 요약의 금일손익·종료손익 **30초 스로틀**, `vp_price_stub` 이어도 틱 `price` 우선(`k_trader_gui.py`, `ui/tabs/virtual_tab.py`).

## 0.8.19 (2026-05-15)

- **실시간 S1 풀계산 트리거**: 고정 1초 간격 대신 **15분봉 꼬리 지문 변경**·**(인트라바 미러 ON 시) 공식 1분 `RtTickM1Store` 반영 리비전** 시에 `calculate_indicators`+`get_buy_signals_detailed` 실행. 지문 불변 시에는 ``realtime_s1_full_calc_fallback_sec``(기본 120초) 안전망(`core/rt_tick_m1_store.py`, `core/signal_engine.py`, `core/defaults.py`, `settings.json`).
- **보유 청산·S2**: 청산 평가는 ``realtime_eval_position_min_interval_sec`` 등 **15초** 간격 유지, **S2(stage==1)** 는 M1 반영 리비전마다 추가 평가. 인트라바 미러는 ``skip_holder_exit``, 레거시는 ``evaluate_position_scoped``(`core/bt_hp_intrabar_unified.py`, `core/strategy_engine.py`, `core/signal_engine.py`).

## 0.8.18 (2026-05-15)

- **주문 상태·체결 추적**: `update_order_status` 가 종목 `code` 불일치로 갱신이 빠지던 문제를 완화(실주문은 `order_no`·당일 기준, `MOCK_ID` 만 `code` 한정). 체결 폴링은 **`filled_qty > 0`이면 확정**(체결가 0 레코드 포함), 실패 시 `exchange_fill_late_reconfirm_sec` 지연 후 `boost` 재시도·`sync_with_actual_account(force=True)` 예약(`data_manager_v2.py`, `core/fill_reconciliation_service.py`, `core/defaults.py`).
- **잔고 없음 매도 SUCCESS → FILLED**: 시장가뿐 아니라 **지정가(00)** 도 동일 보정; 승격 행에 대해 로컬 보유가 남아 있으면 **`on_fill`** 로 청산 정합(`data_manager_v2.patch_market_sell_success_when_not_in_holdings`, `core/portfolio_manager.py`).
- **주문 로그 UI**: `SUCCESS` 이지만 체결 수량이 있으면 상태를 **성공**으로 표시(`ui/tabs/virtual_tab.py`).

## 0.8.17 (2026-05-15)

- **실시간 터치호가 주문**: `realtime_touch_quote_orders`(기본 `true`)일 때 S1·EXIT·S2 REST 주문을 시장가 대신 **매수=매도1호가·매도=매수1호가** 지정가(`ka10004`)로 접수. 미체결 시 `touch_quote_order_poll_sec`(기본 5초)마다 호가 재조회 후 **`kt10002` 정정**(`touch_quote_order_max_amends`, 기본 3회), 이후 취소는 기존 지정가 타임아웃 경로(`core/trading_controller.py`, `core/limit_order_watch_service.py`, `core/order_manager.py`).
- **주문번호 remap**: 정정으로 `order_no`가 바뀌면 `order_history` 갱신 및 `pending_exit_ord_no` / `s2_pending_ord_no` 동기화(`data_manager_v2.remap_order_history_order_no`).
- **설정**: `defaults`·`settings.json`에 `realtime_touch_quote_orders`, `touch_quote_order_poll_sec`, `touch_quote_order_max_amends`. 감시 탭 테스트 주문 터치 모드 연동(`ui/tabs/monitor_tab.py`).

## 0.8.16 (2026-05-15)

- **시장가 매도 접수 → 체결 UI**: 잔고에 종목이 없는데 당일 `order_type=03` 매도가 `SUCCESS`만 남은 경우 `FILLED`로 승격(`data_manager_v2.patch_market_sell_success_when_not_in_holdings`). 계좌 동기화 직후·`kt00007` 백그라운드 병합 후 각각 호출(`core/portfolio_manager.py`). 보유 API 0건·로컬 포지션만 있을 때는 오판 방지로 스킵.
- **주문 로그 코드 정규화**: `log_order` INSERT 시 `canonical_stock_code`로 6자리 통일(`data_manager_v2.py`).
- **가상 DB JSON 저장**: `virtual_history` 등에 `pandas.Timestamp`·`numpy.datetime64` 등이 섞일 때 `json.dump` 실패하던 문제 — 저장 전 `_sanitize_for_virtual_db_json` 적용(`core/portfolio_manager.py`).

## 0.8.15 (2026-05-15)

- **실시간 기본 시장가**: `settings.json` / `defaults` 에 `realtime_use_market_orders`(기본 `true`). S1 매수·EXIT 매도·S2 추매 REST 주문을 `03`(시장가)로 전송, 시장가는 미체결 취소 타이머 없이 메타만 등록(`core/trading_controller.py`, `core/limit_order_watch_service.py`). 체결 시 매수 쪽 `limit_order_watch` 정리(`core/fill_reconciliation_service.py`).
- **지정가 미체결 타임아웃**: 기본값 45초 등(`settings.json`, `core/defaults.py`, `limit_order_watch_service` 폴백).
- **주문·체결 DB**: 동일 `order_no`에 접수·KT00007 행이 공존할 때 체결 우선 조회(`data_manager_v2.get_order_history_row_by_order_no`). 체결 동기화에서 취소/거부만 있고 체결 수량 0이면 폴링 조기 종료(`fill_reconciliation_service`).
- **비상 전량 매도**: 시장가 `03` 고정(`core/portfolio_manager.py`). 감시 탭 테스트 주문 문구·유형 연동(`ui/tabs/monitor_tab.py`). `order_manager` 주석 보강.

## 0.8.14 (2026-05-14)

- **SignalEngine 리플레이** (`tools/replay_realtime_signal_engine_day.py`): `--drive db_1m_closes` 로 DB 공식 1분봉만 순회(세션 JSONL 불필요). 리플레이 구간 동안 `core.signal_engine.is_krx_regular_session` 을 장중으로 고정해 장외 실행 시 S1 전면 차단을 방지. **매 틱 직전 `bars_data` 를 해당 시각 이하로 슬라이스**해, EOD 통째 DataFrame 때문에 `_current_slot_key_from_bars`·`iloc[-1]` 이 말미 슬롯만 보이던 문제를 제거.
- **`_current_slot_key_from_bars`**: 실시간(스트림 갱신) vs 검증용 통째 DF 차이를 설명하는 주석 추가 (`core/signal_engine.py`).
- **도구**: `tools/audit_krx_bars_db_day.py` — `trading_data.db` 1m/15m 시각이 KRX 격자·동시호가(15:20~) 구간과 어긋나는지 점검. `tools/verify_db_unified_vs_dynamic_intraday.py` — 동적 백테 고정밀 체결 vs `evaluate_bt_hp_intrabar_unified` 치환 경로 비교 출력.

## 0.8.13 (2026-05-14)

- **실시간 S1 영구 정지 버그**: 워밍업이 `last_sig_cache`/`last_row_cache`만 채운 뒤 `_s1_full_calc_last_ts`가 비어 있으면 `need_full_calc`가 항상 False로만 이어져 **풀 지표 재계산·인트라바 미러가 한 번도 실행되지 않던** 문제 수정. `last_calc_ts <= 0` 분기에서 스로틀 앵커를 시드(`core/signal_engine.py`).
- **진단 로그**: `signal_engine_wl_only_state`의 `bars_ready`를 15분 봉 수 계산 **이후**에 기록하도록 이동(이전에는 항상 null).

## 0.8.12 (2026-05-11)

- **실시간 인트라바 미러(보유)**: `realtime_s1_mirror_dynamic_intrabar` 가 켜져 있고 공식 1분 윈도우가 있으면, 보유 종목의 **청산(EXIT)·추매(S2)** 도 `evaluate_bt_hp_intrabar_unified` 결과로 판정하고(참고가는 인트라바 보수 체결가), M1 미구축 시에만 기존 `StrategyEngine.evaluate_position` 틱 경로로 폴백(`core/signal_engine.py`). S1 미러용 15m·M1 조립 로직을 `_intrabar_mirror_get_full15m_and_m1` 로 공통화.

## 0.8.11 (2026-05-11)

- **실시간 S1 신규·동적 고정밀 정합**: `realtime_s1_mirror_dynamic_intrabar`(기본 `True`)일 때 미보유 종목의 전체 지표 재계산 직후, `evaluate_bt_hp_intrabar_unified`로 **동적 백테 인트라바와 동일한 S1 신규 판정**을 반영해 `is_buy`·`last_sig`·`last_row`를 덮어씀(`core/defaults.py`, `core/signal_engine.py`).

## 0.8.10 (2026-05-13)

- **실시간 보유 시 UI 부하**: `TradingStrategy.evaluate_position`에서 `bars_df` 기준 `_peak_high_from_bars_df`를 **매 초·매 틱마다 전량 스캔**하던 경로를, 봉 길이·마지막 슬롯·마지막 봉 High·진입 앵커가 같을 때 **포지션 캐시 재사용**으로 줄임(`backtest_engine.py`). `now_price` 기반 고가 갱신은 기존과 동일.

## 0.8.9 (2026-05-13)

- **호가단위(`get_tick_size`)**: 10원·100원 구간 상한을 거래소 기준에 맞게 조정(10원: 1만~2만 미만, 100원: 5만~20만 미만). 잘못된 틱으로 지정가 ±1호가가 어긋나던 원인 완화(`utils/normalization.py`).
- **매도 미체결 타임아웃 후 800033**: 부분 체결 뒤 잔량 취소 시 로컬 보유 수량이 남아 **전량 매도**를 재시도하던 레이스를 줄이기 위해, 취소 직후 **kt00007 병합·`sync_with_actual_account(force=True)`** 로 잔고를 맞춘 뒤 `exit_pending` 해제(`core/limit_order_watch_service.py`).

## 0.8.8 (2026-05-13)

- **감시 신호상태·추매(S2) 완료 표시**: `stage>=2` 이면 추매 지연과 무관하게 「추매(S2) 완료」로 덮어쓰던 로직을, **S1 진입 시각 기준 `s2_delay_hours` 충족 시에만** 완료 문구로 표시. 미충족이면 기존과 같이 `추매(A|B) N/M` 합치도 표시(`TradingStrategy.s2_delay_elapsed_for_position`, `core/signal_engine.py`, `backtest_engine.py`).

## 0.8.7 (2026-05-13)

- **보유 체결 직후 UI 멈춤**: `vp_price_stub` 인데 틱·유효 `last_price` 가 없을 때 `_monitor_live_price` 가 **메인 스레드에서 키움 REST(동기)** 를 호출해 Tk가 블로킹되던 경로 제거. 스텁 구간은 평단만 잠시 표시용으로 쓰고, 틱 수신 후 정상화(`ui/tabs/virtual_tab.py`).

## 0.8.6 (2026-05-13)

- **감시 트리 신호상태 열**: ``status_text or confluence`` 순서 때문에 ``status_text`` 가 ``"대기"`` 등으로 남으면 **틱마다 갱신되는 ``confluence``(합치도)가 가려져** 숫자·문구가 멈춘 것처럼 보이던 문제 수정. 유효한 ``confluence`` 를 우선 표시하도록 ``core/monitor_signal_status_display.py`` 로 통일(`core/signal_engine.py`, `k_trader_gui.py`, `ui/tabs/monitor_tab.py`).

## 0.8.5 (2026-05-13)

- **`SignalEngine._calculate_realtime_rsi` 누락**: RSI 본문이 `_tick_watchlist_synthetic_s2_exit` 안에 잘못 붙어 `AttributeError`로 틱 처리가 반복 실패하던 문제를 **독립 메서드로 복구**(`core/signal_engine.py`).

## 0.8.4 (2026-05-13)

- **관심목록 다이얼로그**: 추가·삭제 시 `watchlist_favorites.json` **자동 저장**, 창 닫기(X)·「닫기」에서도 저장 시도. 기존「파일에 저장」은 **「지금 목록을 파일에 쓰기」**로 바꿔 수동 재기록·확인용임을 분명히 함(`ui/watchlist_dialog.py`).

## 0.8.3 (2026-05-13)

- **실시간 감시 시작 표시**: 워밍업·틱 수신 대상은 일일 선정에 **관심목록·가상보유**를 합친 값인데, 기존에는 합계만 `N종목`으로 표시되어 선정 수와 혼동되기 쉬웠음. 상태줄을 `일일 선정 A · 실시간 수신 B` 형태로 구분(`core/monitor_started_display.py`, `k_trader_gui.py`, `core/ui_trading_status_service.py`, `core/warmup_stream_prep_service.py`, `core/trading_controller.py`).
- **진단 로그**: 선정 수와 병합 수가 다를 때 `watchlist_favorites.json` 경로와 관심·보유 건수를 한 줄로 안내(`core/warmup_stream_prep_service.py`).

## 0.8.2 (2026-05-13)

- **일일 감시목록 선정**: 선행 **일봉 DB 하베스팅 제거**, `select_monitoring_targets(..., api_only, kiwoom_access_token=None)` 로 **FDR 일봉만** 사용(`ui/tabs/monitor_tab.py`, `core/selection_service.py`).
- **`KiwoomDailyHarvestService` 제거**: `core/kiwoom_daily_harvest_service.py` 삭제(감시 탭이 유일 호출부였음).
- **FDR 일봉 조회**: `KRX:{코드}` 경로가 KRX 포털 **400·본문 LOGOUT** 으로 실패하는 경우가 많아, **6자리 종목코드 + (시작, 종료)** 직접 `DataReader` 호출로 변경(`strategy_selector.py`).

## 0.8.1 (2026-05-12)

- **실시간 체결 틱 JSONL 디스크 저장 제거**: `core/realtime_tick_recorder.py` 삭제, `KiwoomApp` 연동·종료 `shutdown` 제거, 설정 키 `realtime_tick_recorder_*` 삭제(`k_trader_gui.py`, `core/defaults.py`, `settings.json`). 틱 배치·신호 경로(`RealtimeTickDispatchService`)는 유지.

## 0.8.0 (2026-05-12)

- **실시간 신호용 봉( S1 / S2 / 청산 )**: 틱으로 15분 OHLCV를 쌓지 않고 **공식 1분(ka10080)** 만으로 현재 슬롯 마지막 행을 갱신·**슬롯 전환 시 롤** 후 `SignalEngine` 재평가 — `core/realtime_official_bars_sync.py`, `Rt15mSlotBoundaryWatcher`, `core/rt_watchlist_m1_api_poller.py`, `core/signal_engine.py`, `core/stream_processor.py`, `core/rt_tick_m1_store.py`, `k_trader_gui.py`, `core/trading_controller.py`.
- **주문가·틱 UI**: 체결 틱 기반 `monitor_info` 등은 기존 유지(신호 봉과 분리).
- **레거시 실시간 설정 제거**: 인과 S1·BT-HP 실시간 브리지·봉 갱신 스로틀 관련 키·코드 정리(`core/defaults.py`, `settings.json`, `core/bt_hp_signal_bridge.py` 주석).
- **ka10080 HTTP 429 완화**: `fetch_kiwoom_minute_chart_page`에 **전역 최소 호출 간격**·429 시 쿨다운·재시도 강화(`data_manager_v2.py`, `config.py`, `realtime_ka10080_global_min_interval_sec`).

## 0.7.19 (2026-05-12)

- **설정 단순화(실시간 정합성)**: `realtime_closed_15m_official_replace_enabled`, `realtime_watchlist_m1_api_enabled`, `realtime_causal_s1_blend_rt_m1_last_row` 제거 — 동작은 각각 **항상 활성**(감시 중·조건 충족 시). 튜닝용 `*_delay_sec`, `*_spread_sec`, 재시도·캐시 상한 등은 유지(`core/rt_closed_15m_official_replace.py`, `core/rt_watchlist_m1_api_poller.py`, `core/signal_engine.py`, `core/defaults.py`, `settings.json`). `config_handler`의 로드 시 강제 `True` 덮어쓰기 제거.

## 0.7.18 (2026-05-12)

- **실시간 확정 15분봉 REST 치환**: 슬롯 전환 직후 틱으로 확정된 봉을 저장한 뒤, 짧은 지연·재시도·페이지 상한으로 **ka10080(설정 `realtime_warmup_ka10080_tic_scope`, 기본 15분)** 을 조회해 **완성 봉 한 줄만** `bars_data`·RT15m DB에 덮어씀 — 틱 누락 시 OHLCV 정합성 완화(`core/rt_closed_15m_official_replace.py`, `k_trader_gui.py`). 설정: `realtime_closed_15m_official_replace_*` (`core/defaults.py`).

## 0.7.17 (2026-05-12)

- **지정가 참고가(주문 시점)**: S1 매수·EXIT 매도·S2 추매에서 신호 시점 가격 대신 **`_live_submit_reference_price`** 로 주문 직전 시세를 사용 — 메인 스레드는 트리 표시가 → `monitor_info` 틱 캐시 → 신호 힌트 순, 주문 워커는 Tk 없이 캐시·힌트(`core/trading_controller.py`). `buy_execute_start`에 `signal_hint_price` 보존.

## 0.7.16 (2026-05-12)

- **실시간 고정 프리시그널 게이트**: `get_buy_signals_detailed` 결과에 `Ready` 키가 없을 때 `ready`가 항상 거짓이 되어 매수가 막히던 문제를 **`_s1_presignal_ready_for_gate`** 로 수정 — 키가 없으면 `Signal`과 동기(`core/signal_engine.py`).
- **실시간 인과 S1·백테 1분 정렬**: KRX 15m 라벨 대응 1분 창(`m1_window_start_for_15m_end`, **`m1_bar_open_label_for_just_completed_minute`**, **`m1_index_bounds_for_15m_end`**)과 **`realtime_causal_s1_eval_on_m1_close`** 로, 분이 닫힐 때마다 interval 스로틀을 한 번 건너뛰어 고정밀 백테 스텝에 맞춤(`core/signal_engine.py`, `utils/krx_bars.py`, `settings.json`).
- **검증 도구**: `tools/verify_rt_gate_at_signal_bars.py`, `tools/verify_shinsegae_hp_m1_buy_1301.py`, `tools/verify_m1_close_label_krx.py`, `tools/compare_rt_tick_vs_bt_15m.py`, `tools/_replay_lg_003550_rt_filters.py` 등.

## 0.7.15 (2026-05-12)

- **지정가 미체결 타임아웃 + RC4033**: 취소 거부(미체결 없음·통상 전량 체결) 시 주문 로그를 `CANCELLED`로 덮지 않고, **`no_open_qty`** 로 구분(`core/order_manager.py`). 매도(`exit_sell`)는 **`exit_pending` 유지** + **`reconcile_exchange_fill_after_order_accept(..., boost_polling=True)`** 로 kt00007 재폴링 강화(`core/limit_order_watch_service.py`, `core/fill_reconciliation_service.py`, `k_trader_gui.py`).
- **체결 동기화 중복**: 부스트 재폴링이 기존 `inflight`와 충돌하지 않도록 **`boost_after_other`** 대기 후 재진입(`core/fill_reconciliation_service.py`).
- **800033 매도가능수량 부족**: 거절 메시지 감지 시 **`exit_pending` 해제** + kt00007 병합·**`sync_with_actual_account(force=True)`** 예약(`core/order_manager.py` `last_order_reject_msg`, `core/trading_controller.py`).

## 0.7.14 (2026-05-12)

- **주문·체결 UI**: 계좌 동기화(ka10085) 후에도 `order_history`가 `SUCCESS`(접수)만 남아 포지션과 어긋나던 문제 — `patch_buy_filled_price_after_reconcile`에서 잔고 수량·주문 수량 일치 시 **`FILLED`·`filled_qty`** 로 승격(`data_manager_v2.py`).
- **주문 사유 `NONE`**: `Sig_Type`이 NONE일 때 그대로 주문 `reason`에 저장되던 것을 **`effective_s1_sig_type_for_buy`** 에서 `TYPE_A`/`TYPE_B` 외는 `TYPE_B`로 정규화(`backtest_engine.py`).
- **포지션 탭 주문 내역**: 사유 표시에서 `NONE`/`NAN`/빈값은 **`—`**; `virtual_tab.log_order`가 DB에 **`name`/`reason`** 을 올바르게 넘기도록 수정(`ui/tabs/virtual_tab.py`).

## 0.7.13 (2026-05-12)

- **실시간 인과 S1**(`core/signal_engine.py`, `core/defaults.py`): tail 룩백으로 `calculate_indicators` + `get_buy_signals_detailed` 재실행, 지문·초당 평가 예산·간격(`realtime_causal_s1_interval_sec` 기본 15초). `last_sig`에 `Met_A`/`Tot_A` 등 상세 행 전부 유지해 **신호상태 합치도**가 0/1로 붕괴하지 않도록 수정.
- **BT-HP 브리지 분리**: 브리지는 **보유 시 EXIT/S2** 판단에만 사용, 미보유 S1은 인과·일반 15분 경로만 사용(`_maybe_refresh_bt_hp_bridge_eval`, S1 분기 정리). `core/bt_hp_signal_bridge.py` 주석 반영.
- **백테 고정밀 인과 S1**(`backtest_engine.py`): 인트라바 S1 진입을 인과 tail 재평가 경로와 정렬(기존 세션 작업 포함).
- **로컬 설정 예시**: `settings.json`에 `realtime_causal_s1_enabled` / `realtime_causal_s1_interval_sec`. `monitor_column_widths.json` 소폭 조정.

## 0.7.12 (2026-05-12)

- **실시간 감시 웹소켓**(`ui/tabs/monitor_tab.py`): `0s` **fid215** 가 모의 등에서 `'0'`/`'00'` 으로 올 때 **명시적 장외**로 오인하지 않고 비어 있음과 동일 처리 → PC 시각 `is_krx_regular_session()` 폴백으로 **0B 체결 틱이 다시 반영**되도록 수정(REG 시 진단 플래그 리셋 포함).

## 0.7.11 (2026-05-12)

- **실시간 틱 JSONL**: `RealtimeTickRecorder` — KRX **거래일**(`is_trading_day`)이면서 **정규장 09:00~15:30**(로컬 시각, `is_krx_regular_session`)인 체결만 저장. 장전·시간외·휴장일·주말은 기록하지 않음(`core/realtime_tick_recorder.py`).

## 0.7.10 (2026-05-12)

- **감시 선정 일봉 준비**: `KiwoomDailyHarvestService` — DB가 이미 기준 장마감일·1m 대비 동기화된 종목은 `1m→1d` 재집계·키움 ka10081 호출을 **생략**(`_code_needs_1m_to_1d_rebuild`, 완결성 선검사).
- **`rebuild_daily_bars_from_1m`**: `DataManagerV2` — 전 구간 1m 풀스캔 대신 **마지막 일봉 다음날~1m 상한일** 구간만 `rebuild_daily_bars_from_1m_range`로 부분 집계. 일봉 없을 때만 1m MIN~MAX 전체.

## 0.7.9 (2026-05-11)

- **키움 일봉 REST**: `core/kiwoom_daily_bars.py`(`ka10081`, 429 백오프·로그), `core/kiwoom_daily_harvest_service.py`(KODEX200 일봉 준비·완결성 `MAX(datetime)` 수정).
- **감시 탭 선정**: `monitor_pre_selection_bars_1d_source` / `monitor_pre_selection_rebuild_sleep_sec`(`core/defaults.py`), 진행 표시·`db_only` 선정·`selected_targets.csv` 저장 흐름(`ui/tabs/monitor_tab.py`).
- **`SelectionService`**: `api_only` 선정 시 `kiwoom_access_token` 전달로 키움 일봉 경로 우선(`core/selection_service.py`).
- **`strategy_selector`·`data_manager_v2`**: 선정·일봉·키움 호출 페이싱 등 누적 수정.
- **도구**: `tools/compare_selection_csvs.py`, `tools/compare_selection_sources.py`, `tools/diagnose_daily_bar_sources.py`, `tools/run_daily_selection_from_db.py`(선정 비교·일봉 소스 진단·DB 선정 실행).
- `monitor_column_widths.json` UI 폭 조정.

## 0.7.8 (2026-05-11)

- 감시 **선정 직전 `bars_1d` 기본 소스**를 **`bars_1m` 전 구간 집계**(`rebuild_daily_bars_from_1m`)로 통일. 동적 백테·`prepare_dynamic_universe`와 동일 파이프라인이라 경로에 관계 없이 일봉 정의가 맞춰짐. 레거시 키움 일봉 직입은 `monitor_pre_selection_bars_1d_source`: `kiwoom_ka10081` 로 선택.
- `KiwoomDailyHarvestService.check_daily_integrity`: `get_daily_bars(..., limit=1)`(ASC라 **최초** 일자만 조회) 오류를 **`MAX(datetime)`** 기준 최신일 검사로 수정.
- `monitor_pre_selection_rebuild_sleep_sec`(기본 0.02): 1m→1d 루프 시 종목 간 간격.

## 0.7.7 (2026-05-11)

- BT-HP 브리지(`realtime_signal_use_bt_hp_bridge`): **S1 신규만**이 아니라 **청산(EXIT)·추매(S2)** 도 동적 백테 `_process_intra_bar_dynamic` 과 동일한 1분 인트라바 루프로 판정. `core/bt_hp_intrabar_unified.py` + `SignalEngine` 포지션 액션 분기. 보유 시 `strategy_engine.evaluate_position` 틱 단발은 동 설정에서 생략(인트라바 결과만 사용).
- `dynamic_backtest.py`: 1분 보수적 체결가 헬퍼를 `bt_hp_intrabar_unified` 로 이관·공유.

## 0.7.6 (2026-05-11)

- 실시간 **체결 틱 저장**: `core/realtime_tick_recorder.py` + `KiwoomApp._on_realtime_data_received`. 호가 전용(`_orderbook_only`)은 제외. 일자별 `realtime_tick_logs/YYYY-MM-DD_trades_{mock|live}.jsonl`. 설정 `realtime_tick_recorder_enabled`(기본 False), `realtime_tick_recorder_subdir`, `realtime_tick_recorder_batch_max`, `realtime_tick_recorder_flush_wait_sec`. 종료 시 큐 flush.

## 0.7.5 (2026-05-11)

- BT-HP 브리지 1분: **실시간 틱 집계**(`core/rt_tick_m1_store.py` + `StreamProcessor` 동일 틱 feed). HTTP 지연 없음. `realtime_signal_bt_hp_bridge_use_tick_1m`(기본 True), `realtime_signal_bt_hp_bridge_tick_m1_max_rows`(기본 24). `realtime_signal_bt_hp_bridge_use_rest_1m` 기본 **False**(필요 시에만 ka10080 보강).

## 0.7.4 (2026-05-11)

- BT-HP 브리지: **`trading_data.db` 의존 제거(운영 기본)**. (후속 v0.7.5에서 1분 소스를 **틱 집계**로 전환, ka10080 는 선택 보강.)

## 0.7.3 (2026-05-11)

- BT-HP 브리지: **`ka10080`(tic_scope=1)** 로 당일 1분 구간 REST 조회·병합 경로 추가(`resolve_m1_chunk_for_bt_hp_bridge`). 설정 `realtime_signal_bt_hp_bridge_use_rest_1m`(기본 True), `realtime_signal_bt_hp_bridge_ka10080_max_pages`(기본 15). (v0.7.4에서 운영은 `trading_data.db` 없이 ka10080 전용으로 정리.)

## 0.7.2 (2026-05-11)

- 실시간 S1 신규 매수를 **동적 백테 고정밀과 동일 로직**으로 맞출 수 있는 옵션 추가:
  - `core/bt_hp_signal_bridge.py`: `compute_intrabar_indicator_step`(인트라바 지표) + `evaluate_bt_hp_s1_new_entry_only`(Signal∧Ready·트랙·스마트바이) + 1분 구간 조회(당시는 DB 전용, v0.7.3에서 REST 보강).
  - `dynamic_backtest._process_intra_bar_dynamic`는 위 브리지의 `compute_intrabar_indicator_step`를 호출하도록 위임(백테 결과·판정 방식 유지).
  - `SignalEngine`: `realtime_signal_use_bt_hp_bridge`(기본 `False`)가 `True`이고 DB 1분 구간이 있으면 해당 판정으로 `is_buy` 등을 채우고, 관심목록/정규장/쿨다운/주문 경로는 기존과 동일.
  - `core/defaults.py`에 기본 키 추가.

## 0.7.1 (2026-05-11)

- 실시간 감시 웹소켓(`ui/tabs/monitor_tab.py`):
  - `REAL` 패킷 `type` 대소문자 정규화(`0b`/`0d`/`0s`) — `0S`만 오는 환경에서 장운영(`0s`) 분기가 동작하지 않던 문제 수정.
  - `fid215`가 비어 있거나 아직 명시되지 않은 경우: PC 로컬 시각 기준 `is_krx_regular_session()`으로 체결·호가(0B/0D) 통과 폴백; `215`가 비어 있지 않게 수신된 뒤에는 `3`/`4`/`03`/`04`가 아니면 브로커 표시를 우선해 장외로 차단.
  - `REG` 재전송 시 장운영 코드·명시 플래그 초기화; `0s`·틱 스킵 관련 진단 로그 보강.
- 로컬 설정·UI: `settings.json`(백테스트 날짜), `monitor_column_widths.json`(감시 탭 열 너비).

## 0.7.0 (2026-05-08)

- 실시간 S1 매수 판정에 15분 슬롯 고정 pre-signal 게이트 추가 (`core/signal_engine.py`):
  - 동일 슬롯 내에서는 첫 판정 시점의 `Signal/Ready/Sig_Type`를 고정해 틱마다 신호가 on/off로 흔들리는 현상을 완화.
  - 슬롯이 바뀌면 게이트를 새로 생성하며, `realtime_use_fixed_s1_presignal_gate`(기본 ON)으로 동작 제어 가능.
  - 목적: 동적 백테 고정밀 경로의 “15분 pre-signal + 1분 체결” 구조와 RT 신호 게이팅 정합 강화.

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
