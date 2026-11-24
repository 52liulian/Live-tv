"""历史结果缓存。

保存上一轮最终线路到 gzip+pickle，供下一轮合并，避免 quick 测速达到目标后
线路骤减。
"""
import copy
import gzip
import logging
import os
import pickle

from core import frozen
from core.identity import stable_result_id


def preserve_unmeasured_history(measured, previous, base_data):
    """未测线路的历史合并。

    只把"本轮采集到但被早停跳过、且上一轮有效、且未被冻结"的历史线路补回，
    避免 quick 模式达到目标数后线路骤减；本轮完全没采集到的陈旧线路不补。

    measured   = 本轮测速后的结果  {主频道名: [item]}
    previous   = 上一轮缓存        {主频道名: [item]}
    base_data  = 本轮采集的原始候选 {主频道名: [item]}
    返回合并后的新字典（不修改入参）。
    """
    preserved = copy.deepcopy(measured or {})
    for name, candidates in (base_data or {}).items():
        current = preserved.setdefault(name, [])
        candidate_ids = {
            stable_result_id(item.get("url", ""), item.get("headers"))
            for item in candidates if isinstance(item, dict) and item.get("url")
        }
        seen = {
            stable_result_id(item.get("url", ""), item.get("headers"))
            for item in current if isinstance(item, dict) and item.get("url")
        }
        for item in (previous or {}).get(name, []):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            rid = stable_result_id(item["url"], item.get("headers"))
            if rid not in candidate_ids or rid in seen or frozen.is_url_frozen(item["url"]):
                continue
            current.append(copy.deepcopy(item))
            seen.add(rid)
    return preserved


def load(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with gzip.open(path, "rb") as f:
            data = pickle.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.debug(f"缓存读取失败: {e}")
        return {}


def save(path, data):
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with gzip.open(path, "wb") as f:
            pickle.dump(data, f)
    except Exception as e:
        logging.debug(f"缓存写入失败: {e}")
