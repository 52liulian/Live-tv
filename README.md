<div align="center">
  <img src="./assets/logo.svg" alt="Live-tv logo" width="120" height="120"/>
</div>

<h1 align="center">Live-tv</h1>

<p align="center">
  📡 IPTV 直播源自动更新工具：聚合公开订阅源，按频道模板匹配、测速筛选，产出可直接导入播放器的 M3U / TXT 播放列表，并附带 EPG 节目单与频道台标。
</p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="License: AGPL-3.0" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python 3.11+" /></a>
  <a href="https://github.com/52liulian/Live-tv/actions"><img src="https://img.shields.io/badge/%E6%9B%B4%E6%96%B0-%E6%AF%8F%E6%97%A5%E8%87%AA%E5%8A%A8-brightgreen" alt="每日自动更新" /></a>
  <a href="https://52liulian.github.io/Live-tv/"><img src="https://img.shields.io/badge/Pages-%E7%BB%93%E6%9E%9C%E9%A1%B5-747bff" alt="Pages 结果页" /></a>
</p>

> 声明 所有播放源均收集于互联网，仅供测试研究学习，不得商用。

<details>
<summary><strong>目录</strong></summary>

- [✨ 核心特性](#核心特性)
- [🚀 快速上手](#快速上手)
- [⚙️ 配置](#配置)
- [📁 配置与结果目录](#配置与结果目录)
- [🔄 处理管线](#处理管线)
- [🕐 自动更新](#自动更新)
- [📶 IPv6 优势](#ipv6-优势)
- [👀 关注](#关注)
- [⚠️ 免责声明](#免责声明)
- [⚖️ 许可证](#许可证)

</details>

## 核心特性

| 功能                 | 说明                                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------- |
| **自动更新**   | GitHub Actions 每日定时运行，无需人工干预；也支持本地命令行执行                               |
| **双栈输出**   | IPv4 / IPv6 按协议分目录输出，可按协议过滤与偏好排序                                          |
| **多源聚合**   | 并发采集订阅源（兼容 M3U/TXT），支持 UA 指定、CDN 加速与代理，失效源自动识别并可注释回写      |
| **测速筛选**   | aiohttp 并发测速，取延迟、速率、分辨率、帧率，无效接口自动剔除，广告/无信号循环源按关键词过滤 |
| **历史合并**   | 合并上一轮有效线路，防止某轮波动导致结果骤减；连续失效线路按指数退避临时冻结                  |
| **频道模板**   | 由 `config/demo.txt` 决定输出哪些频道及分类，别名表自动归一化名称差异                       |
| **EPG 节目单** | 多源合并（白名单/配置/订阅发现三级优先级），流式解析 + 双重去重；结果 m3u 的 `x-tvg-url` 指向合并后的 `epg.xml`   |
| **频道台标**   | m3u 结果自动挂载 yuanzl77 TVlogo 图库                                                         |
| **结果页**     | Pages 单文件结果页，复制 / 弹窗试播（支持换台）/ 下载一步到位                                 |
| **可观测性**   | runtime.log + runtime.jsonl 双写、URL 脱敏、运行状态落盘 run_state.json                       |
| **配置健壮**   | 启动校验配置合法性（非法值回退默认并给拼写建议），任意配置项支持 `LIVETV_*` 环境变量覆盖    |

## 快速上手

### 方式一：GitHub Actions 自动更新（推荐）

1. Fork 或使用本仓库，确认 Actions 未被禁用。
2. 在仓库 Settings → Pages 把 Source 设为 **GitHub Actions**。
3. 等待每日定时任务（北京时间 05:45）或在 Actions 页面手动触发，完成后即可通过上方 Pages 地址订阅。

### 方式二：本地运行

```bash
pip install -r requirements.txt
python main.py
```

结果写入 `output/ipv4` 与 `output/ipv6`，EPG 写入 `output/epg`。单次完整更新通常数分钟。

## 配置

所有开关和阈值集中在 `core/settings.py`，按注释调整即可。几项影响最大的：

- speed_test_mode off / quick / full。默认 quick：达到每频道目标数即停，数分钟内完成，适合 GitHub Actions。full 会对全部候选测速并用 ffprobe 测分辨率帧率，需本地装 ffmpeg，不建议放工作流。
- speed_test_per_channel_cap 每频道最多测多少条线路，0 表示 target 的 4 倍，用来控制最坏耗时。
- url_limit_per_channel / output_urls_limit 每频道保留的线路数，默认 8。
- open_filter_speed / open_filter_resolution 速率与分辨率过滤，默认关闭，避免误杀低码率源；需要时配合 min_speed 与 min_resolution_value 开启。
- open_history 合并上一轮结果，默认开启，防止某轮采集波动导致线路骤减。
- channel_whitelist 只输出指定频道；whitelist_urls 里的线路不参与测速且必定保留。
- sort_by 频道内线路排序维度，默认 speed，可选 delay、resolution，多个用逗号分隔。

### 功能开关（默认全部保持原有输出格式）

| 配置项                   | 默认  | 说明                                                                                                                              |
| ------------------------ | ----- | --------------------------------------------------------------------------------------------------------------------------------- |
| open_auto_disable_source | False | 抓取失败/空内容的订阅源与 EPG 地址自动加 `# ` 注释回写到 subscribe.txt、epg.txt                                                 |
| http_proxy               | ""    | 订阅源与 EPG 抓取走的代理服务器地址（如 Clash 的 http://127.0.0.1:7890）；测速保持直连。注意不能填 gh-proxy.org 这类 URL 加速前缀 |
| cdn_urls                 | []    | GitHub raw 加速前缀列表（如 https://gh-proxy.org/ ），直连失败时自动补位                                                          |
| ssl_verify               | False | 证书校验开关；部分网络环境对部分域名有 SSL 干扰时可关掉                                                                           |
| ipv_type                 | "all" | 结果接口协议过滤：ipv4 / ipv6 / all                                                                                               |
| ipv_type_prefer          | []    | 协议偏好顺序，如 ["ipv6", "ipv4"]；留空沿用 ip_version_priority                                                                   |
| open_update_time         | False | 在结果顶部或底部插入独立的"更新时间"分类（update_time_position: top/bottom）                                                      |
| epg_header_mode          | "self" | 结果 m3u 首行 `x-tvg-url` 指向哪个 EPG：self 用本项目合并后的 `<pages_base_url>/epg.xml`，source 用 config/epg.txt 的原始地址 |
| pages_base_url           | 本站地址 | 站点根地址，Actions 中由 PAGES_BASE_URL 自动注入；留空时回落到原始 EPG 地址 |
| open_line_note           | False | 地址末尾是否输出 `$LIVE•IPV4『线路N』` 备注（默认关闭，输出纯地址）                                                            |
| open_url_info            | False | 在地址末尾追加 来源/协议/分辨率 说明                                                                                              |
| location / isp           | []    | 归属地/运营商白名单关键词筛选，如 ["北京"]、["电信"]                                                                              |
| origin_type_prefer       | []    | 来源偏好顺序，如 ["whitelist", "subscribe"]                                                                                       |
| location_isp_map         | {}    | 轻量归属地/运营商映射 {"host子串": {"location": "北京", "isp": "电信"}}                                                           |

### 环境变量覆盖

任意 settings.py 公开变量都可用 `LIVETV_` 前缀环境变量覆盖，类型按默认值自动转换，例如：

```bash
LIVETV_speed_test_mode=full python main.py
```

非法配置值启动时会被校验拦截并回退默认（带拼写建议），不会中断运行。

## 配置与结果目录

> [!NOTE]
> 本项目不内置任何数据源，需自行在 `config/subscribe.txt` 中添加订阅地址后运行生成结果。

```text
Live-tv/
├── main.py                     # 项目入口,运行 python main.py 即可更新
├── config                      # 纯数据目录,只放 txt 数据文件,不放任何 .py
│   ├── subscribe.txt           # 频道订阅源列表,每行一个,可加 UA="...",支持 [WHITELIST] 段
│   ├── blacklist.txt           # 接口黑名单关键词,每行一个
│   ├── whitelist.txt           # 接口白名单,可选,存在时自动加载
│   ├── epg.txt                 # EPG 订阅源列表,每行一个
│   ├── demo.txt                # 频道模板,决定输出哪些频道及其分类
│   ├── alias.txt               # 频道别名表,含正则别名
│   └── local.txt               # 本地源文件,可选,存在时自动加载
├── core                        # 更新引擎与配置
│   ├── config.py               # 配置聚合入口,import 后用 config.xxx 访问
│   ├── settings.py             # 开关与阈值,全部可调参数集中在此
│   ├── loader.py               # config/*.txt 数据解析
│   ├── validator.py            # 配置校验 + LIVETV_* 环境变量覆盖
│   ├── identity.py             # 稳定 ID 生成,去重/缓存/冻结共用
│   ├── alias.py                # 频道别名匹配
│   ├── custom_aliases.py       # 自定义别名,优先级高于完整别名表
│   ├── whitelist.py            # 黑白名单匹配
│   ├── frozen.py               # 冻结退避,连续失效线路临时封禁
│   ├── cache.py                # 历史结果缓存与合并
│   ├── subscribe.py            # 订阅源并发采集
│   ├── speed.py                # 测速与筛选
│   ├── epg.py                  # EPG 节目单抓取与合并
│   ├── writer.py               # 结果输出,内容比对去重 + 原子写
│   ├── quality.py              # 结果质量判定
│   ├── reporting.py            # 结构化日志 runtime.log/jsonl,URL 脱敏
│   ├── run_state.py            # 运行状态落盘 output/data/run_state.json
│   └── tools.py                # 通用工具:归一化、M3U/txt 解析、失效源回写
├── scripts
│   └── publish_results.py      # Pages 结果页发布脚本,收拢结果 + 生成 index.html
├── output                      # 运行产物目录,全目录 git 忽略,仅通过 Pages 发布
│   ├── ipv4                    # IPv4 结果目录,result.txt / result.m3u
│   ├── ipv6                    # IPv6 结果目录,result.txt / result.m3u
│   ├── epg                     # EPG 结果目录,epg.xml / epg.gz
│   ├── data                    # 缓存、冻结状态与 run_state,Actions 缓存跨运行复用
│   └── log                     # 结构化运行日志,runtime.log / runtime.jsonl
├── .github/workflows           # 定时自动更新 + Pages 发布工作流
├── LICENSE                     # AGPL-3.0
└── README.md
```

数据文件用 `#` 注释，改完直接生效，不用碰 Python。编辑订阅源时注意：

- 每行一个地址，可用 `https://xxx/list.m3u UA="Mozilla/5.0"` 指定 UA。
- `[WHITELIST]` 段里的源，其抓取到的接口不参与测速，始终保留在结果最前。
- 失效源会在采集阶段被自动过滤，历史结果会从缓存合并回来。

## 处理管线

`python main.py` 按以下五个阶段依次执行：

1. **准备** 从 config/ 目录读取频道模板、订阅源、黑白名单、EPG 与别名表，加载历史缓存与冻结状态；环境变量覆盖与配置校验在此阶段完成。
2. **采集** 并发拉取订阅源，兼容 M3U/TXT，提取台标和请求头，自动发现订阅源声明的 EPG 地址；CDN 加速与 HTTP 代理在此阶段生效。
3. **聚合** 频道名归一化与别名匹配，稳定 ID 去重，黑白名单过滤，合并上一轮的有效线路。历史合并只补回本轮采集到但被早停跳过、且未被冻结的线路，避免线路骤减又不引入陈旧源。
4. **测速** 用 aiohttp 并发测速。流式下载按稳定性窗口判定速率，m3u8 地址会跟随 HEAD 重定向、解析 master 列表选最高带宽档取分辨率与帧率、再对尾部 TS 分片抽样测速，广告与无信号源按关键词和 ENDLIST 短循环过滤，full 模式用 ffprobe 补编解码信息，最后多维排序。
5. **输出** 只写 output/ipv4 与 output/ipv6 分目录的 result.txt / result.m3u，另产出 EPG 节目单，根目录不再生成结果文件。写盘采用内容比对去重与原子写。

## 自动更新

`.github/workflows/main.yml` 每 3 天一次（UTC 21:45，北京时间次日 05:45）触发，也可在 Actions 手动触发。工作流做的事：

1. 恢复历史缓存（output/data/cache.gz 与 frozen.gz，按配置 hash 建缓存键），跨运行保留冻结与历史状态。
2. 运行完整更新管线；提交前还原 config/ 下被程序改动的数据文件，配置改动不入库。
3. 运行 scripts/publish_results.py 生成 Pages 结果页并部署到 GitHub Pages；结果文件本身不入仓库。

## IPv6 优势

1. 更低延迟，减少视频缓冲和加载时间。
2. 对多媒体和组播支持更好，传输更稳定。
3. 设计更现代，减少地址转换带来的连接中断。

查看当前网络是否支持 IPv6 可访问 [testipv6.cn](https://testipv6.cn/)

## 关注

<p align="center">
  <a href="https://github.com/52liulian/Live-tv/"><img src="https://img.shields.io/badge/%E4%BB%93%E5%BA%93-GitHub-green" alt="仓库" /></a>
  <a href="https://52liulian.github.io/Live-tv/"><img src="https://img.shields.io/badge/Pages-%E7%BB%93%E6%9E%9C%E9%A1%B5-747bff" alt="Pages 结果页" /></a>
</p>

## 鸣谢

- [**yuanzl77**](https://github.com/yuanzl77)：[TVlogo](https://github.com/yuanzl77/TVlogo) 频道台标库，本项目结果里每条 `tvg-logo` 都取自该仓库。

## 免责声明

本项目只做一件事：把你自行提供的订阅地址整理、去重、测速后输出成播放器可用的列表文件。它本身不内置任何直播源，也不存储、转码、转发任何音视频内容。

1. 本仓库及其 Pages 页面展示的所有地址均为自动化脚本从互联网公开渠道收集的技术测试样本，收集自何处、能否播放、内容是否合规，均与本项目无关。
2. 你添加什么订阅源、播放什么内容，由你自己决定并自行承担相应责任；请确保你的使用行为符合所在国家/地区的法律法规。
3. 本项目按"现状"提供，不作出任何可用性、稳定性或合法性承诺；因使用本项目产生的任何直接或间接后果，作者不承担责任。
4. 如需长期或商业用途，请先自行完成内容授权与合规评估。

## 许可证

[AGPL-3.0](./LICENSE) License &copy; 2026-PRESENT [52liulian](https://github.com/52liulian)

> [!IMPORTANT]
> 本项目采用 AGPL-3.0。若将修改后的程序作为网络服务、公开容器镜像或以其他对外服务方式运行，须向使用者提供包括修改在内的完整源代码。详见：https://www.gnu.org/licenses/agpl-3.0.html
