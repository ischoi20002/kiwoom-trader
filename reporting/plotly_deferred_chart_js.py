# -*- coding: utf-8 -*-
"""
일일 결산 HTML·가상 매매 종합 리포트 공통:
- `<head>`에는 `window.__DAILY_REPORT_PLOTLY_CDN__` 만 두고 plotly.js 는 첫 펼침 시 로드
- 차트 데이터는 `<script type="application/json" class="plotly-fig-json">` 인라인 (file:// 호환)
"""

# 본문은 그대로 `<script>...</script>` 안에 넣습니다 (외부 f-string 이 아님).
PLOTLY_DEFERRED_CHART_IIFE = r"""
(function () {
  /**
   * - plotly.js: **첫 차트 펼칠 때만** CDN 주입.
   * - 차트 데이터: HTML 내 `<script type="application/json" class="plotly-fig-json">` (file:// 호환).
   * - 구버전 HTML: `plotly-fig-b64` (base64) 폴백.
   */
  var plotlyCdn = window.__DAILY_REPORT_PLOTLY_CDN__ || "";
  var plotlyLoadQueue = null;

  function ensurePlotlyJs(cb) {
    if (window.Plotly) {
      cb();
      return;
    }
    if (plotlyLoadQueue) {
      plotlyLoadQueue.push(cb);
      return;
    }
    plotlyLoadQueue = [cb];
    if (!plotlyCdn) {
      var fns = plotlyLoadQueue;
      plotlyLoadQueue = null;
      fns.forEach(function (fn) {
        try {
          fn();
        } catch (e1) {}
      });
      return;
    }
    var s = document.createElement("script");
    s.src = plotlyCdn;
    s.crossOrigin = "anonymous";
    s.onload = function () {
      var fns = plotlyLoadQueue;
      plotlyLoadQueue = null;
      fns.forEach(function (fn) {
        try {
          fn();
        } catch (e2) {}
      });
    };
    s.onerror = function () {
      plotlyLoadQueue = null;
      try {
        console.warn("Plotly.js 로드 실패:", plotlyCdn);
      } catch (e3) {}
    };
    document.head.appendChild(s);
  }

  function renderDeferredPlotlyIn(root, done) {
    ensurePlotlyJs(function () {
      if (!window.Plotly || !root) {
        if (done) done();
        return;
      }
      var wraps = root.querySelectorAll('.plotly-deferred-wrap[data-plotly-rendered="0"]');
      var n = wraps.length;
      if (n === 0) {
        if (done) done();
        return;
      }
      var left = n;
      function oneDone() {
        left--;
        if (left <= 0 && done) done();
      }
      function plotFromFig(w, gd, fig) {
        try {
          Plotly.newPlot(gd, fig.data, fig.layout, fig.config || {});
          w.setAttribute("data-plotly-rendered", "1");
        } catch (e4) {
          try {
            gd.innerHTML =
              '<p class="warn" style="padding:12px;">차트 렌더 오류. 브라우저 콘솔을 확인하세요.</p>';
          } catch (e5) {}
        }
      }
      for (var i = 0; i < wraps.length; i++) {
        (function (w) {
          var gd = w.querySelector(".plotly-graph-div");
          if (!gd) {
            oneDone();
            return;
          }
          var scJ = w.querySelector("script.plotly-fig-json");
          if (scJ) {
            setTimeout(function () {
              try {
                var txt = (scJ.textContent || scJ.innerText || "").trim();
                if (txt) {
                  var fig = JSON.parse(txt);
                  plotFromFig(w, gd, fig);
                }
              } catch (e6) {
                try {
                  gd.innerHTML =
                    '<p class="warn" style="padding:12px;">차트 JSON 파싱 오류</p>';
                } catch (e7) {}
              }
              oneDone();
            }, 0);
            return;
          }
          var sc = w.querySelector("script.plotly-fig-b64");
          var tid = sc ? sc.getAttribute("data-plotly-target") : null;
          var gdx = tid ? document.getElementById(tid) : gd;
          if (sc && gdx) {
            setTimeout(function () {
              try {
                var b64 = (sc.textContent || "").trim();
                if (b64) {
                  var fig = JSON.parse(atob(b64));
                  plotFromFig(w, gdx, fig);
                }
              } catch (e8) {}
              oneDone();
            }, 0);
            return;
          }
          oneDone();
        })(wraps[i]);
      }
    });
  }

  function resizePlotlyInside(root) {
    if (!window.Plotly || !root) return;
    try {
      var nodes = root.querySelectorAll(".plotly-graph-div");
      if (!nodes.length) nodes = root.querySelectorAll(".js-plotly-plot");
      for (var i = 0; i < nodes.length; i++) {
        var gd = nodes[i];
        try {
          if (typeof Plotly.Plots !== "undefined" && Plotly.Plots.resize) {
            Plotly.Plots.resize(gd);
          } else if (typeof Plotly.resize === "function") {
            Plotly.resize(gd);
          }
        } catch (e5) {}
      }
    } catch (e6) {}
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".chart-toggle-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var panelId = btn.getAttribute("aria-controls");
        var panel = panelId ? document.getElementById(panelId) : null;
        if (!panel) return;
        var opening = panel.hasAttribute("hidden");
        var openText = btn.getAttribute("data-text-open") || "접기";
        var closedText = btn.getAttribute("data-text-closed") || "펼치기";
        if (opening) {
          panel.removeAttribute("hidden");
          btn.setAttribute("aria-expanded", "true");
          btn.textContent = openText;
          requestAnimationFrame(function () {
            renderDeferredPlotlyIn(panel, function () {
              resizePlotlyInside(panel);
            });
          });
          setTimeout(function () {
            resizePlotlyInside(panel);
          }, 120);
          setTimeout(function () {
            resizePlotlyInside(panel);
          }, 400);
        } else {
          panel.setAttribute("hidden", "");
          btn.setAttribute("aria-expanded", "false");
          btn.textContent = closedText;
        }
      });
    });
  });
})();
"""
