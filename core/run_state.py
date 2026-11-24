"""运行状态落盘。

把每轮更新的状态(started/success/failed + 关键统计)写到 output/data/run_state.json,
原子写(tempfile + os.replace),供外部脚本/监控读取。
"""
import json
import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)


def _path():
    from core import config
    return getattr(config, "run_state_file", "") or os.path.join("output", "data", "run_state.json")


def read_run_state():
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as file:
            state = json.load(file)
        return state if isinstance(state, dict) else {"status": "never_run"}
    except (OSError, ValueError, TypeError):
        return {"status": "never_run"}


def write_run_state(status, **data):
    path = _path()
    directory = os.path.dirname(path) or "."
    state = {"status": status, "updated_at": time.time(), **data}
    temporary = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="run_state.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    except OSError:
        logger.debug("run_state 写入失败: %s", path)
        try:
            if temporary:
                os.unlink(temporary)
        except OSError:
            pass
    return state
