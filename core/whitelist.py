"""黑白名单匹配。

白名单支持频道专属/全局、精确/关键词四个层级，优先级：
频道精确 -> 全局精确 -> 频道关键词 -> 全局关键词。
"""
from collections import defaultdict

from core import config


def _dedupe(values):
    seen, result = set(), []
    for value in values:
        item = (value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def load_whitelist_maps():
    """从 config 变量构建 (exact, keywords) 两张表，键为频道名，空串为全局。"""
    exact = defaultdict(list)
    keywords = defaultdict(list)

    whitelist_urls = getattr(config, "whitelist_urls", {}) or {}
    for name, values in whitelist_urls.items():
        key = (name or "").strip()
        for value in values:
            value = (value or "").strip()
            if value:
                if value not in exact[key]:
                    exact[key].append(value)

    keyword_urls = getattr(config, "whitelist_keywords", {}) or {}
    for name, values in keyword_urls.items():
        key = (name or "").strip()
        for value in values:
            value = (value or "").strip()
            if value:
                if value not in keywords[key]:
                    keywords[key].append(value)

    return exact, keywords


def is_url_whitelisted(data_map, url: str, channel_name: str | None = None) -> bool:
    if not url or not data_map:
        return False
    exact_map, keyword_map = data_map
    key = channel_name or ""

    def check_exact(k):
        for candidate in exact_map.get(k, []):
            if candidate and candidate.strip() == url:
                return True
        return False

    if check_exact(key) or check_exact(""):
        return True
    for kw in keyword_map.get(key, []) + keyword_map.get("", []):
        if kw and kw in url:
            return True
    return False


def get_whitelist_url(data_map, channel_name: str | None = None):
    exact_map, _ = data_map
    key = channel_name or ""
    return _dedupe(exact_map.get(key, []) + exact_map.get("", []))


def get_whitelist_total_count(data_map) -> int:
    exact_map, keyword_map = data_map
    entries = set()
    for values in list(exact_map.values()) + list(keyword_map.values()):
        for value in values:
            entries.add((value or "").strip())
    return len(entries)
