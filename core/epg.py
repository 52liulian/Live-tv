"""EPG 节目单。

实现要点：
  - EpgStreamParser：XMLPullParser 流式解析，避免超大 XML 占满内存
  - 时区感知的时间窗过滤（%Y%m%d%H%M%S%z）
  - URL canonical 去重（github blob→raw、去默认端口、统一大小写）
  - 内容 hash 整源去重
  - 三级优先级合并：白名单 0 / 配置 1 / 订阅发现 2，数字小者优先
  - gzip/deflate 自动识别、资源上限保护
对外保留 get_epg / parse_epg_xml / write_epg，兼容既有调用。
"""
import gzip
import hashlib
import logging
import os
import re
import shutil
import zlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from core import config
from core import tools

# ---------- 资源限制常量 ----------
EPG_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
EPG_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
EPG_MAX_PROGRAMMES = 500000
EPG_DAYS_BACK = 1
EPG_DAYS_AHEAD = 14
SUBSCRIBE_EPG_MAX_SOURCES = 3

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class EpgResourceLimitError(Exception):
    pass


def _cfg(name, default):
    return getattr(config, name, default)


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag


def _child_text(element, name):
    for child in element:
        if _local_name(child.tag) == name:
            return child.text or ""
    return ""


# ---------- 1. 流式解析 ----------
class EpgStreamParser:
    def __init__(self, normalized_names=None, include_unmatched=True,
                 max_programmes=None, days_back=None, days_ahead=None):
        self.parser = ET.XMLPullParser(events=("end",))
        self.normalized_names = set(normalized_names or ())
        self.include_unmatched = include_unmatched
        self.max_programmes = max_programmes
        self.days_back = days_back
        self.days_ahead = days_ahead
        self.channels = {}
        self.included_channel_ids = set()
        self.programmes = defaultdict(list)
        self.programme_count = 0
        self.retained_programme_count = 0
        self.now_by_timezone = {}

    def feed(self, content):
        self.parser.feed(content)
        self._drain()

    def close(self):
        try:
            self.parser.close()
        except Exception:
            pass
        self._drain()
        return self.channels, self.programmes

    def _drain(self):
        for _, element in self.parser.read_events():
            tag = _local_name(element.tag)
            if tag == "channel":
                self._process_channel(element)
                element.clear()
            elif tag == "programme":
                self._process_programme(element)
                element.clear()

    def _process_channel(self, element):
        channel_id = element.get("id")
        display_name = _child_text(element, "display-name").strip()
        if not channel_id or not display_name:
            return
        normalized_name = tools.format_channel_name(display_name)
        if (not self.include_unmatched and self.normalized_names
                and normalized_name not in self.normalized_names):
            return
        self.channels[channel_id] = display_name
        self.included_channel_ids.add(channel_id)

    def _process_programme(self, element):
        self.programme_count += 1
        if self.max_programmes and self.programme_count > self.max_programmes:
            raise EpgResourceLimitError(f"EPG programme count exceeds {self.max_programmes}")

        channel_id = element.get("channel")
        if channel_id not in self.included_channel_ids:
            return
        try:
            channel_start = datetime.strptime(
                re.sub(r"\s+", "", element.get("start") or ""), "%Y%m%d%H%M%S%z")
            channel_stop = datetime.strptime(
                re.sub(r"\s+", "", element.get("stop") or ""), "%Y%m%d%H%M%S%z")
        except (TypeError, ValueError):
            return

        tz = channel_start.tzinfo
        if tz not in self.now_by_timezone:
            self.now_by_timezone[tz] = datetime.now(tz) if tz else datetime.now()
        now = self.now_by_timezone[tz]
        if self.days_back is not None and channel_stop < now - timedelta(days=self.days_back):
            return
        if self.days_ahead is not None and channel_start > now + timedelta(days=self.days_ahead):
            return

        title = _child_text(element, "title").strip()
        if not title:
            return
        output = ET.Element("programme", attrib={
            "channel": channel_id,
            "start": channel_start.strftime("%Y%m%d%H%M%S %z"),
            "stop": channel_stop.strftime("%Y%m%d%H%M%S %z"),
        })
        ET.SubElement(output, "title", attrib={"lang": "zh"}).text = tools.t2s(title)
        self.programmes[channel_id].append(output)
        self.retained_programme_count += 1


