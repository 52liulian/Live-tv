"""结果质量判定。

channel_result_rejection 是全链路唯一的过滤权威：测速阶段和排序阶段都调用它，
避免规则漂移。
"""
import math

from core import config
from core.tools import get_resolution_value

RETAIN_ORIGINS = frozenset({"whitelist", "hls", "local"})


def channel_result_rejection(item: dict, *, retain_special: bool = False,
                             supply: bool | None = None,
                             filter_speed: bool | None = None,
                             min_speed: float | None = None,
                             resolution_speed_map: dict | None = None,
                             filter_resolution: bool | None = None,
                             min_resolution: int | None = None,
                             max_resolution: int | None = None) -> str | None:
    """返回该测速结果不可用的原因，可用则返回 None。"""
    if retain_special and item.get("origin") in RETAIN_ORIGINS:
        return None

    speed = item.get("speed")
    delay = item.get("delay")
    if (
        isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0 or not math.isfinite(speed)
        or isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0 or not math.isfinite(delay)
    ):
        return "unreachable"

    supply = getattr(config, "open_supply", False) if supply is None else supply
    if supply:
        return None

    filter_speed = getattr(config, "open_filter_speed", False) if filter_speed is None else filter_speed
    if min_speed is None:
        min_speed = getattr(config, "min_speed", 0)
    if resolution_speed_map is None:
        resolution_speed_map = getattr(config, "resolution_speed_map", {})
    resolution = item.get("resolution")
    if filter_speed and speed < resolution_speed_map.get(resolution, min_speed):
        return "filtered_speed"

    filter_resolution = getattr(config, "open_filter_resolution", False) if filter_resolution is None else filter_resolution
    if filter_resolution and resolution:
        if min_resolution is None:
            min_resolution = getattr(config, "min_resolution_value", 0)
        if max_resolution is None:
            max_resolution = getattr(config, "max_resolution_value", 0)
        value = get_resolution_value(resolution)
        if value <= 0 or (min_resolution and value < min_resolution) or (max_resolution and value > max_resolution):
            return "filtered_resolution"

    return None


def is_channel_result_valid(item: dict, **kwargs) -> bool:
    return channel_result_rejection(item, **kwargs) is None
