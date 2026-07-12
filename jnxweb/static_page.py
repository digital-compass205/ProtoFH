"""The jnxweb operator page: one self-contained HTML/CSS/JS string.

Fonts are loaded from Google Fonts' CDN (Inter for UI text, JetBrains
Mono for tabular figures) with a full system-font fallback stack, so
the page still renders correctly if the target box has no outbound
internet access -- it just falls back to the platform's default sans
and monospace. Everything else (structure, WebSocket protocol) stays
dependency-free.

The browser side:

- polls GET /tickers every 2s to refresh the ticker datalist
- polls GET /stats every 1s for the footer (computes updates/s itself
  from the delta between polls)
- opens one WebSocket to /ws, auto-reconnecting with a fixed backoff
- picking a ticker (typing an exact match or choosing from the
  datalist) sends {"sub": "<ticker>"} and renders whatever comes back:
  an {"error": "unknown"} placeholder, a full snapshot (book table +
  trades + static/state panel), or a {"event": "restarted", ...}
  banner on an FH epoch change
- three tabs share that snapshot: Order book and Trades render
  straight from it; All orders is a separate on-demand round trip
  to jnxdb (GET /orders/<ticker>, only fired by the Refresh button --
  it is a live DB query, not part of the push feed, so it never
  fires on its own)

Prices are raw integers with one implied decimal digit on the wire
(JNX_PLAN2.md §3); the JS divides by 10 for display and renders the
NO_PRICE sentinel (0x7FFFFFFF = 2147483647) as "-".
"""

PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>jnxweb</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0d13; --surface: #10151d; --surface2: #161d28; --border: #232b3a;
    --text: #e6e9ef; --muted: #7d8ba0; --accent: #4c8dff; --accent-dim: rgba(76,141,255,0.14);
    --bid: #34d399; --bidbg: rgba(52,211,153,0.08);
    --ask: #fb7185; --askbg: rgba(251,113,133,0.08);
    --warn: #f5b556; --bad: #fb7185;
    --sans: "Inter", -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 18px 22px 24px;
    font-family: var(--sans); font-size: 13px;
    background: var(--bg); color: var(--text);
  }
  .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  a { color: var(--accent); }
  h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }
  .topline { display: flex; align-items: baseline; gap: 14px; margin-bottom: 16px; }
  .topline .sub { font-size: 12px; color: var(--muted); }

  #banner {
    display: none; background: rgba(245,181,86,0.12); border: 1px solid rgba(245,181,86,0.35);
    color: var(--warn); padding: 8px 12px; margin-bottom: 14px; border-radius: 7px; font-size: 12.5px;
  }

  #layout { display: flex; gap: 16px; align-items: flex-start; }

  /* -- ticker picker: searchable combobox, built for a long instrument list -- */
  #tickerpicker { width: 220px; flex: none; }
  #tickerpicker label {
    display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); margin-bottom: 6px; font-weight: 600;
  }
  #tickerbox {
    width: 100%; background: var(--surface); border: 1px solid var(--border); color: var(--text);
    border-radius: 7px; padding: 8px 10px; font: inherit; font-size: 13px; font-family: var(--mono);
  }
  #tickerbox:focus { outline: none; border-color: var(--accent); }
  #tickerbox::placeholder { color: var(--muted); font-family: var(--sans); }
  #tickercount { margin-top: 6px; font-size: 11px; color: var(--muted); }

  #main { flex: 1; min-width: 0; }

  .pricebar {
    display: flex; align-items: baseline; gap: 22px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 9px; padding: 14px 18px; margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .last-px { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; }
  #panel-name { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
  .meta { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .meta b { color: var(--text); font-weight: 500; }

  .tabs { display: flex; gap: 4px; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
  .tab {
    appearance: none; border: none; background: none; color: var(--muted);
    font: inherit; font-size: 12.5px; font-weight: 600; padding: 9px 14px; cursor: pointer;
    border-radius: 6px 6px 0 0; position: relative; top: 1px;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom: 2px solid var(--accent); }
  .tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

  .tabpanel { display: none; }
  .tabpanel.active { display: block; }

  .panel-title {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    margin: 0 0 8px; font-weight: 600;
  }
  .book-wrap, .orders-wrap, .trades-wrap {
    background: var(--surface); border: 1px solid var(--border); border-radius: 9px;
    padding: 12px 4px 4px;
  }
  .orders-wrap, .trades-wrap { max-height: 420px; overflow-y: auto; }

  table { width: 100%; border-collapse: collapse; margin-bottom: 4px; }
  th {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
    font-weight: 600; text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--surface);
  }
  th.left, td.left { text-align: left; }
  td { padding: 6px 10px; text-align: right; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.03); }
  td.bid { color: var(--bid); background: var(--bidbg); }
  td.ask { color: var(--ask); background: var(--askbg); }
  td.side-B { color: var(--bid); }
  td.side-S { color: var(--ask); }

  .orders-toolbar {
    display: flex; align-items: center; gap: 10px; padding: 0 10px 10px; font-size: 12px; color: var(--muted);
  }
  #refresh-orders {
    appearance: none; border: 1px solid var(--border); background: var(--surface2); color: var(--text);
    font: inherit; font-size: 12px; font-weight: 600; padding: 6px 12px; border-radius: 6px; cursor: pointer;
  }
  #refresh-orders:hover { border-color: var(--accent); color: var(--accent); }
  #refresh-orders:disabled { opacity: 0.5; cursor: default; }
  #refresh-orders:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  #orders-status.err { color: var(--bad); }
  #orders-empty { padding: 24px 10px; color: var(--muted); font-size: 12.5px; text-align: center; }

  #footer {
    display: flex; gap: 20px; margin-top: 16px; padding-top: 10px; border-top: 1px solid var(--border);
    font-size: 12px; color: var(--muted); align-items: center;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 6px; background: var(--muted); }
  .dot.ok { background: var(--bid); box-shadow: 0 0 6px var(--bid); }
  .dot.bad { background: var(--bad); box-shadow: 0 0 6px var(--bad); }

  @media (max-width: 760px) {
    #layout { flex-direction: column; }
    #tickerpicker { width: 100%; }
  }
