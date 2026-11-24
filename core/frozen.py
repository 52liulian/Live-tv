"""冻结过滤。

连续失效的 URL 按指数退避临时封禁，成功的探测会清零连败。
状态持久化到 gzip+pickle 文件，跨更新轮次生效。
"""
import gzip
import os
import pickle
import time

MAX_BACKOFF = 24 * 3600
BASE_BACKOFF = 60
FREEZE_THRESHOLD = 3

_frozen: dict[str, dict] = {}


def _now_ts() -> int:
    return int(time.time())


def mark_url_bad(url: str, initial: bool = False) -> None:
    if not url:
        return
    meta = _frozen.setdefault(url, {"bad_count": 0, "last_bad": 0, "last_good": 0, "frozen_until": None})
    if initial:
        meta["bad_count"] = max(meta["bad_count"], FREEZE_THRESHOLD)
    else:
        meta["bad_count"] += 1
    meta["last_bad"] = _now_ts()
    if meta["bad_count"] < FREEZE_THRESHOLD:
        meta["frozen_until"] = None
        return
    backoff = min(MAX_BACKOFF, (2 ** meta["bad_count"]) * BASE_BACKOFF)
    meta["frozen_until"] = _now_ts() + backoff


def mark_url_good(url: str) -> None:
    if not url:
        return
    meta = _frozen.get(url)
    if not meta:
        return
    meta["last_good"] = _now_ts()
    _frozen.pop(url, None)


def is_url_frozen(url: str) -> bool:
    meta = _frozen.get(url)
    if not meta:
        return False
    until = meta.get("frozen_until")
    if not until:
        return False
    now = _now_ts()
    if until > now:
        return True
    meta["frozen_until"] = None
    meta["bad_count"] = max(0, meta.get("bad_count", 0) - 1)
    if meta["bad_count"] == 0:
        _frozen.pop(url, None)
    return False


def get_current_frozen_set() -> set:
    now = _now_ts()
    result = set()
    for url, meta in list(_frozen.items()):
        until = meta.get("frozen_until")
        if until and until > now:
            result.add(url)
        else:
            is_url_frozen(url)
    return result


def load(path):
    if not path or not os.path.exists(path):
        return
    try:
        with gzip.open(path, "rb") as f:
            data = pickle.load(f)
            if isinstance(data, dict):
                for key, value in data.items():
                    if key not in _frozen:
                        _frozen[key] = value
    except Exception:
        pass


def save(path):
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with gzip.open(path, "wb") as f:
            pickle.dump(_frozen, f)
    except Exception:
        pass
