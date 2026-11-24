"""Pages 结果页发布脚本。

从 output/ 收拢结果文件到 pages-site/ 目录(路径拍平改名),并生成单文件
静态结果页 index.html(复制链接 / 在线预览 / 下载),供 Actions 经
upload-pages-artifact + deploy-pages 发布到 GitHub Pages。

用法(Actions 内):
    python scripts/publish_results.py \
        --pages-destination pages-site \
        --pages-base-url "https://<owner>.github.io/<repo>/"

产物直链即订阅地址,例如:
    <base>/ipv4.txt  <base>/ipv4.m3u  <base>/ipv6.txt  <base>/ipv6.m3u
    <base>/epg.xml   <base>/epg.gz
"""
import argparse
import html
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

# output/ 源文件 -> pages-site/ 平铺文件名(缺失则跳过)
OPTIONAL_ASSETS = {
    "ipv4/result.txt": "ipv4.txt",
    "ipv4/result.m3u": "ipv4.m3u",
    "ipv6/result.txt": "ipv6.txt",
    "ipv6/result.m3u": "ipv6.m3u",
    "epg/epg.xml": "epg.xml",
    "epg/epg.gz": "epg.gz",
}

# 可在线预览的文本类资产(.gz 二进制不做预览)
PREVIEWABLE = {"ipv4.txt", "ipv4.m3u", "ipv6.txt", "ipv6.m3u", "epg.xml"}

# 静态依赖：随页面一起发布到 GitHub Pages,不计入结果资产统计
VENDOR_FILES = {
    "assets/hls.min.js": "hls.min.js",
}

# 优先本地 hls.js,缺失时再回落到 CDN,避免外网抖动导致无法试播
HLS_LOADER = """<script src="hls.min.js"></script>
<script>
(function () {
  if (window.Hls) return;
  var s = document.createElement("script");
  s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js";
  s.async = false;
  s.onload = function () {
    try {
      if (typeof PREVIEW !== "undefined" && PREVIEW.channels.length && PREVIEW.index >= 0) {
        playChannel(PREVIEW.index);
      }
    } catch (err) {}
  };
  document.head.appendChild(s);
})();
</script>
"""

PAGE_ASSET_GROUPS = (
    {
        "id": "ipv4-results",
        "eyebrow": "IPV4",
        "title": "IPv4 结果",
        "description": "仅包含 IPv4 地址,适用于未启用 IPv6 或 IPv6 连接不稳定的网络。",
        "assets": (("ipv4.m3u", "M3U", "IPv4 播放列表"), ("ipv4.txt", "TXT", "IPv4 文本列表")),
    },
    {
        "id": "ipv6-results",
        "eyebrow": "IPV6",
        "title": "IPv6 结果",
        "description": "仅包含 IPv6 地址,请在当前网络和播放设备支持 IPv6 时使用。",
        "assets": (("ipv6.m3u", "M3U", "IPv6 播放列表"), ("ipv6.txt", "TXT", "IPv6 文本列表")),
    },
    {
        "id": "epg-results",
        "eyebrow": "EPG",
        "title": "EPG 节目单",
        "description": "为支持 EPG 的播放器提供节目单数据,GZIP 格式体积更小。",
        "assets": (("epg.gz", "GZIP", "EPG 压缩文件"), ("epg.xml", "XML", "EPG XML 文件")),
    },
)

# 卡片按钮内联 SVG 图标(stroke 跟随 currentColor)
SVG_COPY = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>'
SVG_EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="3"/></svg>'
SVG_DOWNLOAD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>'