</style>
</head>
<body>
<div class="topline">
  <h1>jnxweb</h1>
  <span class="sub">Japannext feed monitor</span>
</div>
<div id="banner"></div>
<div id="layout">
  <div id="tickerpicker">
    <label for="tickerbox">Ticker</label>
    <input id="tickerbox" type="text" list="tickerlist" placeholder="type or pick a ticker..." autocomplete="off">
    <datalist id="tickerlist"></datalist>
    <div id="tickercount" class="mono">0 tickers seen</div>
  </div>
  <div id="main">
    <div class="pricebar mono">
      <div>
        <div id="panel-name">(no ticker selected)</div>
        <div class="last-px" id="last-px">-</div>
      </div>
      <div class="meta" id="panel-meta"></div>
    </div>

    <div class="tabs" role="tablist">
      <button class="tab active" data-tab="book">Order book</button>
      <button class="tab" data-tab="orders">All orders</button>
      <button class="tab" data-tab="trades">Trades</button>
    </div>

    <div class="tabpanel active" id="tabpanel-book">
      <div class="book-wrap">
        <p class="panel-title" style="padding-left:10px">Depth</p>
        <table class="mono">
          <thead>
            <tr>
              <th class="left" colspan="3">bid</th>
              <th class="left" colspan="3">ask</th>
            </tr>
            <tr>
              <th>orders</th><th>qty</th><th>price</th>
              <th>price</th><th>qty</th><th>orders</th>
            </tr>
          </thead>
          <tbody id="booktbody"></tbody>
        </table>
      </div>
    </div>

    <div class="tabpanel" id="tabpanel-orders">
      <div class="orders-wrap">
        <div class="orders-toolbar">
          <button id="refresh-orders">Refresh</button>
          <span id="orders-status">not queried yet</span>
        </div>
        <table class="mono" id="orders-table" style="display:none">
          <thead><tr><th class="left">order#</th><th>side</th><th>price</th><th>qty remaining</th><th>type</th></tr></thead>
          <tbody id="orderstbody"></tbody>
        </table>
        <div id="orders-empty">Select a ticker, then press Refresh to query jnxdb for its live resting orders.</div>
      </div>
    </div>

    <div class="tabpanel" id="tabpanel-trades">
      <div class="trades-wrap">
        <table class="mono">
          <thead><tr><th class="left">exch_seq</th><th>price</th><th>qty</th></tr></thead>
          <tbody id="tradestbody"></tbody>
        </table>
      </div>
    </div>

    <div id="footer" class="mono">
      <span><span class="dot" id="wsdot"></span><span id="wsstate">ws: connecting</span></span>
      <span id="rate">updates/s: -</span>
      <span id="gaps">gaps: -</span>
      <span id="bad">bad: -</span>
    </div>
  </div>