def parse_epg_xml(content, target_names=None, include_unmatched=True,
                  max_programmes=None, days_back=None, days_ahead=None):
    """解析 XMLTV 字节/文本，返回 (channels, programmes_by_channel_id)。

    programmes 值为重建后的 programme 元素列表（仅 channel/start/stop/title）。
    target_names 为模板频道名集合，会先归一化为 format_channel_name。
    """
    normalized = {tools.format_channel_name(n) for n in (target_names or ()) if n}
    parser = EpgStreamParser(
        normalized_names=normalized,
        include_unmatched=include_unmatched,
        max_programmes=max_programmes or EPG_MAX_PROGRAMMES,
        days_back=EPG_DAYS_BACK if days_back is None else days_back,
        days_ahead=EPG_DAYS_AHEAD if days_ahead is None else days_ahead,
    )
    try:
        if isinstance(content, str):
            content = content.encode("utf-8")
        elif isinstance(content, bytearray):
            content = bytes(content)
        if isinstance(content, bytes) and content.startswith(b"\x1f\x8b"):
            content = zlib.decompress(content, 16 + zlib.MAX_WBITS)
        parser.feed(content)
        return parser.close()
    except (ET.ParseError, EpgResourceLimitError) as e:
        logging.warning(f"EPG XML 解析失败: {e}")
        return {}, defaultdict(list)


# ---------- 2. URL canonical 去重 ----------
def github_blob_to_raw(url: str) -> str:
    """把 github.com/{owner}/{repo}/blob|tree/{branch}/{path} 转成 raw.githubusercontent.com 地址。

    处理百分号编码路径，保留 raw 地址原样。
    """
    if not url:
        return url
    if "raw.githubusercontent.com" in url:
        return url
    parsed = urlsplit(url)
    netloc = parsed.netloc or ""
    if "github.com" not in netloc:
        return url
    parts = (parsed.path or "").lstrip("/").split("/")
    if len(parts) < 5 or parts[2] not in ("blob", "tree"):
        return url
    owner, repo, branch, file_parts = parts[0], parts[1], parts[3], parts[4:]
    try:
        decoded = "/".join(unquote(p) for p in file_parts)
        safe_path = quote(decoded, safe="/")
    except Exception:
        safe_path = "/".join(file_parts)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{safe_path}"


def canonical_epg_url(url):
    raw = github_blob_to_raw(str(url or "").strip())
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return raw
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
        port = parsed.port
        if port and not ((parsed.scheme.lower() == "http" and port == 80)
                         or (parsed.scheme.lower() == "https" and port == 443)):
            host = f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), f"{userinfo}{host}",
                           parsed.path or "/", parsed.query, ""))
    except (TypeError, ValueError):
        return raw


def entry_key(entry):
    url = entry.get("url") if isinstance(entry, dict) else entry
    headers = entry.get("headers") if isinstance(entry, dict) else None
    header_key = tuple(sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items()))
    return canonical_epg_url(url), header_key


def dedupe_epg_entries(whitelist_entries, default_entries, discovered_entries, discovered_limit=None):
    """三级去重，返回带 _priority/_origin 的条目列表。"""
    if discovered_limit is None:
        discovered_limit = SUBSCRIBE_EPG_MAX_SOURCES
    result, seen = [], set()
    discovered_added = 0
    groups = (
        (whitelist_entries, 0, "whitelist"),
        (default_entries, 1, "configured"),
        (discovered_entries, 2, "discovered"),
    )
    for entries, priority, origin in groups:
        for raw in entries or ():
            if origin == "discovered" and discovered_added >= discovered_limit:
                continue
            entry = dict(raw) if isinstance(raw, dict) else {"url": raw}
            key = entry_key(entry)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            entry["_priority"] = priority
            entry["_origin"] = origin
            result.append(entry)
            if origin == "discovered":
                discovered_added += 1
    return result


# ---------- 3. 单源获取（流式、自动解压、内容 hash） ----------
def _fetch_epg(entry, timeout):
    """抓取单个 EPG 源，返回 (数据, hash, 错误信息)；HTTP 代理只作用于此处。"""
    url = entry.get("url") if isinstance(entry, dict) else entry
    headers = {"User-Agent": _cfg("user_agent", "") or DEFAULT_UA}
    if isinstance(entry, dict) and entry.get("headers"):
        headers.update(entry["headers"])
    try:
        response = requests.get(url, headers=headers, timeout=timeout, stream=True,
                                 proxies=tools.get_request_proxies(),
                                 verify=getattr(config, "ssl_verify", True))
        response.raise_for_status()
        digest = hashlib.sha256()
        chunks, compressed_size = [], 0
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        decompressor = (zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding in ("gzip", "x-gzip")
                        else zlib.decompressobj() if encoding == "deflate" else None)
        decompressed = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            compressed_size += len(chunk)
            if compressed_size > EPG_MAX_DOWNLOAD_BYTES:
                raise EpgResourceLimitError("EPG response too large")
            if decompressor:
                data = decompressor.decompress(chunk)
            else:
                if not chunks and chunk[:2] == b"\x1f\x8b":
                    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    data = decompressor.decompress(chunk)
                else:
                    data = chunk
            chunks.append(data)
            digest.update(data)
            decompressed.extend(data)
            if len(decompressed) > EPG_MAX_DECOMPRESSED_BYTES:
                raise EpgResourceLimitError("EPG decompressed too large")
        if decompressor:
            tail = decompressor.flush()
            if tail:
                digest.update(tail)
                decompressed.extend(tail)
        if not decompressed:
            return None, None, "内容为空"
        return bytes(decompressed), digest.hexdigest(), None
    except Exception as e:
        logging.warning(f"EPG 源获取失败 {url}: {e}")
        return None, None, "请求失败"