# ---------- 在线预览弹窗(播放器 + 换台) ----------
PREVIEW_CSS = r"""/* ---------- 预览弹窗 ---------- */
.preview-modal {
  position: fixed; inset: 0; z-index: 1200;
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
  background: rgba(14, 16, 38, 0.58);
  backdrop-filter: blur(6px);
}
.preview-modal[hidden] { display: none; }
.preview-dialog {
  display: flex; flex-direction: column;
  width: min(1100px, 100%); max-height: min(880px, calc(100vh - 32px));
  border: 1px solid var(--border); border-radius: var(--radius-md);
  background: var(--surface-strong);
  box-shadow: 0 24px 64px rgba(30, 26, 110, 0.34);
  overflow: hidden;
  animation: previewIn 180ms ease-out;
}
@keyframes previewIn {
  from { opacity: 0; transform: translateY(10px) scale(0.985); }
}
.preview-head {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  background: var(--surface-strong);
}
.preview-titles { min-width: 0; }
.preview-titles h3 { margin: 0; font-size: 16px; font-weight: 750; letter-spacing: -0.02em; }
.preview-sub { margin: 2px 0 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.preview-head-actions { display: flex; flex: none; align-items: center; gap: 8px; }
.tab-btn {
  min-height: 36px; padding: 6px 14px;
  border: 1px solid var(--border); border-radius: 9px;
  background: var(--page); color: var(--muted);
  font: 700 13px/1.2 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  cursor: pointer;
  transition: background-color 150ms ease, color 150ms ease, border-color 150ms ease;
}
.tab-btn:hover { border-color: var(--primary); color: var(--primary-strong); }
.tab-btn.is-active { border-color: var(--primary); background: var(--primary-soft); color: var(--primary-strong); }
.preview-close {
  width: 36px; height: 36px; padding: 0;
  border: 1px solid var(--border); border-radius: 9px;
  background: var(--page); color: var(--text); cursor: pointer; font-size: 15px; line-height: 1;
}
.preview-close:hover { border-color: var(--primary); color: var(--primary-strong); }
.preview-body {
  display: grid;
  grid-template-columns: 268px minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  flex: 1; min-height: 0; overflow: hidden;
}
.preview-side {
  grid-column: 1; grid-row: 1;
  display: flex; flex-direction: column; min-height: 0;
  border-right: 1px solid var(--border); background: var(--page);
}
.preview-side[hidden], .preview-stage[hidden], .preview-text[hidden] { display: none; }
.chan-search { padding: 10px 12px; border-bottom: 1px solid var(--border); }
.chan-search input {
  width: 100%; padding: 9px 11px;
  border: 1px solid var(--border); border-radius: 9px;
  background: var(--surface-strong); color: var(--text); font: 13px/1.3 inherit;
}
.chan-search input:focus { outline: none; border-color: var(--primary); }
.chan-list { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 8px 14px; }
.chan-group {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  margin: 12px 4px 6px; color: var(--muted); font-size: 12px; font-weight: 700;
}
.chan-item {
  display: block; width: 100%; margin: 2px 0; padding: 7px 10px;
  border: 1px solid transparent; border-radius: 9px;
  background: transparent; color: var(--text); text-align: left; cursor: pointer;
  transition: background-color 140ms ease, border-color 140ms ease;
}
.chan-item:hover { background: var(--primary-soft); }
.chan-item.is-active { border-color: var(--primary); background: var(--primary-soft); }
.chan-item.is-multicast .chan-name { color: var(--muted); }
.chan-tag {
  display: inline-block; margin-left: 6px; padding: 0 5px;
  border-radius: 5px; background: var(--border); color: var(--muted);
  font-size: 10px; font-weight: 700; vertical-align: 1px;
}
.chan-name { display: block; font: 700 13px/1.35 inherit; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chan-url {
  display: block; color: var(--muted);
  font: 11px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.chan-empty { margin: 18px 6px; color: var(--muted); font-size: 13px; }
.preview-stage {
  grid-column: 2; grid-row: 1;
  display: flex; flex-direction: column; gap: 10px;
  min-height: 0; padding: 14px 16px 16px; overflow-y: auto;
}
.video-wrap { position: relative; border-radius: 12px; overflow: hidden; background: #0b0e1c; aspect-ratio: 16 / 9; }
.video-wrap video { display: block; width: 100%; height: 100%; background: #0b0e1c; }
.stage-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.chan-switch {
  min-height: 38px; padding: 7px 14px;
  border: 1px solid var(--border); border-radius: 9px;
  background: var(--primary-soft); color: var(--primary-strong);
  font: 700 13px/1.2 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  cursor: pointer; transition: border-color 150ms ease, background-color 150ms ease;
}
.chan-switch:hover { border-color: var(--primary); background: #e0e3ff; }
.now-playing { flex: 1 1 200px; min-width: 0; }
.now-name { display: block; font: 750 14px/1.3 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.now-name.is-error { color: #dc2626; }
.now-url {
  display: block; color: var(--muted);
  font: 11px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-wrap: anywhere;
}
.stage-hint { margin: 0; color: var(--muted); font-size: 12px; }
.line-bar { gap: 6px; }
.line-bar[hidden] { display: none; }
.line-label { color: var(--muted); font-size: 12px; font-weight: 700; }
.line-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.line-chip {
  padding: 6px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface-strong); color: var(--text);
  font: 700 12px/1.2 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  cursor: pointer; transition: border-color 150ms ease, background-color 150ms ease;
}
.line-chip:hover { border-color: var(--primary); background: var(--primary-soft); }
.line-chip.is-active { border-color: var(--primary); background: var(--primary); color: #fff; }
.preview-text {
  grid-column: 1 / -1; grid-row: 1;
  display: flex; flex-direction: column; min-height: 0;
  padding: 14px 16px; overflow: hidden;
}
.preview-text pre {
  flex: 1; min-height: 0; margin: 0; padding: 12px; overflow: auto;
  border: 1px solid var(--border); border-radius: 10px; background: var(--page);
  color: var(--text);
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap; word-break: break-all;
}

@media (max-width: 760px) {
  .preview-modal { padding: 0; }
  .preview-dialog { width: 100%; height: 100%; max-height: none; border: none; border-radius: 0; }
  .preview-head { padding: 10px 12px; }
  .preview-head-actions { gap: 6px; }
  .tab-btn { min-height: 32px; padding: 5px 10px; font-size: 12px; }
  .preview-body { grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) 168px; }
  .preview-stage { grid-column: 1; grid-row: 1; }
  .preview-side { grid-column: 1; grid-row: 2; border-right: none; border-top: 1px solid var(--border); }
  .preview-text { grid-column: 1; grid-row: 1; }
  .video-wrap { aspect-ratio: auto; height: 40vh; }
}
"""

