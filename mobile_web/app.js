/* 모바일 읽기 전용 대시보드 */
(function () {
  "use strict";

  var TOKEN_KEY = "ktrader_status_token";
  var POLL_MS = 8000;

  var loginView = document.getElementById("login-view");
  var dashView = document.getElementById("dash-view");
  var loginForm = document.getElementById("login-form");
  var tokenInput = document.getElementById("token-input");
  var loginError = document.getElementById("login-error");
  var btnLogout = document.getElementById("btn-logout");
  var btnWatch = document.getElementById("btn-watchlist");
  var watchPanel = document.getElementById("watchlist-panel");
  var connState = document.getElementById("conn-state");

  var pollTimer = null;
  var lastOk = false;

  function getToken() {
    try {
      return normalizeToken(sessionStorage.getItem(TOKEN_KEY) || "");
    } catch (e) {
      return "";
    }
  }

  function setToken(t) {
    try {
      sessionStorage.setItem(TOKEN_KEY, normalizeToken(t));
    } catch (e) {}
  }

  function normalizeToken(t) {
    return String(t || "").replace(/[\s\-]/g, "");
  }

  function consumeHashToken() {
    try {
      var hash = String(location.hash || "");
      if (!hash) return false;
      var m = hash.match(/(?:^[#&]|[?&])t=([^&]+)/i) || hash.match(/^#t=([^&]+)/i);
      if (!m) {
        // #48291033 형태도 허용
        var bare = hash.replace(/^#/, "");
        if (/^\d{6,12}$/.test(bare)) {
          setToken(bare);
          history.replaceState(null, "", location.pathname + location.search);
          return true;
        }
        return false;
      }
      setToken(decodeURIComponent(m[1]));
      history.replaceState(null, "", location.pathname + location.search);
      return true;
    } catch (e) {
      return false;
    }
  }

  function clearToken() {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }

  function showLogin(msg) {
    stopPoll();
    dashView.classList.add("hidden");
    loginView.classList.remove("hidden");
    if (msg) {
      loginError.hidden = false;
      loginError.textContent = msg;
    } else {
      loginError.hidden = true;
      loginError.textContent = "";
    }
  }

  function showDash() {
    loginView.classList.add("hidden");
    dashView.classList.remove("hidden");
  }

  function fmtMoney(n) {
    var v = Number(n);
    if (!isFinite(v)) return "—";
    return Math.round(v).toLocaleString("ko-KR");
  }

  function fmtPct(n) {
    var v = Number(n);
    if (!isFinite(v)) return "—";
    var sign = v > 0 ? "+" : "";
    return sign + v.toFixed(2) + "%";
  }

  function clsPnL(n) {
    var v = Number(n);
    if (!isFinite(v) || v === 0) return "";
    return v > 0 ? "pos" : "neg";
  }

  function apiGet(path) {
    var token = getToken();
    return fetch(path, {
      method: "GET",
      headers: {
        Authorization: "Bearer " + token,
        Accept: "application/json",
      },
      cache: "no-store",
    }).then(function (res) {
      if (res.status === 401 || res.status === 429) {
        var err = new Error("unauthorized");
        err.status = res.status;
        throw err;
      }
      if (!res.ok) {
        var err2 = new Error("http_" + res.status);
        err2.status = res.status;
        throw err2;
      }
      return res.json();
    });
  }

  function setConn(ok) {
    lastOk = !!ok;
    connState.textContent = ok ? "연결" : "끊김";
    connState.className = "conn " + (ok ? "ok" : "bad");
  }

  function renderBadges(status) {
    var el = document.getElementById("badges");
    var mode = status && status.mode === "live" ? "live" : "mock";
    var mon = !!(status && status.is_monitoring);
    el.innerHTML =
      '<span class="badge ' +
      mode +
      '">' +
      (mode === "live" ? "실전" : "모의") +
      "</span>" +
      '<span class="badge ' +
      (mon ? "on" : "off") +
      '">감시 ' +
      (mon ? "ON" : "OFF") +
      "</span>" +
      '<span class="badge">선정 ' +
      ((status && status.selection_count) || 0) +
      "</span>";
  }

  function renderPortfolio(p) {
    var pnlEl = document.getElementById("day-pnl");
    if (p.day_pnl_na) {
      pnlEl.textContent = "N/A";
      pnlEl.className = "pnl";
    } else if (p.day_pnl_amt == null) {
      pnlEl.textContent = "—";
      pnlEl.className = "pnl";
    } else {
      pnlEl.textContent =
        fmtMoney(p.day_pnl_amt) + " (" + fmtPct(p.day_pnl_pct) + ")";
      pnlEl.className = "pnl " + clsPnL(p.day_pnl_amt);
    }
    document.getElementById("total-assets").textContent = fmtMoney(p.total_assets);
    document.getElementById("cash").textContent = fmtMoney(p.cash);
    document.getElementById("stock-eval").textContent = fmtMoney(p.stock_eval);
  }

  function renderHoldings(data) {
    var list = document.getElementById("holdings-list");
    var rows = (data && data.holdings) || [];
    document.getElementById("holdings-count").textContent = rows.length + "종목";
    if (!rows.length) {
      list.innerHTML = '<div class="empty">보유 없음</div>';
      return;
    }
    list.innerHTML = rows
      .map(function (h) {
        var pct = clsPnL(h.unrealized_pct);
        return (
          '<div class="row"><div><div class="title">' +
          escapeHtml(h.name || h.code) +
          "</div><div class=\"sub\">" +
          escapeHtml(h.code) +
          " · " +
          (h.qty || 0) +
          "주 · S" +
          (h.stage || 1) +
          '</div></div><div class="right ' +
          pct +
          '"><div>' +
          fmtPct(h.unrealized_pct) +
          "</div><div class=\"sub\">" +
          fmtMoney(h.unrealized_amt) +
          "</div></div></div>"
        );
      })
      .join("");
  }

  function renderOrders(data) {
    var list = document.getElementById("orders-list");
    var rows = (data && data.orders) || [];
    if (!rows.length) {
      list.innerHTML = '<div class="empty">오늘 체결/주문 없음</div>';
      return;
    }
    list.innerHTML = rows
      .slice(0, 20)
      .map(function (o) {
        var side = String(o.side || "");
        return (
          '<div class="row"><div><div class="title">' +
          escapeHtml(o.name || o.code) +
          "</div><div class=\"sub\">" +
          escapeHtml(String(o.timestamp || "").slice(11, 19)) +
          " · " +
          escapeHtml(side) +
          " · " +
          (o.filled_qty || o.qty || 0) +
          "주</div></div><div class=\"right\"><div>" +
          fmtMoney(o.filled_price || o.price) +
          '</div><div class="sub">' +
          escapeHtml(o.result || "") +
          "</div></div></div>"
        );
      })
      .join("");
  }

  function renderWatchlist(data) {
    var list = document.getElementById("watchlist-list");
    var rows = (data && data.targets) || [];
    document.getElementById("watch-count").textContent = rows.length + "종목";
    if (!rows.length) {
      list.innerHTML = '<div class="empty">감시목록 없음</div>';
      return;
    }
    list.innerHTML = rows
      .slice(0, 40)
      .map(function (t) {
        return (
          '<div class="row"><div><div class="title">' +
          escapeHtml(t.name || t.code) +
          '</div><div class="sub">' +
          escapeHtml(t.code) +
          " · " +
          escapeHtml(t.sel_track || "") +
          " · " +
          escapeHtml(t.confluence || t.status_text || "") +
          '</div></div><div class="right"><div>' +
          fmtMoney(t.last_price) +
          '</div><div class="sub">' +
          fmtPct(t.ratio) +
          "</div></div></div>"
        );
      })
      .join("");
  }

  function renderEquity(data) {
    var canvas = document.getElementById("equity-chart");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 360;
    var h = 120;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var pts = ((data && data.points) || []).filter(function (p) {
      return isFinite(Number(p.total_assets));
    });
    if (pts.length < 2) {
      ctx.fillStyle = "#8C94A8";
      ctx.font = "12px sans-serif";
      ctx.fillText(
        pts.length === 0 ? "추이 스냅샷 없음" : "추이 데이터 부족 (2일 이상 필요)",
        8,
        24
      );
      return;
    }
    var vals = pts.map(function (p) {
      return Number(p.total_assets);
    });
    var min = Math.min.apply(null, vals);
    var max = Math.max.apply(null, vals);
    if (max <= min) max = min + 1;
    var pad = 8;
    ctx.beginPath();
    pts.forEach(function (p, i) {
      var x = pad + ((w - pad * 2) * i) / (pts.length - 1);
      var y = h - pad - ((Number(p.total_assets) - min) / (max - min)) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#88C0D0";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function refresh() {
    return Promise.all([
      apiGet("/api/v1/status"),
      apiGet("/api/v1/portfolio"),
      apiGet("/api/v1/holdings"),
      apiGet("/api/v1/orders/today"),
      apiGet("/api/v1/watchlist"),
      apiGet("/api/v1/equity/trend?days=30"),
    ])
      .then(function (all) {
        var status = all[0];
        var portfolio = all[1];
        var holdings = all[2];
        var orders = all[3];
        var watch = all[4];
        var equity = all[5];
        renderBadges(status);
        renderPortfolio(portfolio);
        renderHoldings(holdings);
        renderOrders(orders);
        renderWatchlist(watch);
        renderEquity(equity);
        document.getElementById("server-time").textContent =
          "갱신 " + (portfolio.server_time || status.server_time || "");
        setConn(true);
      })
      .catch(function (err) {
        setConn(false);
        if (err && (err.status === 401 || err.message === "unauthorized")) {
          clearToken();
          showLogin("토큰이 유효하지 않습니다.");
        }
      });
  }

  function startPoll() {
    stopPoll();
    refresh();
    pollTimer = setInterval(function () {
      refresh();
    }, POLL_MS);
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  loginForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var t = normalizeToken(tokenInput.value || "");
    if (!t) {
      showLogin("PIN을 입력하세요.");
      loginView.classList.remove("hidden");
      return;
    }
    setToken(t);
    showDash();
    startPoll();
  });

  btnLogout.addEventListener("click", function () {
    clearToken();
    tokenInput.value = "";
    showLogin("");
  });

  btnWatch.addEventListener("click", function () {
    var open = watchPanel.classList.contains("collapsed");
    if (open) {
      watchPanel.classList.remove("collapsed");
      btnWatch.setAttribute("aria-expanded", "true");
    } else {
      watchPanel.classList.add("collapsed");
      btnWatch.setAttribute("aria-expanded", "false");
    }
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    if (getToken()) refresh();
  });

  consumeHashToken();
  if (getToken()) {
    showDash();
    startPoll();
  } else {
    showLogin("");
  }
})();
