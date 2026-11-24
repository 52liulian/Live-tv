"""从 config/ 目录下的 txt 文件加载数据。
  subscribe.txt  每行一个订阅地址，可选跟一个 UA="xxx"；[WHITELIST] 段后为白名单订阅源
  blacklist.txt  每行一个黑名单关键词，# 开头为注释
  epg.txt        每行一个 EPG 地址，# 开头为注释
  alias.txt      每行一个别名，# 开头为注释
  demo.txt       频道模板（由主程序自行解析）
  alias.txt      完整别名表（由 core/alias.py 自行解析）
  whitelist.txt  可选，每行 "频道名,接口地址" 或 "接口地址"，# 开头为注释

加载结果写回 config 包命名空间，保持与旧 config.py 相同的变量名。
"""
import os
import re

# 数据 txt 统一放在项目根的 config/ 目录（该目录只存数据文件，不放任何 .py）
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")

_UA_RE = re.compile(r'(?i)^ua\s*=\s*"?([^"]+?)"?$')


def _read_lines(filename):
    """读取一个按行维护的列表文件，跳过空行和注释。"""
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return []
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _parse_subscribe(filename="subscribe.txt"):
    """解析订阅源，返回 (全部地址, UA 映射, 白名单地址集合)。"""
    path = os.path.join(CONFIG_DIR, filename)
    urls, headers_map, whitelist = [], {}, set()
    if not os.path.exists(path):
        return urls, headers_map, whitelist

    section = "normal"
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().upper()
                continue
            parts = line.split()
            if not parts:
                continue
            url = parts[0].strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            for token in parts[1:]:
                match = _UA_RE.match(token)
                if match and match.group(1).strip():
                    headers_map[url] = {"User-Agent": match.group(1).strip()}
            if section == "WHITELIST":
                whitelist.add(url)
            if url not in urls:
                urls.append(url)
    return urls, headers_map, whitelist


def _parse_whitelist(filename="whitelist.txt"):
    """解析接口白名单，返回 (exact表, keywords表) 两个字典。

    解析规则：
      - 每行 "频道名,地址" 或 "地址"（后者为全局，'' 键）
      - [KEYWORDS] 段内的条目进关键词表，段外进精确表
      - 支持 [WHITELIST] 段标记（段外默认仍属精确表）
    """
    import re
    urls, keywords = {}, {}
    in_keywords = False
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return urls, keywords
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"^\[.*]$", line):
                in_keywords = line.upper() == "[KEYWORDS]"
                continue
            if "," in line:
                channel, value = map(str.strip, line.split(",", 1))
                key = channel or ""
            else:
                key, value = "", line
            if not value:
                continue
            target = keywords if in_keywords else urls
            if value not in target.setdefault(key, []):
                target[key].append(value)
    return urls, keywords


def load_all(namespace):
    """把 txt 数据注入 config 包的命名空间。"""
    source_urls, source_headers, whitelist_sources = _parse_subscribe()
    namespace["source_urls"] = source_urls
    namespace["source_url_headers"] = source_headers
    namespace["whitelist_subscribe_urls"] = sorted(whitelist_sources)

    namespace["url_blacklist"] = _read_lines("blacklist.txt")
    namespace["epg_urls"] = _read_lines("epg.txt")

    demo_path = os.path.join(CONFIG_DIR, "demo.txt")
    namespace["source_file"] = demo_path if os.path.exists(demo_path) else os.path.join(CONFIG_DIR, "..", "demo.txt")

    if os.path.exists(os.path.join(CONFIG_DIR, "whitelist.txt")):
        wl_urls, wl_keywords = _parse_whitelist()
        if wl_urls:
            namespace["whitelist_urls"] = wl_urls
        if wl_keywords:
            namespace["whitelist_keywords"] = wl_keywords
