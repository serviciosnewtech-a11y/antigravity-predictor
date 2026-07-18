/* ═══════════════════════════════════════════════════════════════
   Antigravity Predictor v2 — Dashboard
   Multi-asset · Drawing Tools · Live WebSocket
   ═══════════════════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────────────────────
const state = {
  socket:         null,
  chart:          null,
  candleSeries:   null,
  predLongSeries: null,
  predShortSeries:null,
  markers:        [],

  // Multi-asset snapshot cache keyed by symbol
  snapshots: {},

  // Active symbol / display timeframe
  activeSymbol: "BTC/USDT",
  activeTimeframe: "15m",
  displayCandles: [],

  // Thresholds
  buyThreshold:        0.320,
  exitThreshold:       0.280,
  sellThreshold:       0.320,
  exitShortThreshold:  0.280,

  // Latest live values (updated by WS tick)
  latestClose: null,
  latestATR:   null,

  // Enriched signal polling
  enrichedPollTimer: null,
  lastEnrichedTs:    {},   // sym → ISO string of last received enriched signal

  // Drawing tool state
  activeTool:    "cursor",
  magnetMode:    false,
  drawings:      [],          // persisted drawing objects
  drawingActive: false,       // mid-draw flag
  drawStart:     null,        // {x, y, price, time}
};

function apiBase() {
  if (window.location.protocol === "file:") return "http://localhost";
  return `${window.location.protocol}//${window.location.host || "localhost"}`;
}

function wsBase() {
  if (window.location.protocol === "file:") return "ws://localhost";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host || "localhost"}`;
}

function apiCandidates(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  if (window.location.protocol === "file:") {
    return [`http://localhost${p}`, `http://127.0.0.1${p}`];
  }
  return [`${apiBase()}${p}`];
}

async function fetchJsonWithFallback(path, options = {}) {
  const urls = apiCandidates(path);
  let lastErr = null;
  for (const url of urls) {
    try {
      const res = await fetch(url, options);
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      if (!res.ok) {
        const msg = data?.message || data?.error || `HTTP ${res.status}`;
        const err = new Error(msg);
        err.status = res.status;
        err.source = data?.source || "error";
        throw err;
      }
      return { data, url };
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr || new Error(`No API candidates for ${path}`);
}

function isTypingTarget(target) {
  const el = target || document.activeElement;
  if (!el) return false;
  const tag = (el.tagName || "").toUpperCase();
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return true;
  if (el.isContentEditable) return true;
  if (typeof el.closest === "function" && el.closest("input, textarea, select, [contenteditable=\"true\"], #hermes-chat-panel, #widget-chats, #widget-ideas")) return true;
  return false;
}

function isDemoOnlySymbol(_sym) {
  return false;
}

function isMacroDisplaySymbol(sym) {
  return sym === "XAU/USD";
}

function timeframeLabel(tf) {
  return String(tf || state.activeTimeframe || "15m").toUpperCase();
}

function formatChartTitle(sym) {
  if (isMacroDisplaySymbol(sym)) return `${sym} · 1D Macro Display`;
  return `${sym} · ${timeframeLabel(state.activeTimeframe)} Display`;
}

function updateTimeframeUI() {
  const label = document.getElementById("timeframe-display");
  if (label) label.textContent = `${timeframeLabel(state.activeTimeframe)} DISPLAY`;
  document.querySelectorAll("#timeframe-selector .timeframe-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.timeframe === state.activeTimeframe);
  });
  const title = document.getElementById("chart-title");
  if (title) title.textContent = formatChartTitle(state.activeSymbol);
}


function timeframeSeconds(tf) {
  return ({ "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 })[tf] || 900;
}

function switchTimeframe(tf) {
  state.activeTimeframe = tf || "15m";
  updateTimeframeUI();
  state.markers = [];
  if (state.candleSeries) state.candleSeries.setMarkers([]);
  fetchCandlesForSymbol(state.activeSymbol);
  updateAdvisoryBubble(state.activeSymbol);
}

function initTimeframeSelector() {
  updateTimeframeUI();
  document.querySelectorAll("#timeframe-selector .timeframe-btn").forEach(btn => {
    btn.addEventListener("click", () => switchTimeframe(btn.dataset.timeframe));
  });
}

function getSymbolSourceLabel(sym) {
  if (isMacroDisplaySymbol(sym)) return "Daily macro feed · Gold futures";
  return "WS / REST feed";
}

function updateMarketSourceUI(sym) {
  const label = document.getElementById("market-source-label");
  if (label) label.textContent = getSymbolSourceLabel(sym || state.activeSymbol);
}

function updateAdvisoryBubble(sym, signal, enrichedNote) {
  const bubbleText = document.getElementById("advisory-agent-text");
  const bubble     = document.getElementById("advisory-agent-bubble");
  if (!bubbleText) return;
  const s = sym || state.activeSymbol;
  if (enrichedNote) {
    bubbleText.textContent = `[${signal || "NEUTRAL"}] ${enrichedNote}`;
  } else if (signal && signal !== "NEUTRAL") {
    bubbleText.textContent = `${s} · Model signal: ${signal}. Advisory only — not a trade recommendation.`;
  } else {
    if (isMacroDisplaySymbol(s)) {
      bubbleText.textContent = `${s} · Daily macro reference only. Signal/trading unavailable.`;
      if (bubble) bubble.classList.remove("signal-active", "signal-sell");
      return;
    }
    bubbleText.textContent = `${s} · ${timeframeLabel(state.activeTimeframe)} display. Watching for high-confidence signals.`;
  }
  if (bubble) {
    bubble.classList.toggle("signal-active", signal === "BUY");
    bubble.classList.toggle("signal-sell",   signal === "SELL" || signal === "EXIT");
  }
}

function ensureDemoSnapshot(_sym) {
  return null;
}

function initLocalDemoSnapshots() {
  // No client-facing demo data.
}


// ── Drawing Tool Engine ────────────────────────────────────────────
const DrawingEngine = (() => {
  let canvas, ctx, chartContainer, chart, series;

  function init(_chart, _series) {
    chart          = _chart;
    series         = _series;
    chartContainer = document.getElementById("tv-chart-container");
    canvas         = document.getElementById("drawing-canvas");
    ctx            = canvas.getContext("2d");

    // Size canvas to match container
    resizeCanvas();
    new ResizeObserver(resizeCanvas).observe(chartContainer);

    // Pointer events
    canvas.addEventListener("mousedown",  onMouseDown);
    canvas.addEventListener("mousemove",  onMouseMove);
    canvas.addEventListener("mouseup",    onMouseUp);
    canvas.addEventListener("mouseleave", onMouseLeave);

    // Keyboard shortcuts
    // keyboard shortcuts are wired in initToolbar()

  }

  function resizeCanvas() {
    const r = chartContainer.getBoundingClientRect();
    canvas.width  = r.width;
    canvas.height = r.height;
    redraw();
  }

  // Helper to resolve any chart time format to unix timestamp (seconds)
  function getCandleTimestamp(time) {
    if (typeof time === "number") return time;
    if (time && typeof time === "object") {
      if (time.year && time.month && time.day) {
        return new Date(Date.UTC(time.year, time.month - 1, time.day)).getTime() / 1000;
      }
      if (time.timestamp) return time.timestamp;
    }
    if (typeof time === "string") return new Date(time).getTime() / 1000;
    return null;
  }

  // Convert canvas pixel coords → chart logical coords (with OHLC snapping if magnet mode is enabled)
  function pixelToChart(x, y) {
    try {
      const price = chart.priceScale("right").coordinateToPrice(y);
      const time  = chart.timeScale().coordinateToTime(x);
      const pt = { x, y, price, time };

      if (state.magnetMode && price != null && time != null) {
        const snap = state.snapshots[state.activeSymbol];
        if (snap && snap.candles && snap.candles.length > 0) {
          let bestCandle = null;
          let minDiff = Infinity;
          const targetTime = getCandleTimestamp(time);
          
          if (targetTime) {
            for (let i = 0; i < snap.candles.length; i++) {
              const c = snap.candles[i];
              const cTime = getCandleTimestamp(c.time);
              if (cTime == null) continue;
              const diff = Math.abs(cTime - targetTime);
              if (diff < minDiff) {
                minDiff = diff;
                bestCandle = c;
              }
            }
          }

          if (bestCandle) {
            const prices = [bestCandle.open, bestCandle.high, bestCandle.low, bestCandle.close];
            let bestPrice = price;
            let minPriceDiff = Infinity;
            
            prices.forEach(pr => {
              const diff = Math.abs(pr - price);
              if (diff < minPriceDiff) {
                minPriceDiff = diff;
                bestPrice = pr;
              }
            });

            const snappedY = chart.priceScale("right").priceToCoordinate(bestPrice);
            const snappedX = chart.timeScale().timeToCoordinate(bestCandle.time);

            if (snappedX != null && snappedY != null) {
              const dist = Math.sqrt(Math.pow(snappedX - x, 2) + Math.pow(snappedY - y, 2));
              if (state.magnetType === "strong" || (state.magnetType === "weak" && dist < 50)) {
                pt.x = snappedX;
                pt.y = snappedY;
                pt.price = bestPrice;
                pt.time = bestCandle.time;
              }
            }
          }
        }
      }
      return pt;
    } catch (e) {
      return { x, y, price: null, time: null };
    }
  }

  function setTool(tool) {
    state.activeTool   = tool;
    state.drawingActive = false;
    state.drawStart     = null;
    state.brushPoints   = null;

    // Toggle canvas pointer capture
    const isDrawing = !["cursor"].includes(tool);
    canvas.classList.toggle("active", isDrawing);

    // Update hint
    const hints = {
      cursor:    "Select drawings to edit/delete",
      eraser:    "Click on drawing shapes to delete them",
      trendline: "Click start → Click end",
      ray:       "Click origin → Click direction",
      hline:     "Click to place horizontal line",
      vline:     "Click to place vertical line",
      arrow:     "Click start → Click arrow head",
      channel:   "Click top-left → Click bottom-right to draw parallel channel",
      rect:      "Click start → Click end",
      circle:    "Click center → Click boundary",
      triangle:  "Click top vertex → Click bottom right to define bounding box",
      brush:     "Drag mouse to draw freeform paths",
      fib:       "Click high → Click low",
      fibchannel:"Click top-left → Click bottom-right to draw Fib zones",
      gannfan:   "Click origin → Click direction to project fans",
      gannbox:   "Click start → Click end to project Gann grid",
      text:      "Click to place label",
      callout:   "Click pointer position → Click callout box position",
      pricelabel:"Click to place price badge",
      measure:   "Click start → Click end",
      longposition: "Click entry price → Drag to profit target",
      shortposition:"Click entry price → Drag to profit target"
    };
    const hint = document.getElementById("drawing-hint");
    if (hints[tool]) {
      hint.textContent = hints[tool];
      hint.classList.add("visible");
    } else {
      hint.textContent = "";
      hint.classList.remove("visible");
    }
  }

  function eraseDrawingAt(mx, my) {
    if (state.lockDrawings) return;
    let indexToDelete = -1;
    let minDistance = 15; // pixel tolerance for click detection
    
    state.drawings.forEach((d, idx) => {
      if (d.locked) return;
      
      if (d.type === "hline") {
        const y = chart.priceScale("right").priceToCoordinate(d.price);
        if (y != null && Math.abs(y - my) < minDistance) {
          indexToDelete = idx;
          minDistance = Math.abs(y - my);
        }
      } else if (d.type === "vline") {
        if (d.x != null && Math.abs(d.x - mx) < minDistance) {
          indexToDelete = idx;
          minDistance = Math.abs(d.x - mx);
        }
      } else if (d.p1) {
        const x1 = d.p1.time ? chart.timeScale().timeToCoordinate(d.p1.time) : d.p1.x;
        const y1 = d.p1.price ? chart.priceScale("right").priceToCoordinate(d.p1.price) : d.p1.y;
        if (x1 != null && y1 != null) {
          const dist = Math.sqrt(Math.pow(x1 - mx, 2) + Math.pow(y1 - my, 2));
          if (dist < minDistance) {
            indexToDelete = idx;
            minDistance = dist;
          }
        }
        if (d.p2) {
          const x2 = d.p2.time ? chart.timeScale().timeToCoordinate(d.p2.time) : d.p2.x;
          const y2 = d.p2.price ? chart.priceScale("right").priceToCoordinate(d.p2.price) : d.p2.y;
          if (x2 != null && y2 != null) {
            const dist = Math.sqrt(Math.pow(x2 - mx, 2) + Math.pow(y2 - my, 2));
            if (dist < minDistance) {
              indexToDelete = idx;
              minDistance = dist;
            }
          }
        }
      }
    });
    
    if (indexToDelete > -1) {
      state.drawings.splice(indexToDelete, 1);
      redraw();
    }
  }

  function onMouseDown(e) {
    if (state.activeTool === "cursor") return;
    if (state.activeTool === "eraser") { eraseDrawingAt(e.offsetX, e.offsetY); return; }
    
    // Single click tools
    if (state.activeTool === "text") { placeText(e); return; }
    if (state.activeTool === "pricelabel") { placePriceLabel(e); return; }
    if (state.activeTool && state.activeTool.startsWith("icon-")) { placeSticker(e); return; }

    const pt = pixelToChart(e.offsetX, e.offsetY);

    if (state.activeTool === "brush") {
      state.drawingActive = true;
      state.brushPoints = [pt];
      return;
    }

    if (!state.drawingActive) {
      // First click — begin drawing
      state.drawingActive = true;
      state.drawStart     = pt;
    } else {
      // Second click — commit drawing
      commitDrawing(state.drawStart, pt);
      state.drawingActive = false;
      state.drawStart     = null;
      
      if (!state.lockMode) {
        window.selectTool("cursor");
      }
    }
  }

  function onMouseMove(e) {
    const pt = pixelToChart(e.offsetX, e.offsetY);
    if (state.activeTool === "brush" && state.drawingActive && state.brushPoints) {
      state.brushPoints.push(pt);
      redraw();
      return;
    }
    if (!state.drawingActive) return;
    redraw();
    drawPreview(state.drawStart, pt);
  }

  function onMouseUp(e) {
    if (state.activeTool === "brush" && state.drawingActive && state.brushPoints) {
      state.drawings.push({ type: "brush", points: state.brushPoints });
      state.drawingActive = false;
      state.brushPoints = null;
      redraw();
      if (!state.lockMode) {
        window.selectTool("cursor");
      }
    }
  }

  function onMouseLeave() {
    if (state.drawingActive) {
      redraw();
    }
  }

  function placeText(e) {
    const label = prompt("Enter label text:");
    if (!label) return;
    const pt = pixelToChart(e.offsetX, e.offsetY);
    state.drawings.push({ type: "text", p1: pt, label });
    redraw();
    if (!state.lockMode) {
      window.selectTool("cursor");
    }
  }

  function placePriceLabel(e) {
    const pt = pixelToChart(e.offsetX, e.offsetY);
    if (pt.price == null) return;
    state.drawings.push({ type: "pricelabel", p1: pt });
    redraw();
    if (!state.lockMode) {
      window.selectTool("cursor");
    }
  }

  function placeSticker(e) {
    const pt = pixelToChart(e.offsetX, e.offsetY);
    state.drawings.push({ type: state.activeTool, p1: pt });
    redraw();
    if (!state.lockMode) {
      window.selectTool("cursor");
    }
  }

  function commitDrawing(p1, p2) {
    const tool = state.activeTool;
    
    // For callout, prompt for label text
    if (tool === "callout") {
      const label = prompt("Enter callout text:");
      if (!label) return;
      state.drawings.push({ type: "callout", p1, p2, label });
      redraw();
      return;
    }

    if (tool === "hline") {
      state.drawings.push({ type: "hline", price: p1.price });
    } else if (tool === "vline") {
      state.drawings.push({ type: "vline", time: p1.time, x: p1.x });
    } else {
      state.drawings.push({ type: tool, p1, p2 });
    }
    redraw();
  }

  function redraw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    state.drawings.forEach(d => {
      if (!d.hidden) renderDrawing(d);
    });
    if (window.syncObjectTree) window.syncObjectTree();
  }

  function drawPreview(p1, p2) {
    ctx.save();
    ctx.strokeStyle = "rgba(0,229,255,0.75)";
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([]);
    if (state.activeTool === "brush" && state.brushPoints) {
      drawBrush(state.brushPoints);
    } else {
      renderShape(state.activeTool, p1, p2, true);
    }
    ctx.restore();
  }

  function renderDrawing(d) {
    if (state.hideDrawings) return;
    ctx.save();
    ctx.strokeStyle = colourForTool(d.type);
    ctx.fillStyle   = colourForTool(d.type);
    ctx.lineWidth   = 1.5;

    if (d.type === "brush") {
      drawBrush(d.points);
      ctx.restore();
      return;
    }

    if (d.type.startsWith("icon-")) {
      const y = chart.priceScale("right").priceToCoordinate(d.p1.price);
      const x = chart.timeScale().timeToCoordinate(d.p1.time);
      if (x != null && y != null) {
        const emojiMap = {
          "icon-star": "⭐",
          "icon-heart": "❤️",
          "icon-thumbsup": "👍",
          "icon-arrowup": "⬆️",
          "icon-arrowdown": "⬇️"
        };
        const emoji = emojiMap[d.type] || "⭐";
        ctx.font = "18px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(emoji, x, y);
      }
      ctx.restore();
      return;
    }

    if (d.type === "pricelabel") {
      const y = chart.priceScale("right").priceToCoordinate(d.p1.price);
      const x = chart.timeScale().timeToCoordinate(d.p1.time);
      if (x != null && y != null) {
        const txt = `$${d.p1.price.toFixed(2)}`;
        ctx.font = "10px 'JetBrains Mono', monospace";
        const textWidth = ctx.measureText(txt).width;
        const paddingX = 6;
        const paddingY = 4;
        const boxW = textWidth + paddingX * 2;
        const boxH = 14 + paddingY * 2;
        
        ctx.fillStyle = "rgba(0, 229, 255, 0.25)";
        ctx.strokeStyle = "rgba(0, 229, 255, 0.85)";
        ctx.beginPath();
        ctx.roundRect(x - boxW / 2, y - boxH / 2, boxW, boxH, 4);
        ctx.fill();
        ctx.stroke();
        
        ctx.fillStyle = "var(--color-text-main)";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(txt, x, y);
      }
      ctx.restore();
      return;
    }

    if (d.type === "callout") {
      const x1 = chart.timeScale().timeToCoordinate(d.p1.time);
      const y1 = chart.priceScale("right").priceToCoordinate(d.p1.price);
      const x2 = chart.timeScale().timeToCoordinate(d.p2.time);
      const y2 = chart.priceScale("right").priceToCoordinate(d.p2.price);
      if (x1 != null && y1 != null && x2 != null && y2 != null) {
        drawCallout({ x: x1, y: y1 }, { x: x2, y: y2 }, d.label);
      }
      ctx.restore();
      return;
    }

    if (d.type === "hline") {
      const y = chart.priceScale("right").priceToCoordinate(d.price);
      if (y == null) { ctx.restore(); return; }
      drawHLine({ y }, { y }, false, d.price);
    } else if (d.type === "vline") {
      drawVLine({ x: d.x });
    } else if (d.type === "text") {
      const y = chart.priceScale("right").priceToCoordinate(d.p1.price);
      const x = chart.timeScale().timeToCoordinate(d.p1.time);
      if (x == null || y == null) { ctx.restore(); return; }
      ctx.font      = "12px 'Outfit', sans-serif";
      ctx.fillText(d.label, x + 4, y - 4);
    } else {
      const x1 = d.p1.time ? chart.timeScale().timeToCoordinate(d.p1.time) : d.p1.x;
      const y1 = d.p1.price ? chart.priceScale("right").priceToCoordinate(d.p1.price) : d.p1.y;
      const x2 = d.p2.time ? chart.timeScale().timeToCoordinate(d.p2.time) : d.p2.x;
      const y2 = d.p2.price ? chart.priceScale("right").priceToCoordinate(d.p2.price) : d.p2.y;
      if (x1 == null || y1 == null || x2 == null || y2 == null) { ctx.restore(); return; }
      renderShape(d.type, { x: x1, y: y1 }, { x: x2, y: y2 }, false, d.type === "hline" ? d.price : null);
    }
    ctx.restore();
  }

  function renderShape(tool, p1, p2, preview, labelVal) {
    switch (tool) {
      case "trendline":
        drawLine(p1, p2); break;
      case "ray":
        drawRay(p1, p2); break;
      case "hline":
        drawHLine(p1, p2, preview, labelVal); break;
      case "vline":
        drawVLine(p1); break;
      case "rect":
        drawRect(p1, p2); break;
      case "circle":
        drawCircle(p1, p2); break;
      case "triangle":
        drawTriangle(p1, p2); break;
      case "channel":
        drawChannel(p1, p2); break;
      case "arrow":
        drawArrow(p1, p2); break;
      case "fib":
        drawFib(p1, p2); break;
      case "fibchannel":
        drawFibChannel(p1, p2); break;
      case "gannfan":
        drawGannFan(p1, p2); break;
      case "gannbox":
        drawGannBox(p1, p2); break;
      case "measure":
        drawMeasure(p1, p2); break;
      case "callout":
        drawCallout(p1, p2, preview ? "..." : ""); break;
      case "longposition":
        drawLongPosition(p1, p2); break;
      case "shortposition":
        drawShortPosition(p1, p2); break;
    }
  }

  function drawLine(p1, p2) {
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    dot(p1); dot(p2);
  }

  function drawRay(p1, p2) {
    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    const len = Math.sqrt(dx*dx + dy*dy) || 1;
    const ext = 4000;
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p1.x + dx/len*ext, p1.y + dy/len*ext);
    ctx.stroke();
    dot(p1);
  }

  function drawHLine(p1, _p2, preview, label) {
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(0, p1.y);
    ctx.lineTo(canvas.width, p1.y);
    ctx.stroke();
    ctx.setLineDash([]);
    if (label != null) {
      ctx.font = "10px 'JetBrains Mono', monospace";
      ctx.fillText(label.toFixed(2), canvas.width - 65, p1.y - 3);
    }
  }

  function drawVLine(p1) {
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(p1.x, 0);
    ctx.lineTo(p1.x, canvas.height);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawRect(p1, p2) {
    ctx.globalAlpha = 0.12;
    ctx.fillRect(p1.x, p1.y, p2.x - p1.x, p2.y - p1.y);
    ctx.globalAlpha = 1;
    ctx.strokeRect(p1.x, p1.y, p2.x - p1.x, p2.y - p1.y);
  }

  function drawCircle(p1, p2) {
    const radius = Math.sqrt(Math.pow(p2.x - p1.x, 2) + Math.pow(p2.y - p1.y, 2));
    ctx.beginPath();
    ctx.arc(p1.x, p1.y, radius, 0, Math.PI * 2);
    ctx.save();
    ctx.globalAlpha = 0.08;
    ctx.fill();
    ctx.restore();
    ctx.stroke();
    dot(p1);
  }

  function drawTriangle(p1, p2) {
    const topX = (p1.x + p2.x) / 2;
    const topY = p1.y;
    ctx.beginPath();
    ctx.moveTo(topX, topY);
    ctx.lineTo(p2.x, p2.y);
    ctx.lineTo(p1.x, p2.y);
    ctx.closePath();
    ctx.save();
    ctx.globalAlpha = 0.08;
    ctx.fill();
    ctx.restore();
    ctx.stroke();
    dot({ x: topX, y: topY });
    dot(p2);
    dot({ x: p1.x, y: p2.y });
  }

  function drawChannel(p1, p2) {
    const leftX = Math.min(p1.x, p2.x);
    const rightX = Math.max(p1.x, p2.x);
    
    ctx.beginPath();
    ctx.moveTo(leftX, p1.y);
    ctx.lineTo(rightX, p1.y);
    ctx.stroke();
    
    ctx.beginPath();
    ctx.moveTo(leftX, p2.y);
    ctx.lineTo(rightX, p2.y);
    ctx.stroke();
    
    const centerY = (p1.y + p2.y) / 2;
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(leftX, centerY);
    ctx.lineTo(rightX, centerY);
    ctx.stroke();
    ctx.restore();
    
    ctx.save();
    ctx.globalAlpha = 0.05;
    ctx.fillRect(leftX, Math.min(p1.y, p2.y), rightX - leftX, Math.abs(p2.y - p1.y));
    ctx.restore();
    
    dot({ x: leftX, y: p1.y });
    dot({ x: rightX, y: p2.y });
  }

  function drawArrow(p1, p2) {
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    
    const angle = Math.atan2(p2.y - p1.y, p2.x - p1.x);
    const headLen = 10;
    
    ctx.beginPath();
    ctx.moveTo(p2.x, p2.y);
    ctx.lineTo(p2.x - headLen * Math.cos(angle - Math.PI / 6), p2.y - headLen * Math.sin(angle - Math.PI / 6));
    ctx.lineTo(p2.x - headLen * Math.cos(angle + Math.PI / 6), p2.y - headLen * Math.sin(angle + Math.PI / 6));
    ctx.closePath();
    ctx.fill();
    
    dot(p1);
  }

  function drawFib(p1, p2) {
    const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const labels = ["0", "0.236", "0.382", "0.5", "0.618", "0.786", "1"];
    const dy = p2.y - p1.y;
    levels.forEach((lvl, i) => {
      const y = p1.y + dy * lvl;
      ctx.globalAlpha = 0.7;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(p1.x < p2.x ? p1.x : p2.x, y);
      ctx.lineTo(p1.x < p2.x ? p2.x : p1.x, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      ctx.font = "9px 'JetBrains Mono', monospace";
      ctx.fillText(labels[i], Math.min(p1.x, p2.x) + 3, y - 2);
    });
  }

  function drawFibChannel(p1, p2) {
    const leftX = Math.min(p1.x, p2.x);
    const rightX = Math.max(p1.x, p2.x);
    const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const dy = p2.y - p1.y;
    
    levels.forEach(lvl => {
      const y = p1.y + dy * lvl;
      ctx.beginPath();
      ctx.moveTo(leftX, y);
      ctx.lineTo(rightX, y);
      ctx.stroke();
    });
    
    ctx.save();
    for (let i = 0; i < levels.length - 1; i++) {
      const y1 = p1.y + dy * levels[i];
      const y2 = p1.y + dy * levels[i + 1];
      ctx.globalAlpha = 0.03 * (i + 1);
      ctx.fillRect(leftX, y1, rightX - leftX, y2 - y1);
    }
    ctx.restore();
    
    dot(p1);
    dot(p2);
  }

  function drawGannFan(p1, p2) {
    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;
    const ratios = [1/8, 1/4, 1/3, 1/2, 1, 2, 3, 4, 8];
    
    ctx.save();
    ratios.forEach((ratio, idx) => {
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p1.x + dx, p1.y + dy * ratio);
      ctx.globalAlpha = 0.8 - (idx % 3) * 0.15;
      ctx.stroke();
    });
    ctx.restore();
    
    dot(p1);
    dot(p2);
  }

  function drawGannBox(p1, p2) {
    const leftX = Math.min(p1.x, p2.x);
    const rightX = Math.max(p1.x, p2.x);
    const topY = Math.min(p1.y, p2.y);
    const bottomY = Math.max(p1.y, p2.y);
    const w = rightX - leftX;
    const h = bottomY - topY;
    
    ctx.strokeRect(leftX, topY, w, h);
    
    const ratios = [0.25, 0.382, 0.5, 0.618, 0.75];
    ctx.save();
    ctx.globalAlpha = 0.35;
    ctx.setLineDash([2, 2]);
    
    ratios.forEach(r => {
      ctx.beginPath();
      ctx.moveTo(leftX + w * r, topY);
      ctx.lineTo(leftX + w * r, bottomY);
      ctx.stroke();
    });
    
    ratios.forEach(r => {
      ctx.beginPath();
      ctx.moveTo(leftX, topY + h * r);
      ctx.lineTo(rightX, topY + h * r);
      ctx.stroke();
    });
    
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(leftX, topY);
    ctx.lineTo(rightX, bottomY);
    ctx.moveTo(leftX, bottomY);
    ctx.lineTo(rightX, topY);
    ctx.stroke();
    ctx.restore();
    
    dot(p1);
    dot(p2);
  }

  function drawCallout(p1, p2, labelText) {
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    
    ctx.font = "11px 'Outfit', sans-serif";
    const textWidth = ctx.measureText(labelText).width;
    const paddingX = 8;
    const paddingY = 6;
    const boxW = textWidth + paddingX * 2;
    const boxH = 14 + paddingY * 2;
    
    ctx.fillStyle = "rgba(18, 22, 33, 0.9)";
    ctx.fillRect(p2.x - boxW/2, p2.y - boxH/2, boxW, boxH);
    ctx.strokeRect(p2.x - boxW/2, p2.y - boxH/2, boxW, boxH);
    
    ctx.fillStyle = "var(--color-text-main)";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(labelText, p2.x, p2.y);
    
    dot(p1);
  }

  function drawLongPosition(p1, p2) {
    const leftX = Math.min(p1.x, p2.x);
    const rightX = Math.max(p1.x, p2.x);
    const w = rightX - leftX || 100;
    const dy = Math.abs(p2.y - p1.y) || 50;
    
    const targetY = p1.y - dy;
    const stopY = p1.y + dy;
    
    ctx.save();
    
    ctx.fillStyle = "rgba(0, 230, 118, 0.15)";
    ctx.strokeStyle = "rgba(0, 230, 118, 0.6)";
    ctx.fillRect(leftX, targetY, w, dy);
    ctx.strokeRect(leftX, targetY, w, dy);
    
    ctx.fillStyle = "rgba(255, 61, 0, 0.15)";
    ctx.strokeStyle = "rgba(255, 61, 0, 0.6)";
    ctx.fillRect(leftX, p1.y, w, dy);
    ctx.strokeRect(leftX, p1.y, w, dy);
    
    ctx.strokeStyle = "rgba(255, 255, 255, 0.8)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(leftX, p1.y);
    ctx.lineTo(rightX, p1.y);
    ctx.stroke();
    
    ctx.font = "9px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = "#ffffff";
    
    if (p1.price != null) {
      const targetPrice = chart.priceScale("right").coordinateToPrice(targetY);
      const stopPrice = chart.priceScale("right").coordinateToPrice(stopY);
      if (targetPrice != null && stopPrice != null) {
        const priceDiff = Math.abs(targetPrice - p1.price);
        const stopDiff = Math.abs(p1.price - stopPrice);
        const targetPct = (priceDiff / p1.price * 100).toFixed(2);
        const stopPct = (stopDiff / p1.price * 100).toFixed(2);
        
        ctx.fillText(`Target: +${targetPct}% ($${targetPrice.toFixed(2)})`, leftX + w / 2, targetY + dy / 2);
        ctx.fillText(`Stop: -${stopPct}% ($${stopPrice.toFixed(2)})`, leftX + w / 2, p1.y + dy / 2);
      }
    }
    ctx.restore();
    dot(p1);
  }

  function drawShortPosition(p1, p2) {
    const leftX = Math.min(p1.x, p2.x);
    const rightX = Math.max(p1.x, p2.x);
    const w = rightX - leftX || 100;
    const dy = Math.abs(p2.y - p1.y) || 50;
    
    const targetY = p1.y + dy;
    const stopY = p1.y - dy;
    
    ctx.save();
    
    ctx.fillStyle = "rgba(255, 61, 0, 0.15)";
    ctx.strokeStyle = "rgba(255, 61, 0, 0.6)";
    ctx.fillRect(leftX, stopY, w, dy);
    ctx.strokeRect(leftX, stopY, w, dy);
    
    ctx.fillStyle = "rgba(0, 230, 118, 0.15)";
    ctx.strokeStyle = "rgba(0, 230, 118, 0.6)";
    ctx.fillRect(leftX, p1.y, w, dy);
    ctx.strokeRect(leftX, p1.y, w, dy);
    
    ctx.strokeStyle = "rgba(255, 255, 255, 0.8)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(leftX, p1.y);
    ctx.lineTo(rightX, p1.y);
    ctx.stroke();
    
    ctx.font = "9px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = "#ffffff";
    
    if (p1.price != null) {
      const targetPrice = chart.priceScale("right").coordinateToPrice(targetY);
      const stopPrice = chart.priceScale("right").coordinateToPrice(stopY);
      if (targetPrice != null && stopPrice != null) {
        const priceDiff = Math.abs(p1.price - targetPrice);
        const stopDiff = Math.abs(stopPrice - p1.price);
        const targetPct = (priceDiff / p1.price * 100).toFixed(2);
        const stopPct = (stopDiff / p1.price * 100).toFixed(2);
        
        ctx.fillText(`Target: +${targetPct}% ($${targetPrice.toFixed(2)})`, leftX + w / 2, p1.y + dy / 2);
        ctx.fillText(`Stop: -${stopPct}% ($${stopPrice.toFixed(2)})`, leftX + w / 2, stopY + dy / 2);
      }
    }
    ctx.restore();
    dot(p1);
  }

  function drawBrush(points) {
    if (!points || points.length === 0) return;
    ctx.beginPath();
    const firstX = points[0].time ? chart.timeScale().timeToCoordinate(points[0].time) : points[0].x;
    const firstY = points[0].price ? chart.priceScale("right").priceToCoordinate(points[0].price) : points[0].y;
    if (firstX != null && firstY != null) {
      ctx.moveTo(firstX, firstY);
    }
    
    for (let i = 1; i < points.length; i++) {
      const px = points[i].time ? chart.timeScale().timeToCoordinate(points[i].time) : points[i].x;
      const py = points[i].price ? chart.priceScale("right").priceToCoordinate(points[i].price) : points[i].y;
      if (px != null && py != null) {
        ctx.lineTo(px, py);
      }
    }
    ctx.stroke();
  }

  function drawMeasure(p1, p2) {
    drawLine(p1, p2);
    if (p1.price != null && p2.price != null) {
      const diff = Math.abs(p2.price - p1.price);
      const pct  = (diff / p1.price * 100).toFixed(2);
      const mx   = (p1.x + p2.x) / 2;
      const my   = (p1.y + p2.y) / 2;
      ctx.font = "11px 'Outfit', sans-serif";
      ctx.fillText(`Δ${diff.toFixed(2)} (${pct}%)`, mx + 4, my - 4);
    }
  }

  function dot(p) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
    ctx.fill();
  }

  function colourForTool(tool) {
    const map = {
      trendline: "rgba(0,229,255,0.85)",
      ray:       "rgba(0,229,255,0.7)",
      hline:     "rgba(255,196,0,0.8)",
      vline:     "rgba(255,145,0,0.7)",
      rect:      "rgba(179,136,255,0.85)",
      circle:    "rgba(179,136,255,0.8)",
      triangle:  "rgba(255,128,0,0.8)",
      arrow:     "rgba(0,229,255,0.9)",
      channel:   "rgba(0,229,255,0.75)",
      fib:       "rgba(0,230,118,0.8)",
      fibchannel:"rgba(0,230,118,0.75)",
      gannfan:   "rgba(156,39,176,0.8)",
      gannbox:   "rgba(156,39,176,0.75)",
      text:      "rgba(238,240,243,0.9)",
      callout:   "rgba(238,240,243,0.85)",
      pricelabel:"rgba(0,229,255,0.85)",
      measure:   "rgba(255,61,0,0.8)",
      brush:     "rgba(255,196,0,0.85)",
      longposition: "rgba(0,230,118,0.8)",
      shortposition:"rgba(255,61,0,0.8)"
    };
    return map[tool] || "rgba(0,229,255,0.8)";
  }

  function clearAll() {
    state.drawings = [];
    state.drawingActive = false;
    state.drawStart     = null;
    redraw();
  }

  return { init, setTool, redraw, clearAll };
})();


// ── Chart Initialization ───────────────────────────────────────────
function initChart() {
  const container = document.getElementById("tv-chart-container");

  state.chart = LightweightCharts.createChart(container, {
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor:  "#7a8494",
      fontSize:   12,
      fontFamily: "Outfit",
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.03)" },
      horzLines: { color: "rgba(255,255,255,0.03)" },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.07)", autoScale: true },
    timeScale: {
      borderColor:    "rgba(255,255,255,0.07)",
      timeVisible:    true,
      secondsVisible: false,
    },
  });

  state.candleSeries = state.chart.addCandlestickSeries({
    upColor:        "#00e676",
    downColor:      "#ff3d00",
    borderUpColor:  "#00e676",
    borderDownColor:"#ff3d00",
    wickUpColor:    "#00e676",
    wickDownColor:  "#ff3d00",
  });

  state.predLongSeries = state.chart.addLineSeries({
    color: "rgba(0,230,118,0.6)", lineWidth: 2, title: "",
    priceScaleId: "left",
  });
  state.predShortSeries = state.chart.addLineSeries({
    color: "rgba(255,145,0,0.6)", lineWidth: 2, title: "",
    priceScaleId: "left",
  });

  state.chart.priceScale("left").applyOptions({
    visible: false, autoScale: false,
    scaleMargins: { top: 0.72, bottom: 0.04 },
    borderColor: "rgba(255,255,255,0.07)",
  });

  // Resize observer
  new ResizeObserver(() => {
    const r = container.getBoundingClientRect();
    state.chart.resize(r.width, r.height);
    DrawingEngine.redraw();
  }).observe(container);

  // Init drawing engine
  DrawingEngine.init(state.chart, state.candleSeries);

  // Hook data-window crosshair listener (set up by initToolbar)
  if (window._hookDataWindow) window._hookDataWindow(state.chart, state.candleSeries);

}


// ── Toolbar Wiring ─────────────────────────────────────────────────
// Active sub-tools per category state
const categoryActiveTools = {
  cursor: "cursor",
  trendlines: "trendline",
  gannfib: "fib",
  shapes: "rect",
  annotation: "text",
  prediction: "measure",
  icons: "icon-star",
  magnet: "magnet-weak",
  hide: "hide-drawings",
  trash: "clear-drawings"
};

// Initialize helper states on global state
state.hideDrawings = false;
state.hideIndicators = false;
state.lockDrawings = false;
state.lockMode = false;
state.magnetType = "weak"; // default magnet type

// Update indicators visibility based on state
window.updateIndicatorsVisibility = function() {
  state.predLongSeries.setData([]);
  state.predShortSeries.setData([]);
};

window.selectTool = function(toolName) {
  // Find the flyout item representing this tool
  const flyoutItem = document.querySelector(`.flyout-item[data-tool="${toolName}"]`);
  if (flyoutItem) {
    const flyout = flyoutItem.closest(".flyout-menu");
    const wrapper = flyoutItem.closest(".tool-category-wrapper");
    const parentBtn = wrapper.querySelector(".parent-btn");
    const category = parentBtn.dataset.category;

    // Update active sub-tool for this category
    categoryActiveTools[category] = toolName;

    // Remove active class from all other items in this flyout
    flyout.querySelectorAll(".flyout-item").forEach(item => item.classList.remove("active"));
    flyoutItem.classList.add("active");

    // Swap the parent button's SVG or emoji
    const currentSVG = parentBtn.querySelector("svg");
    const currentSpan = parentBtn.querySelector("span:not(.sub-arrow)");
    
    const newSVG = flyoutItem.querySelector("svg");
    const newSpan = flyoutItem.querySelector("span:not(.kbd)");

    if (newSVG) {
      if (currentSVG) parentBtn.replaceChild(newSVG.cloneNode(true), currentSVG);
      if (currentSpan) currentSpan.remove();
    } else if (newSpan && newSpan.textContent.match(/[\uD800-\uDFFF\u2600-\u27BF]/)) {
      // It's an emoji/sticker!
      const emojiSpan = document.createElement("span");
      emojiSpan.style.fontSize = "1.1rem";
      emojiSpan.style.marginRight = "8px";
      emojiSpan.textContent = newSpan.textContent.trim();
      if (currentSVG) currentSVG.remove();
      if (currentSpan) {
        parentBtn.replaceChild(emojiSpan, currentSpan);
      } else {
        parentBtn.insertBefore(emojiSpan, parentBtn.querySelector(".sub-arrow"));
      }
    }

    // Deactivate all parent category buttons, activate this one
    document.querySelectorAll(".parent-btn").forEach(btn => btn.classList.remove("active"));
    parentBtn.classList.add("active");

    // Set drawing tool
    DrawingEngine.setTool(toolName);

    // Close any sticky flyout menus
    document.querySelectorAll(".flyout-menu").forEach(menu => menu.classList.remove("show-sticky"));
  } else {
    // If it's a magnet sub-tool
    if (toolName.startsWith("magnet-")) {
      const mode = toolName.split("-")[1];
      if (mode === "off") {
        state.magnetMode = false;
        state.magnetType = "off";
      } else {
        state.magnetMode = true;
        state.magnetType = mode; // "weak" or "strong"
      }
      
      categoryActiveTools.magnet = toolName;

      const parentBtn = document.getElementById("tcat-magnet");
      parentBtn.classList.toggle("active", state.magnetMode);
      
      // Update item active class in magnet flyout
      const flyout = document.getElementById("flyout-magnet");
      flyout.querySelectorAll(".flyout-item").forEach(item => {
        item.classList.toggle("active", item.dataset.tool === toolName);
      });
      
      // Swap magnet parent icon with selected tool icon
      const currentSVG = parentBtn.querySelector("svg");
      const flyoutItem = flyout.querySelector(`.flyout-item[data-tool="${toolName}"]`);
      const newSVG = flyoutItem?.querySelector("svg");
      if (currentSVG && newSVG) {
        parentBtn.replaceChild(newSVG.cloneNode(true), currentSVG);
      }
      
      flyout.classList.remove("show-sticky");
    }
  }
};

function initToolbar() {
  // Handle parent category button click
  document.querySelectorAll(".parent-btn").forEach(parentBtn => {
    parentBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const category = parentBtn.dataset.category;
      
      // If it's hide, toggle hide-drawings
      if (category === "hide") {
        state.hideDrawings = !state.hideDrawings;
        const item = document.querySelector('[data-action="hide-drawings"]');
        if (item) item.classList.toggle("active", state.hideDrawings);
        parentBtn.classList.toggle("active", state.hideDrawings);
        DrawingEngine.redraw();
        return;
      }
      
      // If it's trash, clear all drawings
      if (category === "trash") {
        DrawingEngine.clearAll();
        return;
      }
      
      // If it's magnet, toggle magnet mode
      if (category === "magnet") {
        const currentActive = categoryActiveTools.magnet;
        if (state.magnetMode) {
          window.selectTool("magnet-off");
        } else {
          const savedMode = currentActive && currentActive !== "magnet-off" ? currentActive : "magnet-weak";
          window.selectTool(savedMode);
        }
        return;
      }
      
      // For other categories, activate the currently active sub-tool
      const activeTool = categoryActiveTools[category];
      if (activeTool) {
        window.selectTool(activeTool);
      }
    });
  });

  // Handle sub-arrow click to toggle sticky flyout
  document.querySelectorAll(".sub-arrow").forEach(arrow => {
    arrow.addEventListener("click", (e) => {
      e.stopPropagation();
      const wrapper = arrow.closest(".tool-category-wrapper");
      const flyout = wrapper.querySelector(".flyout-menu");
      const wasOpen = flyout.classList.contains("show-sticky");
      
      // Close all other sticky flyouts first
      document.querySelectorAll(".flyout-menu").forEach(menu => menu.classList.remove("show-sticky"));
      
      // Toggle this one
      if (!wasOpen) {
        flyout.classList.add("show-sticky");
      }
    });
  });

  // Handle flyout items click (for tools and actions)
  document.querySelectorAll(".flyout-item").forEach(item => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const tool = item.dataset.tool;
      const action = item.dataset.action;

      if (tool) {
        window.selectTool(tool);
      } else if (action) {
        if (action === "hide-drawings") {
          state.hideDrawings = !state.hideDrawings;
          item.classList.toggle("active", state.hideDrawings);
          DrawingEngine.redraw();
        } else if (action === "hide-indicators") {
          state.hideIndicators = !state.hideIndicators;
          item.classList.toggle("active", state.hideIndicators);
          window.updateIndicatorsVisibility();
        } else if (action === "hide-all") {
          const hideAll = !(state.hideDrawings && state.hideIndicators);
          state.hideDrawings = hideAll;
          state.hideIndicators = hideAll;
          
          const hdEl = document.querySelector('[data-action="hide-drawings"]');
          const hiEl = document.querySelector('[data-action="hide-indicators"]');
          if (hdEl) hdEl.classList.toggle("active", state.hideDrawings);
          if (hiEl) hiEl.classList.toggle("active", state.hideIndicators);
          item.classList.toggle("active", hideAll);
          
          DrawingEngine.redraw();
          window.updateIndicatorsVisibility();
        } else if (action === "clear-drawings") {
          DrawingEngine.clearAll();
        } else if (action === "clear-indicators") {
          state.markers = [];
          state.predLongSeries.setData([]);
          state.predShortSeries.setData([]);
        } else if (action === "clear-both") {
          DrawingEngine.clearAll();
          state.markers = [];
          state.predLongSeries.setData([]);
          state.predShortSeries.setData([]);
        }
        
        // Highlight active parent if it is hide
        const wrapper = item.closest(".tool-category-wrapper");
        const parentBtn = wrapper.querySelector(".parent-btn");
        if (parentBtn && parentBtn.id === "tcat-hide") {
          parentBtn.classList.toggle("active", state.hideDrawings || state.hideIndicators);
        }
        
        // Close flyout
        wrapper.querySelector(".flyout-menu").classList.remove("show-sticky");
      }
    });
  });

  // Close any sticky flyouts when clicking outside
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".tool-category-wrapper")) {
      document.querySelectorAll(".flyout-menu").forEach(menu => menu.classList.remove("show-sticky"));
    }
  });

  // Lock mode (Stay in Drawing Mode)
  const lockModeBtn = document.getElementById("tool-lockmode");
  if (lockModeBtn) {
    lockModeBtn.addEventListener("click", () => {
      state.lockMode = !state.lockMode;
      lockModeBtn.classList.toggle("active", state.lockMode);
    });
  }

  // Lock all drawings
  const lockDrawingsBtn = document.getElementById("tool-lockdrawings");
  if (lockDrawingsBtn) {
    lockDrawingsBtn.addEventListener("click", () => {
      state.lockDrawings = !state.lockDrawings;
      lockDrawingsBtn.classList.toggle("active", state.lockDrawings);
    });
  }

  // Keyboard shortcuts mapping to selectTool
  document.addEventListener("keydown", e => {
    if (isTypingTarget(e.target)) return;
    const map = {
      "v": "cursor",
      "c": "crosshair",
      "t": "trendline",
      "r": "ray",
      "h": "hline",
      "l": "vline",
      "b": "rect",
      "f": "fib",
      "a": "text",
      "m": "measure"
    };
    const toolName = map[e.key.toLowerCase()];
    if (toolName) {
      window.selectTool(toolName);
      e.preventDefault();
    }
  });

  // ── Right rail buttons & Widget Toggles ─────────────────────
  const modal   = document.getElementById("shortcuts-modal");
  const dataWin = document.getElementById("data-window");
  let dataWindowActive = false;
  let layersVisible = true;

  const widgetMapping = {
    "rtool-watchlist": "widget-watchlist",
    "rtool-alerts": "widget-alerts",
    "rtool-news": "widget-news",
    "rtool-datawindow": "widget-datawindow",
    "rtool-hotlists": "widget-hotlists",
    "rtool-calendar": "widget-calendar",
    "rtool-ideas": "widget-ideas",
    "rtool-chats": "widget-chats",
    "rtool-ideasstream": "widget-ideasstream",
    "rtool-streams": "widget-streams",
    "rtool-notifications": "widget-notifications",
    "rtool-order": "widget-order",
    "rtool-dom": "widget-dom",
    "rtool-objecttree": "widget-objecttree"
  };

  document.querySelectorAll(".rtool-btn:not(.rtool-dim)").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.id;

      // Help modal
      if (id === "rtool-help") {
        modal.hidden = false;
        return;
      }

      const targetWidgetId = widgetMapping[id];
      if (!targetWidgetId) return;

      const pane = document.querySelector(".grid-right-pane");
      const isAlreadyActive = btn.classList.contains("active");

      if (isAlreadyActive) {
        // Toggle collapse: hide right pane entirely
        if (pane) pane.style.display = "none";
        btn.classList.remove("active");
        
        // Stop stream webcast if leaving
        if (id === "rtool-streams") stopStreamWebcast();
      } else {
        // Show right pane
        if (pane) pane.style.display = "";
        
        // Remove active class from all other buttons
        document.querySelectorAll(".rtool-btn").forEach(b => {
          if (b.id === "rtool-streams") stopStreamWebcast();
          b.classList.remove("active");
        });
        
        // Activate this button
        btn.classList.add("active");

        // Hide all widget panels, show the target one
        document.querySelectorAll(".widget-panel").forEach(w => {
          w.hidden = true;
        });
        const targetWidget = document.getElementById(targetWidgetId);
        if (targetWidget) targetWidget.hidden = false;

        // Custom triggers on tab opening
        if (id === "rtool-dom" && state.snapshots[state.activeSymbol]?.candles?.length > 0) {
          const last = state.snapshots[state.activeSymbol].candles[state.snapshots[state.activeSymbol].candles.length - 1];
          updateDOM(last.close);
        } else if (id === "rtool-objecttree") {
          window.syncObjectTree();
        } else if (id === "rtool-streams") {
          startStreamWebcast();
        } else if (id === "rtool-hotlists") {
          updateHotlists();
        }
      }
    });
  });

  // Close modal on backdrop click or ✕
  document.getElementById("shortcuts-close").addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", e => { if (e.target === modal) modal.hidden = true; });

  // ? key opens shortcuts, Esc closes modal
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { modal.hidden = true; return; }
    if (isTypingTarget(e.target)) return;
    if (e.key === "?" && !e.ctrlKey && !e.metaKey) { modal.hidden = false; e.preventDefault(); return; }
    // Number keys for asset switching
    const assetMap = { "1": "BTC/USDT", "2": "ETH/USDT", "3": "SOL/USDT", "4": "XAU/USD" };
    if (assetMap[e.key] && !e.ctrlKey && !e.metaKey) {
      const btn = document.querySelector(`.asset-btn[data-symbol="${assetMap[e.key]}"]`);
      if (btn) btn.click();
    }
  });

  // Data Window: subscribe to chart crosshair move
  // This is called once the chart is ready (wired in initChart)
  window._hookDataWindow = function(chart, candleSeries) {
    chart.subscribeCrosshairMove(param => {
      if (!dataWindowActive || !param.time) return;
      const bar = param.seriesData?.get(candleSeries);
      if (!bar) return;
      const fmt = v => Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      document.getElementById("dw-open").textContent  = fmt(bar.open);
      document.getElementById("dw-high").textContent  = fmt(bar.high);
      document.getElementById("dw-low").textContent   = fmt(bar.low);
      document.getElementById("dw-close").textContent = fmt(bar.close);
      document.getElementById("dw-vol").textContent   = bar.volume ? fmt(bar.volume) : "—";
    });
  };
}



// ── Asset Switcher ─────────────────────────────────────────────────
function initAssetSelector() {
  document.getElementById("asset-selector").addEventListener("click", e => {
    const btn = e.target.closest(".asset-btn");
    if (!btn) return;
    const sym = btn.dataset.symbol;
    if (!sym || sym === state.activeSymbol) return;
    switchAsset(sym);
  });
}

function switchAsset(sym) {
  state.activeSymbol = sym;
  if (isMacroDisplaySymbol(sym)) state.activeTimeframe = "1d";

  // Update button states
  document.querySelectorAll("#asset-selector .asset-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.symbol === sym);
  });

  // Highlight watchlist active row
  document.querySelectorAll(".watchlist-row").forEach(row => {
    row.classList.toggle("active", row.getAttribute("data-symbol") === sym);
  });

  // Update order form unit labels
  const orderAssetTag = document.getElementById("order-asset-tag");
  if (orderAssetTag) orderAssetTag.textContent = sym;
  const orderQtyUnit = document.getElementById("order-qty-unit");
  if (orderQtyUnit) orderQtyUnit.textContent = sym.split("/")[0];
  updateOrderAvailability(sym);

  // Update header labels
  document.getElementById("chart-title").textContent = formatChartTitle(sym);
  updateTimeframeUI();
  document.getElementById("engine-asset-tag").textContent = sym;
  updateMarketSourceUI(sym);
  updateAdvisoryBubble(sym);

  // Clear drawings and reset enriched panel when switching asset
  state.markers = [];
  const enrichedWrap = document.getElementById("enriched-context-wrap");
  if (enrichedWrap) enrichedWrap.style.display = "none";
  const confBadge = document.getElementById("agent-confidence-badge");
  if (confBadge) { confBadge.textContent = "—"; confBadge.className = "agent-confidence-badge"; }

  // Start enriched signal polling for new asset
  startEnrichedPoll(sym);

  // Load cached snapshot if available
  const snap = ensureDemoSnapshot(sym) || state.snapshots[sym];
  if (snap) {
    applySnapshot(sym, snap);
  } else {
    // Fetch candles from REST
    fetchCandlesForSymbol(sym);
  }
}

function fetchCandlesForSymbol(sym) {
  if (isMacroDisplaySymbol(sym)) state.activeTimeframe = "1d";
  if (isDemoOnlySymbol(sym)) {
    const snap = ensureDemoSnapshot(sym);
    if (snap) applySnapshot(sym, snap);
    return;
  }

  const enc  = encodeURIComponent(sym);
  const tf   = encodeURIComponent(state.activeTimeframe || "15m");
  fetchJsonWithFallback(`/api/candles?symbol=${enc}&timeframe=${tf}&limit=300`)
    .then(({ data: candles }) => {
      if (!candles || candles.length === 0) throw new Error(`No ${state.activeTimeframe} candles returned for ${sym}`);
      state.displayCandles = candles;
      state.candleSeries.setData(candles);
      const last = candles[candles.length - 1];
      state.latestClose = last.close;
      state.predLongSeries.setData([]);
      state.predShortSeries.setData([]);
      updateTickerUI(last.close, last.open);
      updateWatchlistRow(sym, last.close, last.open);
      if (isMacroDisplaySymbol(sym)) updateEngineUI(0, 0, "NEUTRAL", null, { total_trades: 0, win_trades: 0, total_pnl: 0 });
      updateDataWindowPanel(last, state.snapshots[sym]?.prediction_long ?? 0, state.snapshots[sym]?.prediction_short ?? 0, state.snapshots[sym]?.signal ?? "NEUTRAL");
      state.chart.timeScale().fitContent();
    })
    .catch(e => {
      console.error("fetchCandles:", e);
      state.displayCandles = [];
      if (state.candleSeries) state.candleSeries.setData([]);
      const title = document.getElementById("chart-title");
      if (title) title.textContent = `${sym} · ${timeframeLabel(state.activeTimeframe)} unavailable`;
    });
}

function applySnapshot(sym, snap) {
  if (snap.candles && snap.candles.length > 0) {
    const candles = state.activeTimeframe === "15m" ? snap.candles : state.displayCandles;
    state.candleSeries.setData(candles && candles.length ? candles : snap.candles);
    state.predLongSeries.setData([]);
    state.predShortSeries.setData([]);

    const last = snap.candles[snap.candles.length - 1];

    // Seed live price + ATR from snapshot
    state.latestClose = last.close;
    if (last.atr_proxy && last.atr_proxy > 0) state.latestATR = last.atr_proxy;

    const predL = snap.prediction_long  ?? 0.0;
    const predS = snap.prediction_short ?? 0.0;

    updateTickerUI(last.close, last.open);
    updateEngineUI(predL, predS, snap.signal, snap.position, snap.stats);
    state.chart.timeScale().fitContent();
    updateMarketSourceUI(sym);
    updateAdvisoryBubble(sym, snap.signal);

    // Update active widget modules
    updateDOM(last.close);
    updateHotlists();
    updateDataWindowPanel(last, predL, predS, snap.signal);
  }
  fetchTradesHistory(sym);
  fetchEnrichedSignal(sym);
}


// ── WebSocket ──────────────────────────────────────────────────────
function connectWS() {
  const url   = `${wsBase()}/ws`;

  const statusEl  = document.getElementById("connection-status");
  const statusTxt = document.getElementById("status-text-disp");
  statusTxt.textContent = "Connecting…";

  state.socket = new WebSocket(url);

  state.socket.onopen = () => {
    statusEl.className  = "connection-status active";
    statusTxt.textContent = "Live";
  };

  state.socket.onmessage = e => {
    try {
      if (typeof e.data !== "string" || !e.data.trim().startsWith("{")) return;
      const data = JSON.parse(e.data);
      if (data.type === "init")  handleInit(data);
      else if (data.type === "tick") handleTick(data);
      else if (data.type === "enriched_signal") fetchEnrichedSignal(state.activeSymbol);
    } catch (err) { console.debug("WS ignored non-dashboard message:", err); }
  };

  state.socket.onclose = () => {
    statusEl.className  = "connection-status";
    statusTxt.textContent = "Reconnecting…";
    setTimeout(connectWS, 5000);
  };

  state.socket.onerror = () => state.socket.close();
}

function handleInit(data) {
  // data.snapshots = { "BTC/USDT": {...}, "ETH/USDT": {...}, "SOL/USDT": {...} }
  if (data.snapshots) {
    Object.entries(data.snapshots).forEach(([sym, snap]) => {
      state.snapshots[sym] = snap;
      if (snap.candles && snap.candles.length > 0) {
        const last = snap.candles[snap.candles.length - 1];
        updateWatchlistRow(sym, last.close, last.open);
      }
    });
      applySnapshot(state.activeSymbol, state.snapshots[state.activeSymbol]);
    startEnrichedPoll(state.activeSymbol);
  }
}

function handleTick(data) {
  const sym    = data.symbol;
  const candle = data.candle;

  // Cache in snapshots
  if (!state.snapshots[sym]) state.snapshots[sym] = {};
  const snap = state.snapshots[sym];
  snap.prediction_long  = data.prediction_long;
  snap.prediction_short = data.prediction_short;
  snap.signal   = data.signal;
  snap.position = data.position;
  snap.stats    = data.stats;
  if (!snap.candles) snap.candles = [];
  
  // Update internal snapshot candles buffer
  const lastC = snap.candles[snap.candles.length - 1];
  if (lastC && lastC.time === candle.time) {
    snap.candles[snap.candles.length - 1] = candle;
  } else {
    snap.candles.push(candle);
    if (snap.candles.length > 500) snap.candles.shift();
  }

  // Update watchlist pricing live
  updateWatchlistRow(sym, candle.close, candle.open);

  // Trigger alert logs if signal triggers
  if (["BUY", "SELL", "EXIT"].includes(data.signal)) {
    addAlertNotification(sym, `Engine ${data.signal} @ $${candle.close.toLocaleString()}`);
  }

  // Only update chart/detailed UI if this is the active asset
  if (sym !== state.activeSymbol) return;

  // Cache live price + ATR for price-level computation
  state.latestClose = candle.close;
  if (candle.atr_proxy && candle.atr_proxy > 0) state.latestATR = candle.atr_proxy;

  // Ticker
  updateTickerUI(candle.close, candle.open);

  // Candle + prediction (live WS updates the model timeframe; other display
  // timeframes are refreshed through REST selection to avoid mixing intervals).
  if (state.activeTimeframe === "15m") state.candleSeries.update(candle);
  state.predLongSeries.setData([]);
  state.predShortSeries.setData([]);

  updateEngineUI(data.prediction_long, data.prediction_short, data.signal, data.position, data.stats);

  if (["BUY","SELL","EXIT"].includes(data.signal)) {
    addMarker(candle.time, data.signal, candle.close);
    fetchTradesHistory(sym);
  }

  // Live DOM updating
  updateDOM(candle.close);
  updateHotlists();

  // Data Window table view update
  updateDataWindowPanel(candle, data.prediction_long, data.prediction_short, data.signal);

  // Manual Portfolio PnL check
  if (state.manualPosition && state.manualPosition.symbol === sym) {
    const pnl = calculateManualPnL(candle.close);
    const pnlEl = document.getElementById("manual-pnl-disp");
    if (pnlEl) {
      pnlEl.textContent = `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT`;
      pnlEl.className = pnl >= 0 ? "text-green" : "text-red";
    }
  }
}


// ── UI Updaters ────────────────────────────────────────────────────
function updateTickerUI(price, open) {
  const priceEl  = document.getElementById("live-price");
  const changeEl = document.getElementById("price-change");
  const pct      = open > 0 ? ((price - open) / open * 100) : 0;

  priceEl.textContent = `$${price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  priceEl.className   = `ticker-price ${pct >= 0 ? "up" : "down"}`;

  changeEl.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  changeEl.className   = `ticker-change ${pct >= 0 ? "up" : "down"}`;
}

function updateOrderAvailability(sym) {
  const disabled = isMacroDisplaySymbol(sym);
  ["order-buy-btn", "order-sell-btn", "btn-submit-order"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = disabled;
    el.title = disabled ? "Gold macro reference only — paper trading unavailable" : "";
    el.style.opacity = disabled ? "0.45" : "";
  });
}

function updateEngineUI(predL, predS, signal, position, stats) {
  // ── Signal badge ──────────────────────────────────────────────────
  const badge = document.getElementById("agent-signal-badge");
  if (badge) {
    const sig = (signal || "NEUTRAL").toUpperCase();
    badge.textContent = sig;
    badge.className = "agent-signal-badge " + {
      "BUY":     "buy",
      "SELL":    "sell",
      "EXIT":    "exit",
      "NEUTRAL": "neutral",
    }[sig] || "neutral";
  }

  // ── Probability bars ──────────────────────────────────────────────
  const longPct  = ((predL || 0) * 100);
  const shortPct = ((predS || 0) * 100);

  const fillLong  = document.getElementById("prob-fill-long");
  const fillShort = document.getElementById("prob-fill-short");
  const pctLong   = document.getElementById("prob-pct-long");
  const pctShort  = document.getElementById("prob-pct-short");

  if (fillLong)  fillLong.style.width  = Math.min(longPct,  100) + "%";
  if (fillShort) fillShort.style.width = Math.min(shortPct, 100) + "%";
  if (pctLong)   pctLong.textContent   = longPct.toFixed(1)  + "%";
  if (pctShort)  pctShort.textContent  = shortPct.toFixed(1) + "%";

  // ── Price levels ──────────────────────────────────────────────────
  if (state.latestClose && state.latestATR && signal && signal !== "NEUTRAL") {
    updatePriceLevels(signal, state.latestClose, state.latestATR);
  }

  // ── Advisory bubble ───────────────────────────────────────────────
  updateAdvisoryBubble(state.activeSymbol, signal);

  // ── Agent report note ─────────────────────────────────────────────
  const noteEl = document.getElementById("agent-report-note");
  if (noteEl) {
    if (isMacroDisplaySymbol(state.activeSymbol)) {
      noteEl.textContent = "Gold macro reference only — signal/trading unavailable.";
    } else if (signal && signal !== "NEUTRAL") {
      noteEl.textContent = `Position: ${position || "flat"} · Advisory only, no execution.`;
    } else {
      noteEl.textContent = "No active signal. Monitoring 15m candles.";
    }
  }

  // ── Scalping stats ────────────────────────────────────────────────
  if (stats) {
    const pnlEl = document.getElementById("net-profit-disp");
    if (pnlEl) {
      const pnl = stats.total_pnl ?? 0;
      pnlEl.textContent = `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} USDT`;
      pnlEl.className   = `metric-value ${pnl >= 0 ? "positive" : "negative"}`;
    }
    const wrEl = document.getElementById("win-rate-disp");
    if (wrEl) {
      const wr = stats.total_trades > 0 ? (stats.win_trades / stats.total_trades * 100) : 0;
      wrEl.textContent = `${wr.toFixed(1)}%`;
    }
    const ttEl = document.getElementById("total-trades-disp");
    if (ttEl) ttEl.textContent = stats.total_trades ?? 0;
  }
  if (window.__updateHermesChatSignalContext) window.__updateHermesChatSignalContext();
}

// ── ATR-based price levels ─────────────────────────────────────────
// Uses same multipliers as the training labels: TP1=1.5×ATR, SL=1.0×ATR
function updatePriceLevels(signal, close, atr) {
  const isLong = signal === "BUY";
  const tp1Mult = 1.5, tp2Mult = 2.5, slMult = 1.0;
  const feeDrag = close * 0.0015; // ~0.15% round-trip fee

  let entry, sl, tp1, tp2;
  if (isLong) {
    entry = close;
    sl    = close - slMult  * atr - feeDrag;
    tp1   = close + tp1Mult * atr - feeDrag;
    tp2   = close + tp2Mult * atr - feeDrag;
  } else {
    entry = close;
    sl    = close + slMult  * atr + feeDrag;
    tp1   = close - tp1Mult * atr + feeDrag;
    tp2   = close - tp2Mult * atr + feeDrag;
  }

  const risk   = Math.abs(entry - sl);
  const reward = Math.abs(tp1 - entry);
  const rr     = risk > 0 ? (reward / risk).toFixed(2) : "—";

  const fmt = v => v > 0 ? `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—";

  const set = (id, val) => { const el = document.getElementById(id); if (el) { el.textContent = val; el.className = "stat-value"; } };
  set("level-entry", fmt(entry));
  set("level-sl",    fmt(sl));
  set("level-tp1",   fmt(tp1));
  set("level-tp2",   fmt(tp2));
  set("level-atr",   `$${atr.toFixed(2)}`);
  set("level-rr",    `${rr}:1`);
  if (window.__updateHermesChatSignalContext) window.__updateHermesChatSignalContext();
}

function addMarker(time, type, price) {
  if (state.markers.some(m => m.time === time && m.text === type)) return;
  const marker = {
    time,
    position: (type === "BUY") ? "belowBar" : "aboveBar",
    color:    type === "BUY" ? "var(--color-green)" : type === "SELL" ? "var(--color-orange)" : "var(--color-red)",
    shape:    type === "BUY" ? "arrowUp" : "arrowDown",
    text:     type,
    size:     2,
  };
  state.markers.push(marker);
  state.markers.sort((a, b) => a.time - b.time);
  state.candleSeries.setMarkers(state.markers);
}


// ── Enriched Signal (Hermes signal agent) ──────────────────────────

function fetchEnrichedSignal(sym) {
  const s    = sym || state.activeSymbol;
  const base = apiBase();
  const key  = s.replace("/", "_");

  fetch(`${base}/api/enriched-signal/${key}`)
    .then(r => {
      if (r.status === 204) return null;   // No signal yet — normal
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(data => {
      if (data && s === state.activeSymbol) updateEnrichedUI(data);
    })
    .catch(e => console.debug("enriched-signal fetch:", e));
}

function updateEnrichedUI(data) {
  if (!data) return;

  // Confidence badge
  const confBadge = document.getElementById("agent-confidence-badge");
  if (confBadge) {
    const lvl = (data.confidence_label || "").toLowerCase();
    confBadge.textContent = data.confidence_label || "—";
    confBadge.className   = `agent-confidence-badge ${lvl}`;
  }

  // Show enriched context block
  const wrap = document.getElementById("enriched-context-wrap");
  if (wrap) wrap.style.display = "flex";

  const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val || "—";
  };

  setText("enriched-model-context",  data.model_context);
  setText("enriched-news-summary",   data.news_summary);
  setText("enriched-key-risks",      data.key_risks);
  setText("enriched-analyst-note",   data.analyst_note);

  // Timestamp
  const tsEl = document.getElementById("enriched-timestamp");
  if (tsEl && data.generated_at) {
    try {
      tsEl.textContent = new Date(data.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) { tsEl.textContent = data.generated_at; }
  }

  // Propagate analyst note to advisory bubble
  updateAdvisoryBubble(state.activeSymbol, data.model_signal || data.signal, data.analyst_note);
}

function startEnrichedPoll(sym) {
  if (state.enrichedPollTimer) clearInterval(state.enrichedPollTimer);
  fetchEnrichedSignal(sym);   // Immediate fetch on asset switch
  state.enrichedPollTimer = setInterval(() => fetchEnrichedSignal(state.activeSymbol), 30_000);
}

// ── Trades Table ───────────────────────────────────────────────────
function fetchTradesHistory(sym) {
  const base = apiBase();
  const enc  = encodeURIComponent(sym || state.activeSymbol);
  fetch(`${base}/api/trades?symbol=${enc}`)
    .then(r => r.json())
    .then(trades => populateTradesTable(trades))
    .catch(e => console.error("fetchTrades:", e));
}

function populateTradesTable(trades) {
  const tbody = document.getElementById("trade-history-body");
  if (!trades || trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center empty-state">No trades yet. Waiting for signals…</td></tr>`;
    return;
  }
  const display = [...trades].reverse().slice(0, 50);
  tbody.innerHTML = display.map(t => {
    const time   = new Date(t.exit_time * 1000).toLocaleTimeString([], { hour:"2-digit", minute:"2-digit", second:"2-digit" });
    const isLong = t.type === "LONG";
    const pnlCls = t.pnl >= 0 ? "up" : "down";
    const sign   = t.pnl >= 0 ? "+" : "";
    return `
      <tr>
        <td class="time-cell">${time}</td>
        <td><span class="legend-dot ${isLong ? "green" : "orange"}"></span> ${isLong ? "Long" : "Short"}</td>
        <td>$${t.entry_price.toFixed(2)}</td>
        <td>$${t.exit_price.toFixed(2)}</td>
        <td class="pnl-cell ${pnlCls}">${sign}${t.pnl.toFixed(2)}</td>
        <td>${t.reason}</td>
      </tr>`;
  }).join("");
}


// ── Theme ──────────────────────────────────────────────────────────
function initTheme() {
  const btn   = document.getElementById("theme-toggle");
  const saved = localStorage.getItem("ag-theme") || "dark";

  const apply = (theme) => {
    const isLight = theme === "light";
    document.body.classList.toggle("light-mode", isLight);
    btn.textContent = isLight ? "🌙" : "☀️";
    if (state.chart) applyChartTheme(isLight);
  };

  apply(saved);
  btn.addEventListener("click", () => {
    const isLight = document.body.classList.contains("light-mode");
    const next    = isLight ? "dark" : "light";
    localStorage.setItem("ag-theme", next);
    apply(next);
  });
}

function applyChartTheme(isLight) {
  const text   = isLight ? "#606878" : "#7a8494";
  const grid   = isLight ? "rgba(0,0,0,0.04)" : "rgba(255,255,255,0.03)";
  const border = isLight ? "rgba(0,0,0,0.07)"  : "rgba(255,255,255,0.07)";
  state.chart.applyOptions({
    layout: { textColor: text },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: border },
    timeScale:       { borderColor: border },
  });
  state.chart.priceScale("left").applyOptions({ borderColor: border });
}


// ── Watchlist live price updates ─────────────────────────────────
function updateWatchlistRow(sym, price, open) {
  const safeId = sym.replace("/", "-");
  const priceEl = document.getElementById(`wl-price-${safeId}`);
  const changeEl = document.getElementById(`wl-change-${safeId}`);
  if (!priceEl || !changeEl) return;

  const isBTC = sym.startsWith("BTC");
  const fmtPrice = price.toLocaleString(undefined, { minimumFractionDigits: isBTC ? 1 : 2, maximumFractionDigits: isBTC ? 1 : 2 });
  priceEl.textContent = `$${fmtPrice}`;

  const pct = ((price - open) / open) * 100;
  const fmtPct = pct.toFixed(2);
  const isUp = pct >= 0;
  changeEl.textContent = `${isUp ? "+" : ""}${fmtPct}%`;
  changeEl.className = `wl-change text-right ${isUp ? "text-green" : "text-red"}`;
}

// ── DOM / Order Book ─────────────────────────────────────────────
function updateDOM(price) {
  const sym = state.activeSymbol;
  if (!sym) return;

  const asksContainer = document.getElementById("dom-asks-container");
  const bidsContainer = document.getElementById("dom-bids-container");
  const midPriceEl = document.getElementById("dom-mid-price");
  const spreadEl = document.getElementById("dom-spread");
  if (!asksContainer || !bidsContainer || !midPriceEl || !spreadEl) return;

  const decimals = sym.startsWith("BTC") ? 1 : 2;
  const base = apiBase();
  const enc = encodeURIComponent(sym);
  fetch(`${base}/api/orderbook?symbol=${enc}&limit=10`)
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(book => {
      const bids = book.bids || [];
      const asks = book.asks || [];
      if (!bids.length || !asks.length) throw new Error("empty orderbook");
      const bestBid = bids[0].price;
      const bestAsk = asks[0].price;
      const mid = (bestBid + bestAsk) / 2;
      midPriceEl.textContent = mid.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
      spreadEl.textContent = `Spread: ${(bestAsk - bestBid).toFixed(decimals)} USDT · Bybit`;
      const maxSize = Math.max(...bids.map(x => x.size), ...asks.map(x => x.size), 1);
      asksContainer.innerHTML = "";
      [...asks].reverse().forEach(lvl => {
        const pct = Math.max(3, (lvl.size / maxSize) * 100);
        const row = document.createElement("div");
        row.className = "dom-level-row";
        row.innerHTML = `
          <div class="dom-size">${lvl.size.toLocaleString(undefined, { maximumFractionDigits: 3 })}</div>
          <div class="dom-price text-red">${lvl.price.toFixed(decimals)}</div>
          <div class="dom-depth-pct">${pct.toFixed(0)}%</div>
          <div class="dom-bar ask" style="width: ${Math.min(pct, 100)}%"></div>
        `;
        asksContainer.appendChild(row);
      });
      bidsContainer.innerHTML = "";
      bids.forEach(lvl => {
        const pct = Math.max(3, (lvl.size / maxSize) * 100);
        const row = document.createElement("div");
        row.className = "dom-level-row";
        row.innerHTML = `
          <div class="dom-size">${lvl.size.toLocaleString(undefined, { maximumFractionDigits: 3 })}</div>
          <div class="dom-price text-green">${lvl.price.toFixed(decimals)}</div>
          <div class="dom-depth-pct">${pct.toFixed(0)}%</div>
          <div class="dom-bar bid" style="width: ${Math.min(pct, 100)}%"></div>
        `;
        bidsContainer.appendChild(row);
      });
    })
    .catch(e => {
      console.error("orderbook:", e);
      midPriceEl.textContent = price ? price.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : "—";
      spreadEl.textContent = "Order book unavailable";
      asksContainer.innerHTML = '<div class="empty-state text-center">Live order book unavailable.</div>';
      bidsContainer.innerHTML = '<div class="empty-state text-center">Live order book unavailable.</div>';
    });
}

// ── Hotlists Updater ─────────────────────────────────────────────
function updateHotlists() {
  const volBody = document.getElementById("hotlist-vol-body");
  const gainersBody = document.getElementById("hotlist-gainers-body");
  if (!volBody || !gainersBody) return;

  fetch(`${apiBase()}/api/market-tickers`)
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(data => {
      const assets = data.assets || [];
      if (!assets.length) throw new Error("empty market tickers");
      volBody.innerHTML = "";
      [...assets].sort((a, b) => b.turnover_24h - a.turnover_24h).forEach(a => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td style="padding:6px 4px; font-weight:600;">${a.symbol.split("/")[0]}USD</td>
          <td style="padding:6px 4px; text-align:right; font-family:var(--font-mono); font-size:0.75rem;">$${a.last_price.toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
          <td style="padding:6px 4px; text-align:right; font-family:var(--font-mono); color:var(--color-text-muted); font-size:0.72rem;">${a.turnover_24h.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
        `;
        volBody.appendChild(tr);
      });
      gainersBody.innerHTML = "";
      [...assets].sort((a, b) => b.change_24h - a.change_24h).forEach(a => {
        const tr = document.createElement("tr");
        const isUp = a.change_24h >= 0;
        tr.innerHTML = `
          <td style="padding:6px 4px; font-weight:600;">${a.symbol.split("/")[0]}USD</td>
          <td style="padding:6px 4px; text-align:right; font-family:var(--font-mono); font-weight:bold; font-size:0.75rem;" class="${isUp ? 'text-green' : 'text-red'}">${isUp ? '+' : ''}${a.change_24h.toFixed(2)}%</td>
          <td style="padding:6px 4px; text-align:right; font-family:var(--font-mono); font-size:0.75rem;">$${a.last_price.toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
        `;
        gainersBody.appendChild(tr);
      });
    })
    .catch(e => {
      console.error("market-tickers:", e);
      volBody.innerHTML = '<tr><td colspan="3" class="empty-state text-center">Live exchange ticker data unavailable.</td></tr>';
      gainersBody.innerHTML = '<tr><td colspan="3" class="empty-state text-center">Live exchange ticker data unavailable.</td></tr>';
    });
}

// ── Data Window panel update ─────────────────────────────────────
function updateDataWindowPanel(candle, prediction_long, prediction_short, signal) {
  void prediction_long;
  void prediction_short;
  void signal;
  const tEl = document.getElementById("dw-p-time");
  const oEl = document.getElementById("dw-p-open");
  const hEl = document.getElementById("dw-p-high");
  const lEl = document.getElementById("dw-p-low");
  const cEl = document.getElementById("dw-p-close");
  const vEl = document.getElementById("dw-p-vol");
  const modeEl = document.getElementById("dw-p-mode");
  const sourceEl = document.getElementById("dw-p-source");
  const advisoryEl = document.getElementById("dw-p-advisory");

  if (!tEl || !oEl || !hEl || !lEl || !cEl || !vEl || !modeEl || !sourceEl || !advisoryEl) return;

  const date = new Date((candle.time.timestamp || candle.time) * 1000);
  tEl.textContent = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  oEl.textContent = candle.open.toLocaleString();
  hEl.textContent = candle.high.toLocaleString();
  lEl.textContent = candle.low.toLocaleString();
  cEl.textContent = candle.close.toLocaleString();
  vEl.textContent = candle.volume.toLocaleString(undefined, { maximumFractionDigits: 0 });
  modeEl.textContent = `${timeframeLabel(state.activeTimeframe)} display`;
  sourceEl.textContent = getSymbolSourceLabel(state.activeSymbol);
  advisoryEl.textContent = "Live exchange feed";
  advisoryEl.className = "text-muted";
}

// ── Object Tree manager ──────────────────────────────────────────
window.syncObjectTree = function() {
  const container = document.getElementById("object-tree-container");
  if (!container) return;

  const drawings = state.drawings || [];
  if (drawings.length === 0) {
    container.innerHTML = '<div class="empty-state text-center">No drawings on the chart.</div>';
    return;
  }

  container.innerHTML = "";
  drawings.forEach((d, idx) => {
    const item = document.createElement("div");
    item.className = "tree-item";
    const displayName = d.type.charAt(0).toUpperCase() + d.type.slice(1);
    const isHidden = d.hidden || false;
    const isLocked = d.locked || false;

    item.innerHTML = `
      <div class="tree-item-left">
        <span class="tree-item-name">${displayName}</span>
      </div>
      <div class="tree-item-btns">
        <button class="tree-btn ${isHidden ? '' : 'active'}" data-action="toggle-hide" data-index="${idx}" title="${isHidden ? 'Show' : 'Hide'}">
          <svg viewBox="0 0 18 18" style="width: 14px; height: 14px;"><circle cx="9" cy="9" r="2.5" fill="currentColor"/><path d="M9 3.5 C5 3.5 2.5 9 2.5 9 C2.5 9 5 14.5 9 14.5 C13 14.5 15.5 9 15.5 9 C15.5 9 13 3.5 9 3.5 Z" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>
        </button>
        <button class="tree-btn ${isLocked ? 'active' : ''}" data-action="toggle-lock" data-index="${idx}" title="${isLocked ? 'Unlock' : 'Lock'}">
          <svg viewBox="0 0 18 18" style="width: 14px; height: 14px;"><rect x="4.5" y="7.5" width="9" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M6.5 7.5 V5 A2.5 2.5 0 0 1 11.5 5 V7.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>
        </button>
        <button class="tree-btn danger-btn" data-action="delete" data-index="${idx}" title="Delete">
          <svg viewBox="0 0 18 18" style="width: 14px; height: 14px;"><line x1="4" y1="4" x2="14" y2="14" stroke="currentColor" stroke-width="1.5"/><line x1="14" y1="4" x2="4" y2="14" stroke="currentColor" stroke-width="1.5"/></svg>
        </button>
      </div>
    `;
    container.appendChild(item);
  });

  container.querySelectorAll("button[data-action]").forEach(btn => {
    btn.addEventListener("click", () => {
      const action = btn.getAttribute("data-action");
      const idx = parseInt(btn.getAttribute("data-index"));
      if (action === "toggle-hide") {
        drawings[idx].hidden = !drawings[idx].hidden;
      } else if (action === "toggle-lock") {
        drawings[idx].locked = !drawings[idx].locked;
      } else if (action === "delete") {
        drawings.splice(idx, 1);
      }
      DrawingEngine.redraw();
    });
  });
};

  const btnClearTree = document.getElementById("btn-clear-drawings-tree");
  if (btnClearTree) {
    btnClearTree.addEventListener("click", () => {
      DrawingEngine.clearAll();
    });
  }

  // simulated portfolio state
  state.balance = 10000.00;
  state.manualPosition = null;

  const orderBuyBtn = document.getElementById("order-buy-btn");
  const orderSellBtn = document.getElementById("order-sell-btn");
  const orderTypeSelect = document.getElementById("order-type-select");
  const limitPriceRow = document.getElementById("limit-price-row");
  const orderPriceInput = document.getElementById("order-price-input");
  const orderQtyInput = document.getElementById("order-qty-input");
  const btnSubmitOrder = document.getElementById("btn-submit-order");
  const portfolioBalanceDisp = document.getElementById("portfolio-balance-disp");
  const manualPositionDisp = document.getElementById("manual-position-disp");
  const manualPnlDisp = document.getElementById("manual-pnl-disp");
  const btnClosePosition = document.getElementById("btn-close-position");

  let orderSide = "buy";

  if (orderBuyBtn && orderSellBtn) {
    orderBuyBtn.addEventListener("click", () => {
      orderSide = "buy";
      orderBuyBtn.className = "asset-btn active";
      orderBuyBtn.style.background = "rgba(0,230,118,0.1)";
      orderBuyBtn.style.borderColor = "var(--color-green)";
      orderSellBtn.className = "asset-btn";
      orderSellBtn.style.background = "transparent";
      orderSellBtn.style.borderColor = "var(--border-color)";
    });

    orderSellBtn.addEventListener("click", () => {
      orderSide = "sell";
      orderSellBtn.className = "asset-btn active";
      orderSellBtn.style.background = "rgba(255,61,0,0.1)";
      orderSellBtn.style.borderColor = "var(--color-red)";
      orderBuyBtn.className = "asset-btn";
      orderBuyBtn.style.background = "transparent";
      orderBuyBtn.style.borderColor = "var(--border-color)";
    });
  }

  if (orderTypeSelect) {
    orderTypeSelect.addEventListener("change", () => {
      const isLimit = orderTypeSelect.value === "limit";
      limitPriceRow.style.display = isLimit ? "flex" : "none";
      if (isLimit && state.snapshots[state.activeSymbol]?.candles?.length > 0) {
        const lastCandle = state.snapshots[state.activeSymbol].candles[state.snapshots[state.activeSymbol].candles.length - 1];
        orderPriceInput.value = lastCandle.close;
      }
    });
  }

  if (btnSubmitOrder) {
    btnSubmitOrder.addEventListener("click", () => {
      const sym = state.activeSymbol;
      const qty = parseFloat(orderQtyInput.value);
      if (isNaN(qty) || qty <= 0) {
        alert("Please enter a valid quantity.");
        return;
      }

      if (state.manualPosition) {
        alert("Close your active position first before opening a new one.");
        return;
      }

      let price = 0;
      if (orderTypeSelect.value === "limit") {
        price = parseFloat(orderPriceInput.value);
        if (isNaN(price) || price <= 0) {
          alert("Please enter a valid price.");
          return;
        }
      } else {
        const snap = state.snapshots[sym];
        if (snap && snap.candles && snap.candles.length > 0) {
          price = snap.candles[snap.candles.length - 1].close;
        } else {
          // fallback to UI live ticker price
          const priceTxt = document.getElementById("live-price").textContent.replace("$","").replace(/,/g,"");
          price = parseFloat(priceTxt) || 0;
        }
        if (price <= 0) {
          alert("Price data not loaded yet.");
          return;
        }
      }

      const cost = price * qty;
      if (cost > state.balance) {
        alert("Insufficient balance to execute this simulated trade.");
        return;
      }

      state.manualPosition = {
        symbol: sym,
        side: orderSide,
        entryPrice: price,
        qty: qty,
        cost: cost
      };

      state.balance -= cost;

      const time = Math.floor(Date.now() / 1000);
      addManualMarker(time, orderSide === "buy" ? "BUY" : "SELL", price);

      updatePortfolioUI();
      addAlertNotification(sym, `Manual ${orderSide.toUpperCase()} Filled: ${qty} @ $${price}`);
    });
  }

  if (btnClosePosition) {
    btnClosePosition.addEventListener("click", () => {
      closeManualPosition();
    });
  }

  function closeManualPosition() {
    if (!state.manualPosition) return;
    const sym = state.manualPosition.symbol;
    const snap = state.snapshots[sym];
    let currentPrice = 0;
    if (snap && snap.candles && snap.candles.length > 0) {
      currentPrice = snap.candles[snap.candles.length - 1].close;
    } else {
      const priceTxt = document.getElementById("live-price").textContent.replace("$","").replace(/,/g,"");
      currentPrice = parseFloat(priceTxt) || 0;
    }

    if (currentPrice <= 0) return;
    
    const pnl = calculateManualPnL(currentPrice);
    state.balance += state.manualPosition.cost + pnl;
    
    const time = Math.floor(Date.now() / 1000);
    addManualMarker(time, "EXIT", currentPrice);

    addAlertNotification(sym, `Manual Position Closed: ${state.manualPosition.qty} @ $${currentPrice}. PnL: ${pnl.toFixed(2)} USDT`);

    state.manualPosition = null;
    updatePortfolioUI();
  }

  function calculateManualPnL(currentPrice) {
    if (!state.manualPosition) return 0;
    const pos = state.manualPosition;
    const entry = pos.entryPrice;
    const qty = pos.qty;
    return pos.side === "buy" ? (currentPrice - entry) * qty : (entry - currentPrice) * qty;
  }

  function updatePortfolioUI() {
    portfolioBalanceDisp.textContent = `${state.balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;
    if (state.manualPosition) {
      const pos = state.manualPosition;
      manualPositionDisp.textContent = `${pos.side.toUpperCase()} ${pos.qty} ${pos.symbol.split("/")[0]}`;
      manualPositionDisp.className = pos.side === "buy" ? "text-green" : "text-red";
      btnClosePosition.style.display = "block";
    } else {
      manualPositionDisp.textContent = "None";
      manualPositionDisp.className = "text-muted";
      manualPnlDisp.textContent = "0.00 USDT";
      manualPnlDisp.className = "";
      btnClosePosition.style.display = "none";
    }
  }

  function addManualMarker(time, type, price) {
    if (!state.markers) state.markers = [];
    const marker = {
      time: time,
      position: type === "BUY" ? "belowBar" : (type === "SELL" ? "aboveBar" : "inBar"),
      color: type === "BUY" ? "#00e676" : (type === "SELL" ? "#ff3d00" : "#ffd600"),
      shape: type === "BUY" ? "arrowUp" : (type === "SELL" ? "arrowDown" : "circle"),
      text: `Manual ${type}`
    };
    state.markers.push(marker);
    state.candleSeries.setMarkers(state.markers);
  }

  // ── News and Events ──────────────────────────────────────────────
  // No fabricated client-facing news/calendar data. These panels stay explicit
  // until a real news/calendar feed is wired.

  const chatSendBtn = document.getElementById("btn-send-chat");
  const chatInputField = document.getElementById("chat-input-field");
  const chatMessagesContainer = document.getElementById("chat-messages-container");

  function initNewsAndCalendar() {
    const newsContainer = document.getElementById("news-container");
    if (newsContainer) {
      newsContainer.innerHTML = '<div class="empty-state text-center">Loading live news…</div>';
      fetch(`${apiBase()}/api/news?limit=8`)
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then(data => {
          const items = data.items || [];
          if (!items.length) {
            newsContainer.innerHTML = '<div class="empty-state text-center">Live news feed has no current items.</div>';
            return;
          }
          newsContainer.innerHTML = "";
          items.forEach(item => {
            const div = document.createElement("div");
            div.className = "news-item";
            div.innerHTML = `
              <div style="font-weight:600; color:var(--color-text-main);">${item.title}</div>
              <div style="display:flex; justify-content:space-between; font-size:0.65rem; color:var(--color-text-muted); margin-top:4px;">
                <span>${item.source || "RSS"}</span>
                <span>${item.published || "Live feed"}</span>
              </div>
            `;
            newsContainer.appendChild(div);
          });
        })
        .catch(e => {
          console.error("news:", e);
          newsContainer.innerHTML = '<div class="empty-state text-center">Live news feed unavailable.</div>';
        });
    }

    const calendarContainer = document.getElementById("calendar-events-container");
    if (calendarContainer) {
      calendarContainer.innerHTML = '<div class="empty-state text-center">Loading live economic calendar…</div>';
      fetch(`${apiBase()}/api/calendar?limit=8`)
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then(data => {
          const items = data.items || [];
          if (!items.length) {
            calendarContainer.innerHTML = '<div class="empty-state text-center">Live economic calendar has no current events.</div>';
            return;
          }
          calendarContainer.innerHTML = "";
          items.forEach(item => {
            const div = document.createElement("div");
            div.className = "calendar-item";
            const impact = item.impact || "—";
            const impactColor = impact === "High" ? "var(--color-gold)" : (impact === "Medium" ? "var(--color-blue)" : "var(--color-text-muted)");
            div.innerHTML = `
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:600; color:var(--color-text-main);">${item.title}</span>
                <span style="font-size:0.6rem; font-weight:bold; color:#0f141c; background:${impactColor}; padding:1px 4px; border-radius:3px;">${impact}</span>
              </div>
              <div class="event-time" style="margin-top:4px;">${[item.country, item.date, item.time].filter(Boolean).join(" · ")}</div>
            `;
            calendarContainer.appendChild(div);
          });
        })
        .catch(e => {
          console.error("calendar:", e);
          calendarContainer.innerHTML = '<div class="empty-state text-center">Live economic calendar feed unavailable.</div>';
        });
    }

    if (chatMessagesContainer) {
      chatMessagesContainer.innerHTML = `
        <div class="chat-msg system">
          <div class="msg-header"><span class="msg-sender system">Hermes</span><span>Just now</span></div>
          <div>Live advisory chat is wired through /api/chat. Non-executing selectable-timeframe context only.</div>
        </div>
      `;
    }
  }

  const alertsLogContainer = document.getElementById("alerts-log-container");
  const notificationsContainer = document.getElementById("notifications-container");

  function addAlertNotification(sym, message) {
    if (!alertsLogContainer) return;
    const empty = alertsLogContainer.querySelector(".empty-state");
    if (empty) empty.remove();

    const time = new Date().toLocaleTimeString();
    const isBuy = message.includes("BUY") || message.includes("Long");
    const isSell = message.includes("SELL") || message.includes("Short");
    const cl = isBuy ? "buy" : (isSell ? "sell" : "");

    const div = document.createElement("div");
    div.className = `alert-item ${cl}`;
    div.innerHTML = `
      <div style="font-weight:600; color:var(--color-text-main);">${sym}: ${message}</div>
      <div class="alert-time">${time}</div>
    `;
    alertsLogContainer.insertBefore(div, alertsLogContainer.firstChild);

    if (notificationsContainer) {
      const nEmpty = notificationsContainer.querySelector(".empty-state");
      if (nEmpty) nEmpty.remove();

      const nDiv = document.createElement("div");
      nDiv.className = "notification-item";
      nDiv.innerHTML = `
        <div style="font-weight:600;">${message}</div>
        <div class="notif-time">${time}</div>
      `;
      notificationsContainer.insertBefore(nDiv, notificationsContainer.firstChild);
    }
  }

  let streamAnimationId = null;
  function startStreamWebcast() {
    const canvas = document.getElementById("stream-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    canvas.width = canvas.clientWidth || 320;
    canvas.height = canvas.clientHeight || 180;

    let phase = 0;

    function draw() {
      ctx.fillStyle = "#0c0e14";
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      ctx.strokeStyle = "rgba(255,255,255,0.02)";
      ctx.lineWidth = 1;
      const step = 20;
      for (let x = 0; x < canvas.width; x += step) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, canvas.height);
        ctx.stroke();
      }
      for (let y = 0; y < canvas.height; y += step) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(canvas.width, y);
        ctx.stroke();
      }

      ctx.strokeStyle = "rgba(0, 229, 255, 0.4)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let x = 0; x < canvas.width; x++) {
        const y = canvas.height / 2 + Math.sin(x * 0.02 + phase) * 25 + Math.cos(x * 0.05 - phase * 0.5) * 10;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();

      ctx.fillStyle = "rgba(255,255,255,0.6)";
      ctx.font = "8px monospace";
      const sym = state.activeSymbol;
      ctx.fillText(`Webcast: ${sym}`, 12, 25);
      ctx.fillText(`Mode: 15m scalping advisory`, 12, 38);
      ctx.fillText(`Source: ${getSymbolSourceLabel(sym)}`, 12, 50);

      phase += 0.05;
      streamAnimationId = requestAnimationFrame(draw);
    }

    if (streamAnimationId) cancelAnimationFrame(streamAnimationId);
    draw();
  }

  function stopStreamWebcast() {
    if (streamAnimationId) {
      cancelAnimationFrame(streamAnimationId);
      streamAnimationId = null;
    }
  }

  // Notepad Save
  const notepad = document.getElementById("ideas-notepad");
  const notepadStatus = document.getElementById("notepad-status");
  const btnClearNotepad = document.getElementById("btn-clear-notepad");

  if (notepad) {
    notepad.value = localStorage.getItem("trading_ideas") || "";
    let saveTimeout;
    notepad.addEventListener("input", () => {
      if (notepadStatus) notepadStatus.textContent = "Saving...";
      clearTimeout(saveTimeout);
      saveTimeout = setTimeout(() => {
        localStorage.setItem("trading_ideas", notepad.value);
        if (notepadStatus) notepadStatus.textContent = "Saved locally.";
      }, 800);
    });
  }

  if (btnClearNotepad && notepad) {
    btnClearNotepad.addEventListener("click", () => {
      if (confirm("Are you sure you want to clear your notepad?")) {
        notepad.value = "";
        localStorage.removeItem("trading_ideas");
        if (notepadStatus) notepadStatus.textContent = "Cleared notepad.";
      }
    });
  }


// ── Bootstrap ──────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initChart();
  initTheme();
  initToolbar();
  initTimeframeSelector();
  initAssetSelector();
  updateMarketSourceUI(state.activeSymbol);
  updateAdvisoryBubble(state.activeSymbol);
  connectWS();
  initNewsAndCalendar();
  hermesChat.init();
  initAdvisoryChat();

  // Watchlist click switching
  document.querySelectorAll(".watchlist-row").forEach(row => {
    row.addEventListener("click", () => {
      const sym = row.getAttribute("data-symbol");
      if (sym) switchAsset(sym);
    });
  });

  // Refresh trades every 10s
  setInterval(() => fetchTradesHistory(state.activeSymbol), 10_000);
});

// ── Hermes Chat ────────────────────────────────────────────────────
const hermesChat = (() => {
  const CHAT_HISTORY_MAX = 20;   // messages kept in memory for context
  const I18N = {
    en: {
      langButton: "ES",
      langTitle: "Cambiar idioma / Change language",
      subtitlePrefix: "Signal Agent",
      advisoryMode: "Advisory only · No execution",
      macroMode: "Daily macro context · No trading",
      longLabel: "Long",
      shortLabel: "Short",
      entryLabel: "Entry",
      stopLabel: "Stop",
      proposalExplain: "Explain signal",
      proposalTriggers: "Trigger conditions",
      proposalRisk: "Risk levels",
      proposalSession: "Session state",
      introText: "Online. I provide advisory signal context for BTC, ETH, SOL, and daily Gold macro context. Ask me about current signals, position state, or session performance.",
      placeholder: "Ask about signals, positions, stats…",
      fabTitle: "Chat with Hermes",
      expandTitle: "Expand chat",
      compactTitle: "Compact chat",
      closeTitle: "Close",
      sendTitle: "Send",
      youLabel: "You",
      systemLabel: "System",
      noResponse: "No response.",
      unavailable: "Could not reach Hermes",
    },
    es: {
      langButton: "EN",
      langTitle: "Change language / Cambiar idioma",
      subtitlePrefix: "Agente de señales",
      advisoryMode: "Solo asesoría · Sin ejecución",
      macroMode: "Contexto macro diario · Sin trading",
      longLabel: "Largo",
      shortLabel: "Corto",
      entryLabel: "Entrada",
      stopLabel: "Stop",
      proposalExplain: "Explicar señal",
      proposalTriggers: "Condiciones",
      proposalRisk: "Niveles de riesgo",
      proposalSession: "Estado de sesión",
      introText: "En línea. Doy contexto asesor para señales de BTC, ETH, SOL y contexto macro diario de oro. Pregúntame por señales actuales, posición o rendimiento de sesión.",
      placeholder: "Pregunta por señales, posiciones, estadísticas…",
      fabTitle: "Chatear con Hermes",
      expandTitle: "Expandir chat",
      compactTitle: "Compactar chat",
      closeTitle: "Cerrar",
      sendTitle: "Enviar",
      youLabel: "Tú",
      systemLabel: "Sistema",
      noResponse: "Sin respuesta.",
      unavailable: "No se pudo contactar a Hermes",
    },
  };
  const savedLang = localStorage.getItem("hermes_chat_lang_user_set") === "1"
    ? localStorage.getItem("hermes_chat_lang")
    : "es";
  let lang = savedLang === "en" ? "en" : "es";
  localStorage.setItem("hermes_chat_lang", lang);
  let history = [];              // [{role, content}]
  let pending = false;

  function t(key) {
    return I18N[lang][key] || I18N.en[key] || key;
  }

  function applyLanguage(root = document) {
    root.querySelectorAll("[data-i18n]").forEach(el => {
      const key = el.getAttribute("data-i18n");
      el.textContent = t(key);
    });
    root.querySelectorAll("[data-i18n-title]").forEach(el => {
      const key = el.getAttribute("data-i18n-title");
      el.title = t(key);
    });
    const input = document.getElementById("hermes-chat-input");
    if (input) input.placeholder = t("placeholder");
    const toggle = document.getElementById("hermes-lang-toggle");
    if (toggle) {
      toggle.textContent = t("langButton");
      toggle.title = t("langTitle");
    }
  }

  function formatTime() {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function appendMsg(role, text, container) {
    const div = document.createElement("div");
    div.className = `hermes-msg hermes-msg--${role === "user" ? "user" : "system"}`;
    const sender = role === "user" ? t("youLabel") : "Hermes";
    div.innerHTML =
      `<span class="hermes-msg-sender">${sender} · ${formatTime()}</span><p>${escapeHtml(text)}</p>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function send(message, messagesEl, typingEl) {
    if (pending || !message.trim()) return;
    pending = true;

    appendMsg("user", message, messagesEl);
    history.push({ role: "user", content: message });

    typingEl.hidden = false;
    messagesEl.scrollTop = messagesEl.scrollHeight;

    try {
      const { data, url } = await fetchJsonWithFallback(`/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          symbol: state.activeSymbol,
          language: lang,
          history: history.slice(-CHAT_HISTORY_MAX - 1, -1),
        }),
      });
      typingEl.hidden = true;
      const reply = data.reply || t("noResponse");
      appendMsg("assistant", reply, messagesEl);
      history.push({ role: "assistant", content: reply });
      if (history.length > CHAT_HISTORY_MAX) history = history.slice(-CHAT_HISTORY_MAX);
    } catch (err) {
      typingEl.hidden = true;
      appendMsg("assistant", `⚠ ${t("unavailable")}: ${err.message}`, messagesEl);
    } finally {
      pending = false;
    }
  }

  function init() {
    const fab       = document.getElementById("hermes-fab");
    const panel     = document.getElementById("hermes-chat-panel");
    const closeBtn  = document.getElementById("hermes-chat-close");
    const expandBtn = document.getElementById("hermes-chat-expand");
    const langBtn   = document.getElementById("hermes-lang-toggle");
    const input     = document.getElementById("hermes-chat-input");
    const sendBtn   = document.getElementById("hermes-chat-send");
    const messagesEl = document.getElementById("hermes-chat-messages");
    const typingEl  = document.getElementById("hermes-typing");
    const subtitleEl = document.getElementById("hermes-context-label");
    const proposalsEl = document.getElementById("hermes-proposals");

    if (!fab || !panel) return;
    applyLanguage(document);
    applyLanguage(panel);

    // Toggle panel on FAB click
    fab.addEventListener("click", () => {
      const isOpen = !panel.hidden;
      panel.hidden = isOpen;
      fab.classList.toggle("open", !isOpen);
      if (!isOpen && input) input.focus();
    });

    // Close button
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        panel.hidden = true;
        fab.classList.remove("open");
      });
    }

    if (expandBtn) {
      expandBtn.addEventListener("click", () => {
        panel.classList.toggle("full");
        const isFull = panel.classList.contains("full");
        expandBtn.textContent = isFull ? "▢" : "□";
        expandBtn.title = isFull ? t("compactTitle") : t("expandTitle");
        if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
      });
    }

    if (langBtn) {
      langBtn.addEventListener("click", () => {
        lang = lang === "es" ? "en" : "es";
        localStorage.setItem("hermes_chat_lang", lang);
        localStorage.setItem("hermes_chat_lang_user_set", "1");
        applyLanguage(document);
        if (subtitleEl) subtitleEl.textContent = `${t("subtitlePrefix")} · ${state.activeSymbol}`;
        updateSignalContext();
      });
    }

    // Send on button click or Enter
    const doSend = () => {
      if (!input) return;
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      input.style.height = "";
      send(text, messagesEl, typingEl);
    };

    const sendPrompt = prompt => {
      if (!input || !prompt) return;
      input.value = prompt;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      doSend();
    };

    if (sendBtn) sendBtn.addEventListener("click", doSend);
    if (input) {
      input.addEventListener("input", () => {
        input.style.height = "";
        input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
      });
      input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          doSend();
        }
      });
    }

    if (proposalsEl) {
      proposalsEl.querySelectorAll("button").forEach(btn => {
        btn.addEventListener("click", () => sendPrompt(btn.dataset[`prompt${lang === "es" ? "Es" : "En"}`]));
      });
    }

    function readText(id) {
      const el = document.getElementById(id);
      return el ? (el.textContent || "—").trim() : "—";
    }

    function pctNumber(text) {
      const n = Number(String(text || "").replace("%", ""));
      return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
    }

    function updateSignalContext() {
      const sig = readText("agent-signal-badge") || "NEUTRAL";
      const sigEl = document.getElementById("hermes-chat-signal");
      const modeEl = document.getElementById("hermes-chat-mode");
      const longEl = document.getElementById("hermes-chat-long");
      const shortEl = document.getElementById("hermes-chat-short");
      const longFill = document.getElementById("hermes-chat-long-fill");
      const shortFill = document.getElementById("hermes-chat-short-fill");
      const isMacro = isMacroDisplaySymbol(state.activeSymbol);
      if (sigEl) {
        sigEl.textContent = sig;
        sigEl.className = `hermes-signal-badge ${String(sig).toLowerCase()}`;
      }
      if (modeEl) modeEl.textContent = isMacro ? t("macroMode") : t("advisoryMode");
      const longPct = readText("prob-pct-long");
      const shortPct = readText("prob-pct-short");
      if (longEl) longEl.textContent = longPct;
      if (shortEl) shortEl.textContent = shortPct;
      if (longFill) longFill.style.width = `${pctNumber(longPct)}%`;
      if (shortFill) shortFill.style.width = `${pctNumber(shortPct)}%`;
      [
        ["hermes-chat-entry", "level-entry"],
        ["hermes-chat-sl", "level-sl"],
        ["hermes-chat-tp1", "level-tp1"],
        ["hermes-chat-rr", "level-rr"],
      ].forEach(([to, from]) => {
        const el = document.getElementById(to);
        if (el) el.textContent = isMacro ? "—" : readText(from);
      });
    }

    window.__updateHermesChatSignalContext = updateSignalContext;
    updateSignalContext();

    // Keep subtitle in sync with active asset
    const origSwitch = window._hermesAssetSwitchHook;
    // Patch into switchAsset: update subtitle whenever asset changes
    const _origSwitchAsset = window.switchAsset;
    function updateSubtitle(sym) {
      if (subtitleEl) subtitleEl.textContent = `${t("subtitlePrefix")} · ${sym || state.activeSymbol}`;
      updateSignalContext();
    }
    updateSubtitle(state.activeSymbol);

    // Observe asset selector buttons to update subtitle
    const assetBtns = document.querySelectorAll(".asset-btn[data-symbol]");
    assetBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        setTimeout(() => updateSubtitle(state.activeSymbol), 50);
      });
    });
  }

  return { init };
})();

// ── Upgrade existing Advisory Chat widget to call /api/chat ────────
function initAdvisoryChat() {
  const chatSend = document.getElementById("btn-send-chat");
  const chatInput = document.getElementById("chat-input-field");
  const chatContainer = document.getElementById("chat-messages-container");
  if (!chatSend || !chatInput || !chatContainer) return;

  // Remove existing listeners by cloning
  const newBtn = chatSend.cloneNode(true);
  chatSend.parentNode.replaceChild(newBtn, chatSend);
  const newInput = chatInput.cloneNode(true);
  chatInput.parentNode.replaceChild(newInput, chatInput);

  function appendChatMsg(cls, sender, text) {
    const div = document.createElement("div");
    div.className = `chat-msg ${cls}`;
    div.innerHTML = `<div class="msg-header"><span class="msg-sender ${cls}">${sender}</span><span>${new Date().toLocaleTimeString()}</span></div><div>${text}</div>`;
    chatContainer.appendChild(div);
    chatContainer.scrollTop = chatContainer.scrollHeight;
  }

  async function sendAdvisory() {
    const text = newInput.value.trim();
    if (!text) return;
    newInput.value = "";
    appendChatMsg("user", "You", text);
    try {
      const { data } = await fetchJsonWithFallback(`/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, symbol: state.activeSymbol, language: localStorage.getItem("hermes_chat_lang") === "en" ? "en" : "es", history: [] }),
      });
      appendChatMsg("advisory", "Hermes", data.reply || (localStorage.getItem("hermes_chat_lang") === "en" ? "No response." : "Sin respuesta."));
    } catch (err) {
      const es = localStorage.getItem("hermes_chat_lang") !== "en";
      appendChatMsg("system", es ? "Sistema" : "System", `${es ? "No se pudo contactar a Hermes" : "Could not reach Hermes"}: ${err.message}`);
    }
  }

  newBtn.addEventListener("click", sendAdvisory);
  newInput.addEventListener("keydown", e => { if (e.key === "Enter") sendAdvisory(); });
}
