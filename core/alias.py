"""频道别名匹配。

三套索引：主名 -> 别名集合、别名 -> 主名、正则 -> 主名。
匹配顺序：精确别名 -> 正则（文件顺序优先）-> 归一化后再精确 -> 原名兜底。
数据源为 config/alias.txt（完整表），
并叠加 config.channel_aliases 中的自定义映射（后者优先）。

config.channel_aliases 支持两种键：
  "央视一套": "CCTV1"                  精确别名
  "re:(?i)^CCTV[-\\s_]*0?1$": "CCTV1"  正则别名（re: 开头）
"""
import os
import re

from core import config
from core import tools


class Alias:
    def __init__(self):
        self.reload()

    def reload(self):
        self.primary_to_aliases: dict[str, set[str]] = {}
        self.alias_to_primary: dict[str, str] = {}
        self.pattern_to_primary: list[tuple[re.Pattern, str]] = []

        # 第一数据源：config/alias.txt 完整表（主名,别名1,别名2,...）
        config_dir = getattr(config, "CONFIG_DIR", "config")
        alias_path = os.path.join(config_dir, "alias.txt")
        if os.path.exists(alias_path):
            with open(alias_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "," not in stripped:
                        continue
                    parts = [p.strip() for p in stripped.split(",")]
                    primary = parts[0]
                    aliases = set(p for p in parts[1:] if p)
                    self._register(primary, aliases)

        # 模板保底：模板中的每个频道名必须映射到它自己，
        # 避免完整别名表把模板名（如“凤凰卫视”）指到另一主名造成归并冲突。
        self._template_names = self._load_template_names()
        for name in self._template_names:
            self._register(name, {name})

        # 归一化桥接：完整别名表主名（如 CCTV-1）与模板主名（如 CCTV1）
        # 归一化后相同时，把前者对齐到后者，避免两侧主名不一致导致漏匹配。
        self._normalized_to_template = {}
        for name in self._template_names:
            self._normalized_to_template.setdefault(tools.format_name(name), name)

        # 第二数据源：config.py 自定义映射（覆盖式，优先级更高）
        for alias, primary in config.channel_aliases.items():
            alias = str(alias).strip()
            primary = str(primary).strip()
            if not alias or not primary or alias.startswith("#"):
                continue
            existing = set(self.primary_to_aliases.get(primary, set()))
            existing.add(alias)
            normalized = tools.format_name(primary)
            if normalized:
                existing.add(normalized)
            # 自定义别名若与原表其它主名冲突，移除旧指向
            old_primary = self.alias_to_primary.get(alias)
            if old_primary and old_primary != primary:
                self.primary_to_aliases.get(old_primary, set()).discard(alias)
            self._register(primary, existing, reset=True)

    @staticmethod
    def _load_template_names():
        """读取模板频道名列表，供保底映射使用。"""
        names = []
        path = getattr(config, "source_file", "demo.txt") or "demo.txt"
        if not os.path.exists(path):
            path = "demo.txt"
        if not os.path.exists(path):
            return names
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "#genre#" in stripped:
                    continue
                name = stripped.split(",")[0].strip()
                if name:
                    names.append(name)
        return names

    def _register(self, primary, aliases, reset=False):
        if reset:
            current = self.primary_to_aliases.get(primary, set())
            current.update(aliases)
            aliases = current
        aliases = set(aliases)
        aliases.add(tools.format_name(primary))
        self.primary_to_aliases[primary] = aliases
        for item in aliases:
            self.alias_to_primary[item] = primary
            if item.startswith("re:"):
                try:
                    pattern = re.compile(item[3:])
                    if (pattern, primary) not in self.pattern_to_primary:
                        self.pattern_to_primary.append((pattern, primary))
                except re.error:
                    pass
        self.alias_to_primary[primary] = primary

    def get(self, name: str) -> set[str]:
        return self.primary_to_aliases.get(name, set())

    def get_primary(self, name: str) -> str:
        if not name:
            return name
        primary = self.alias_to_primary.get(name)
        if primary is None:
            primary = self.get_primary_by_pattern(name)
        if primary is None:
            normalized = tools.format_name(name)
            primary = self.alias_to_primary.get(normalized, name)
        # 若得到的主名不是模板名，尝试按归一化值桥接到模板主名
        if primary not in self._template_names:
            bridged = self._normalized_to_template.get(tools.format_name(primary))
            if bridged:
                primary = bridged
        return primary

    def get_primary_by_pattern(self, name: str):
        for pattern, primary in self.pattern_to_primary:
            if pattern.search(name):
                return primary
        return None


# 全局单例
channel_alias = Alias()
