# -*- coding: utf-8 -*-
"""
모바일 읽기 전용 상태 API (로컬 HTTP/HTTPS).

- 데몬 스레드에서 ThreadingHTTPServer 가동
- /api/* 는 Bearer 토큰 필수
- 정적 파일은 mobile_web/ 에서 제공
- Tk / Kiwoom REST 호출 금지
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple, Type
from urllib.parse import parse_qs, urlparse

from core import status_snapshot as snap
from utils.server_mode_paths import project_root


LogFn = Callable[[str], None]

# 모바일·스캐너가 요청 중 소켓을 끊을 때 흔한 예외 — traceback 스팸·로그 섞임 방지
_BENIGN_DISCONNECT_EXC: Tuple[Type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """연결 강제 종료 시 Python 기본 handle_error traceback을 억제."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        try:
            import sys

            exc = sys.exc_info()[1]
            if isinstance(exc, _BENIGN_DISCONNECT_EXC):
                return
            # WinError 10054/10053 등 OSError 계열도 무해 끊김으로 취급
            if isinstance(exc, OSError):
                winerr = getattr(exc, "winerror", None)
                errno = getattr(exc, "errno", None)
                if winerr in (10054, 10053, 10058) or errno in (104, 32, 54):
                    return
        except Exception:
            pass
        try:
            super().handle_error(request, client_address)
        except Exception:
            pass


def normalize_status_api_token(raw: Any) -> str:
    """입력·저장 토큰 정규화(공백·하이픈 제거). 숫자 PIN·기존 문자열 모두 허용."""
    try:
        s = str(raw or "").strip()
    except Exception:
        return ""
    try:
        # 모바일 입력 편의: "1234 5678" / "1234-5678" → "12345678"
        return "".join(ch for ch in s if not ch.isspace() and ch != "-")
    except Exception:
        return s


def generate_status_api_token(nbytes: int = 24) -> str:
    """
    모바일 입력용 짧은 접속 토큰.

    기본: 숫자 8자리(PIN). ``nbytes`` 인자는 호환용이며 무시합니다.
    """
    try:
        # 10000000~99999999 — 선행 0 없이 8자리 고정
        n = secrets.randbelow(90_000_000) + 10_000_000
        return f"{n:08d}"
    except Exception:
        try:
            return f"{secrets.randbelow(100_000_000):08d}"
        except Exception:
            return "58291034"