PREVIEW_HTML = r"""<div class="preview-modal" id="preview-modal" hidden>
  <div class="preview-dialog" role="dialog" aria-modal="true" aria-labelledby="preview-title">
    <header class="preview-head">
      <div class="preview-titles">
        <h3 id="preview-title">预览</h3>
        <p class="preview-sub" id="preview-sub"></p>
      </div>
      <div class="preview-head-actions">
        <button class="tab-btn is-active" id="tab-play" onclick="switchPreviewTab('play')">播放</button>
        <button class="tab-btn" id="tab-text" onclick="switchPreviewTab('text')">原文</button>
        <button class="preview-close" onclick="closePreview()" aria-label="关闭预览">✕</button>
      </div>
    </header>
    <div class="preview-body">
      <aside class="preview-side" id="preview-side">
        <div class="chan-search"><input id="chan-search" type="search" placeholder="搜索频道 / 地址" autocomplete="off"></div>
        <div class="chan-list" id="chan-list"></div>
      </aside>
      <main class="preview-stage" id="preview-stage">
        <div class="video-wrap"><video id="preview-video" controls playsinline preload="none"></video></div>
        <div class="stage-bar">
          <button class="chan-switch" onclick="stepChannel(-1)">◀ 上一台</button>
          <div class="now-playing">
            <span class="now-name" id="now-name">未选择频道</span>
            <span class="now-url" id="now-url"></span>
          </div>
          <button class="chan-switch" onclick="stepChannel(1)">下一台 ▶</button>
          <button class="chan-switch" onclick="copyCurrentChannel(this)">复制地址</button>
        </div>
        <div class="stage-bar line-bar" id="line-bar" hidden>
          <span class="line-label">线路</span>
          <div class="line-chips" id="line-chips"></div>
        </div>
        <p class="stage-hint" id="stage-hint">← → 换台 · Esc 关闭 · 多线路频道点“线路”逐个试播</p>
      </main>
      <section class="preview-text" id="preview-text-panel" hidden><pre id="preview-text"></pre></section>
    </div>
  </div>
</div>
"""

