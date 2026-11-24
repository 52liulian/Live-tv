"""Live-tv 更新引擎核心模块。
模块按依赖顺序组织：
  identity  -> alias -> tools -> quality/frozen/whitelist
            -> subscribe -> speed -> epg -> writer
  main.py 负责五阶段编排。
"""