class StatusApiServer:
    """읽기 전용 상태 API 서버 수명주기."""

    def __init__(self, app: Any, log_func: Optional[LogFn] = None) -> None:
        self.app = app
        self._log: LogFn = log_func or (lambda _m: None)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.is_running: bool = False
        self.listen_host: str = "0.0.0.0"
        self.listen_port: int = 8765
        self.using_tls: bool = False
        self.last_error: str = ""
        # 연속 인증 실패 쿨다운 (IP → (fails, blocked_until))
        self._auth_fail: Dict[str, Tuple[int, float]] = {}

    def _settings(self) -> Dict[str, Any]:
        s = getattr(self.app, "settings", None)
        return s if isinstance(s, dict) else {}

    def configured_token(self) -> str:
        try:
            return normalize_status_api_token(
                self._settings().get("mobile_status_api_token")
            )
        except Exception:
            return ""

    def is_enabled(self) -> bool:
        try:
            return bool(self._settings().get("mobile_status_api_enabled", False))
        except Exception:
            return False

    def public_url(self) -> str:
        try:
            return str(self._settings().get("mobile_status_public_url") or "").strip()
        except Exception:
            return ""

    def status_summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.is_enabled(),
            "running": self.is_running,
            "host": self.listen_host,
            "port": self.listen_port,
            "tls": self.using_tls,
            "public_url": self.public_url(),
            "last_error": self.last_error,
            "token_set": bool(self.configured_token()),
        }

    def start(self) -> bool:
        """설정에 따라 서버 기동. 이미 가동 중이면 True."""
        with self._lock:
            if self.is_running and self._httpd is not None:
                return True
            if not self.is_enabled():
                self.last_error = ""
                return False
            token = self.configured_token()
            if not token:
                self.last_error = "토큰이 비어 있어 API를 기동하지 않습니다."
                try:
                    self._log(f"[MobileAPI] {self.last_error}")
                except Exception:
                    pass
                return False

            st = self._settings()
            try:
                host = str(st.get("mobile_status_api_host") or "0.0.0.0").strip() or "0.0.0.0"
            except Exception:
                host = "0.0.0.0"
            try:
                port = int(float(st.get("mobile_status_api_port", 8765)))
            except (TypeError, ValueError):
                port = 8765
            port = max(1, min(port, 65535))

            cert = str(st.get("mobile_status_tls_cert") or "").strip()
            key = str(st.get("mobile_status_tls_key") or "").strip()
            use_tls = bool(cert and key and os.path.isfile(cert) and os.path.isfile(key))

            try:
                handler = self._make_handler()
                httpd = _QuietThreadingHTTPServer((host, port), handler)
                httpd.daemon_threads = True
                if use_tls:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(certfile=cert, keyfile=key)
                    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
                self._httpd = httpd
                self.listen_host = host
                self.listen_port = port
                self.using_tls = use_tls
                self.last_error = ""
                self.is_running = True
            except Exception as e:
                self._httpd = None
                self.is_running = False
                self.last_error = str(e)
                try:
                    self._log(f"[MobileAPI] 기동 실패: {e}")
                except Exception:
                    pass
                return False

            t = threading.Thread(
                target=self._serve_forever,
                name="MobileStatusApi",
                daemon=True,
            )
            self._thread = t
            t.start()
            scheme = "https" if use_tls else "http"
            try:
                self._log(
                    f"[MobileAPI] 기동 {scheme}://{host}:{port} "
                    f"(TLS={'ON' if use_tls else 'OFF'})"
                )
                if not use_tls:
                    self._log(
                        "[MobileAPI] 경고: TLS 미사용(HTTP). "
                        "포트포워딩 시 토큰이 평문으로 노출될 수 있습니다."
                    )
            except Exception:
                pass
            return True

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
            self.is_running = False
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        try:
            self._log("[MobileAPI] 중지")
        except Exception:
            pass

    def restart(self) -> bool:
        """설정 변경 후 재기동."""
        try:
            self.stop()
        except Exception:
            pass
        time.sleep(0.15)
        if not self.is_enabled():
            return False
        return self.start()

    def _serve_forever(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as e:
            try:
                self.last_error = str(e)
                self._log(f"[MobileAPI] 서버 루프 종료: {e}")
            except Exception:
                pass
        finally:
            self.is_running = False

    def _client_blocked(self, ip: str) -> bool:
        try:
            fails, until = self._auth_fail.get(ip, (0, 0.0))
            if until and time.time() < until:
                return True
            if until and time.time() >= until:
                self._auth_fail.pop(ip, None)
        except Exception:
            pass
        return False

    def _note_auth_fail(self, ip: str) -> None:
        try:
            fails, _ = self._auth_fail.get(ip, (0, 0.0))
            fails += 1
            until = 0.0
            if fails >= 8:
                until = time.time() + 60.0
                fails = 0
            self._auth_fail[ip] = (fails, until)
        except Exception:
            pass

    def _note_auth_ok(self, ip: str) -> None:
        try:
            self._auth_fail.pop(ip, None)
        except Exception:
            pass

    def _make_handler(self) -> type:
        server_ref = self
        web_root = os.path.join(project_root(), "mobile_web")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                # 기본 stderr 로그 억제 (대시보드 스팸 방지)
                return

            def handle(self) -> None:
                """원격 호스트 강제 끊김은 traceback 없이 종료."""
                try:
                    super().handle()
                except _BENIGN_DISCONNECT_EXC:
                    return
                except OSError as e:
                    try:
                        winerr = getattr(e, "winerror", None)
                        errno = getattr(e, "errno", None)
                        if winerr in (10054, 10053, 10058) or errno in (104, 32, 54):
                            return
                    except Exception:
                        pass
                    raise

            def _client_ip(self) -> str:
                try:
                    return str(self.client_address[0] or "")
                except Exception:
                    return ""

            def _send(
                self,
                code: int,
                body: bytes,
                content_type: str = "application/json; charset=utf-8",
            ) -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass

            def _json(self, code: int, payload: Dict[str, Any]) -> None:
                try:
                    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                except Exception:
                    raw = b'{"error":"serialize_failed"}'
                self._send(code, raw)

            def _extract_token(self) -> str:
                try:
                    auth = self.headers.get("Authorization") or ""
                    if auth.lower().startswith("bearer "):
                        return normalize_status_api_token(auth[7:])
                except Exception:
                    pass
                # 폴백: 일부 클라이언트용 (권장하지 않음)
                try:
                    qs = parse_qs(urlparse(self.path).query)
                    vals = qs.get("token") or []
                    if vals:
                        return normalize_status_api_token(vals[0])
                except Exception:
                    pass
                return ""

            def _require_api_auth(self) -> bool:
                ip = self._client_ip()
                if server_ref._client_blocked(ip):
                    self._json(429, {"error": "too_many_failures", "message": "잠시 후 다시 시도하세요."})
                    return False
                expected = server_ref.configured_token()
                got = self._extract_token()
                ok = False
                try:
                    if expected and got and len(expected) == len(got):
                        ok = bool(secrets.compare_digest(got, expected))
                    elif expected and got:
                        # 길이 불일치도 실패로 처리 (타이밍 유출 최소화용 더미 비교)
                        secrets.compare_digest(expected, expected)
                        ok = False
                except Exception:
                    ok = False
                if not ok:
                    server_ref._note_auth_fail(ip)
                    self._json(401, {"error": "unauthorized", "message": "인증이 필요합니다."})
                    return False
                server_ref._note_auth_ok(ip)
                return True

            def do_GET(self) -> None:  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    path = parsed.path or "/"
                    if path != "/" and path.endswith("/"):
                        path = path.rstrip("/")

                    if path.startswith("/api/"):
                        if not self._require_api_auth():
                            return
                        self._handle_api(path, parsed)
                        return

                    self._handle_static(path)
                except Exception as e:
                    try:
                        self._json(500, {"error": "internal", "message": str(e)})
                    except Exception:
                        pass

            def do_POST(self) -> None:  # noqa: N802
                # 2단계: 쓰기 API 없음
                self._json(405, {"error": "method_not_allowed"})

            def _handle_api(self, path: str, parsed: Any) -> None:
                app = server_ref.app
                try:
                    if path == "/api/v1/status":
                        self._json(200, snap.build_status(app))
                        return
                    if path == "/api/v1/portfolio":
                        self._json(200, snap.build_portfolio(app))
                        return
                    if path == "/api/v1/holdings":
                        self._json(200, snap.build_holdings(app))
                        return
                    if path == "/api/v1/watchlist":
                        self._json(200, snap.build_watchlist(app))
                        return
                    if path == "/api/v1/orders/today":
                        self._json(200, snap.build_orders_today(app))
                        return
                    if path == "/api/v1/trades/recent":
                        qs = parse_qs(parsed.query)
                        lim = 30
                        try:
                            lim = int((qs.get("limit") or ["30"])[0])
                        except (TypeError, ValueError):
                            lim = 30
                        self._json(200, snap.build_trades_recent(app, lim))
                        return
                    if path == "/api/v1/equity/trend":
                        qs = parse_qs(parsed.query)
                        days = 30
                        try:
                            days = int((qs.get("days") or ["30"])[0])
                        except (TypeError, ValueError):
                            days = 30
                        self._json(200, snap.build_equity_trend(app, days))
                        return
                    if path == "/api/v1/logs":
                        qs = parse_qs(parsed.query)
                        lim = 100
                        try:
                            lim = int((qs.get("limit") or ["100"])[0])
                        except (TypeError, ValueError):
                            lim = 100
                        self._json(200, snap.build_logs(app, lim))
                        return
                    self._json(404, {"error": "not_found"})
                except Exception as e:
                    self._json(500, {"error": "snapshot_failed", "message": str(e)})

            def _handle_static(self, path: str) -> None:
                rel = path.lstrip("/")
                if path in ("/", "/login", "/index.html"):
                    rel = "index.html"
                elif path == "/manifest.webmanifest":
                    rel = "manifest.webmanifest"
                elif path.startswith("/static/"):
                    rel = path.lstrip("/")
                else:
                    # 허용 파일만
                    base = os.path.basename(path)
                    if base in ("app.js", "styles.css", "sw.js"):
                        rel = base
                    else:
                        self._json(404, {"error": "not_found"})
                        return

                # path traversal 방지
                full = os.path.normpath(os.path.join(web_root, rel))
                if not full.startswith(os.path.normpath(web_root)):
                    self._json(403, {"error": "forbidden"})
                    return
                if not os.path.isfile(full):
                    self._json(404, {"error": "not_found"})
                    return
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except Exception as e:
                    self._json(500, {"error": "read_failed", "message": str(e)})
                    return
                ctype, _ = mimetypes.guess_type(full)
                if not ctype:
                    if full.endswith(".js"):
                        ctype = "application/javascript"
                    elif full.endswith(".webmanifest"):
                        ctype = "application/manifest+json"
                    elif full.endswith(".css"):
                        ctype = "text/css"
                    else:
                        ctype = "application/octet-stream"
                if ctype.startswith("text/") or "json" in ctype or "javascript" in ctype:
                    ctype = f"{ctype}; charset=utf-8"
                self._send(200, data, ctype)

        return Handler
