"""测速与筛选。

测速主流程：
  - get_speed_with_download：流式下载 + 稳定性窗口判定速率
  - get_headers / get_url_content：HEAD 跟随重定向、限长读取播放列表
  - get_result：HEAD→Location 重定向、m3u8 master/media 解析、
    取分辨率/帧率、广告过滤、尾部 TS 分片抽样测速、ffprobe 兜底
  - is_ad_playlist：关键词 + ENDLIST 短循环判定
  - probe_url：ffprobe 读分辨率/帧率/编解码
  - get_sort_result：多维排序
网络层用 aiohttp 异步并发，外层仍提供同步接口给 main 编排调用，
并保留 quick 早停、每频道上限、off/full 模式这些工程化控制。
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from time import time as _time
from urllib.parse import quote, urljoin

import aiohttp
import m3u8
import requests
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from core import config
from core import quality, tools
from core.identity import stable_result_id

# ---------- 测速常量 ----------
MIN_MEASURE_TIME = 1.0
STABILITY_WINDOW = 4
STABILITY_THRESHOLD = 0.12
SEGMENT_SAMPLE_LIMIT = 2
PLAYLIST_MAX_BYTES = 2 * 1024 * 1024
STREAM_SAMPLE_MAX_BYTES = 4 * 1024 * 1024
STREAM_SAMPLE_MAX_SECONDS = 2.0
AD_MAX_LOOP_DURATION = 90
DEFAULT_IPv6_DELAY = 0.1
DEFAULT_IPv6_RESOLUTION = "1920x1080"
DEFAULT_IPv6_RESULT = {
    "speed": float("inf"),
    "delay": DEFAULT_IPv6_DELAY,
    "resolution": DEFAULT_IPv6_RESOLUTION,
}
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 这些常量允许在 config 里覆盖，取不到时用内置默认值。
def _cfg(name, default):
    return getattr(config, name, default)


def _m3u8_content_types():
    return _cfg("m3u8_content_types", [
        "application/x-mpegurl", "application/vnd.apple.mpegurl",
        "audio/mpegurl", "audio/x-mpegurl",
    ])


def _ad_keywords():
    return _cfg("ad_filter_keywords", [
        "no_signal", "nosignal", "no-signal", "signal_offline", "no_video",
        "novideo", "advertisement", "advert", "placeholder", "default_video",
        "cctv_off", "/ad/", "/ads/",
    ])


def is_ipv6_url(url: str) -> bool:
    return bool(re.match(r"^https?://\[[0-9a-fA-F:]+\]", url or ""))


def _headers_for(headers=None):
    """合并基础 UA 与线路自带 headers 的请求头。"""
    result = {"User-Agent": _cfg("user_agent", "") or DEFAULT_UA}
    if headers:
        result.update(headers)
    return result


def _session_kwargs():
    # trust_env=False，避免本机代理干扰；ssl=False 容忍自签证书。
    return {"connector": TCPConnector(ssl=False), "trust_env": False}


# ---------- 1. 流式下载测速 ----------
async def get_speed_with_download(url, headers=None, session=None, timeout=None,
                                  semaphore=None, max_bytes=None, max_duration=None):
    created = False
    if session is None:
        session = ClientSession(**_session_kwargs())
        created = True
    timeout = timeout or _cfg("speed_test_timeout", 10)
    max_bytes = STREAM_SAMPLE_MAX_BYTES if max_bytes is None else max_bytes
    max_duration = STREAM_SAMPLE_MAX_SECONDS if max_duration is None else max_duration
    request_timeout = min(timeout, max_duration) if max_duration else timeout

    start_time = _time()
    delay = -1
    total_size = 0
    min_bytes = 64 * 1024
    last_sample_time = start_time
    last_sample_size = 0
    speed_samples = deque(maxlen=STABILITY_WINDOW)
    try:
        async with session.get(url, headers=headers or {}, timeout=request_timeout) as response:
            if response.status != 200:
                raise Exception("Invalid response")
            delay = int(round((_time() - start_time) * 1000))
            async for chunk in response.content.iter_chunked(64 * 1024):
                if not chunk:
                    continue
                total_size += len(chunk)
                now = _time()
                elapsed = now - start_time
                delta_t = now - last_sample_time
                delta_b = total_size - last_sample_size
                if delta_t > 0 and delta_b > 0:
                    speed_samples.append(delta_b / delta_t / 1024.0 / 1024.0)
                    last_sample_time, last_sample_size = now, total_size
                if (max_bytes and total_size >= max_bytes) or (max_duration and elapsed >= max_duration):
                    break
                if (elapsed >= MIN_MEASURE_TIME and total_size >= min_bytes
                        and len(speed_samples) >= STABILITY_WINDOW):
                    mean = sum(speed_samples) / len(speed_samples)
                    if mean > 0 and (max(speed_samples) - min(speed_samples)) / mean < STABILITY_THRESHOLD:
                        break
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if created:
            await session.close()
    total_time = max(_time() - start_time, 0.001)
    return {
        "speed": total_size / total_time / 1024 / 1024 if total_time > 0 else 0.0,
        "delay": delay,
        "size": total_size,
        "time": total_time,
    }


async def get_headers(url, headers=None, session=None, timeout=3):
    created = False
    if session is None:
        session = ClientSession(**_session_kwargs())
        created = True
    res_headers = {}
    try:
        async with session.head(url, headers=headers or {}, timeout=timeout,
                                allow_redirects=False) as response:
            res_headers = dict(response.headers)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if created:
            await session.close()
    return res_headers


async def get_url_content(url, headers=None, session=None, timeout=None):
    created = False
    if session is None:
        session = ClientSession(**_session_kwargs())
        created = True
    timeout = timeout or _cfg("speed_test_timeout", 10)
    content = ""
    try:
        async with session.get(url, headers=headers or {}, timeout=timeout) as response:
            if response.status == 200:
                payload = await response.content.read(PLAYLIST_MAX_BYTES + 1)
                if len(payload) > PLAYLIST_MAX_BYTES:
                    raise Exception("Response too large")
                content = payload.decode(response.charset or "utf-8", errors="replace")
            else:
                raise Exception("Invalid response")
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if created:
            await session.close()
    return content


def check_m3u8_valid(headers: dict) -> bool:
    content_type = headers.get("Content-Type", "").lower()
    if not content_type:
        return False
    return any(item in content_type for item in _m3u8_content_types())


# ---------- 2. 广告判定 ----------
def is_ad_playlist(media_playlist, base_url: str = "") -> bool:
    """media_playlist 为 m3u8.M3U8 对象；也兼容纯文本与段 URI 列表。"""
    segments = getattr(media_playlist, "segments", None)
    if segments is None and isinstance(media_playlist, str):
        # 文本兜底：转成假段列表，尽量保持同一判定
        uris = re.findall(r"^\s*(?!#)([^#\s]+\.ts[^#\s]*)", media_playlist, re.MULTILINE)
        durations = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", media_playlist)]
        segments = [type("Seg", (), {"uri": u, "duration": d})()
                    for u, d in zip(uris + [""] * len(durations), durations)]
    if not segments:
        return False
    haystack = (base_url + " " + " ".join(seg.uri or "" for seg in segments)).lower()
    if any(kw in haystack for kw in _ad_keywords()):
        return True
    if getattr(media_playlist, "is_endlist", False):
        total_duration = sum(seg.duration or 0 for seg in segments)
        if 0 < total_duration <= AD_MAX_LOOP_DURATION:
            return True
    return False


def _parse_time_to_seconds(t: str) -> float:
    """解析冒号分隔的时间字符串。"""
    if not t:
        return 0.0
    parts = [p.strip() for p in t.split(":") if p.strip() != ""]
    if not parts:
        return 0.0
    try:
        total = 0.0
        for i, part in enumerate(reversed(parts)):
            total += float(part) * (60 ** i)
        return total
    except Exception:
        return 0.0


# ---------- 3. ffprobe 探测 ----------
def _parse_probe_data(data):
    if not data:
        return None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def safe(d, key):
        return d.get(key) if d else None

    def parse_rate(rate):
        try:
            if not rate:
                return None
            if isinstance(rate, (int, float)):
                return float(rate)
            if "/" in rate:
                num, den = rate.split("/")
                return float(num) / float(den) if float(den) else None
            return float(rate)
        except Exception:
            return None

    fps = parse_rate(safe(video, "avg_frame_rate") or safe(video, "r_frame_rate"))
    w, h = safe(video, "width"), safe(video, "height")
    return {
        "video_codec": safe(video, "codec_name"),
        "audio_codec": safe(audio, "codec_name"),
        "resolution": f"{w}x{h}" if w and h else None,
        "fps": fps,
    }


def probe_url(url, headers=None, timeout=None):
    """同步 ffprobe，返回 {video_codec,audio_codec,resolution,fps} 或 None。"""
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    timeout = timeout or _cfg("speed_test_timeout", 10)
    header_str = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    args = [
        executable, "-v", "error", "-probesize", "512000", "-analyzeduration", "1000000",
        "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-print_format", "json",
    ]
    if header_str:
        args += ["-headers", header_str]
    args += [url]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.stdout:
            return _parse_probe_data(json.loads(result.stdout))
    except Exception:
        pass
    return None


# ---------- 4. get_result：m3u8 master/media 解析与分片抽样 ----------
async def get_result(url, headers=None, resolution=None,
                     filter_resolution=None, timeout=None, session=None,
                     redirects_remaining=5):
    created = False
    if session is None:
        session = ClientSession(**_session_kwargs())
        created = True
    timeout = timeout or _cfg("speed_test_timeout", 10)
    if filter_resolution is None:
        filter_resolution = _cfg("open_filter_resolution", False)

    info = {"speed": 0.0, "delay": -1, "resolution": resolution}
    location = None
    segment_urls = []
    try:
        url = quote(url, safe=":/?$&=@[]%").partition("$")[0]
        res_headers = await get_headers(url, headers, session, timeout=3)
        location = res_headers.get("Location") if res_headers else None
        if location:
            if redirects_remaining <= 0:
                raise Exception("Too many redirects")
            info.update(await get_result(
                urljoin(url, location), headers, resolution, filter_resolution,
                timeout, session=session, redirects_remaining=redirects_remaining - 1,
            ))
        else:
            should_parse_m3u8 = (".m3u8" in url.lower()) or check_m3u8_valid(res_headers)
            url_content = ""
            if should_parse_m3u8:
                url_content = await get_url_content(url, headers, session, timeout)
            if should_parse_m3u8 and url_content:
                m3u8_obj = m3u8.loads(url_content)
                playlists = m3u8_obj.playlists
                segments = m3u8_obj.segments
                if playlists:
                    best = max(playlists, key=lambda p: p.stream_info.bandwidth or 0)
                    stream_resolution = getattr(best.stream_info, "resolution", None)
                    if stream_resolution and len(stream_resolution) == 2:
                        info["resolution"] = f"{stream_resolution[0]}x{stream_resolution[1]}"
                    frame_rate = getattr(best.stream_info, "frame_rate", None)
                    if frame_rate:
                        info["fps"] = frame_rate
                    playlist_url = urljoin(url, best.uri)
                    playlist_content = await get_url_content(playlist_url, headers, session, timeout)
                    if playlist_content:
                        media_playlist = m3u8.loads(playlist_content)
                        if _cfg("open_filter_ad", True) and is_ad_playlist(media_playlist, playlist_url):
                            raise Exception("Ad source filtered")
                        segment_urls = [urljoin(playlist_url, seg.uri) for seg in media_playlist.segments]
                else:
                    if _cfg("open_filter_ad", True) and is_ad_playlist(m3u8_obj, url):
                        raise Exception("Ad source filtered")
                    segment_urls = [urljoin(url, seg.uri) for seg in segments]
                if not segment_urls:
                    raise Exception("Segment urls not found")
            else:
                res = await get_speed_with_download(url, headers, session, timeout)
                info.update({"speed": res["speed"], "delay": res["delay"]})

            if segment_urls:
                sampled = sample_segment_urls(segment_urls, _cfg("segment_sample_limit", SEGMENT_SAMPLE_LIMIT))
                tasks = [get_speed_with_download(u, headers, session, timeout) for u in sampled]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                valid = [r for r in results if isinstance(r, dict) and r.get("size") and r.get("time")]
                total_size = sum(r["size"] for r in valid)
                total_time = sum(r["time"] for r in valid)
                info["speed"] = total_size / total_time / 1024 / 1024 if total_time > 0 else 0
                delays = [r["delay"] for r in valid if r.get("delay", -1) >= 0]
                info["delay"] = int(sum(delays) / len(delays)) if delays else -1
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    # ffprobe 兜底：判定与主流程一致
    min_speed_value = _cfg("min_speed", 0.5)
    res_map = _cfg("resolution_speed_map", {}) or {}
    probe_threshold = min([min_speed_value, *res_map.values()], default=min_speed_value)
    open_supply = _cfg("open_supply", False)
    open_filter_speed = _cfg("open_filter_speed", False)
    should_probe = open_supply or not open_filter_speed or (info.get("speed") or 0) >= probe_threshold
    if (filter_resolution and should_probe and not location
            and not info.get("resolution") and info.get("delay") != -1):
        probed = probe_url(url, headers, timeout=timeout)
        if probed:
            for key in ("resolution", "fps", "video_codec", "audio_codec"):
                if probed.get(key) is not None:
                    info[key] = probed[key]
    finally_close(session, created)
    return info


def finally_close(session, created):
    if created and not session.closed:
        # 同步关闭在事件循环内由调用处处理；此处仅作兜底
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(session.close())
        except Exception:
            pass


def sample_segment_urls(segment_urls, limit):
    """均匀抽样算法。"""
    if not segment_urls:
        return []
    try:
        limit = int(limit) if limit is not None else 0
    except Exception:
        limit = 0
    total = len(segment_urls)
    if limit <= 0 or limit >= total:
        return list(segment_urls)
    if limit == 1:
        return [segment_urls[total // 2]]
    indices = [round(i * (total - 1) / (limit - 1)) for i in range(limit)]
    seen, sampled = set(), []
    for idx in indices:
        idx = max(0, min(idx, total - 1))
        if idx not in seen:
            seen.add(idx)
            sampled.append(segment_urls[idx])
    return sampled


# ---------- 5. 单条线路测速（同步包装，供线程池调用） ----------
def test_item(item, session_holder=None):
    """测单条线路，返回带测速字段的新 dict（不修改入参）。兼容旧调用。"""
    result = dict(item)
    url = item.get("url") or ""
    result.setdefault("speed", 0.0)
    result.setdefault("delay", -1)
    headers = item.get("headers") or {}
    mode = _cfg("speed_test_mode", "quick")
    loop = asyncio.new_event_loop()
    try:
        info = loop.run_until_complete(_test_item_async(url, headers, mode))
        result.update(info)
    except Exception as e:
        result["test_status"] = "unreachable"
        logging.debug(f"测速失败 {url[:60]}: {e}")
    finally:
        loop.close()
    return result


async def _test_item_async(url, headers, mode):
    session = ClientSession(**_session_kwargs())
    try:
        info = await get_result(url, headers, session=session)
    finally:
        await session.close()
    info = dict(info)
    delay = info.get("delay", -1)
    speed = info.get("speed", 0) or 0
    info["test_status"] = ("measured" if delay not in (-1, None) and speed > 0 else "unreachable")
    return info


# ---------- 6. 质量过滤与排序 ----------
def passes_quality(item) -> bool:
    return quality.is_channel_result_valid(
        item,
        retain_special=True,
        supply=_cfg("open_supply", False),
        filter_speed=_cfg("open_filter_speed", False),
        min_speed=_cfg("min_speed", 0),
        resolution_speed_map=_cfg("resolution_speed_map", {}),
        filter_resolution=_cfg("open_filter_resolution", False),
        min_resolution=_cfg("min_resolution_value", 0),
        max_resolution=_cfg("max_resolution_value", 0),
    )


def sort_results(items):
    """按 config.sort_by 多维排序，与主流程排序键一致。"""
    dims = [d.strip() for d in _cfg("sort_by", "speed").split(",") if d.strip()] or ["speed"]

    def key(item):
        keys = []
        for dim in dims:
            if dim == "speed":
                keys.append(-(item.get("speed") or 0))
            elif dim == "delay":
                delay = item.get("delay")
                keys.append(delay if isinstance(delay, (int, float)) and delay >= 0 else float("inf"))
            elif dim == "resolution":
                keys.append(-(tools.get_resolution_value(item.get("resolution") or "") or 0))
        return tuple(keys)

    return sorted(items, key=key)


# ---------- 7. 并发编排（保留工程化早停/上限外壳） ----------
def speed_test_all(channel_data, on_result=None, only_names=None):
    mode = _cfg("speed_test_mode", "quick")
    if mode == "off":
        logging.info("测速已关闭，沿用全部候选线路")
        return channel_data

    target = _cfg("speed_test_target", 0) or _cfg("output_urls_limit", 0)
    cap = _cfg("speed_test_per_channel_cap", 0) or (target * 4 if target else 40)

    scope = {}
    for name, items in channel_data.items():
        if only_names is not None and name not in only_names:
            continue
        candidates = [i for i in items if i.get("url") and i.get("origin") not in ("whitelist", "local")]
        if candidates:
            candidates.sort(key=lambda x: (0 if is_ipv6_url(x.get("url", "")) else 1,))
            scope[name] = candidates[:cap]

    total = sum(len(v) for v in scope.values())
    logging.info(f"测速模式 {mode}，测 {len(scope)} 个频道 / {total} 条线路")

    # 用固定大小的线程池驱动同步 test_item，避免每条线路新建事件循环带来的开销失控。
    workers = _cfg("speed_test_workers", 32)
    valid_count = {name: 0 for name in scope}
    measured_by_name = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for name, items in scope.items():
            for item in items:
                futures[pool.submit(test_item, item)] = (name, item)
        for future in futures:
            name, item = futures[future]
            if mode == "quick" and target and valid_count.get(name, 0) >= target:
                continue
            try:
                result = future.result()
            except Exception as e:
                logging.debug(f"测速任务异常: {e}")
                continue
            if on_result:
                on_result(result)
            if passes_quality(result):
                valid_count[name] = valid_count.get(name, 0) + 1
                measured_by_name.setdefault(name, []).append(result)

    for name, items in channel_data.items():
        special = [i for i in items if i.get("origin") in ("whitelist", "local")]
        candidates = sort_results(measured_by_name.get(name, []))
        if target:
            candidates = candidates[:max(target, 1)]
        if candidates:
            channel_data[name] = special + candidates
        else:
            channel_data[name] = items
    return channel_data