# ---------- 4. 主入口：多源合并（优先级与 hash 去重） ----------
def get_epg(epg_urls=None, discovered=None, target_names=None, include_unmatched=True):
    """获取并合并多个 EPG 源，返回 {频道主名: [programme元素]}。

    epg_urls 为配置源（优先级 1）；discovered 为订阅发现源（优先级 2）。
    """
    if not _cfg("open_epg", True):
        return {}

    configured = list(epg_urls if epg_urls is not None else _cfg("epg_urls", []))
    extra = list(discovered or [])[:SUBSCRIBE_EPG_MAX_SOURCES]
    entries = dedupe_epg_entries([], configured, extra)
    if not entries:
        return {}

    timeout = min(max(60, _cfg("request_timeout", 10) * 6), 180)
    seen_hashes = set()
    disabled = {}  # 源地址 -> 失效原因
    programme_records = defaultdict(dict)  # name -> {(start,stop): (priority, prog)}
    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as pool:
        futures = {pool.submit(_fetch_epg, entry, timeout): entry for entry in entries}
        for future in futures:
            entry = futures[future]
            source_url = entry.get("url")
            try:
                data, content_hash, error = future.result()
            except Exception as e:
                logging.warning(f"EPG 任务异常: {e}")
                disabled[source_url] = "任务异常"
                continue
            if not data:
                disabled[source_url] = error or "请求失败"
                continue
            if content_hash in seen_hashes:
                logging.debug(f"EPG 内容重复，跳过: {entry.get('url')}")
                continue
            seen_hashes.add(content_hash)
            channels, programmes = parse_epg_xml(
                data, target_names=target_names, include_unmatched=include_unmatched)
            if not channels:
                disabled[source_url] = "没有匹配到符合条件的值"
                continue
            priority = entry.get("_priority", 2)
            for channel_id, display_name in channels.items():
                primary = tools.format_channel_name(display_name)
                for prog in programmes.get(channel_id, ()):
                    key = (prog.get("start"), prog.get("stop"))
                    existing = programme_records[primary].get(key)
                    if existing is None or priority < existing[0]:
                        programme_records[primary][key] = (priority, prog)

    if disabled and _cfg("open_auto_disable_source", False):
        epg_path = os.path.join(config.CONFIG_DIR, "epg.txt")
        counts = tools.disable_urls_in_file(epg_path, disabled.keys())
        logging.info(f"自动停用 EPG 源完成：有效地址 {counts['active']} 个，停用地址 {counts['disabled']} 个")
    for url, reason in disabled.items():
        logging.warning(f"EPG 源失效已记录: {url}（{reason}）")

    result = {}
    for name, records in programme_records.items():
        ordered = sorted(records.values(), key=lambda p: (p[1].get("start") or "", p[0]))
        result[name] = [item[1] for item in ordered]
    logging.info(f"EPG 合并完成，覆盖频道 {len(result)} 个")
    return result


def write_epg(programmes, xml_path="output/epg/epg.xml", gz_path="output/epg/epg.gz"):
    """写出 epg.xml 与 epg.gz，tvg-id 与 M3U 主名对齐。"""
    root = ET.Element("tv", attrib={"date": datetime.now().strftime("%Y%m%d%H%M%S %z")})
    for channel_id, progs in programmes.items():
        channel_elem = ET.SubElement(root, "channel", attrib={"id": channel_id})
        display = ET.SubElement(channel_elem, "display-name", attrib={"lang": "zh"})
        display.text = channel_id
        for prog in progs:
            prog.set("channel", channel_id)
            root.append(prog)
    os.makedirs(os.path.dirname(xml_path) or ".", exist_ok=True)
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="\t")
    except AttributeError:
        pass
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    with open(xml_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return xml_path, gz_path
