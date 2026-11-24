"""通用工具函数。

包含与精简管线相关的部分：
频道名归一化、M3U/txt 解析（含引号内逗号与 EXTVLCOPT 状态机）、
URL 说明信息处理、EPG 地址提取、M3U 生成、CDN 候选等。
"""
import io
import logging
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime

from core import config

try:
    from opencc import OpenCC
    _opencc = OpenCC("t2s")
except Exception:
    _opencc = None

# 去分隔符、括号内容和常见画质/来源噪声
SUB_PATTERN = re.compile(
    r"-|_|\((.*?)\)|（(.*?)）|\[(.*?)]|「(.*?)」| |｜|频道|普清|标清|高清|HD|hd|超清|超高|超高清|4K|4k|中央|央视|电视台|台|电信|联通|移动"
)
KEY_VALUE_PATTERN = re.compile(r'(?P<key>[\w-]+)=(?P<value>"[^"]*"|\'[^\']*\'|\S+)')
URL_HOST_PATTERN = re.compile(r"((https?|rtmp|rtsp)://)?([^:@/]+(:[^:@/]*)?@)?(\[[0-9a-fA-F:]+]|([\w-]+\.)+[\w-]+)")
URL_PATTERN = re.compile(r"(?P<url>" + URL_HOST_PATTERN.pattern + r"\S*)")
TXT_PATTERN = re.compile(r"^(?P<name>[^,，]+)[,，](?!#genre#)(?P<value>.+)$", re.MULTILINE)

REPLACE_DICT = {"plus": "+", "PLUS": "+", "＋": "+"}

# 广告关键词从 config 读取，缺失时用兜底表。
AD_FILTER_KEYWORDS_FALLBACK = (
    "no_signal", "nosignal", "no-signal", "signal_offline", "no_video", "novideo",
    "advertisement", "advert", "placeholder", "default_video", "cctv_off", "/ad/", "/ads/",
)


class _KeywordProxy:
    """动态代理到 config.ad_filter_keywords，让调用方 AD_FILTER_KEYWORDS 保持引用稳定。"""
    def __iter__(self):
        keywords = getattr(config, "ad_filter_keywords", None) or AD_FILTER_KEYWORDS_FALLBACK
        return iter(keywords)


AD_FILTER_KEYWORDS = _KeywordProxy()


def t2s(text: str) -> str:
    """繁体转简体，缺少 opencc 时降级为原样返回。"""
    return _opencc.convert(text) if _opencc else text


def format_name(name: str) -> str:
    """归一化频道名：繁转简、去噪声、替换、小写。全链路统一入口。"""
    if not name:
        return ""
    name = t2s(str(name))
    name = SUB_PATTERN.sub("", name)
    for old, new in REPLACE_DICT.items():
        name = name.replace(old, new)
    return name.lower()


def format_channel_name(name: str) -> str:
    """把任意别名映射为主频道名，供匹配和 EPG 共用。"""
    from core.alias import channel_alias
    return channel_alias.get_primary(name)


def get_resolution_value(resolution_str) -> int:
    """从 '1920x1080' 之类的字符串解析出宽高的乘积。"""
    try:
        if resolution_str:
            match = re.search(r"(\d+)[xX*](\d+)", str(resolution_str))
            if match:
                width, height = map(int, match.groups())
                return width * height
    except Exception:
        pass
    return 0


def get_url_host(url: str):
    match = URL_HOST_PATTERN.search(url or "")
    return match.group() if match else None


def check_url_by_keywords(url: str, keywords=None) -> bool:
    """黑名单关键词子串匹配，命中返回 True。"""
    keywords = config.url_blacklist if keywords is None else keywords
    lowered = (url or "").lower()
    for keyword in keywords or []:
        if keyword and keyword.lower() in lowered:
            return True
    return False


def add_url_info(url: str, info: str) -> str:
    """在 URL 尾部追加 $ 说明信息，已含 $ 时用 - 连接。"""
    if info:
        separator = "-" if "$" in url else "$"
        url += f"{separator}{info}"
    return url


def remove_cache_info(string: str) -> str:
    return re.sub(r"[.*]?\$?-?cache:.*", "", string or "")


def get_headers_key_value(content: str) -> dict:
    """从属性文本里解析出 key=value，key 做归一化（去 http- 与 -，refer 归为 referer）。"""
    key_value = {}
    for match in KEY_VALUE_PATTERN.finditer(content or ""):
        key = match.group("key").strip().replace("http-", "").replace("-", "").lower()
        if "refer" in key:
            key = "referer"
        value = match.group("value").replace('"', "").strip()
        if key and value:
            key_value[key] = value
    return key_value


