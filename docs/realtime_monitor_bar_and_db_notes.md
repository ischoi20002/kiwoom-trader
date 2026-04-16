# 실시간 감시 · 봉 데이터 · DB 설계 메모

이 문서는 대화에서 정리한 **실시간 감시용 15분봉**, **봉 개수·ATR**, **전용 DB·메타·주문/스냅샷 분리**에 대한 합의·추천 사항을 한곳에 남긴다. 구현 시 `CHANGELOG.md`·`core/defaults.py`와 함께 참고한다.

---

## 1. 실시간에서 1분봉 vs 15분봉

- **전략·지표·신호**에 쓰는 시계열은 **15분봉**이다. (`bars_data`, `SignalEngine`, `min_15m_bars_for_signal` 등)
- **실시간**에서는 `StreamProcessor`가 틱을 **곧바로 현재 15분 슬롯 봉**에 반영한다. **1분봉을 만든 뒤 15분으로 합치는 구조가 아니다.**
- **1분봉 DB/API**는 주로 **워밍업**에서 `bars_15m`이 비었을 때 **1m→15m 리샘플**로 채우는 **폴백**에 가깝다. 백테스트 하베스팅용 1분과 역할이 다르다.

---

## 2. 최소 봉 개수 (15분봉 기준)

| 구분 | 기본·의미 |
|------|-----------|
| **S1 매수 신호·합치도·지표 풀가동** | `min_15m_bars_for_signal` (**기본 60**). `bars_ready`가 될 때까지 신호·매매 파이프는 보류. |
| **보유 시 청산·S2 등** | `bars_ready`가 아니어도 `_indicator_last_row_for_exit`로 **봉 2개 이상**이면 마지막 지표 행을 만든다. **“2개면 충분”이 아니라**, 예전에 `bars_ready` False면 청산이 멈추던 문제를 막기 위한 **최소 가드**이며, MA·ATR 품질은 **봉이 더 쌓일수록** 나아진다. |

---

## 3. ATR 주기(`atr_period` 등)와 봉 개수

- 설정값(예: **100**)은 **DataFrame의 한 행 = 한 봉** 기준 **기간**이다. 실시간·청산 경로의 봉이 **15분봉**이므로, **이상적으로는 15분봉 100개 구간**이 쌓인 뒤의 ATR이 설정 주기를 충실히 반영한다.
- `strategy/indicators.py`의 `calculate_atr`는 `min_periods=period` 등으로, **그보다 짧으면** 마지막 행이 NaN이거나, 코드상 **직전 유효 ATR·TR 근사** 등으로 이어질 수 있다. **에러를 피하기 위한 근사**에 가깝다.
- **`min_15m_bars_for_signal`(60)** 과 **`atr_period`(100)** 는 별도 knob이다. 60봉 시점에도 ATR은 아직 불완전할 수 있다.

---

## 4. 실시간 전용 15분봉 DB (하베스팅 DB와 분리)

### 4.1 분리 이유

- **속도**만이 아니라 **배포 시 저장소 크기**: 하베스팅(1분 등) 대용량 DB는 배포물에 넣지 않고, **실시간용 가벼운 15분 전용 파일**만 선택적으로 포함·동기화하기 쉽게 한다.

### 4.2 역할

- **종목별 15분 OHLCV 캐시** (예: 최대 1년 롤링). 워밍업 시 **로컬 우선 → 부족 시 API**로 속도·API 호출을 줄인다.
- **감시 리스트 전용 테이블이 아님**: 키는 `(code, datetime)` 등. **매일 감시 200종이 바뀌어도**, 과거에 감시되었던 종목의 봉은 남고, 나중에 다시 뽑히면 **캐시 히트**가 난다.

### 4.3 쓰기 부하

- **완성된 15분봉당 종목당 INSERT 1회** 수준이면 부하는 통상 작다. 그래도 장중 디스크를 최소화하려면 **메모리만 갱신하고, 장후·감시 종료 시 flush** 같은 정책도 가능하다.

### 4.4 메타: `last_seen` (추천)

