"""订阅源采集。

并发拉取 config.source_urls，兼容 m3u/txt，提取 headers/tvg-logo/catchup，
经别名归一化后返回 {主频道名: [ChannelData]}。含 CDN 候选重试、稳定 ID 去重、
冻结过滤。网络层用 requests 线程池。
"""
import logging
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from core import config
from core import tools, frozen
from core.identity import stable_result_id

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _base_headers() -> dict:
    ua = getattr(config, "user_agent", "") or DEFAULT_UA
    return {"User-Agent": ua}


def _channel_item_key(item) -> tuple:
    return (
        stable_result_id(item.get("url") or "", item.get("headers")),
        tuple(sorted((str(k), str(v)) for k, v in (item.get("catchup") or {}).items() if v is not None)),
        item.get("tvg_logo"),
        item.get("extra_info", ""),
    )


def _build_item(entry, source_headers=None):
    url, _, extra_info = (entry.get("value") or "").strip().partition("$")
    headers = {**(source_headers or {}), **(entry.get("headers") or {})}
    return {
        "url": url.strip(),
        "headers": headers or None,
        "catchup": entry.get("catchup") or None,
        "tvg_logo": entry.get("tvg_logo") or None,
        "extra_info": extra_info,
    }


def _fetch_content(url, timeout, retries=2, headers=None):
    """依次尝试 CDN 候选，返回 (响应文本, 错误信息)。

    单个源设总时间预算（connection+read 用元组超时，整体最多约 2*timeout），
    避免慢源长时间占用线程拖垮整轮采集。HTTP 代理只作用于此处（采集），
    不作用于测速。
    """
    headers = headers or _base_headers()
    candidates = tools.get_request_url_candidates(url)[:2]  # 最多试原始地址 + 1 个镜像
    last_error = None
    connect_timeout = min(timeout, getattr(config, "connect_timeout", 12) or 12)
    proxies = tools.get_request_proxies()
    for candidate in candidates:
        for attempt in range(retries + 1):
            try:
                response = requests.get(candidate, headers=headers,
                                        timeout=(connect_timeout, timeout), proxies=proxies,
                                        verify=getattr(config, "ssl_verify", True))
                response.raise_for_status()
                response.encoding = "utf-8"
                if response.text and response.text.strip():
                    return response.text, None
                last_error = "empty_content"
            except Exception as e:
                last_error = e
    logging.debug(f"订阅源拉取失败 {url}: {last_error}")
    return None, str(last_error or "request_failed")


def fetch_one(url, whitelisted=False):
    """拉取并解析单个订阅源，返回 ({主频道名: [item]}, EPG 地址列表, 停用原因)。

    停用原因非空表示该源失效（请求失败/内容为空/未匹配到频道），供上层汇总回写。
    whitelisted=True 表示该源来自 subscribe.txt 的 [WHITELIST] 段，
    其接口不参与测速并始终保留在结果最前。
    """
    result = OrderedDict()
    timeout = getattr(config, "request_timeout", 15)
    source_headers = (getattr(config, "source_url_headers", {}) or {}).get(url)
    content, error = _fetch_content(url, timeout, headers=source_headers)
    if not content:
        reason = "内容为空" if error == "empty_content" else "请求失败"
        logging.warning(f"订阅源无内容: {url}（{reason}）")
        return result, [], reason

    open_headers = getattr(config, "open_headers", True)
    entries, is_m3u = tools.parse_subscribe_content(content, open_headers=open_headers)
    discovered_epg = tools.get_m3u_epg_urls(content) if is_m3u else []

    for entry in entries:
        raw_name = entry.get("name", "")
        if not raw_name:
            continue
        primary = tools.format_channel_name(raw_name)
        item = _build_item(entry)
        if not item["url"]:
            continue
        if whitelisted:
            item["origin"] = "whitelist"
        result.setdefault(primary, []).append(item)

    reason = None
    if not result:
        reason = "没有匹配到符合条件的值"
        logging.warning(f"订阅源未匹配到频道: {url}（{reason}）")
    else:
        logging.info(f"订阅源解析成功: {url} ({'m3u' if is_m3u else 'txt'}, {len(entries)} 条)")
    return result, discovered_epg, reason


def fetch_subscribe(source_urls=None):
    """并发采集全部订阅源。

    返回 (subscribe_result, discovered_epg_urls)。
    subscribe_result = {主频道名: [ChannelData]}，已按稳定 ID 去重并过滤冻结源。
    当 open_auto_disable_source=True 时，把失效源地址回写到 config/subscribe.txt。
    """
    source_urls = source_urls if source_urls is not None else config.source_urls
    unique = list(dict.fromkeys(u for u in source_urls if u))
    whitelist_sources = set(getattr(config, "whitelist_subscribe_urls", []) or [])
    workers = getattr(config, "fetch_workers", 16)
    logging.info(f"开始采集 {len(unique)} 个订阅源（白名单源 {len(whitelist_sources)} 个），并发 {workers}")

    merged = OrderedDict()
    seen = {}
    discovered = []
    disabled = {}  # source_url -> 失效原因
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, url, url in whitelist_sources): url for url in unique}
        for future in as_completed(futures):
            source_url = futures[future]
            try:
                partial, epg_urls, reason = future.result()
            except Exception as e:
                logging.error(f"订阅源任务异常 {source_url}: {e}")
                disabled[source_url] = "任务异常"
                continue
            if reason:
                disabled[source_url] = reason
                logging.warning(f"订阅源失效已记录: {source_url}（{reason}）")
            for epg in epg_urls:
                if epg not in discovered:
                    discovered.append(epg)
            for name, items in partial.items():
                bucket = merged.setdefault(name, [])
                name_seen = seen.setdefault(name, set())
                for item in items:
                    if frozen.is_url_frozen(item["url"]):
                        continue
                    key = _channel_item_key(item)
                    if key in name_seen:
                        continue
                    name_seen.add(key)
                    item["id"] = stable_result_id(item["url"], item.get("headers"))
                    item.setdefault("origin", "subscribe")
                    bucket.append(item)

    if disabled and getattr(config, "open_auto_disable_source", False):
        subscribe_path = os.path.join(config.CONFIG_DIR, "subscribe.txt")
        counts = tools.disable_urls_in_file(subscribe_path, disabled.keys())
        logging.info(f"自动停用订阅源完成：有效地址 {counts['active']} 个，停用地址 {counts['disabled']} 个")

    logging.info(f"订阅采集完成，命中频道 {len(merged)} 个，线路合计 {sum(len(v) for v in merged.values())} 条")
    return merged, discovered