PREVIEW_SCRIPT = r"""/* ---------- 预览弹窗：解析频道、试播、换台 ---------- */
const PREVIEW = { name: "", raw: "", channels: [], index: -1, line: 0, hls: null, tab: "play" };
const EL = (id) => document.getElementById(id);

async function previewAsset(name) {
  if (!PREVIEWABLE.includes(name)) return;
  const textOnly = name.endsWith(".xml");
  PREVIEW.name = name;
  PREVIEW.raw = "";
  PREVIEW.channels = [];
  PREVIEW.index = -1;
  stopPlayback();
  EL("preview-title").textContent = "预览 · " + name;
  EL("preview-sub").textContent = "加载中…";
  EL("chan-list").innerHTML = "";
  EL("preview-text").textContent = "";
  EL("now-name").textContent = textOnly ? "文本预览" : "未选择频道";
  EL("now-name").classList.remove("is-error");
  EL("now-url").textContent = "";
  PREVIEW.line = 0;
  EL("line-bar").hidden = true;
  EL("line-chips").innerHTML = "";
  EL("chan-search").value = "";
  EL("stage-hint").textContent = textOnly ? "" : "← → 换台 · Esc 关闭 · 多线路频道点“线路”逐个试播";
  EL("tab-play").hidden = textOnly;
  EL("preview-modal").hidden = false;
  document.body.style.overflow = "hidden";
  switchPreviewTab(textOnly ? "text" : "play");
  try {
    const res = await fetch(name, { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const text = await res.text();
    PREVIEW.raw = text;
    const channels = name.endsWith(".m3u") ? parseM3U(text) : name.endsWith(".txt") ? parseTXT(text) : [];
    PREVIEW.channels = channels;
    const groupCount = new Set(channels.map((c) => c.group)).size;
    const lineCount = channels.reduce((n, c) => n + c.lines.length, 0);
    const hlsCount = channels.reduce((n, c) => n + c.lines.filter((l) => l.kind === "hls").length, 0);
    EL("preview-sub").textContent = textOnly
      ? name + " · " + (text.length / 1024).toFixed(0) + " KB"
      : name + " · " + channels.length + " 个频道 · " + lineCount + " 条线路 · " + groupCount + " 个分组 · " +
        hlsCount + " 条 HLS 可试播（多线路可逐条切换）";
    renderChannelList("");
    renderText(text);
    if (channels.length) playChannel(0);
  } catch (err) {
    EL("preview-sub").textContent = name + " · 加载失败";
    renderText(
      "预览失败：" + err.message +
      "\n\n如果是本地打开 index.html，浏览器会拦截 file:// 下的 fetch，请用本地服务器（例如 python -m http.server）访问页面。"
    );
    switchPreviewTab("text");
  }
}

function parseM3U(text) {
  const entries = [];
  let info = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("#EXTINF")) {
      const attrs = {};
      const re = /([\w-]+)="([^"]*)"/g;
      let m;
      while ((m = re.exec(line)) !== null) attrs[m[1]] = m[2];
      const comma = line.lastIndexOf(",");
      const title = comma > -1 ? line.slice(comma + 1).trim() : "";
      info = {
        name: title || attrs["tvg-name"] || "未命名频道",
        group: (attrs["group-title"] || "未分组").trim() || "未分组",
        logo: attrs["tvg-logo"] || "",
      };
      continue;
    }
    if (line.startsWith("#")) continue;
    if (!info) continue;
    if (!/^(http|https|rtmp|rtsp|rtp|udp|igmp)/i.test(line)) continue;
    entries.push({ name: info.name, group: info.group, logo: info.logo, url: line });
    info = null;
  }
  return dedupeChannels(entries);
}

function parseTXT(text) {
  const entries = [];
  let group = "未分组";
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.endsWith("#genre#")) {
      group = line.slice(0, -7).replace(/,$/, "").trim() || "未分组";
      continue;
    }
    const comma = line.indexOf(",");
    if (comma < 1) continue;
    const name = line.slice(0, comma).trim();
    const url = line.slice(comma + 1).trim();
    if (!/^(http|https|rtmp|rtsp|rtp|udp|igmp)/i.test(url)) continue;
    entries.push({ name: name, group: group, logo: "", url: url });
  }
  return dedupeChannels(entries);
}

function urlKind(url) {
  const u = String(url).toLowerCase();
  if (/\.m3u8(\?|$)/.test(u)) return "hls";
  if (/\.mp4(\?|$)/.test(u)) return "mp4";
  if (/\/rtp\/|\/udp\/|\/rtsp\/|\/igmp\//.test(u) || /^(rtp|udp|rtsp|igmp):/.test(u)) return "multicast";
  if (/\.ts(\?|$)/.test(u)) return "ts";
  return "other";
}

const KIND_RANK = { hls: 0, mp4: 1, ts: 2, other: 3, multicast: 4 };

function dedupeChannels(entries) {
  const map = new Map();
  for (const e of entries) {
    const key = e.group + "\u0001" + e.name;
    const kind = urlKind(e.url);
    let ch = map.get(key);
    if (!ch) {
      ch = { name: e.name, group: e.group, logo: e.logo, lines: [], kind: kind };
      map.set(key, ch);
    }
    if (!ch.lines.some((l) => l.url === e.url)) ch.lines.push({ url: e.url, kind: kind });
  }
  const list = Array.from(map.values());
  list.forEach((ch) => {
    ch.lines.sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind]);
    ch.url = ch.lines[0].url;
    ch.kind = ch.lines[0].kind;
  });
  return list;
}

function renderChannelList(keyword) {
  const kw = String(keyword || "").trim().toLowerCase();
  const list = EL("chan-list");
  const groups = new Map();
  PREVIEW.channels.forEach((c, i) => {
    if (kw && !(c.name.toLowerCase().indexOf(kw) > -1 ||
      c.lines.some((l) => l.url.toLowerCase().indexOf(kw) > -1))) return;
    if (!groups.has(c.group)) groups.set(c.group, []);
    groups.get(c.group).push({ c: c, i: i });
  });
  if (!groups.size) {
    list.innerHTML = '<p class="chan-empty">没有匹配的频道</p>';
    return;
  }
  const parts = [];
  groups.forEach((arr, group) => {
    parts.push('<p class="chan-group"><span>' + escapeHtml(group) + "</span><span>" + arr.length + "</span></p>");
    arr.forEach((item) => {
      const cls = "chan-item" + (item.i === PREVIEW.index ? " is-active" : "") +
        (item.c.kind === "multicast" ? " is-multicast" : "");
      parts.push(
        '<button class="' + cls + '" data-index="' + item.i + '" title="' + escapeAttr(item.c.url) + '">' +
        '<span class="chan-name">' + escapeHtml(item.c.name) +
        (item.c.kind === "multicast" ? '<span class="chan-tag">组播</span>' : "") +
        (item.c.lines.length > 1 ? '<span class="chan-tag">' + item.c.lines.length + " 线路</span>" : "") + "</span>" +
        '<span class="chan-url">' + escapeHtml(shortUrl(item.c.url)) + "</span></button>"
      );
    });
  });
  list.innerHTML = parts.join("");
  Array.prototype.forEach.call(list.querySelectorAll(".chan-item"), (btn) => {
    btn.addEventListener("click", () => playChannel(Number(btn.getAttribute("data-index"))));
  });
}

function highlightChannel(index) {
  Array.prototype.forEach.call(EL("chan-list").querySelectorAll(".chan-item"), (btn) => {
    const active = Number(btn.getAttribute("data-index")) === index;
    btn.classList.toggle("is-active", active);
    if (active) btn.scrollIntoView({ block: "nearest" });
  });
}

const KIND_LABEL = { hls: "HLS", mp4: "MP4", ts: "TS", other: "直链", multicast: "组播" };

function renderLineChips(ch, active) {
  const bar = EL("line-bar");
  const box = EL("line-chips");
  if (!ch || ch.lines.length < 2) {
    bar.hidden = true;
    box.innerHTML = "";
    return;
  }
  bar.hidden = false;
  box.innerHTML = ch.lines.map((l, k) =>
    '<button class="line-chip' + (k === active ? " is-active" : "") + '" data-line="' + k +
    '" title="' + escapeAttr(l.url) + '">' + (k + 1) + " · " + (KIND_LABEL[l.kind] || "直链") + "</button>"
  ).join("");
  Array.prototype.forEach.call(box.querySelectorAll(".line-chip"), (btn) => {
    btn.addEventListener("click", () => playChannel(PREVIEW.index, Number(btn.getAttribute("data-line"))));
  });
}

function playChannel(index, lineIndex) {
  const channels = PREVIEW.channels;
  if (!channels.length) return;
  const i = ((index % channels.length) + channels.length) % channels.length;
  const ch = channels[i];
  const total = ch.lines.length;
  const li = total > 1 ? (((lineIndex || 0) % total) + total) % total : 0;
  const line = ch.lines[li];
  PREVIEW.index = i;
  PREVIEW.line = li;
  EL("now-name").textContent = ch.name + " · " + ch.group +
    (total > 1 ? " · 线路 " + (li + 1) + "/" + total : "");
  EL("now-name").classList.remove("is-error");
  EL("now-url").textContent = line.url;
  renderLineChips(ch, li);
  highlightChannel(i);
  switchPreviewTab("play");
  stopPlayback();
  const video = EL("preview-video");
  const kind = line.kind || urlKind(line.url);
  if (location.protocol === "https:" && /^http:\/\//i.test(line.url)) {
    EL("stage-hint").textContent = "该线路是 http 明文流，HTTPS 页面会被按混合内容拦截，可点“复制地址”到本地播放器打开。";
    return;
  }

  if (kind === "multicast") {
    EL("stage-hint").textContent = "该频道是组播 / 内网线路（rtp/udp/rtsp），浏览器无法直连播放，建议复制地址到本地播放器使用。";
    return;
  }

  if (kind === "hls") {
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        manifestLoadingTimeOut: 8000,
        levelLoadingTimeOut: 8000,
        fragLoadingTimeOut: 12000,
      });
      PREVIEW.hls = hls;
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (!data || !data.fatal) return;
        EL("stage-hint").textContent = hlsErrorHint(data);
        EL("now-name").classList.add("is-error");
      });
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => {});
      });
      hls.loadSource(line.url);
      hls.attachMedia(video);
      return;
    }
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = line.url;
      video.play().catch(() => {});
      EL("stage-hint").textContent = "使用浏览器原生 HLS 播放 · ← → 换台";
      return;
    }
    EL("stage-hint").textContent = "当前浏览器缺少 HLS 支持（hls.js 未加载），无法直接播放 m3u8，可复制地址到播放器。";
    return;
  }

  video.src = line.url;
  video.play().catch(() => {});
  if (kind === "ts") {
    EL("stage-hint").textContent = "TS 直链在浏览器里通常无法播放，建议复制地址到播放器。";
  }
}

function hlsErrorHint(data) {
  const code = data && data.response ? data.response.code : 0;
  const details = (data && data.details) || "";
  if (code === 403) return "源站返回 403（拒绝访问），线路可能已失效或需要鉴权，点“下一台”继续。";
  if (code === 404) return "源站返回 404（地址不存在），线路大概率已失效，点“下一台”继续。";
  if (code === 0) return "浏览器无法直连该源（跨域限制或网络被拦），可点“复制地址”到本地播放器打开。";
  if (details === "manifestParsingError") return "播放列表格式无法解析，线路可能已失效，点“下一台”继续。";
  return "该源加载失败（可能已失效），点“下一台”继续切换。";
}

function stepChannel(delta) {
  if (!PREVIEW.channels.length) return;
  playChannel(PREVIEW.index + delta);
}

function copyCurrentChannel(btn) {
  const url = EL("now-url").textContent;
  if (!url) return;
  const done = () => {
    const old = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = old; }, 1400);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(url).then(done, () => prompt("请手动复制:", url));
  } else {
    prompt("请手动复制:", url);
  }
}

function switchPreviewTab(tab) {
  PREVIEW.tab = tab;
  EL("tab-play").classList.toggle("is-active", tab === "play");
  EL("tab-text").classList.toggle("is-active", tab === "text");
  EL("preview-stage").hidden = tab !== "play";
  EL("preview-side").hidden = tab !== "play";
  EL("preview-text-panel").hidden = tab !== "text";
}

function closePreview() {
  stopPlayback();
  EL("preview-modal").hidden = true;
  document.body.style.overflow = "";
}

function stopPlayback() {
  if (PREVIEW.hls) {
    PREVIEW.hls.destroy();
    PREVIEW.hls = null;
  }
  const video = EL("preview-video");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
}

function renderText(text) {
  const lines = String(text).split(/\r?\n/);
  const shown = lines.slice(0, 400).join("\n");
  EL("preview-text").textContent = shown + (lines.length > 400
    ? "\n\n… 仅显示前 400 行，共 " + lines.length + " 行，完整内容请点“下载”"
    : "");
}

function shortUrl(url) {
  const u = String(url);
  return u.length > 62 ? u.slice(0, 59) + "…" : u;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/'/g, "&#39;");
}

document.addEventListener("DOMContentLoaded", () => {
  const modal = EL("preview-modal");
  const search = EL("chan-search");
  const video = EL("preview-video");
  if (search) search.addEventListener("input", () => renderChannelList(search.value));
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closePreview(); });
  if (video) {
    video.addEventListener("playing", () => {
      EL("stage-hint").textContent = "← → 换台 · Esc 关闭 · 点击左侧频道可直接切换";
      EL("now-name").classList.remove("is-error");
    });
    video.addEventListener("error", () => {
      if (PREVIEW.tab !== "play" || PREVIEW.index < 0) return;
      EL("stage-hint").textContent = "该直链浏览器无法播放（跨域限制或格式不支持），可点“复制地址”到本地播放器打开。";
      EL("now-name").classList.add("is-error");
    });
  }
  document.addEventListener("keydown", (e) => {
    if (!modal || modal.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); closePreview(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepChannel(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepChannel(1); }
  });
});
"""

