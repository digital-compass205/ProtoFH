"""The jnxweb operator page: one self-contained HTML/CSS/JS string.

No external assets (no CDN fonts/libs -- stdlib-only server, and the
target box may have no internet access at all). Monospace tables,
minimal styling: legible over pretty. The browser side:

- polls GET /tickers every 2s to refresh the clickable ticker list
- polls GET /stats every 1s for the footer (computes updates/s itself
  from the delta between polls)
- opens one WebSocket to /ws, auto-reconnecting with a fixed backoff
- clicking a ticker sends {"sub": "<ticker>"} and renders whatever
  comes back: an {"error": "unknown"} placeholder, a full snapshot
  (book table + trades + static/state panel), or a
  {"event": "restarted", ...} banner on an FH epoch change

Prices are raw integers with one implied decimal digit on the wire
(JNX_PLAN2.md §3); the JS divides by 10 for display and renders the
NO_PRICE sentinel (0x7FFFFFFF = 2147483647) as "-".
"""

PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>jnxweb</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 12px;
    font-family: Menlo, Consolas, "Courier New", monospace;
    font-size: 13px;
    background: #111; color: #ddd;
  }
  h1 { font-size: 15px; margin: 0 0 8px; }
  a { color: #6cf; }
  #layout { display: flex; gap: 12px; align-items: flex-start; }
  #tickers {
    width: 160px; max-height: 80vh; overflow-y: auto;
    border: 1px solid #444; padding: 4px;
  }
  #tickers input {
    width: 100%; margin-bottom: 6px; background: #222; color: #ddd;
    border: 1px solid #444; padding: 3px;
  }
  #tickers .row { padding: 2px 4px; cursor: pointer; }
  #tickers .row:hover { background: #333; }
  #tickers .row.active { background: #245; color: #fff; }
  #main { flex: 1; min-width: 0; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 10px; }
  th, td {
    border: 1px solid #333; padding: 2px 6px; text-align: right;
    white-space: nowrap;
  }
  th { background: #1a1a1a; text-align: center; }
  td.left, th.left { text-align: left; }
  .bidcol { color: #6f6; }
  .askcol { color: #f66; }
  #panel { margin-bottom: 10px; }
  #panel span { margin-right: 14px; }
  #banner {
    display: none; background: #722; color: #fff; padding: 6px 10px;
    margin-bottom: 10px; border-radius: 3px;
  }
  #footer {
    margin-top: 14px; padding-top: 6px; border-top: 1px solid #444;
    color: #999;
  }
  #footer span { margin-right: 18px; }
  .ok { color: #6f6; }
  .bad { color: #f66; }
</style>
</head>
<body>
<h1>jnxweb -- Japannext feed monitor</h1>
<div id="banner"></div>
<div id="layout">
  <div id="tickers">
    <input id="filter" placeholder="filter...">
    <div id="tickerlist"></div>
  </div>
  <div id="main">
    <div id="panel">(select a ticker)</div>
    <table id="book">
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
    <table id="trades">
      <thead><tr><th class="left">exch_seq</th><th>price</th><th>qty</th></tr></thead>
      <tbody id="tradestbody"></tbody>
    </table>
  </div>
</div>
<div id="footer">
  <span id="wsstate">ws: connecting</span>
  <span id="rate">updates/s: -</span>
  <span id="gaps">gaps: -</span>
  <span id="bad">bad: -</span>
</div>
<script>
(function () {
  "use strict";
  var NO_PRICE = 2147483647;
  var selected = null;
  var ws = null;
  var allTickers = [];
  var lastStats = null;

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

  function refreshTickers() {
    fetch("/tickers").then(function (r) { return r.json(); }).then(function (list) {
      allTickers = list;
      renderTickerList();
    }).catch(function () {});
  }

  function renderTickerList() {
    var filterVal = document.getElementById("filter").value.trim().toUpperCase();
    var box = document.getElementById("tickerlist");
    box.innerHTML = "";
    allTickers.forEach(function (t) {
      if (filterVal && t.toUpperCase().indexOf(filterVal) === -1) return;
      var row = el("div", "row" + (t === selected ? " active" : ""), t);
      row.onclick = function () { selectTicker(t); };
      box.appendChild(row);
    });
  }

  function selectTicker(t) {
    selected = t;
    renderTickerList();
    document.getElementById("panel").textContent = "loading " + t + " ...";
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ sub: t }));
    }
  }

  function renderSnapshot(rec) {
    if (rec.error) {
      document.getElementById("panel").textContent =
        selected + ": " + rec.error;
      document.getElementById("booktbody").innerHTML = "";
      document.getElementById("tradestbody").innerHTML = "";
      return;
    }
    var panel = document.getElementById("panel");
    panel.innerHTML = "";
    var fields = [
      ["ticker", rec.ticker], ["isin", rec.isin],
      ["state", rec.trading_state], ["short_sell", rec.short_sell_restriction],
      ["short_sell_price", fmtPrice(rec.short_sell_price)],
      ["ref_price", fmtPrice(rec.reference_price)],
      ["last", fmtPrice(rec.last_price) + " x " + (rec.last_qty || 0)],
      ["cum_qty", rec.cum_qty], ["exch_seq", rec.exch_seq],
    ];
    fields.forEach(function (kv) {
      var span = el("span", null, kv[0] + "=" + kv[1]);
      panel.appendChild(span);
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
      tr.appendChild(el("td", null, haveBid ? bid[2] : "-"));
      tr.appendChild(el("td", "bidcol", haveBid ? bid[1] : "-"));
      tr.appendChild(el("td", "bidcol", haveBid ? fmtPrice(bid[0]) : "-"));
      tr.appendChild(el("td", "askcol", haveAsk ? fmtPrice(ask[0]) : "-"));
      tr.appendChild(el("td", "askcol", haveAsk ? ask[1] : "-"));
      tr.appendChild(el("td", null, haveAsk ? ask[2] : "-"));
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

  function showBanner(text) {
    var b = document.getElementById("banner");
    b.textContent = text;
    b.style.display = "block";
    setTimeout(function () { b.style.display = "none"; }, 8000);
  }

  function setWsState(text, ok) {
    var e = document.getElementById("wsstate");
    e.textContent = "ws: " + text;
    e.className = ok ? "ok" : "bad";
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
        document.getElementById("rate").textContent =
          "updates/s: " + rate.toFixed(1);
      }
      lastStats = { t: now, updates: s.updates };
      document.getElementById("gaps").textContent = "gaps: " + s.gaps;
      document.getElementById("bad").textContent = "bad: " + s.bad;
    }).catch(function () {});
  }

  document.getElementById("filter").addEventListener("input", renderTickerList);

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