def get_m3u_epg_urls(content: str) -> list:
    """从 m3u 的 #EXTM3U 头部提取 url-tvg / x-tvg-url 声明的 EPG 地址。"""
    urls = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#EXTM3U"):
            continue
        attributes = get_headers_key_value(stripped[len("#EXTM3U"):])
        for key in ("urltvg", "xtvgurl"):
            for url in attributes.get(key, "").split(","):
                url = url.strip()
                if url.startswith(("http://", "https://")) and url not in urls:
                    urls.append(url)
        break
    return urls


def get_name_value(content, pattern, open_headers=False, check_value=True):
    """从订阅源文本中提取频道条目。

    m3u 走手写逐行状态机，正确处理引号内逗号和 #EXTVLCOPT 选项；
    txt 走正则。返回 [{name, value, headers, catchup, tvg_logo}, ...]。
    """
    result = []

    def append_item(name, value, attributes):
        if not name or (check_value and not value):
            return
        headers = {
            "User-Agent": attributes.get("useragent", ""),
            "Referer": attributes.get("referer", ""),
            "Origin": attributes.get("origin", ""),
        }
        catchup = {
            "catchup": attributes.get("catchup", ""),
            "catchup-source": attributes.get("catchupsource", ""),
        }
        headers = {k: v for k, v in headers.items() if v}
        catchup = {k: v for k, v in catchup.items() if v}
        if not open_headers and headers:
            return
        item = {"name": name, "value": value, "catchup": catchup,
                "tvg_logo": attributes.get("tvglogo", "")}
        if open_headers:
            item["headers"] = headers
        result.append(item)

    is_m3u_state_machine = pattern == "m3u"
    if is_m3u_state_machine:
        lines = (content or "").splitlines()
        index, total = 0, len(lines)
        while index < total:
            stripped = lines[index].strip()
            if not stripped.startswith("#EXTINF:-1"):
                index += 1
                continue
            remainder = stripped[len("#EXTINF:-1"):].strip()
            if not remainder:
                index += 1
                continue

            in_quote = None
            separator_index = None
            for pos, char in enumerate(remainder):
                if char in ('"', "'"):
                    if in_quote == char:
                        in_quote = None
                    elif in_quote is None:
                        in_quote = char
                elif char == "," and in_quote is None:
                    separator_index = pos
                    break
            if separator_index is None:
                separator_index = remainder.rfind(",")
                if separator_index < 0:
                    index += 1
                    continue

            attributes_text = remainder[:separator_index].strip()
            name = remainder[separator_index + 1:].strip()
            index += 1

            options_lines, value = [], ""
            while index < total:
                candidate = lines[index].strip()
                if not candidate:
                    index += 1
                    continue
                if candidate.startswith("#EXTVLCOPT:"):
                    options_lines.append(candidate)
                    index += 1
                    continue
                if candidate.startswith("#EXTINF:-1"):
                    break
                value = candidate
                index += 1
                break

            attributes = get_headers_key_value("\n".join(p for p in [attributes_text, *options_lines] if p))
            append_item(name, value, attributes)
        return result

    for match in pattern.finditer(content or ""):
        group = match.groupdict()
        name = (group.get("name", "") or "").strip()
        value = (group.get("value", "") or "").strip()
        attributes = {**get_headers_key_value(group.get("options", "")),
                      **get_headers_key_value(group.get("attributes", ""))}
        append_item(name, value, attributes)
    return result


def parse_subscribe_content(content: str, open_headers=True):
    """判别 m3u / txt 并解析成条目列表。"""
    is_m3u = "#EXTM3U" in (content or "")[:2000].upper() or any(
        line.strip().startswith("#EXTINF") for line in (content or "").splitlines()[:15]
    )
    if is_m3u:
        return get_name_value(content, "m3u", open_headers=open_headers), True
    return get_name_value(content, TXT_PATTERN, open_headers=open_headers), False


def github_blob_to_raw(url: str) -> str:
    """github.com blob/tree 页面地址转 raw 地址。"""
    if not url:
        return url
    if "raw.githubusercontent.com" in url:
        return url
    from urllib.parse import quote, unquote, urlsplit
    parsed = urlsplit(url)
    if "github.com" not in (parsed.netloc or ""):
        return url
    parts = (parsed.path or "").lstrip("/").split("/")
    if len(parts) < 5 or parts[2] not in ("blob", "tree"):
        return url
    owner, repo, branch, file_parts = parts[0], parts[1], parts[3], parts[4:]
    try:
        safe_path = quote("/".join(unquote(p) for p in file_parts), safe="/")
    except Exception:
        safe_path = "/".join(file_parts)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{safe_path}"