| 방식 | 요지 |
|------|------|
| **bars만 + 시간 롤링** | 구현 단순. 종목 단위 purge는 `GROUP BY code` 등으로 주기적 집계 필요. |
| **`last_seen`(또는 마지막 봉 시각 메타)** | 감시 온보딩 시 갱신하면, **오래된 종목만 삭제** 등이 단순해지고, 풀 회전이 커질수록 유리. |

- **하이브리드**: 평소는 시간 롤링만, **월 1회** 등으로 `MAX(datetime)` 집계해 오래된 코드만 정리해도 됨.

---

## 5. 주문·체결·신호 스냅샷 DB

- **15분 봉 캐시 DB**와 **합치지 않는 것**을 추천한다 (역할·크기·배포·손실 시 재구축 가능 여부가 다름).
- **매수/매도·주문·체결·스냅샷**은 기존처럼 **상태 DB** (`order_history`, `active_positions`, 스냅 컬럼 등) **한 묶음**으로 두는 구성이 자연스럽다.
- 스냅샷 테이블만 비대해지면 그때 **파일 분리**를 검토하면 된다.

---

## 6. 한 줄 요약

- 실시간 **판단**은 **메모리 15분봉**이 중심이고, **영속화**는 **전용 15분 DB**(하베스팅과 분리, 필요 시 `last_seen`) + **주문/상태는 기존 상태 DB** 유지로 정리한다.

---

## 7. 구현 (2026-04)

- **파일**: 모의 `realtime_15m_mock.db`, 실전 `realtime_15m_live.db` (프로젝트 루트). 경로: `utils/server_mode_paths.realtime_monitor_15m_db_path()`
- **모듈**: `core/realtime_monitor_bars_store.py` — 테이블 `bars_15m_rt`, `symbol_meta_rt`(last_seen)
- **적재**: `core/stream_processor.py` — 15분 슬롯이 바뀔 때 직전 봉을 `upsert_completed_bar`로 저장
- **워밍업**: `core/warmup_service.py` — `realtime_warmup_ohlcv_source == "realtime_15m_db_then_api"` 이면 위 DB 우선 조회 후 `_maybe_api_backfill_15m`. 검증 통과 시 병합 15분 OHLCV를 `bulk_upsert_ohlcv_index_frame`으로 디스크에 반영(다음 실행 캐시). `realtime_warmup_persist_to_rt15m_db` 로 끌 수 있음.
- **롤링**: `realtime_15m_calendar_retention_days`(기본 365) — 캘린더 기준 이전 봉은 종목별 삭제. 조회 시에도 동일 cutoff로 필터.
- **워밍업 깊이**: `realtime_warmup_fetch_calendar_history` — True이면 ka10080 을 위 일수만큼 과거까지 채운 뒤 조기 종료(페이지 상한 `realtime_warmup_ka10080_max_pages_deep`). False이면 예전처럼 신호용 최소봉·당일 갭만 보충.
- **기본 설정**: `core/defaults.py` 의 `realtime_warmup_ohlcv_source` 기본값을 `realtime_15m_db_then_api` 로 두어 실시간 DB를 쓴다. 하베스팅 DB만 쓰려면 `db_then_api`, API만이면 `api_only`.

---

## 8. 관련 코드·설정 (참고)

- `core/signal_engine.py`: `min_15m_bars_for_signal`, `bars_ready`, `_indicator_last_row_for_exit`
- `strategy/indicators.py`: `calculate_atr`, `atr_period_a` / `atr_period_b`
- `core/defaults.py`: `min_15m_bars_for_signal`, `realtime_warmup_*`, `atr_period*`
- `core/stream_processor.py`: 실시간 15분 슬롯 봉 갱신
- `data_manager_v2.py`: `bars_1m` / `bars_15m` (백테·하베스팅 계열)
- `core/warmup_service.py`: 워밍업·DB/API 소스
- `core/realtime_monitor_bars_store.py`, `k_trader_gui.py`: 실시간 전용 DB 스토어·`StreamProcessor` 콜백 연결

---

*문서 추가일: 2026-04-16 · 구현 섹션 갱신: 2026-04-16*