</div>
<script>
(function () {
  "use strict";
  var NO_PRICE = 2147483647;
  var selected = null;
  var lastRec = null;
  var ws = null;
  var allTickers = [];
  var lastStats = null;
  var ordersTicker = null;  // ticker the current /orders table content belongs to

  function fmtPrice(raw) {
    if (raw === NO_PRICE || raw === undefined || raw === null) return "-";
    var whole = Math.trunc(raw / 10);
    var frac = Math.abs(raw % 10);
    return whole + "." + frac;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // -- ticker picker (datalist combobox, built for a long instrument list) --

  function refreshTickers() {
    fetch("/tickers").then(function (r) { return r.json(); }).then(function (list) {
      allTickers = list;
      var box = document.getElementById("tickerlist");
      box.innerHTML = "";
      list.forEach(function (t) {
        var opt = document.createElement("option");
        opt.value = t;
        box.appendChild(opt);
      });
      document.getElementById("tickercount").textContent = list.length + " tickers seen";
    }).catch(function () {});
  }

  function selectTicker(t) {
    if (t === selected) return;
    selected = t;
    lastRec = null;
    ordersTicker = null;
    resetOrdersPanel();
    document.getElementById("panel-name").textContent = "loading " + t + " ...";
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ sub: t }));
    }
  }

  document.getElementById("tickerbox").addEventListener("input", function (ev) {
    var v = ev.target.value.trim();
    if (v && allTickers.indexOf(v) !== -1) selectTicker(v);
  });
  document.getElementById("tickerbox").addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter") return;
    var v = ev.target.value.trim().toUpperCase();
    var match = allTickers.filter(function (t) { return t.toUpperCase() === v; })[0];
    if (match) selectTicker(match);
  });

  // -- tabs --

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
      document.querySelectorAll(".tabpanel").forEach(function (p) { p.classList.remove("active"); });
      tab.classList.add("active");
      document.getElementById("tabpanel-" + tab.dataset.tab).classList.add("active");
    });
  });

  // -- order book + trades: rendered straight from the push snapshot --

  function renderSnapshot(rec) {
    if (rec.error) {
      document.getElementById("panel-name").textContent = selected + ": " + rec.error;
      document.getElementById("last-px").textContent = "-";
      document.getElementById("panel-meta").innerHTML = "";
      document.getElementById("booktbody").innerHTML = "";
      document.getElementById("tradestbody").innerHTML = "";
      return;
    }
    lastRec = rec;
    document.getElementById("panel-name").textContent = rec.ticker + (rec.isin ? " · " + rec.isin : "");
    document.getElementById("last-px").textContent = fmtPrice(rec.last_price);

    var meta = document.getElementById("panel-meta");
    meta.innerHTML = "";
    [
      ["x " + (rec.last_qty || 0), "last qty"],
      [(rec.cum_qty || 0), "cum_qty"],
      [fmtPrice(rec.reference_price), "ref_price"],
      [rec.trading_state || "?", "state"],
      [rec.short_sell_restriction || "?", "short_sell"],
      [fmtPrice(rec.short_sell_price), "short_sell_price"],
      [rec.exch_seq, "exch_seq"],
    ].forEach(function (pair) {
      var span = el("span", null, pair[1] + " ");
      var b = el("b", null, pair[0]);
      span.appendChild(b);
      meta.appendChild(span);
    });

    var body = document.getElementById("booktbody");
    body.innerHTML = "";
    var depth = Math.max(rec.level_count_bid || 0, rec.level_count_ask || 0, 1);
    for (var i = 0; i < Math.min(depth, 10); i++) {
      var bid = (rec.bids && rec.bids[i]) || [0, 0, 0];
      var ask = (rec.asks && rec.asks[i]) || [0, 0, 0];
      var haveBid = i < (rec.level_count_bid || 0);
      var haveAsk = i < (rec.level_count_ask || 0);
      var tr = document.createElement("tr");
      tr.appendChild(el("td", "bid", haveBid ? bid[2] : "-"));
      tr.appendChild(el("td", "bid", haveBid ? bid[1] : "-"));
      tr.appendChild(el("td", "bid", haveBid ? fmtPrice(bid[0]) : "-"));
      tr.appendChild(el("td", "ask", haveAsk ? fmtPrice(ask[0]) : "-"));
      tr.appendChild(el("td", "ask", haveAsk ? ask[1] : "-"));
      tr.appendChild(el("td", "ask", haveAsk ? ask[2] : "-"));
      body.appendChild(tr);
    }

    var tbody = document.getElementById("tradestbody");
    tbody.innerHTML = "";
    (rec.trades || []).forEach(function (t) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", "left", t.exch_seq));
      tr.appendChild(el("td", null, fmtPrice(t.price)));
      tr.appendChild(el("td", null, t.qty));
      tbody.appendChild(tr);
    });
  }

  // -- all orders: on-demand jnxdb query, driven only by the Refresh button --

  function resetOrdersPanel() {
    document.getElementById("orders-status").textContent = "not queried yet";
    document.getElementById("orders-status").className = "";
    document.getElementById("orders-table").style.display = "none";
    document.getElementById("orders-empty").style.display = "block";
    document.getElementById("orders-empty").textContent =
      "Select a ticker, then press Refresh to query jnxdb for its live resting orders.";
    document.getElementById("orderstbody").innerHTML = "";
  }

  function refreshOrders() {
    if (!selected) return;
    var btn = document.getElementById("refresh-orders");
    var status = document.getElementById("orders-status");
    btn.disabled = true;
    status.className = "";
    status.textContent = "querying jnxdb for " + selected + " ...";
    fetch("/orders/" + encodeURIComponent(selected)).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
    }).then(function (res) {
      btn.disabled = false;
      if (ordersTicker !== selected) {
        // ticker changed while the request was in flight -- drop it
        return;
      }
      if (!res.ok) {
        status.className = "err";
        status.textContent = "error: " + (res.body.error || res.status);
        document.getElementById("orders-table").style.display = "none";
        document.getElementById("orders-empty").style.display = "block";
        document.getElementById("orders-empty").textContent = "-";
        return;
      }
      renderOrders(res.body.orders || []);
      var stamp = new Date().toLocaleTimeString();
      status.textContent = res.body.orders.length + " resting orders as of " + stamp;
    }).catch(function (err) {
      btn.disabled = false;
      status.className = "err";
      status.textContent = "request failed: " + err;
    });
  }

  function renderOrders(orders) {
    var tbody = document.getElementById("orderstbody");
    tbody.innerHTML = "";
    if (orders.length === 0) {
      document.getElementById("orders-table").style.display = "none";
      document.getElementById("orders-empty").style.display = "block";
      document.getElementById("orders-empty").textContent = "No resting orders.";
      return;
    }
    document.getElementById("orders-table").style.display = "table";
    document.getElementById("orders-empty").style.display = "none";
    orders.forEach(function (o) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", "left", o.order_number));
      tr.appendChild(el("td", "side-" + o.side, o.side));
      tr.appendChild(el("td", null, o.price));
      tr.appendChild(el("td", null, o.qty_remaining));
      tr.appendChild(el("td", null, o.order_type));
      tbody.appendChild(tr);
    });
  }

  document.getElementById("refresh-orders").addEventListener("click", function () {
    ordersTicker = selected;
    refreshOrders();
  });

  // -- websocket --

  function showBanner(text) {
    var b = document.getElementById("banner");
    b.textContent = text;
    b.style.display = "block";
    setTimeout(function () { b.style.display = "none"; }, 8000);
  }

  function setWsState(text, ok) {
    document.getElementById("wsstate").textContent = "ws: " + text;
    document.getElementById("wsdot").className = "dot" + (ok === true ? " ok" : ok === false ? " bad" : "");
  }

  function connectWs() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = function () {
      setWsState("connected", true);
      if (selected) ws.send(JSON.stringify({ sub: selected }));
    };
    ws.onclose = function () {
      setWsState("disconnected, retrying", false);
      setTimeout(connectWs, 1000);
    };
    ws.onerror = function () { setWsState("error", false); };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.event === "restarted") {
        showBanner("feed restarted (epoch=" + msg.epoch + ")");
        return;
      }
      renderSnapshot(msg);
    };
  }

  function refreshStats() {
    fetch("/stats").then(function (r) { return r.json(); }).then(function (s) {
      var now = Date.now();
      if (lastStats) {
        var dt = (now - lastStats.t) / 1000;
        var rate = dt > 0 ? (s.updates - lastStats.updates) / dt : 0;
        document.getElementById("rate").textContent = "updates/s: " + rate.toFixed(1);
      }
      lastStats = { t: now, updates: s.updates };
      document.getElementById("gaps").textContent = "gaps: " + s.gaps;
      document.getElementById("bad").textContent = "bad: " + s.bad;
    }).catch(function () {});
  }

  refreshTickers();
  setInterval(refreshTickers, 2000);
  refreshStats();
  setInterval(refreshStats, 1000);
  connectWs();
})();
</script>
</body>
</html>
"""