def get_request_url_candidates(url: str):
    """为 GitHub raw 地址生成 CDN 候选。"""
    raw_url = github_blob_to_raw(url)
    candidates = [raw_url]
    cdn = getattr(config, "cdn_urls", None) or []
    lowered = raw_url.lower()
    is_raw = "raw.githubusercontent.com" in lowered or "githubusercontent.com" in lowered
    if is_raw:
        for prefix in cdn:
            prefix = prefix.rstrip("/")
            if prefix and prefix not in raw_url:
                candidates.append(f"{prefix}/{raw_url}")
    return list(dict.fromkeys(candidates))


def join_url(base: str, path: str) -> str:
    if not base:
        return path
    return f"{base.rstrip('/')}/{str(path).lstrip('/')}"


def get_datetime_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def convert_to_m3u(txt_path=None, content=None, data=None, epg_url="", logo_url="",
                   logo_type="png", open_epg=True, channel_epg_id=None,
                   first_channel_name="", open_url_info=False):
    """把结果 txt 转成 m3u。data 为 {频道名: [info]}，用于回填 tvg-logo/catchup/headers。

    返回 m3u 文本，写文件交给调用方，便于统一原子写。
    """
    source = io.StringIO(content) if content is not None else open(txt_path, "r", encoding="utf-8")
    m3u_output = f'#EXTM3U x-tvg-url="{epg_url}"\n' if open_epg and epg_url else "#EXTM3U\n"
    current_group = None
    data_positions = defaultdict(int)
    with source as file:
        for line in file:
            trimmed = line.strip()
            if not trimmed:
                continue
            if "#genre#" in trimmed:
                current_group = trimmed.replace(",#genre#", "").strip()
                continue
            original_name, _, channel_link = map(str.strip, trimmed.partition(","))
            if not original_name or not channel_link:
                continue

            use_name = first_channel_name or original_name
            item = {}
            if data:
                item_list = data.get(original_name, [])
                pos = data_positions[original_name]
                if pos < len(item_list):
                    item = item_list[pos]
                    data_positions[original_name] = pos + 1

            tvg_id = (channel_epg_id(use_name) if channel_epg_id else format_channel_name(use_name)) or use_name
            channel_logo = (item.get("tvg_logo") or "") if item else ""
            if not channel_logo and logo_url:
                channel_logo = join_url(logo_url, f"{use_name}.{logo_type}")

            m3u_output += f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{use_name}" tvg-logo="{channel_logo}"'
            if current_group:
                m3u_output += f' group-title="{current_group}"'
            if item:
                for key, value in (item.get("catchup") or {}).items():
                    m3u_output += f' {key}="{value}"'
            m3u_output += f",{original_name}\n"

            item_headers = dict(item.get("headers") or {}) if item else {}
            ua = getattr(config, "user_agent", "")
            if ua and "User-Agent" not in item_headers:
                item_headers["User-Agent"] = ua
            for key, value in item_headers.items():
                m3u_output += f"#EXTVLCOPT:http-{key.lower()}={value}\n"
            m3u_output += f"{channel_link}\n"
    return m3u_output


def atomic_write(path: str, content: str):
    """原子写文件，避免半截文件。"""
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                     dir=target_dir, prefix=os.path.basename(path) + ".tmp.") as f:
        f.write(content)
        tmp_path = f.name
    os.replace(tmp_path, path)


