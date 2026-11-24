"""Live-tv 直播源聚合更新入口（完整管线版）。

五阶段：准备 -> 采集 -> 聚合 -> 测速 -> 输出。
各阶段实现位于 core/ 包，本文件只做编排。
结果只产出 output/ipv4、output/ipv6 分目录（result.txt / result.m3u），
根目录不再生成 live.m3u / live.txt。
"""
import logging
import os
import time
from core import config
from core import validator
from core import alias, cache, epg, frozen, reporting, run_state, speed, subscribe, tools, whitelist, writer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
# 测速时大量死域名会触发 aiohttp DNS 解析失败,asyncio 库对每条都打 ERROR 全堆栈,
# 属预期噪音(该线路会被标记不可达后继续),这里静音,不影响真正的错误排查。
logging.getLogger("asyncio").setLevel(logging.CRITICAL)


def prepare():
    """阶段一：加载模板、黑白名单、历史缓存与冻结状态。"""
    template_file = getattr(config, "source_file", "demo.txt") or "demo.txt"
    if not os.path.exists(template_file):
        template_file = "demo.txt"
    template = _parse_template(template_file)
    whitelist_maps = whitelist.load_whitelist_maps()
    previous = cache.load(getattr(config, "cache_file", "")) if getattr(config, "open_history", True) else {}
    frozen.load(getattr(config, "frozen_file", ""))
    channel_names = {name for names in template.values() for name in names}
    return template, whitelist_maps, previous, channel_names


def _parse_template(path):
    """解析频道模板为 {分类: [频道名]}。"""
    result = {}
    category = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#genre#" in line:
                category = line.split(",")[0].strip()
                result[category] = []
            elif category:
                name = line.split(",")[0].strip()
                if name:
                    result[category].append(name)
    return result


def collect(channel_names):
    """阶段二：并发采集订阅源；可选发现 EPG 地址。"""
    if not getattr(config, "open_subscribe", True):
        return {}, []
    return subscribe.fetch_subscribe(config.source_urls)


def aggregate(subscribe_result, whitelist_maps, previous, channel_names):
    """阶段三：套用黑白名单。返回 (本轮候选, 历史候选)。

    本轮候选送去测速，历史候选在测速后补回，避免重复测速拖慢更新。
    """
    data = {}
    for name, items in subscribe_result.items():
        bucket = []
        for item in items:
            if tools.check_url_by_keywords(item.get("url", "")):
                continue
            if item.get("origin") == "whitelist" or whitelist.is_url_whitelisted(whitelist_maps, item.get("url", ""), name):
                item["origin"] = "whitelist"
            bucket.append(item)
        if bucket:
            data[name] = bucket

    # 频道白名单：非空时只保留名单内频道
    channel_whitelist = getattr(config, "channel_whitelist", [])
    if channel_whitelist:
        allowed = set(channel_whitelist) | {tools.format_channel_name(n) for n in channel_whitelist}
        data = {name: items for name, items in data.items() if name in allowed}

    return data


def run_speed_test(data, base_snapshot, previous, channel_names):
    """阶段四：对模板频道测速，随后补回未测的历史线路。

    base_snapshot 为本轮测速前的原始候选，用于判定哪些线路是"本轮采集到但被
    早停跳过"的；只有这些才允许用上一轮的有效版本补回。
    """
    if not getattr(config, "open_update", True):
        return data
    # 模板频道名 -> 主名，作为测速范围
    scope = {alias.channel_alias.get_primary(name) for name in channel_names}
    speed.speed_test_all(data, on_result=lambda r: _on_speed_result(r), only_names=scope)

    # 精确补回未测的历史线路
    if getattr(config, "open_history", True) and previous:
        merged = cache.preserve_unmeasured_history(data, previous, base_snapshot)
        data.clear()
        data.update(merged)
    return data


def _on_speed_result(result):
    """测速回调：按有效性更新冻结状态。"""
    url = result.get("url")
    if not url:
        return
    if result.get("test_status") == "measured":
        frozen.mark_url_good(url)
    elif result.get("test_status") in ("unreachable", "ad", "timeout", "request_error"):
        frozen.mark_url_bad(url)


def write_output(data, template, epg_programmes):
    """阶段五：写结果文件。"""
    return writer.write_results(data, template, epg_programmes)


def main():
    started = time.time()
    # 配置:env 覆盖 -> 校验(非法项带拼写建议回退默认)
    validator.apply_env_overrides(config)
    validator.validate_settings(config)

    # 结构化日志:run_id + 阶段标记 + JSONL 双写镜像
    reporter = reporting.Reporter(run_id=time.strftime("%Y%m%d%H%M%S"))
    mirror = reporting.JsonlMirrorHandler(reporter)
    logging.getLogger().addHandler(mirror)

    run_state.write_run_state("running", started=started)
    logging.info("========== 开始更新 ==========")
    try:
        if not getattr(config, "open_update", True):
            logging.info("open_update=False，跳过本次更新")
            run_state.write_run_state("skipped", reason="open_update_false")
            return

        reporter.set_phase("prepare")
        template, whitelist_maps, previous, channel_names = prepare()
        logging.info(f"模板加载完成，{len(channel_names)} 个频道")

        reporter.set_phase("collect")
        subscribe_result, discovered_epg = collect(channel_names)
        data = aggregate(subscribe_result, whitelist_maps, previous, channel_names)
        # 测速前快照：历史补回只针对本轮真实采集到的线路
        base_snapshot = {name: list(items) for name, items in data.items()}

        reporter.set_phase("epg")
        # EPG：配置源 + 订阅发现源
        epg_programmes = {}
        if getattr(config, "open_epg", True):
            extra = discovered_epg if getattr(config, "open_subscribe_epg", False) else []
            epg_programmes = epg.get_epg(config.epg_urls, extra, channel_names)

        reporter.set_phase("speed_test")
        run_speed_test(data, base_snapshot, previous, channel_names)

        reporter.set_phase("output")
        stats = write_output(data, template, epg_programmes)

        # 持久化缓存与冻结状态
        cache.save(getattr(config, "cache_file", ""), data)
        frozen.save(getattr(config, "frozen_file", ""))

        elapsed = round(time.time() - started, 1)
        logging.info(f"========== 更新完成，用时 {elapsed}s，空结果频道 {stats['empty_channels']} 个 ==========")
        run_state.write_run_state(
            "success",
            duration=elapsed,
            empty_channels=stats.get("empty_channels"),
            aggregated_channels=len(data),
        )
    except Exception as exc:
        logging.exception(f"更新失败:{exc}")
        run_state.write_run_state("failed", error=str(exc), duration=round(time.time() - started, 1))
        raise
    finally:
        reporter.close()


if __name__ == "__main__":
    main()
