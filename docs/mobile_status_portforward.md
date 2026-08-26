# 모바일 현황 API — 공유기 포트포워딩

K-Trader PC에서 읽기 전용 현황 페이지(`mobile_web/`)를 열고, 공유기 포트포워딩으로 외부(LTE 등)에서 접속합니다. **주문·선정·재시작은 없습니다.**

## 1. 앱 설정

1. **전략 설정 → 자금 → 모바일 현황 API**
2. `모바일 현황 API 사용` 체크
3. **PIN 생성·채우기**(숫자 8자리) 후 **설정 저장 및 즉시 적용**
4. `API 포트` 기본 `8765` (내부 listen)
5. `공개 URL` 예: `http://mydomain.ddns.net:28765` (외부에서 치는 주소)
6. (권장) TLS: `mobile_status_tls_cert` / `mobile_status_tls_key`에 PEM 경로

대시보드 **모바일 현황** 칸:

- 가동 여부 확인
- **URL 복사** / **설정 링크** 복사  
  - 설정 링크 = `공개URL/#t=PIN` → 폰 메신저로 보내 열면 **PIN 입력 없이** 접속

기존에 긴 토큰을 쓰고 있었다면 **PIN 생성**으로 교체·저장하세요.

## 2. Windows 방화벽

인바운드 규칙으로 **TCP 8765**(또는 설정한 포트)를 허용합니다.

## 3. 공유기 포트포워딩

| 항목 | 예 |
|------|-----|
| 외부(WAN) 포트 | `28765` (내부와 다르게 두는 것을 권장) |
| 내부 IP | K-Trader PC의 LAN IP (DHCP 고정 권장) |
| 내부 포트 | `8765` |
| 프로토콜 | TCP |

CGNAT(통신사 공유 공인 IP)이면 포트포워딩이 안 될 수 있습니다. 공유기 WAN IP가 `100.64.x.x` 등이면 해당됩니다.

## 4. DDNS

가정 IP가 바뀌면 DDNS(공유기 내장 또는 DuckDNS 등)를 `공개 URL`에 맞춥니다.

## 5. 폰에서 접속

1. LTE(집 Wi‑Fi 아님)로 `공개 URL` 접속
2. **숫자 8자리 PIN** 입력(띄어쓰기 가능) → 잔고·보유·체결·감시 조회  
   또는 대시보드 **설정 링크**를 폰에서 열어 자동 로그인
3. 홈 화면 추가(PWA)는 선택

## 보안

- 읽기만 가능해도 **잔고·보유가 노출**됩니다. 토큰을 채팅/캡처에 남기지 마세요.
- **HTTP(무인증서)** 는 공용 Wi‑Fi에서 토큰 가로채기 위험이 있습니다. 가능하면 HTTPS(인증서)를 쓰세요.
- 토큰은 주기적으로 재발급하세요.
- UPnP로 임의 포트를 열지 마세요.

## API (참고)

모두 `Authorization: Bearer <token>` 필요.

- `GET /api/v1/status`
- `GET /api/v1/portfolio`
- `GET /api/v1/holdings`
- `GET /api/v1/watchlist`
- `GET /api/v1/orders/today`
- `GET /api/v1/trades/recent?limit=30`
- `GET /api/v1/equity/trend?days=30`
- `GET /api/v1/logs?limit=100` (PC 대시보드와 동일 포맷의 최근 로그)