# ---------- HTTP 代理 ----------
def get_request_proxies():
    """返回 requests 使用的 proxies 字典；未配置时代理为空，行为与直连一致。

    只对订阅源与 EPG 抓取调用，测速、媒体探测不调用本函数。
    """
    proxy = (getattr(config, "http_proxy", "") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


# ---------- IP 类型判断 ----------
_ip_type_cache = {}


def get_ipv_type(url: str) -> str:
    """判断 URL 的 IP 类型，返回 'ipv6' 或 'ipv4'。

    字面 IPv6（http://[...]）直接判定；其余走 DNS 解析，按 host 缓存。
    解析失败兜底为 'ipv4'。
    """
    text = url or ""
    if re.match(r"^https?://\[[0-9a-fA-F:]+\]", text):
        return "ipv6"
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(text).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "ipv4"
    if host.startswith("["):
        return "ipv6"
    if host in _ip_type_cache:
        return _ip_type_cache[host]
    result = "ipv4"
    try:
        import socket
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if any(info[0] == socket.AF_INET6 for info in infos):
            result = "ipv6"
    except Exception:
        pass
    _ip_type_cache[host] = result
    return result


# ---------- 失效源自动停用 ----------
_URL_IN_LINE = re.compile(r"(https?://[^\s,\"']+)", re.IGNORECASE)


def extract_url(text: str) -> str:
    """从一行文本中提取首个 http(s) 地址，供失效源匹配使用。"""
    match = _URL_IN_LINE.search(text or "")
    return match.group(1).strip() if match else ""


def count_disabled_urls(path: str) -> int:
    """统计配置文件中已被 # 注释掉的地址行数。"""
    if not path or not os.path.exists(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("#"):
                    continue
                if extract_url(line.lstrip("#").strip()):
                    count += 1
    except Exception as e:
        logging.debug(f"统计停用地址失败 {path}: {e}")
    return count


def disable_urls_in_file(path: str, urls) -> dict:
    """把匹配的地址行加 "# " 注释写入配置文件（幂等），返回统计字典。

    分段重排逻辑：按 [SECTION] 分段，
    失效地址统一移到段末并加 "# "；已注释行重复调用不会叠加 #；
    非目标行保持原样。单个异常不会中断调用方。
    """
    targets = {str(u).strip() for u in (urls or []) if u and str(u).strip()}
    if not targets or not path or not os.path.exists(path):
        return {"disabled": 0, "active": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
        header_re = re.compile(r"^\s*\[.*\]\s*$")

        def new_section():
            return {"header": None, "misc": [], "active": [], "disabled": []}

        sections = [new_section()]
        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            if header_re.match(stripped):
                sections.append(new_section())
                sections[-1]["header"] = raw.rstrip("\r\n")
                continue
            current = sections[-1]
            indent = raw[: len(raw) - len(raw.lstrip())]
            if stripped.startswith("#"):
                content = stripped.lstrip("#").strip()
                url = extract_url(content)
                if url and url in targets:
                    current["disabled"].append((indent, content))
                else:
                    current["misc"].append(stripped)
                continue
            url = extract_url(stripped)
            if url and url in targets:
                current["disabled"].append((indent, stripped))
            else:
                current["active"].append(stripped)

        output_lines = []
        disabled_count = 0
        active_count = 0
        for section in sections:
            if section["header"] is not None:
                if output_lines and output_lines[-1] != "":
                    output_lines.append("")
                output_lines.append(section["header"])
            block = list(section["misc"]) + list(section["active"])
            active_count += len(section["active"])
            if section["disabled"]:
                if block and block[-1] != "":
                    block.append("")
                for indent, content in section["disabled"]:
                    block.append(f"{indent}# {content}")
                    disabled_count += 1
            cleaned, prev_blank = [], False
            for line in block:
                if not line.strip():
                    if not prev_blank:
                        cleaned.append("")
                    prev_blank = True
                else:
                    cleaned.append(line)
                    prev_blank = False
            output_lines.extend(cleaned)

        new_content = newline.join(output_lines)
        if lines and lines[-1].endswith(("\n", "\r")):
            new_content += newline
        if new_content != "".join(lines):
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        return {"disabled": disabled_count, "active": active_count}
    except Exception as e:
        logging.warning(f"自动停用写回失败 {path}: {e}")
        return {"disabled": 0, "active": 0}


# ---------- 归属地 / 运营商推断 ----------
def infer_location_isp(item: dict) -> tuple:
    """返回 (location, isp)。优先取源数据自带字段，其次用轻量映射按 host/IP 子串推断。

    本项目不携带 IP 地址库，映射为空时仅使用 item 自带字段，默认不改变任何输出。
    """
    location = (item.get("location") or "").strip() if isinstance(item, dict) else ""
    isp = (item.get("isp") or "").strip() if isinstance(item, dict) else ""
    if location and isp:
        return location, isp
    url = item.get("url") or "" if isinstance(item, dict) else ""
    mapping = getattr(config, "location_isp_map", {}) or {}
    if isinstance(mapping, dict) and url:
        for key, value in mapping.items():
            if key and key in url and isinstance(value, dict):
                if not location:
                    location = str(value.get("location") or "").strip()
                if not isp:
                    isp = str(value.get("isp") or "").strip()
                break
    return location, isp
