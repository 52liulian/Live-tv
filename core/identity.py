"""稳定 ID 生成。

去重、缓存、冻结全链路共用同一套 ID，headers 需规范化后参与计算。

"""
import hashlib
import json


def stable_result_id(url: str, headers: dict | None = None) -> str:
    """根据 url 与规范化 headers 生成 24 位稳定 ID。"""
    normalized_headers = {
        str(key).strip().lower(): str(value).strip()
        for key, value in sorted((headers or {}).items(), key=lambda item: str(item[0]).lower())
        if value is not None
    }
    payload = json.dumps(
        {"url": (url or "").strip(), "headers": normalized_headers},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def stable_channel_id(category: str, name: str) -> str:
    """根据分类与频道名生成稳定 ID。"""
    payload = f"{(category or '').strip()}\x1f{(name or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
