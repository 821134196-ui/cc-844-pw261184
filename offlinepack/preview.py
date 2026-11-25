"""Static preview page for a bundle diff.

The page is a single self-contained HTML file: no network, no build step, so it
can be opened from the same isolated environment the bundle targets.  It shows
the diff between two bundles and the reuse accounting of the newer one.

All bundle-derived strings (chains, names, digests) are inserted with
``textContent``/``createTextNode``, never string-concatenated into ``innerHTML``.
"""

from __future__ import annotations

from pathlib import Path

from .canon import TOOL_NAME, TOOL_VERSION
from .diff import BundleDiff
from .errors import BundleError

_PAGE = """<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light;
    --plane: #f9f9f7;
    --surface-1: #fcfcfb;
    --surface-2: #f2f1ee;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11, 11, 11, 0.10);
    --series-1: #2a78d6;  /* shared */
    --series-2: #eb6834;  /* added / fresh */
    --series-3: #1baf7a;  /* removed */
    --good-ink: #006300;
    --bad-ink: #b3261e;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface-1: #1a1a19;
      --surface-2: #232322;
      --text-primary: #f0efec;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255, 255, 255, 0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --good-ink: #0ca30c;
      --bad-ink: #ff8a80;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --plane: #0d0d0d;
    --surface-1: #1a1a19;
    --surface-2: #232322;
    --text-primary: #f0efec;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --good-ink: #0ca30c;
    --bad-ink: #ff8a80;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--plane);
    color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .viz-root { min-height: 100vh; background: var(--plane); color: var(--text-primary); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 32px 28px 64px; }
  header.top { display: flex; align-items: flex-start; gap: 24px; flex-wrap: wrap; }
  .masthead { flex: 1 1 420px; }
  .eyebrow {
    font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--text-muted); margin: 0 0 6px;
  }
  h1 { font-size: 22px; line-height: 1.25; margin: 0 0 6px; font-weight: 600; }
  .pair { color: var(--text-secondary); font-size: 13px; margin: 0; }
  .pair code { font-size: 12px; }
  .actions { display: flex; gap: 8px; align-items: center; }
  button.ghost {
    font: inherit; font-size: 13px; color: var(--text-secondary);
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; cursor: pointer;
  }
  button.ghost:hover { background: var(--surface-2); }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
  }
  .hero-row {
    display: grid; grid-template-columns: minmax(260px, 1.15fr) 2.2fr;
    gap: 16px; margin: 24px 0 16px;
  }
  @media (max-width: 860px) { .hero-row { grid-template-columns: 1fr; } }
  .hero-value {
    font-size: 52px; line-height: 1.05; font-weight: 600;
    letter-spacing: -0.01em; margin: 2px 0 6px;
  }
  .hero-label { font-size: 13px; color: var(--text-secondary); margin: 0; }
  .hero-sub { font-size: 12.5px; color: var(--text-muted); margin: 10px 0 0; }
  .tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  @media (min-width: 700px) and (max-width: 860px) {
    .tiles { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px 16px;
  }
  .tile .label { font-size: 12px; color: var(--text-secondary); margin: 0 0 4px; }
  .tile .value {
    font-size: 24px; font-weight: 600; margin: 0; letter-spacing: -0.01em;
  }
  .tile .sub { font-size: 12px; color: var(--text-muted); margin: 3px 0 0; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
  .card h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
  .card .kicker { font-size: 12.5px; color: var(--text-muted); margin: 0 0 18px; }
  /* ---- segmented bar ---- */
  .segbar {
    display: flex; gap: 2px; height: 28px; width: 100%;
    background: transparent; margin: 4px 0 14px;
  }
  .seg {
    appearance: none; border: 0; padding: 0 8px; margin: 0;
    height: 28px; border-radius: 4px; cursor: default;
    display: flex; align-items: center; justify-content: center;
    font: inherit; font-size: 12px; font-weight: 600;
    white-space: nowrap; overflow: hidden;
    color: #fff; background: var(--series-1);
    transition: filter .12s ease;
  }
  .seg:hover, .seg:focus-visible { filter: brightness(1.08); outline: none; }
  .seg:focus-visible { box-shadow: 0 0 0 2px var(--surface-1), 0 0 0 4px var(--axis); }
  .seg .txt { pointer-events: none; }
  .seg[data-tone="dark"] { color: #0b0b0b; }
  .legend {
    display: flex; flex-wrap: wrap; gap: 8px 18px;
    margin: 0 0 6px; padding: 0; list-style: none;
  }
  .legend li { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-secondary); }
  .legend .key { width: 14px; height: 8px; border-radius: 2px; flex: none; }
  .legend .amount { color: var(--text-primary); font-weight: 600; }
  .legend .share { color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .empty {
    border: 1px dashed var(--axis); border-radius: 8px;
    padding: 18px 16px; color: var(--text-secondary); font-size: 13px;
    margin: 4px 0 14px;
  }
  .breakdown { margin: 14px 0 0; padding: 0; list-style: none; }
  .breakdown li {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 9px 0; border-top: 1px solid var(--grid); font-size: 13px;
  }
  .breakdown li:first-child { border-top: 0; }
  .breakdown .k { color: var(--text-secondary); }
  .breakdown .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .breakdown .note { color: var(--text-muted); font-weight: 400; font-size: 12px; }
  details.tableview { margin-top: 14px; }
  details.tableview > summary {
    cursor: pointer; font-size: 12.5px; color: var(--text-secondary);
    padding: 4px 0; list-style-position: inside;
  }
  details.tableview table { margin-top: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  th { font-weight: 600; color: var(--text-secondary); font-size: 11.5px; letter-spacing: .02em; text-transform: uppercase; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.mono, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11.5px; }
  .muted { color: var(--text-muted); }
  /* ---- filter row ---- */
  .filters {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    margin: 0 0 12px;
  }
  .filters label { font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
  select, input[type="search"] {
    font: inherit; font-size: 12.5px; color: var(--text-primary);
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 7px; padding: 6px 9px;
  }
  input[type="search"] { min-width: 220px; }
  .count { font-size: 12px; color: var(--text-muted); margin-left: auto; }
  /* ---- status badge: icon + label, never color alone ---- */
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11.5px; font-weight: 600; white-space: nowrap;
  }
  .badge .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .badge[data-status="shared"] .dot { background: var(--series-1); }
  .badge[data-status="added"] .dot { background: var(--series-2); }
  .badge[data-status="removed"] .dot { background: var(--series-3); }
  .badge .glyph { font-size: 11px; color: var(--text-muted); }
  .chain { color: var(--text-secondary); font-size: 11.5px; line-height: 1.45; }
  .chain .arrow { color: var(--text-muted); padding: 0 4px; }
  .foot { margin-top: 28px; color: var(--text-muted); font-size: 12px; }
  .foot code { font-size: 11.5px; }
  .tooltip {
    position: fixed; z-index: 40; pointer-events: none; opacity: 0;
    transform: translate(-50%, -100%);
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 6px 24px rgba(0,0,0,.16);
    padding: 9px 11px; min-width: 140px; font-size: 12.5px;
  }
  .tooltip .t-val { font-size: 16px; font-weight: 600; margin: 0 0 2px; }
  .tooltip .t-name { color: var(--text-secondary); display: flex; align-items: center; gap: 7px; }
  .tooltip .t-key { width: 12px; height: 2px; border-radius: 1px; flex: none; }
  .tooltip .t-share { color: var(--text-muted); font-size: 11.5px; margin-top: 3px; }
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
  <header class="top">
    <div class="masthead">
      <p class="eyebrow">__TOOL__ preview</p>
      <h1>Package difference &amp; content reuse</h1>
      <p class="pair" id="pair"></p>
    </div>
    <div class="actions">
      <button class="ghost" id="theme" type="button" aria-label="Toggle colour scheme">Theme</button>
    </div>
  </header>

  <section class="hero-row" aria-labelledby="reuse-heading">
    <div class="card">
      <p class="eyebrow" id="reuse-heading">Same-digest content reused</p>
      <p class="hero-value" id="hero-value">–</p>
      <p class="hero-label">of the combined content of A and B</p>
      <p class="hero-sub" id="hero-sub"></p>
    </div>
    <div class="tiles" id="tiles"></div>
  </section>

  <section class="grid2">
    <div class="card" id="diff-card">
      <h2>Content across both bundles</h2>
      <p class="kicker">A and B combined, split by whether each digest is shared, only in B, or only in A.</p>
      <div id="diff-chart"></div>
      <div id="diff-breakdown"></div>
      <details class="tableview">
        <summary>Table view</summary>
        <div id="diff-table"></div>
      </details>
    </div>
    <div class="card" id="reuse-card">
      <h2>How B was assembled</h2>
      <p class="kicker">Where the bytes stored in B came from when it was built.</p>
      <div id="reuse-chart"></div>
      <div id="reuse-breakdown"></div>
      <details class="tableview">
        <summary>Table view</summary>
        <div id="reuse-table"></div>
      </details>
    </div>
  </section>

  <section class="card" aria-labelledby="blobs-heading">
    <h2 id="blobs-heading">Blobs by digest</h2>
    <p class="kicker">Every content digest in A or B. Shared rows are byte-identical, so a delivery run can reuse them without copying.</p>
    <div class="filters" role="group" aria-label="Filter blobs">
      <label>Status
        <select id="f-status">
          <option value="all">All</option>
          <option value="added">Only in B</option>
          <option value="removed">Only in A</option>
          <option value="shared">Shared</option>
        </select>
      </label>
      <label>Kind
        <select id="f-kind"><option value="all">All</option></select>
      </label>
      <label><span class="sr-only">Search digest or path</span>
        <input type="search" id="f-search" placeholder="Filter digest or path">
      </label>
      <span class="count" id="row-count"></span>
    </div>
    <div id="blob-table"></div>
    <p class="muted" style="margin:12px 0 0;font-size:12px;">
      Sizes are stored bytes. A reference count above 1 means the same digest is required more than once; it is stored once.
    </p>
  </section>

  <p class="foot" id="foot"></p>
</div>
<div class="tooltip" id="tip" role="status" aria-live="polite">
  <p class="t-val" id="tip-val"></p>
  <p class="t-name"><span class="t-key" id="tip-key"></span><span id="tip-name"></span></p>
  <p class="t-share" id="tip-share"></p>
</div>
</div>
<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("payload").textContent);
  var root = document.querySelector(".viz-root");

  // ---- helpers -----------------------------------------------------------
  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value === null || value === undefined) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "style") node.setAttribute("style", value);
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function bytes(n) {
    n = Number(n) || 0;
    var neg = n < 0 ? "-" : "";
    n = Math.abs(n);
    if (n < 1024) return neg + n + " B";
    var units = ["KiB", "MiB", "GiB", "TiB", "PiB"], v = n, i = -1;
    do { v /= 1024; i += 1; } while (v >= 1024 && i < units.length - 1);
    var digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
    return neg + v.toFixed(digits) + " " + units[i];
  }
  function pct(part, whole) {
    if (!whole) return "0%";
    var value = (100 * part) / whole;
    if (value > 0 && value < 1) return "<1%";
    return (value >= 10 ? value.toFixed(0) : value.toFixed(1)) + "%";
  }
  function cssVar(name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }
  // Pick a text colour for a filled segment: white unless the fill is light.
  function inkFor(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex);
    if (!m) return "#ffffff";
    var value = parseInt(m[1], 16);
    var r = (value >> 16) & 255, g = (value >> 8) & 255, b = value & 255;
    var lin = [r, g, b].map(function (c) {
      c /= 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    var luminance = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
    return luminance > 0.36 ? "#0b0b0b" : "#ffffff";
  }
  function digestShort(digest) {
    if (!digest) return "";
    var hex = String(digest).split(":")[1] || String(digest);
    return "sha256:" + hex.slice(0, 12);
  }
  function chainNode(chain) {
    var wrap = el("div", { class: "chain" });
    var parts = chain && chain.length ? chain : ["<entry>"];
    parts.forEach(function (part, index) {
      if (index) wrap.appendChild(el("span", { class: "arrow", text: "→" }));
      wrap.appendChild(el("span", { text: part }));
    });
    return wrap;
  }

  // ---- tooltip -----------------------------------------------------------
  var tip = document.getElementById("tip");
  var tipVal = document.getElementById("tip-val");
  var tipName = document.getElementById("tip-name");
  var tipKey = document.getElementById("tip-key");
  var tipShare = document.getElementById("tip-share");
  function showTip(event, item) {
    tipVal.textContent = bytes(item.bytes);
    tipName.textContent = item.name;
    tipKey.style.background = item.color;
    tipShare.textContent = item.share;
    tip.style.opacity = "1";
    var x, y;
    if (event && event.clientX) { x = event.clientX; y = event.clientY - 14; }
    else {
      var rect = event.target.getBoundingClientRect();
      x = rect.left + rect.width / 2; y = rect.top - 8;
    }
    x = Math.max(120, Math.min(window.innerWidth - 120, x));
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.style.opacity = "0"; }

  // ---- segmented bar -----------------------------------------------------
  function segmentedBar(container, segments, whole) {
    clear(container);
    var bar = el("div", { class: "segbar", role: "group" });
    segments.forEach(function (segment) {
      if (segment.bytes <= 0) return;
      var share = pct(segment.bytes, whole);
      var label = segment.name + ": " + bytes(segment.bytes) + ", " + share + " of total";
      var button = el("button", {
        type: "button",
        class: "seg",
        "aria-label": label,
        title: label,
        style: "flex: 0 0 " + ((100 * segment.bytes) / whole) + "%; background: " + segment.color +
               "; color: " + inkFor(segment.color)
      }, [el("span", { class: "txt", text: "" })]);
      var item = { bytes: segment.bytes, name: segment.name, color: segment.color, share: share + " of total" };
      button.addEventListener("pointermove", function (event) { showTip(event, item); });
      button.addEventListener("pointerleave", hideTip);
      button.addEventListener("focus", function (event) { showTip(event, item); });
      button.addEventListener("blur", hideTip);
      bar.appendChild(button);
    });
    container.appendChild(bar);
    // Fill in labels only where they fit without clipping.
    requestAnimationFrame(function () {
      var nodes = bar.querySelectorAll(".seg");
      for (var i = 0; i < nodes.length; i += 1) {
        var node = nodes[i];
        var rect = node.getBoundingClientRect();
        var text = segments.filter(function (s) { return s.bytes > 0; })[i];
        if (!text) continue;
        var span = node.querySelector(".txt");
        span.textContent = pct(text.bytes, whole);
        if (span.scrollWidth + 24 > rect.width) span.textContent = "";
      }
    });
  }

  function legend(container, segments, whole, extra) {
    var list = el("ul", { class: "legend" });
    segments.forEach(function (segment) {
      if (segment.bytes <= 0 && !segment.alwaysShow) return;
      list.appendChild(el("li", null, [
        el("span", { class: "key", style: "background: " + segment.color }),
        el("span", { text: segment.name }),
        el("span", { class: "amount", text: bytes(segment.bytes) }),
        el("span", { class: "share", text: pct(segment.bytes, whole) })
      ]));
    });
    container.appendChild(list);
    if (extra) container.appendChild(el("p", { class: "muted", style: "margin:0 0 14px;font-size:12px;", text: extra }));
  }

  function tableFrom(rows, columns) {
    var table = el("table");
    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (column) {
      headRow.appendChild(el("th", { class: column.num ? "num" : null, scope: "col", text: column.label }));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr");
      columns.forEach(function (column) {
        var cell = el("td", { class: (column.num ? "num " : "") + (column.mono ? "mono" : "") });
        var value = row[column.key];
        if (value && typeof value === "object" && value.nodeType) cell.appendChild(value);
        else cell.textContent = value === undefined || value === null ? "" : String(value);
        tr.appendChild(cell);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  // ---- header ------------------------------------------------------------
  var a = DATA.a, b = DATA.b, summary = DATA.summary;
  document.getElementById("pair").appendChild(el("span", null, [
    el("strong", { text: "A" }), " " + (a.entry || "?") + " @ " + (a.target || "?") + "  ",
    el("code", { class: "muted", text: digestShort(a.digest) }), "   ·   ",
    el("strong", { text: "B" }), " " + (b.entry || "?") + " @ " + (b.target || "?") + "  ",
    el("code", { class: "muted", text: digestShort(b.digest) })
  ]));

  var ratio = (100 * summary.reuseRatio);
  document.getElementById("hero-value").textContent =
    (ratio > 0 && ratio < 1 ? "<1" : ratio.toFixed(1)) + "%";
  document.getElementById("hero-sub").textContent =
    bytes(summary.sharedBytes) + " of " + bytes(summary.unionBytes) +
    " identical by digest (" + summary.sharedBlobs + " of " +
    (summary.sharedBlobs + summary.addedBlobs + summary.removedBlobs) + " blobs)";

  // ---- tiles -------------------------------------------------------------
  var sources = b.sources || {};
  function srcBytes(name) { return (sources[name] || {}).bytes || 0; }
  function srcCount(name) { return (sources[name] || {}).count || 0; }
  var buildReuse = srcBytes("cache") + srcBytes("checkpoint") + srcBytes("existing");
  var tiles = [
    { label: "Stored in B", value: bytes(b.bytes), sub: (b.blobs || 0) + " blobs" },
    { label: "New in B", value: bytes(summary.addedBytes), sub: summary.addedBlobs + " blobs, not in A" },
    { label: "Only in A", value: bytes(summary.removedBytes), sub: summary.removedBlobs + " blobs, gone from B" },
    {
      label: "Reused while building B",
      value: bytes(buildReuse),
      sub: srcCount("cache") + " from cache · " + srcCount("checkpoint") + " from resume"
    }
  ];
  var tileHost = document.getElementById("tiles");
  tiles.forEach(function (tile) {
    tileHost.appendChild(el("div", { class: "tile" }, [
      el("p", { class: "label", text: tile.label }),
      el("p", { class: "value", text: tile.value }),
      el("p", { class: "sub", text: tile.sub })
    ]));
  });

  // ---- diff card ---------------------------------------------------------
  var diffSegments = [
    { name: "Shared", bytes: summary.sharedBytes, color: cssVar("--series-1") },
    { name: "Only in B", bytes: summary.addedBytes, color: cssVar("--series-2") },
    { name: "Only in A", bytes: summary.removedBytes, color: cssVar("--series-3") }
  ];
  var diffHost = document.getElementById("diff-chart");
  segmentedBar(diffHost, diffSegments, summary.unionBytes || 1);
  var diffLegend = el("div");
  diffHost.appendChild(diffLegend);
  legend(diffLegend, diffSegments, summary.unionBytes || 1, null);

  var diffBreakdown = document.getElementById("diff-breakdown");
  diffBreakdown.appendChild(el("ul", { class: "breakdown" }, [
    el("li", null, [
      el("span", { class: "k", text: "Reusable without copying" }),
      el("span", { class: "v", text: bytes(summary.sharedBytes) })
    ]),
    el("li", null, [
      el("span", { class: "k", text: "Must transfer for B" }),
      el("span", { class: "v", text: bytes(summary.addedBytes) })
    ]),
    el("li", null, [
      el("span", { class: "k", text: "Dropped from A" }),
      el("span", { class: "v", text: bytes(summary.removedBytes) })
    ])
  ]));
  var notes = DATA.notes || [];
  if (notes.length) {
    var noteList = el("ul", { class: "breakdown" });
    notes.forEach(function (note) {
      noteList.appendChild(el("li", null, [
        el("span", { class: "k", text: "Note" }), el("span", { class: "v note", text: note })
      ]));
    });
    diffBreakdown.appendChild(noteList);
  }

  var diffTableRows = diffSegments.map(function (segment) {
    return { name: segment.name, blobs: segment.name === "Shared" ? summary.sharedBlobs :
      segment.name === "Only in B" ? summary.addedBlobs : summary.removedBlobs,
      bytes: bytes(segment.bytes), share: pct(segment.bytes, summary.unionBytes || 1) };
  });
  document.getElementById("diff-table").appendChild(tableFrom(diffTableRows, [
    { key: "name", label: "Set" }, { key: "blobs", label: "Blobs", num: true },
    { key: "bytes", label: "Bytes", num: true }, { key: "share", label: "Share of union", num: true }
  ]));

  // ---- reuse card --------------------------------------------------------
  var reuseHost = document.getElementById("reuse-chart");
  var reuseBreakdown = document.getElementById("reuse-breakdown");
  var freshBytes = srcBytes("copy");
  var storedB = (b.bytes || 0);
  if (buildReuse <= 0) {
    reuseHost.appendChild(el("div", { class: "empty", text:
      "B was assembled entirely from freshly copied content: no cache or resumed content was available." }));
  } else {
    var reuseSegments = [
      { name: "Freshly copied", bytes: freshBytes, color: cssVar("--series-2") },
      { name: "Reused (cache + resume)", bytes: buildReuse, color: cssVar("--series-1") }
    ];
    var reuseWhole = freshBytes + buildReuse || storedB || 1;
    segmentedBar(reuseHost, reuseSegments, reuseWhole);
    var reuseLegend = el("div");
    reuseHost.appendChild(reuseLegend);
    legend(reuseLegend, reuseSegments, reuseWhole, null);
  }
  var dedupeBytes = (b.summary && b.summary.dedupedBytes) || 0;
  reuseBreakdown.appendChild(el("ul", { class: "breakdown" }, [
    el("li", null, [
      el("span", { class: "k", text: "Seeded from another bundle" }),
      el("span", { class: "v", text: bytes(srcBytes("cache")) })
    ]),
    el("li", null, [
      el("span", { class: "k", text: "Resumed from checkpoint" }),
      el("span", { class: "v", text: bytes(srcBytes("checkpoint")) })
    ]),
    el("li", null, [
      el("span", { class: "k" }, [
        el("span", { text: "Repeated references" }),
        el("br"),
        el("span", { class: "note", text: "same digest required twice, stored once" })
      ]),
      el("span", { class: "v", text: bytes(dedupeBytes) })
    ])
  ]));

  var reuseTableRows = ["copy", "cache", "checkpoint", "existing"].map(function (name) {
    var labels = { copy: "Freshly copied", cache: "Seeded from another bundle",
                   checkpoint: "Resumed from checkpoint", existing: "Already in bundle" };
    return { name: labels[name], blobs: srcCount(name), bytes: bytes(srcBytes(name)) };
  });
  document.getElementById("reuse-table").appendChild(tableFrom(reuseTableRows, [
    { key: "name", label: "Source" }, { key: "blobs", label: "Blobs", num: true },
    { key: "bytes", label: "Bytes", num: true }
  ]));

  // ---- blob table --------------------------------------------------------
  var columns = [
    { key: "status", label: "Status" },
    { key: "digest", label: "Digest", mono: true },
    { key: "kind", label: "Kind" },
    { key: "size", label: "Bytes", num: true },
    { key: "shared", label: "Reuse", num: true },
    { key: "chain", label: "Dependency path" }
  ];
  var statusLabels = { shared: "Shared", added: "Only in B", removed: "Only in A" };
  var statusGlyphs = { shared: "≡", added: "+", removed: "−" };
  var kindSelect = document.getElementById("f-kind");
  var kinds = [];
  DATA.blobs.forEach(function (blob) {
    if (blob.kind && kinds.indexOf(blob.kind) === -1) kinds.push(blob.kind);
  });
  kinds.sort();
  kinds.forEach(function (kind) {
    kindSelect.appendChild(el("option", { value: kind, text: kind }));
  });
  var state = { status: "all", kind: "all", q: "" };
  var blobHost = document.getElementById("blob-table");
  var rowCount = document.getElementById("row-count");

  function badge(status) {
    return el("span", { class: "badge", "data-status": status }, [
      el("span", { class: "dot" }),
      el("span", { class: "glyph", text: statusGlyphs[status] || "" }),
      el("span", { text: statusLabels[status] || status })
    ]);
  }

  function renderBlobs() {
    var query = state.q.toLowerCase();
    var rows = DATA.blobs.filter(function (blob) {
      if (state.status !== "all" && blob.status !== state.status) return false;
      if (state.kind !== "all" && blob.kind !== state.kind) return false;
      if (!query) return true;
      var hay = [blob.digest, (blob.chainA || []).join(" "), (blob.chainB || []).join(" ")].join(" ").toLowerCase();
      return hay.indexOf(query) !== -1;
    }).sort(function (x, y) { return (y.size || 0) - (x.size || 0); });

    var table = el("table");
    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (column) {
      headRow.appendChild(el("th", { class: column.num ? "num" : null, scope: "col", text: column.label }));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (blob) {
      var chain = blob.status === "removed" ? (blob.chainA || []) :
        (blob.chainB && blob.chainB.length ? blob.chainB : (blob.chainA || []));
      var reuse = blob.status === "shared"
        ? (blob.referenceDelta ? "refs " + (blob.referenceDelta > 0 ? "+" : "") + blob.referenceDelta : "identical")
        : "";
      var tr = el("tr", null, [
        el("td", null, [badge(blob.status)]),
        el("td", { class: "mono" }, [el("span", { text: digestShort(blob.digest) })]),
        el("td", { text: blob.kind || "" }),
        el("td", { class: "num", text: bytes(blob.size) }),
        el("td", { class: "num muted", text: reuse }),
        el("td", null, [chainNode(chain)])
      ]);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    clear(blobHost);
    if (!rows.length) {
      blobHost.appendChild(el("div", { class: "empty", text: "No blobs match the current filters." }));
    } else {
      blobHost.appendChild(table);
    }
    rowCount.textContent = rows.length + " of " + DATA.blobs.length + " blobs";
  }
  document.getElementById("f-status").addEventListener("change", function (event) {
    state.status = event.target.value; renderBlobs();
  });
  kindSelect.addEventListener("change", function (event) {
    state.kind = event.target.value; renderBlobs();
  });
  document.getElementById("f-search").addEventListener("input", function (event) {
    state.q = event.target.value; renderBlobs();
  });
  renderBlobs();

  // ---- chrome ------------------------------------------------------------
  document.getElementById("foot").appendChild(el("span", null, [
    "Generated by " + __TOOL_JSON__ + " " + __VERSION_JSON__ + " · diff digest ",
    el("code", { text: DATA.digest || "" }),
    " · bundle A " + digestShort(a.digest) + " · bundle B " + digestShort(b.digest)
  ]));

  var themeButton = document.getElementById("theme");
  function currentTheme() {
    var attr = document.documentElement.getAttribute("data-theme");
    if (attr && attr !== "auto") return attr;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    themeButton.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  }
  try {
    var saved = localStorage.getItem("offlinepack-theme");
    if (saved === "dark" || saved === "light") applyTheme(saved);
    else applyTheme(currentTheme());
  } catch (err) { applyTheme(currentTheme()); }
  themeButton.addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("offlinepack-theme", next); } catch (err) {}
    // Segments carry resolved colours; repaint so both modes are chosen steps.
    document.querySelectorAll(".seg").forEach(function (node) { node.style.background = ""; });
    repaintCharts();
  });

  function repaintCharts() {
    var s = {
      shared: cssVar("--series-1"), added: cssVar("--series-2"), removed: cssVar("--series-3"),
      fresh: cssVar("--series-2"), reused: cssVar("--series-1")
    };
    var segs = document.querySelectorAll("#diff-chart .seg");
    var diffColors = [s.shared, s.added, s.removed];
    for (var i = 0; i < segs.length; i += 1) {
      segs[i].style.background = diffColors[i];
      segs[i].style.color = inkFor(diffColors[i]);
    }
    var reuseSegs = document.querySelectorAll("#reuse-chart .seg");
    var reuseColors = [s.fresh, s.reused];
    for (var j = 0; j < reuseSegs.length; j += 1) {
      reuseSegs[j].style.background = reuseColors[j];
      reuseSegs[j].style.color = inkFor(reuseColors[j]);
    }
    var keys = document.querySelectorAll("#diff-chart .legend .key");
    for (var k = 0; k < keys.length; k += 1) keys[k].style.background = diffColors[k];
    var reuseKeys = document.querySelectorAll("#reuse-chart .legend .key");
    for (var m = 0; m < reuseKeys.length; m += 1) reuseKeys[m].style.background = reuseColors[m];
  }
})();
</script>
</body>
</html>
"""


def render_preview(diff: BundleDiff, *, out: Path | str | None = None) -> str:
    """Render the diff into a self-contained HTML page."""
    doc = diff.to_dict()
    import json

    payload = json.dumps(doc, ensure_ascii=False)
    # Keep untrusted data from closing the script element.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = _escape(
        f"{doc['a'].get('entry') or 'bundle A'} vs {doc['b'].get('entry') or 'bundle B'}"
    )
    html = (
        _PAGE.replace("__TITLE__", title)
        .replace("__TOOL__", f"{TOOL_NAME} {TOOL_VERSION}")
        .replace("__DATA__", payload)
        .replace("__TOOL_JSON__", json.dumps(TOOL_NAME))
        .replace("__VERSION_JSON__", json.dumps(TOOL_VERSION))
    )
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(html, encoding="utf-8")
        except OSError as exc:
            raise BundleError(f"cannot write preview to {path}: {exc}") from exc
    return html


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
