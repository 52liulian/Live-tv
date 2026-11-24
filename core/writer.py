"""结果输出。

写 output/ipv4 与 output/ipv6 分目录的 result.txt / result.m3u
（分类、纯地址行、yuanzl77 图库；线路备注由 open_line_note 开关控制）。
根目录不再产生 live.txt / live.m3u。另生成 output/epg 节目单。
采用内容比对去重与原子写，避免无意义重写和半截文件。
"""
import logging
import os
import re
from datetime import datetime

from core import config
from core import tools, epg
from core.alias import channel_alias

LOGO_BASE = "https://gcore.jsdelivr.net/gh/yuanzl77/TVlogo@master/png"


def is_ipv6(url: str) -> bool:
    return re.match(r"^https?://\[[0-9a-fA-F:]+\]", url or "") is not None


def format_url(url: str, index: int, total: int, keep_info=True, extra_info="") -> str:
    """生成地址行。

    默认（open_line_note=False）只返回纯地址，不附加任何 "$LIVE•IPV4『线路N』" 备注。
    open_line_note=True 时输出线路备注；open_url_info=True 时再追加
    来源/协议/分辨率说明（已有备注时用 "-" 连接，否则以 "$" 开头）。
    index / total / keep_info 为兼容性保留参数。
    """
    base = url.split("$", 1)[0]
    if not keep_info:
        return base
    result = base
    if getattr(config, "open_line_note", False):
        label = "IPV6" if is_ipv6(url) else "IPV4"
        suffix = f"$LIVE•{label}" if total == 1 else f"$LIVE•{label}『线路{index}』"
        result += suffix
    if getattr(config, "open_url_info", False) and extra_info:
        result += f"{'-' if '$' in result else '$'}{extra_info}"
    return result


def build_extra_info(item: dict, url: str) -> str:
    """拼接口说明信息：优先源自带说明，否则用来源名，再补协议与分辨率。"""
    parts = []
    custom = str(item.get("extra_info") or "").strip()
    origin = str(item.get("origin") or "").strip()
    origin_map = getattr(config, "origin_info_map", {}) or {}
    source = custom or origin_map.get(origin, "")
    if source:
        parts.append(source)
    parts.append("IPV6" if is_ipv6(url) else "IPV4")
    resolution = str(item.get("resolution") or "").strip()
    if resolution:
        parts.append(resolution)
    return "•".join(part for part in parts if part)


def _sort_key(item, ip_prefer, type_prefer=None, origin_prefer=None):
    """排序键：IP 偏好 -> 来源偏好 -> 速率（高优先）-> URL（稳定）。

    默认（type_prefer/origin_prefer 均为空）时与原有实现完全一致。
    """
    url = item.get("url") or ""
    ipv6 = is_ipv6(url)
    if type_prefer:
        actual = tools.get_ipv_type(url)
        try:
            primary = type_prefer.index(actual)
        except ValueError:
            primary = len(type_prefer)
    elif ip_prefer == "ipv4":
        primary = 1 if ipv6 else 0
    else:
        primary = 0 if ipv6 else 1
    keys = [primary]
    if origin_prefer:
        origin = item.get("origin") or ""
        try:
            keys.append(origin_prefer.index(origin))
        except ValueError:
            keys.append(len(origin_prefer))
    speed = item.get("speed") or 0
    keys.append(-speed)
    keys.append(url)
    return tuple(keys)


def _dedupe_and_limit(items, written, limit, ip_prefer, type_prefer=None, origin_prefer=None):
    """按偏好排序、IP 类型过滤、归属地/运营商筛选、限量。"""
    ipv_filter = getattr(config, "ipv_type", "all") or "all"
    loc_list = getattr(config, "location", []) or []
    isp_list = getattr(config, "isp", []) or []
    supply = getattr(config, "open_supply", False)
    primary, backup = [], []
    for item in sorted(items, key=lambda x: _sort_key(x, ip_prefer, type_prefer, origin_prefer)):
        url = (item.get("url") or "").strip()
        if not url or url in written:
            continue
        if tools.check_url_by_keywords(url):
            continue
        if ipv_filter in ("ipv4", "ipv6") and tools.get_ipv_type(url) != ipv_filter:
            continue
        location, isp = tools.infer_location_isp(item)
        demote = False
        if loc_list and location and not any(key in location for key in loc_list):
            demote = True
        if isp_list and isp and not any(key in isp for key in isp_list):
            demote = True
        if demote and not supply:
            continue
        if demote:
            backup.append(item)
        else:
            primary.append(item)
        written.add(url)
    result = primary + backup
    if limit:
        result = result[:limit]
    return result


def _epg_header_urls():
    """m3u 首行 x-tvg-url 的取值：优先本项目合并后的 EPG，取不到则用原始配置源。"""
    mode = str(getattr(config, "epg_header_mode", "self") or "self").lower()
    base = str(getattr(config, "pages_base_url", "") or "").strip().rstrip("/")
    if mode == "self" and base and getattr(config, "open_epg", True):
        return [base + "/epg.xml"]
    return list(getattr(config, "epg_urls", []) or [])


