"""配置校验 + 环境变量覆盖(轻量实现)。

- env 覆盖:任意 settings.py 公开变量都可用环境变量 LIVETV_<大写名> 覆盖,
  按默认值类型自动转换(bool/int/float/str/list/dict)。
- 校验:对枚举值做合法性检查并给出 difflib 拼写建议(如 speed_test_mode 填成
  "quik" 会提示"是否想填写 quick");数值项做下界检查。
- 失败策略:记录 WARNING 并回退到默认值,不让一次笔误中断整轮更新
  (严格报错会因笔误中止整轮更新,这里放宽为记录警告并回退)。
"""
import difflib
import json
import logging

logger = logging.getLogger(__name__)

# 环境变量前缀
ENV_PREFIX = "LIVETV_"

# 枚举校验:变量名 -> 允许值集合
_ENUM_RULES = {
    "speed_test_mode": {"off", "quick", "full"},
    "update_time_position": {"top", "bottom"},
    "ip_version_priority": {"ipv6", "ipv4"},
    "ipv_type": {"all", "ipv4", "ipv6"},
    "epg_header_mode": {"self", "source"},
}

# 数值下界校验:变量名 -> 最小值
_NUM_MIN_RULES = {
    "fetch_workers": 1,
    "request_timeout": 1,
    "connect_timeout": 1,
    "speed_test_timeout": 1,
    "speed_test_workers": 1,
    "speed_test_target": 0,
    "speed_test_per_channel_cap": 0,
    "url_limit_per_channel": 0,
    "output_urls_limit": 0,
    "min_speed": 0,
    "min_resolution_value": 0,
    "max_resolution_value": 0,
    "ad_max_loop_duration": 0,
    "segment_sample_limit": 0,
}

# 集合元素校验:变量名 -> 允许元素集合
_ELEM_RULES = {
    "ipv_type_prefer": {"ipv4", "ipv6", "auto"},
}

# CSV 字符串元素校验:变量名 -> 允许元素集合(逗号分隔字符串)
_CSV_RULES = {
    "sort_by": {"speed", "delay", "resolution"},
}

# loader 从 txt 数据文件注入的变量,env 覆盖无意义,跳过
_LOADER_OWNED = {
    "source_urls", "whitelist_subscribe_urls", "source_url_headers",
    "url_blacklist", "epg_urls",
}


def _coerce(raw, default):
    """按默认值的类型把环境变量字符串转成对应类型,失败返回 None。"""
    try:
        if isinstance(default, bool):
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
        if isinstance(default, int):
            return int(str(raw).strip())
        if isinstance(default, float):
            return float(str(raw).strip())
        if isinstance(default, dict):
            return json.loads(raw)
        if isinstance(default, list):
            text = str(raw).strip()
            if text.startswith("["):
                return json.loads(text)
            return [p.strip() for p in text.split(",") if p.strip()]
        return raw
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def apply_env_overrides(config_module, settings_module=None):
    """用 LIVETV_* 环境变量覆盖 config 模块变量,返回 {变量名: 原始值}。"""
    import os
    if settings_module is None:
        settings_module = config_module
    overridden = {}
    for name in dir(settings_module):
        if name.startswith("_") or name.upper() == name or name in _LOADER_OWNED:
            continue
        default = getattr(settings_module, name, None)
        if callable(default) or isinstance(default, type):
            continue
        raw = os.environ.get(ENV_PREFIX + name.upper())
        if raw is None or raw == "":
            continue
        value = _coerce(raw, default)
        if value is None:
            logger.warning("环境变量 %s%s=%r 无法按 %s 类型解析,忽略",
                           ENV_PREFIX, name.upper(), raw, type(default).__name__)
            continue
        setattr(config_module, name, value)
        overridden[name] = raw
        logger.info("配置已由环境变量覆盖: %s = %r (来自 %s%s)", name, value, ENV_PREFIX, name.upper())
    return overridden


def validate_settings(config_module, settings_module=None):
    """校验配置合法性,非法项回退默认值。返回问题列表 [{name, value, message}]。"""
    from core import config as root_config
    if settings_module is None:
        settings_module = root_config.settings
    issues = []

    def _fallback(name, value, message):
        default = getattr(settings_module, name, None)
        setattr(config_module, name, default)
        issue = {"name": name, "value": value, "message": message, "fallback": default}
        issues.append(issue)
        logger.warning("配置项 %s=%r 非法:%s 已回退默认值 %r", name, value, message, default)

    for name, choices in _ENUM_RULES.items():
        value = getattr(config_module, name, None)
        if value not in choices:
            sug = difflib.get_close_matches(str(value), choices, n=1)
            msg = f"应为 {sorted(choices)} 之一"
            if sug:
                msg += f';是否想填写 "{sug[0]}"?'
            _fallback(name, value, msg)

    for name, minimum in _NUM_MIN_RULES.items():
        value = getattr(config_module, name, None)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
            _fallback(name, value, f"应为 >= {minimum} 的数值")

    for name, allowed in _ELEM_RULES.items():
        value = getattr(config_module, name, None)
        if not isinstance(value, (list, tuple)):
            _fallback(name, value, "应为列表")
            continue
        bad = [v for v in value if v not in allowed]
        if bad:
            sug = difflib.get_close_matches(str(bad[0]), sorted(allowed), n=1)
            msg = f"元素 {bad!r} 不在 {sorted(allowed)} 中"
            if sug:
                msg += f';是否想填写 "{sug[0]}"?'
            cleaned = [v for v in value if v in allowed]
            setattr(config_module, name, cleaned)
            issue = {"name": name, "value": value, "message": msg, "fallback": cleaned}
            issues.append(issue)
            logger.warning("配置项 %s=%r 非法:%s 已剔除非法元素,保留 %r", name, value, msg, cleaned)

    for name, allowed in _CSV_RULES.items():
        value = getattr(config_module, name, None)
        if not isinstance(value, str):
            _fallback(name, value, f"应为逗号分隔字符串,元素取值 {sorted(allowed)}")
            continue
        parts = [p.strip() for p in value.split(",") if p.strip()]
        bad = [p for p in parts if p not in allowed]
        if bad:
            sug = difflib.get_close_matches(str(bad[0]), sorted(allowed), n=1)
            msg = f"元素 {bad!r} 不在 {sorted(allowed)} 中"
            if sug:
                msg += f';是否想填写 "{sug[0]}"?'
            cleaned = ",".join(p for p in parts if p in allowed) or ",".join(sorted(allowed)[:1])
            setattr(config_module, name, cleaned)
            issue = {"name": name, "value": value, "message": msg, "fallback": cleaned}
            issues.append(issue)
            logger.warning("配置项 %s=%r 非法:%s 已回退为 %r", name, value, msg, cleaned)

    return issues