_ASSET_META = {}  # 文件名 -> {"url":..., "previewable":bool, "size":int}


def normalize_base_url(value):
    base = str(value or "").strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        raise ValueError(f"pages-base-url 必须是绝对 HTTP(S) URL: {value}")
    return base


def collect_assets(destination, generated_at):
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    meta = {}
    for src, name in OPTIONAL_ASSETS.items():
        src_path = Path("output") / src
        if not src_path.exists() or src_path.stat().st_size == 0:
            continue
        shutil.copyfile(src_path, dest / name)
        meta[name] = {
            "url": name,
            "previewable": name in PREVIEWABLE,
            "size": src_path.stat().st_size,
        }
    if not meta:
        raise SystemExit("output/ 下没有可发布的结果文件,请先运行 main.py")
    for src, name in VENDOR_FILES.items():
        src_path = Path(src)
        if src_path.exists() and src_path.stat().st_size > 0:
            shutil.copyfile(src_path, dest / name)
    (dest / "manifest.json").write_text(
        json.dumps({"generated_at": generated_at, "assets": meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def _fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def render_index(base_url, meta, generated_at):
    groups_html = []
    for group in PAGE_ASSET_GROUPS:
        cards = []
        for name, fmt, desc in group["assets"]:
            info = meta.get(name)
            if not info:
                continue
            url = f"{base_url}/{name}" if base_url else name
            url_html = html.escape(url)
            desc_html = html.escape(desc)
            preview_btn = (
                f'<button class="card-action preview-action" onclick="previewAsset(\'{name}\')">'
                f'{SVG_EYE}<span>预览</span></button>'
                if info["previewable"]
                else ""
            )
            cards.append(
                f'<article class="result-card">'
                f'<div class="result-name">{desc_html}</div>'
                f'<span class="file-type">{fmt}</span>'
                f'<code>{url_html}</code>'
                f'<div class="result-actions">'
                f'<button class="card-action copy-action" onclick="copyUrl(this,\'{url}\')">'
                f'{SVG_COPY}<span>复制链接</span></button>'
                f'{preview_btn}'
                f'<a class="card-action download-action" href="{url}" download>'
                f'{SVG_DOWNLOAD}<span>下载</span></a>'
                f'</div></article>'
            )
        if not cards:
            continue
        groups_html.append(
            f'<section class="result-section" id="{group["id"]}">'
            f'<div class="section-heading"><p class="eyebrow">{group["eyebrow"]}</p>'
            f'<h2>{group["title"]}</h2>'
            f'<p>{group["description"]}</p></div>'
            f'<div class="result-grid">{"".join(cards)}</div>'
            f'</section>'
        )

    previewable_names = json.dumps([n for n, i in meta.items() if i["previewable"]])
    generated_text = html.escape(generated_at)
    total_size = _fmt_size(sum(i["size"] for i in meta.values()))
    asset_count = len(meta)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Live-tv 直播源更新结果</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='23' fill='%23747bff'/%3E%3Ccircle cx='50' cy='50' r='29' fill='none' stroke='%23ffffff' stroke-width='4'/%3E%3Cpath d='M43 37 L60 47 Q62 50 60 53 L43 63 Q40 65 40 61 L40 39 Q40 36 43 37 Z' fill='%23ffffff'/%3E%3C/svg%3E">
<style>
:root {{
  color-scheme: light dark;
  --page: #f5f6fd;
  --surface-strong: #ffffff;
  --text: #1e2145;
  --muted: #5f6480;
  --border: #dcdef5;
  --primary: #747bff;
  --primary-strong: #575ce0;
  --primary-soft: #eef0ff;
  --hero-gradient: linear-gradient(135deg, #4640c8 0%, #747bff 55%, #9a6bff 100%);
  --shadow: 0 18px 50px rgba(84, 74, 220, 0.12);
  --radius-lg: 24px;
  --radius-md: 16px;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  min-height: 100vh;
  margin: 0;
  background:
    radial-gradient(circle at 10% 0%, rgba(116, 123, 255, 0.14), transparent 34rem),
    radial-gradient(circle at 95% 12%, rgba(64, 209, 255, 0.10), transparent 28rem),
    var(--page);
  color: var(--text);
  font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}}
a {{ color: inherit; }}
.page-shell {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }}

/* ---------- Hero ---------- */
.hero {{
  position: relative;
  overflow: hidden;
  padding: clamp(26px, 4.5vw, 48px);
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: var(--radius-lg);
  background: var(--hero-gradient);
  box-shadow: var(--shadow);
  color: #ffffff;
}}
.hero::after {{
  position: absolute;
  inset: auto -8% -60% auto;
  width: 360px;
  height: 360px;
  border: 72px solid rgba(255, 255, 255, 0.08);
  border-radius: 50%;
  content: "";
}}
.brand {{ display: inline-flex; align-items: center; gap: 10px; margin: 0 0 24px; font-weight: 750; letter-spacing: -0.02em; }}
.brand-logo {{
  display: block;
  width: 42px; height: 42px;
  border-radius: 10px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
}}
.hero h1 {{ margin: 0 0 10px; font-size: clamp(26px, 4vw, 38px); line-height: 1.2; letter-spacing: -0.03em; }}
.hero .lede {{ margin: 0; max-width: 46em; color: rgba(255, 255, 255, 0.86); font-size: 15px; }}
.hero-stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
.hero-stats span {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
  font-size: 13px;
  backdrop-filter: blur(4px);
}}
.hero-stats b {{ font-weight: 800; }}

/* ---------- 分节 ---------- */
.result-section {{ margin-top: 48px; }}
.section-heading {{ max-width: 720px; margin-bottom: 18px; }}
.eyebrow {{
  margin: 0 0 4px;
  color: var(--primary);
  font-size: 12px; font-weight: 800; letter-spacing: 0.14em; text-transform: uppercase;
  width: fit-content;
}}
.section-heading h2 {{ margin: 0; font-size: clamp(24px, 3vw, 32px); line-height: 1.2; letter-spacing: -0.03em; }}
.section-heading > p:last-child {{ margin: 8px 0 0; color: var(--muted); font-size: 14px; }}
.result-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}

/* ---------- 结果卡片 ---------- */
.result-card {{
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 3px 16px;
  min-width: 0;
  padding: 22px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-strong);
  box-shadow: 0 8px 24px rgba(84, 74, 220, 0.06);
  transition: border-color 180ms ease, box-shadow 180ms ease, transform 180ms ease;
}}
.result-card:hover {{ border-color: var(--primary); box-shadow: 0 14px 32px rgba(84, 74, 220, 0.14); transform: translateY(-2px); }}
.file-type {{
  grid-column: 2; grid-row: 1 / span 2;
  align-self: start;
  padding: 5px 11px;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary-strong);
  font-size: 11px; font-weight: 800; letter-spacing: 0.05em;
}}
.result-name {{ grid-column: 1; font-size: 17px; font-weight: 750; letter-spacing: -0.02em; }}
.result-card code {{
  grid-column: 1 / -1;
  min-width: 0;
  margin-top: 14px;
  padding: 10px 12px;
  overflow-wrap: anywhere;
  border-radius: 10px;
  background: var(--page);
  color: var(--primary-strong);
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
.result-actions {{ display: flex; grid-column: 1 / -1; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
.card-action {{
  display: inline-flex;
  min-height: 44px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 9px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  font: 700 13px/1.3 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  text-decoration: none;
  cursor: pointer;
  transition: background-color 160ms ease, border-color 160ms ease, color 160ms ease;
}}
.card-action svg {{ width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }}
.copy-action {{ background: var(--surface-strong); color: var(--primary); }}
.copy-action:hover {{ border-color: var(--primary); background: var(--primary-soft); }}
.copy-action.is-copied {{ border-color: #16a34a; background: #dcfce7; color: #166534; }}
.preview-action {{ border-color: var(--border); background: var(--primary-soft); color: var(--primary-strong); }}
.preview-action:hover {{ border-color: var(--primary); background: #e0e3ff; }}
.download-action {{ border-color: var(--primary); background: var(--primary); color: #ffffff; font-weight: 750; }}
.download-action:hover {{ border-color: var(--primary-strong); background: var(--primary-strong); }}

{PREVIEW_CSS}

footer {{
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px;
  margin-top: 56px; padding: 24px 0 8px;
  border-top: 1px solid var(--border);
  color: var(--muted); font-size: 13px;
}}
footer p {{ margin: 0; }}
footer a {{ color: var(--primary-strong); font-weight: 600; text-decoration: none; }}
footer a:hover {{ text-decoration: underline; }}

@media (max-width: 700px) {{
  .page-shell {{ width: min(100% - 24px, 1120px); padding-top: 16px; }}
  .hero {{ padding: 24px 20px 28px; border-radius: 20px; }}
  .result-grid {{ grid-template-columns: 1fr; }}
  .result-section {{ margin-top: 40px; }}
  .result-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .download-action {{ grid-column: 1 / -1; }}
  footer {{ flex-direction: column; }}
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --page: #0d1022;
    --surface-strong: #151933;
    --text: #eef0ff;
    --muted: #a9adcf;
    --border: #2c3160;
    --primary: #9aa2ff;
    --primary-strong: #c9ccff;
    --primary-soft: #23285a;
    --shadow: 0 18px 50px rgba(0, 0, 0, 0.3);
  }}
  body {{
    background:
      radial-gradient(circle at 10% 0%, rgba(116, 123, 255, 0.16), transparent 34rem),
      radial-gradient(circle at 95% 12%, rgba(64, 209, 255, 0.10), transparent 28rem),
      var(--page);
  }}
  .hero {{ background: linear-gradient(135deg, #2f2a8f 0%, #5a5fd0 55%, #8a5bd6 100%); }}
  .result-card code {{ background: var(--page); color: #c3c6ff; }}
  .copy-action {{ background: var(--surface-strong); }}
  .copy-action.is-copied {{ border-color: #4ade80; background: #153d2a; color: #bbf7d0; }}
  .preview-action {{ border-color: #36537a; background: #23285a; color: #c9ccff; }}
  .preview-action:hover {{ border-color: var(--primary); background: #2e3470; }}
  .download-action {{ border-color: #6d74ff; background: #6d74ff; color: #ffffff; }}
  .download-action:hover {{ border-color: #8a90ff; background: #8a90ff; }}
}}
@media (prefers-reduced-motion: reduce) {{
  html {{ scroll-behavior: auto; }}
  .result-card, .card-action {{ transition: none; }}
  .result-card:hover {{ transform: none; }}
}}
</style>
{HLS_LOADER}
</head>
<body>
<div class="page-shell">
<header class="hero">
  <p class="brand"><svg class="brand-logo" viewBox="0 0 256 256" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect width="256" height="256" rx="58" fill="#747bff"/><circle cx="128" cy="128" r="74" fill="#ffffff" fill-opacity="0.12"/><circle cx="128" cy="128" r="74" fill="none" stroke="#ffffff" stroke-width="9" stroke-opacity="0.92"/><path d="M 110 96 L 158 122 Q 164 128 158 134 L 110 160 Q 103 164 103 156 L 103 100 Q 103 92 110 96 Z" fill="#ffffff" stroke="#ffffff" stroke-width="10" stroke-linejoin="round"/></svg>Live-tv</p>
  <h1>直播源更新结果</h1>
  <p class="lede">自动聚合多个公开直播源,按频道模板匹配、测速筛选后生成。选择与您网络匹配的格式,复制链接粘贴到播放器的订阅地址栏即可;点“预览”可在弹窗里直接试播并换台。</p>
  <div class="hero-stats">
    <span>🕐 生成于 <b>{generated_text}</b></span>
    <span>📦 资产 <b>{asset_count}</b> 个 · 共 <b>{total_size}</b></span>
    <span>📡 每日自动更新</span>
  </div>
</header>
{"".join(groups_html)}
{PREVIEW_HTML}
<footer>
  <p>Live-tv · 每日自动聚合公开直播源,仅供测试研究学习,不得商用</p>
  <p><a href="https://github.com/52liulian/Live-tv">GitHub 仓库</a> · <a href="https://github.com/52liulian/Live-tv#直播源调用地址">使用文档</a></p>
</footer>
</div>
<script>
const PREVIEWABLE = {previewable_names};
function copyUrl(btn, url) {{
  navigator.clipboard.writeText(url).then(
    () => flashCopied(btn),
    () => {{ prompt("请手动复制:", url); }}
  );
}}
function flashCopied(btn) {{
  const label = btn.querySelector("span");
  const old = label ? label.textContent : btn.textContent;
  btn.classList.add("is-copied");
  if (label) {{ label.textContent = "已复制"; }} else {{ btn.textContent = "已复制"; }}
  setTimeout(() => {{
    btn.classList.remove("is-copied");
    if (label) {{ label.textContent = old; }} else {{ btn.textContent = old; }}
  }}, 1400);
}}
{PREVIEW_SCRIPT}
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="生成 GitHub Pages 结果站点")
    parser.add_argument("--pages-destination", default="pages-site")
    parser.add_argument("--pages-base-url", default="")
    parser.add_argument(
        "--time-zone", default="Asia/Shanghai",
        help="生成时间显示时区,如 Asia/Shanghai",
    )
    args = parser.parse_args()

    base_url = normalize_base_url(args.pages_base_url)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(args.time_zone)
    except Exception:
        tz = timezone(timedelta(hours=8))
    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z").strip()

    meta = collect_assets(args.pages_destination, generated_at)
    index = render_index(base_url, meta, generated_at)
    (Path(args.pages_destination) / "index.html").write_text(index, encoding="utf-8")

    print(f"pages-site 生成完成,包含 {len(meta)} 个资产:")
    for name, info in sorted(meta.items()):
        print(f"  {name:14s} {_fmt_size(info['size']):>9s}  {base_url}/{name}")
    print(f"  index.html     结果页入口{' (base_url=' + base_url + ')' if base_url else ' (未提供 base_url,链接为相对路径)'}")


if __name__ == "__main__":
    main()
