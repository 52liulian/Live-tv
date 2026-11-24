"""结构化日志 + URL 脱敏。

- redact_url / sanitize_fields / redact_urls_in_text / normalize_message:
  敏感 query 键(token/auth/secret/password/key 等)打码为 ***,
  剥离消息前缀装饰 emoji,文本中的 URL 统一脱敏。
- JsonlMirrorHandler:挂到根 logger 上,把现有日志镜像成结构化 JSONL
  (output/log/runtime.jsonl),与纯文本 runtime.log 双写。
- Reporter:统一入口 report(event, message, ...),同时写文本 sink 和 JSONL sink。
"""
import json
import logging
import os
import re
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:token|auth|authorization|secret|signature|password|passwd|expires?|(?:^|[_-])key(?:$|[_-]))",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"(?:https?|rtmp|rtsp)://[^\s，。；）)\]}]+", re.IGNORECASE)
_LEADING_DECORATIONS = (
    "⚡️", "⚡", "⚙️", "⚙", "🚧", "✅", "⚠️", "⚠", "🕒", "🥳", "🔍",
    "⛔", "🆗", "📊", "🆙", "🌐", "🚀", "❌", "🫥", "⬆️", "⬆", "📢",
)


def redact_url(value):
    """Hide credentials and sensitive query values while preserving diagnostics."""
    if not value or not isinstance(value, str):
        return value
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{hostname}{port}"
        query = urlencode(
            [
                (key, "***" if _SENSITIVE_QUERY_KEY.search(key) else item_value)
                for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return value


def sanitize_fields(value, key=""):
    if isinstance(value, dict):
        return {str(k): sanitize_fields(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_fields(item, key) for item in value]
    if isinstance(value, str) and ("url" in key.lower() or value.startswith(("http://", "https://", "rtmp://", "rtsp://"))):
        return redact_url(value)
    return value


def redact_urls_in_text(value):
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)) or "", value)


def normalize_message(value):
    message = redact_urls_in_text(value).strip()
    changed = True
    while changed:
        changed = False
        for decoration in _LEADING_DECORATIONS:
            if message.startswith(decoration):
                message = message[len(decoration):].lstrip()
                changed = True
                break
    return message


class _Sink:
    """文本 + JSONL 双写 sink(RotatingFileHandler,线程安全)。"""

    def __init__(self, text_path, jsonl_path):
        for p in (text_path, jsonl_path):
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._text_logger = logging.getLogger(f"livetv.runtime.{id(self)}")
        self._text_logger.setLevel(logging.INFO)
        self._text_logger.propagate = False
        handler = RotatingFileHandler(text_path, maxBytes=5 * 1024 * 1024,
                                      backupCount=2, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._text_logger.handlers.clear()
        self._text_logger.addHandler(handler)
        self._json_logger = logging.getLogger(f"livetv.runtime.json.{id(self)}")
        self._json_logger.setLevel(logging.INFO)
        self._json_logger.propagate = False
        json_handler = RotatingFileHandler(jsonl_path, maxBytes=10 * 1024 * 1024,
                                           backupCount=2, encoding="utf-8", delay=True)
        json_handler.setFormatter(logging.Formatter("%(message)s"))
        self._json_logger.handlers.clear()
        self._json_logger.addHandler(json_handler)

    def emit(self, payload, line):
        with self._lock:
            level = getattr(logging, payload.get("level", "INFO"), logging.INFO)
            self._text_logger.log(level, line)
            self._json_logger.info(json.dumps(payload, ensure_ascii=False, default=str))

    def close(self):
        with self._lock:
            for lg in (self._text_logger, self._json_logger):
                for handler in lg.handlers[:]:
                    handler.flush()
                    handler.close()
                    lg.removeHandler(handler)


class Reporter:
    """轻量版统一报告入口:终端(经标准 logging)+ 双写 sink。"""

    def __init__(self, run_id=None, log_dir=None):
        from core import config
        self.run_id = run_id or datetime.now().strftime("%Y%m%d%H%M%S")
        directory = log_dir or getattr(config, "runtime_log_dir", "") or os.path.join("output", "log")
        self._sink = _Sink(os.path.join(directory, "runtime.log"),
                           os.path.join(directory, "runtime.jsonl"))
        self._lock = threading.RLock()
        self._phase = None

    def set_phase(self, phase):
        with self._lock:
            self._phase = phase

    def report(self, event, message, level="INFO", data=None, phase=None):
        payload = {
            "event": event,
            "message": normalize_message(str(message)),
            "level": level,
            "run_id": self.run_id,
            "phase": phase or self._phase,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": sanitize_fields(data or {}),
        }
        context = []
        if payload["run_id"] is not None:
            context.append(f"run={payload['run_id']}")
        if payload["phase"]:
            context.append(f"phase={payload['phase']}")
        context.append(f"event={payload['event']}")
        line = f"{payload['timestamp']} {payload['level']:<7} [{' '.join(context)}] {payload['message']}"
        self._sink.emit(payload, line)

    def mirror_record(self, record):
        """把标准 logging 记录镜像进 sink(供 JsonlMirrorHandler 调用)。"""
        self.report(
            event="log",
            message=record.getMessage(),
            level=record.levelname,
            data={"logger": record.name},
        )

    def close(self):
        self._sink.close()


class JsonlMirrorHandler(logging.Handler):
    """挂在根 logger 上,把全部日志记录镜像到结构化 sink。"""

    def __init__(self, reporter):
        super().__init__(level=logging.INFO)
        self._reporter = reporter

    def emit(self, record):
        try:
            self._reporter.mirror_record(record)
        except Exception:
            pass