def build_content(channel_data, template_channels, epg_urls=None):
    """构建主 txt 与 m3u 内容，返回 (txt_text, m3u_text, m3u_data)。

    m3u_data 供 convert_to_m3u 风格的字段回填（保留 headers/logo/catchup）。
    """
    epg_urls = epg_urls if epg_urls is not None else _epg_header_urls()
    limit = getattr(config, "url_limit_per_channel", 0) or getattr(config, "output_urls_limit", 0)
    ip_prefer = getattr(config, "ip_version_priority", "ipv6")
    type_prefer = list(getattr(config, "ipv_type_prefer", []) or [])
    if any(pref == "auto" for pref in type_prefer):
        has_ipv6 = bool(getattr(config, "ipv6_support", False)) or any(
            is_ipv6((item or {}).get("url", ""))
            for items in channel_data.values() for item in (items or [])
        )
        type_prefer = ["ipv6", "ipv4"] if has_ipv6 else ["ipv4", "ipv6"]
    origin_prefer = list(getattr(config, "origin_type_prefer", []) or [])

    txt_lines = []
    m3u_lines = []
    m3u_data = {}
    written = set()

    tvg = ",".join(f'"{u}"' for u in epg_urls)
    m3u_lines.append(f"#EXTM3U x-tvg-url={tvg}" if epg_urls else "#EXTM3U")

    # 频道区：按模板顺序输出，从 channel_data 按主名取线路
    empty_channels = []
    for category, names in template_channels.items():
        txt_lines.append(f"{category},#genre#")
        for name in names:
            primary = channel_alias.get_primary(name)
            items = channel_data.get(primary, []) or channel_data.get(name, [])
            kept = _dedupe_and_limit(items, written, limit, ip_prefer, type_prefer, origin_prefer)
            if not kept:
                empty_channels.append((category, name))
                continue
            total = len(kept)
            for idx, item in enumerate(kept, start=1):
                new_url = format_url(item["url"], idx, total,
                                     extra_info=build_extra_info(item, item["url"]))
                logo = f"{LOGO_BASE}/{name}.png"
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" '
                    f'group-title="{category}",{name}'
                )
                m3u_lines.append(new_url)
                txt_lines.append(f"{name},{new_url}")
                m3u_data.setdefault(name, []).append(item)

    # 可选分类（仅写 txt，便于排查）
    if getattr(config, "open_unmatched_category", False):
        unmatched = channel_data.get("未匹配", [])
        if unmatched:
            txt_lines.append("未匹配,#genre#")
            for item in unmatched[:50]:
                txt_lines.append(f"{item.get('name', '未命名')},{item.get('url', '')}")
    if getattr(config, "open_empty_category", False) and empty_channels:
        txt_lines.append("空结果,#genre#")
        for _, name in empty_channels:
            txt_lines.append(f"{name},")

    # 更新时间分类
    if getattr(config, "open_update_time", False):
        title = "更新时间"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sample = ""
        for items in channel_data.values():
            for item in items or []:
                url = (item or {}).get("url", "")
                if url:
                    sample = format_url(url, 1, 1, extra_info=build_extra_info(item, url))
                    break
            if sample:
                break
        update_txt = [f"{title},#genre#", f"{now},{sample}"]
        update_m3u = [
            f'#EXTINF:-1 tvg-id="1" tvg-name="{now}" tvg-logo="" group-title="{title}",{now}',
            sample,
        ]
        if getattr(config, "update_time_position", "top") == "bottom":
            txt_lines += [""] + update_txt
            m3u_lines += update_m3u
        else:
            # M3U 头部必须保持在第一行
            txt_lines = update_txt + [""] + txt_lines
            m3u_lines = [m3u_lines[0]] + update_m3u + m3u_lines[1:]

    txt_text = "\n".join(txt_lines) + "\n"
    m3u_text = "\n".join(m3u_lines) + "\n"
    return txt_text, m3u_text, m3u_data, empty_channels


def _write_if_changed(path, content):
    """内容未变则不写盘（直接比对现有文件，不再产生 .digest 标记文件）。"""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return False
        except (OSError, UnicodeDecodeError):
            pass
    tools.atomic_write(path, content)
    return True


def write_results(channel_data, template_channels, epg_programmes=None):
    """写全部输出文件，返回统计 dict。

    结果只落在 output/ipv4、output/ipv6 分目录（result.txt / result.m3u），
    根目录不再产生 live.txt / live.m3u。
    """
    empty = sum(1 for items in channel_data.values() if not items)
    for version in ("ipv4", "ipv6"):
        subset = {}
        for name, items in channel_data.items():
            filtered = [i for i in items if (is_ipv6(i.get("url", "")) == (version == "ipv6"))]
            if filtered:
                subset[name] = filtered
        txt, m3u, _, _ = build_content(subset, template_channels)
        _write_if_changed(os.path.join("output", version, "result.txt"), txt)
        _write_if_changed(os.path.join("output", version, "result.m3u"), m3u)

    # EPG
    if epg_programmes:
        epg.write_epg(epg_programmes)

    logging.info(f"输出完成，空结果频道 {empty} 个")
    return {"empty_channels": empty}
