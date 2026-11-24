"""配置聚合。

config/ 目录只存放数据文件（subscribe.txt、blacklist.txt、epg.txt、demo.txt、
alias.txt、whitelist.txt），不放任何 .py。开关与阈值在 core/settings.py，
数据解析在 core/loader.py，本模块把它们聚合为一个命名空间。

各模块统一用法：
    from core import config
    config.source_urls / config.open_epg / config.CONFIG_DIR ...

数据文件格式：
  config/subscribe.txt  订阅源，每行一个，支持 UA="xxx"，[WHITELIST] 段
  config/blacklist.txt  接口黑名单关键词，每行一个
  config/epg.txt        EPG 订阅地址，每行一个
  config/demo.txt       频道模板
  config/alias.txt      完整频道别名表
  config/whitelist.txt  接口白名单（可选，存在时自动加载）
"""
import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
ROOT_DIR = os.path.dirname(CONFIG_DIR)

from core import settings as settings  # noqa: F401  供 validator 读取默认值
from core.settings import *  # noqa: F401,F403
from core.custom_aliases import channel_aliases  # noqa: F401
from core.loader import load_all as _load_all

# 从 config/ 下的 txt 文件载入数据，覆盖 settings.py 里的同名占位值
_load_all(globals())

from core import validator as validator  # noqa: E402,F401  配置校验 + env 覆盖
