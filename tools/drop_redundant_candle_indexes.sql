-- trading_data.db: PRIMARY KEY (code, datetime) 과 컬럼·순서가 같은 보조 인덱스는
-- 테이블 데이터와 동일 키를 두 번 저장해 용량만 키웁니다.
-- 적용 전 반드시 파일 백업 후, 아래 실행 뒤 필요하면 VACUUM (시간 오래 걸림).
--
-- 유지: idx_bars_1d_date — datetime 단일 컬럼이라 PK와 다름(날짜 범위 조회용).

DROP INDEX IF EXISTS idx_bars_1m_code_dt;
DROP INDEX IF EXISTS idx_bars_15m_code_dt;
DROP INDEX IF EXISTS idx_bars_1d_code_dt;
DROP INDEX IF EXISTS idx_bars_1d_code_date;

-- 인덱스 삭제 후에도 파일 크기는 freelist 로만 돌아가고, 실제 .db 파일 축소는:
--   sqlite3 trading_data.db "VACUUM;"
-- (20GB급이면 수십 분~수 시간 이상일 수 있음)
