"""引擎开关与阈值。
这里只放可调参数。订阅源、黑名单、EPG、频道模板、别名表等数据统一放在
config/ 目录下的 txt 文件里，由 core/loader.py 读取后注入包命名空间。
"""

# ---------- 总开关 ----------
open_update = True          # 关闭后停止所有采集与测速
open_subscribe = True       # 开启订阅源采集
open_history = True         # 合并上一轮结果，避免 quick 测速导致线路骤减
open_epg = True             # 生成并写入 EPG
open_subscribe_epg = True   # 自动提取订阅源 m3u 头里的 EPG 地址并合并（增加网络开销）
open_headers = True         # 使用 m3u 内的 UA/Referer 等请求头
open_m3u_result = True      # 生成 m3u 结果

# ---------- 抓取设置 ----------
fetch_workers = 16          # 并发拉取订阅源的线程数，过大易被源站限流
request_timeout = 15        # 单个订阅源拉取超时（秒）
connect_timeout = 12        # 建立连接超时（秒）。本机到部分源站建连抖动大（实测 1.5s~25s），
                            # 过小会把浏览器能打开的源误判失效；Actions 网络干净，影响可忽略
user_agent = ""             # 自定义 UA，留空用内置默认值
# 证书校验：本机网络对部分域名有 SSL 干扰，会让浏览器能打开的源在程序里被误判失效
# （实测 98 个源里 54 个因此失败）。设为 False 跳过校验即可恢复；对抓公开直播源清单无安全影响。
# GitHub Actions 直连环境没有此问题，如在意可改回 True。
ssl_verify = False
cdn_urls = []               # GitHub raw 加速前缀，可填多个

# ---------- 频道匹配 ----------
ip_version_priority = "ipv6"  # IP 优先级：ipv6 或 ipv4
url_limit_per_channel = 8   # 每频道最多保留的线路数，0 表示不限制
output_urls_limit = 8       # 每频道导出上限（兼容字段，与上面保持一致即可）
channel_whitelist = []      # 频道白名单，非空时只输出名单内频道，按模板名填写
whitelist_urls = {}         # URL 白名单，格式 {"CCTV1": ["http://..."], "": ["全局"]}
whitelist_keywords = {}     # URL 白名单关键词，格式同上
open_unmatched_category = False  # 未匹配的源频道登记到 txt 供排查
open_empty_category = False      # 模板里无线路的频道登记到 txt 供排查

# ---------- 测速与过滤 ----------
# speed_test_mode 三选一：
#   "off"   不测速，沿用聚合结果，最快
#   "quick" 测可达性、延迟、速率，达到目标数即停，适合 GitHub Actions（推荐）
#   "full"  测完全部候选并用 ffprobe 补分辨率/帧率，需本地安装 ffmpeg
speed_test_mode = "quick"
speed_test_timeout = 5      # 单条链接测速超时（秒）
speed_test_workers = 64     # 测速并发线程数
speed_test_target = 3       # quick 每频道有效线路目标；0 表示跟随 output_urls_limit(8)
                            # 模板频道多时跟随 8 会测近万条线路、耗时半小时以上，故固定为 3
speed_test_per_channel_cap = 5  # 每频道最多测多少条线路，0 表示 target 的 4 倍；控制最坏耗时
open_speed_test_filter_host = False  # 同 host 共享测速结果，快但略不准
open_filter_ad = True       # 过滤广告/无信号循环源
open_filter_speed = False   # 按速率过滤（默认关，避免误杀低码率源）
open_filter_resolution = False  # 按分辨率过滤（需 full 模式 + ffprobe）
min_speed = 0.5             # 最低速率（MiB/s），需 open_filter_speed=True
min_resolution_value = 0    # 最低分辨率像素数，例如 1280*720=921600，0 表示不限
max_resolution_value = 0    # 最高分辨率像素数，0 表示不限
resolution_speed_map = {}   # 按分辨率设最低速率，例如 {"1920x1080": 0.5}
open_supply = False         # 补偿机制：线路不足时保留不达标但可能可用的源
sort_by = "speed"           # 排序维度，逗号分隔：speed、delay、resolution

# ---------- 测速算法内部常量（通常无需改） ----------
ad_filter_keywords = [      # 广告/无信号关键词，命中即过滤
    "no_signal", "nosignal", "no-signal", "signal_offline", "no_video", "novideo",
    "advertisement", "advert", "placeholder", "default_video", "cctv_off", "/ad/", "/ads/",
]
ad_max_loop_duration = 90   # 短于此时长的 ENDLIST 播放列表判为广告循环（秒）
m3u8_content_types = [      # 视为 m3u8 的 Content-Type
    "application/x-mpegurl", "application/vnd.apple.mpegurl", "audio/mpegurl", "audio/x-mpegurl",
]
segment_sample_limit = 2    # m3u8 取尾部几个分片参与测速
playlist_max_bytes = 2 * 1024 * 1024   # 播放列表最大读取字节数
default_ipv6_delay = 0.1    # 无 IPv6 时 IPv6 线路的替代延迟（毫秒）
default_ipv6_resolution = "1920x1080"

# ---------- 输出 ----------
logo_url = "https://gcore.jsdelivr.net/gh/yuanzl77/TVlogo@master/png"
logo_type = "png"
source_file = "config/demo.txt"            # 频道模板，由 loader 解析为绝对路径
cache_file = "output/data/cache.gz"        # 历史结果缓存
frozen_file = "output/data/frozen.gz"      # 冻结状态
run_state_file = "output/data/run_state.json"   # 本轮运行状态
runtime_log_dir = "output/log"             # 结构化日志双写目录(runtime.log + runtime.jsonl)

# ---------- 数据占位（由 loader 从 config/*.txt 覆盖） ----------
source_urls = []            # <- config/subscribe.txt
whitelist_subscribe_urls = []  # <- config/subscribe.txt 的 [WHITELIST] 段
source_url_headers = {}     # <- subscribe.txt 中每行可选的 UA="..."
url_blacklist = []          # <- config/blacklist.txt
epg_urls = []               # <- config/epg.txt

# ---------- HTTP 代理 ----------
# 仅作用于订阅源与 EPG 的抓取；测速、媒体探测保持直连。留空时与不设置完全一致。
# 注意：这里只能填代理服务器地址（如 Clash 的 "http://127.0.0.1:7890"），
# 不能填 gh-proxy.org 这类 URL 加速前缀——加速前缀请填到上面的 cdn_urls。
http_proxy = ""             # 例如 "http://127.0.0.1:7890"

# ---------- 自动停用失效源 ----------
# 开启后，抓取失败/内容为空/未匹配到频道的订阅源与 EPG 地址会在
# config/subscribe.txt、config/epg.txt 中自动加 "# " 注释。默认关闭以保持现状。
# 提示：GitHub Actions 工作流提交前会还原这两个文件，因此该改动不会入库；
#       需要持久保存请使用本地/服务器部署。
open_auto_disable_source = False

# ---------- IP 类型过滤与偏好 ----------
ipv_type = "all"            # 结果接口协议类型：ipv4 / ipv6 / all；all 表示不过滤（保持现状）
ipv_type_prefer = []        # 协议偏好顺序，如 ["ipv6", "ipv4"]；含 "auto" 时按 ipv6_support 展开；
                            # 留空时沿用 ip_version_priority 的单一偏好排序（保持现状）
ipv6_support = False        # 强制认为当前网络支持 IPv6（供 auto 展开判断使用）

# ---------- 线路备注与接口说明信息 ----------
# 线路备注开关：开启后在地址末尾输出 "$LIVE•IPV4『线路1』" 后缀；
# 默认关闭，输出纯地址（播放器兼容性最好，也便于直接复制）。
open_line_note = False
# 接口说明：开启后在地址末尾追加 来源/协议/分辨率 说明；默认关闭保持格式不变。
# 与线路备注同时开启时用 "-" 连接（如 "$LIVE•IPV4『线路1』-订阅源•IPV4"）。
open_url_info = False
origin_info_map = {         # 来源 -> $ 后缀显示的中文说明
    "subscribe": "订阅源",
    "whitelist": "白名单",
    "local": "本地源",
}

# ---------- 更新时间开关与位置 ----------
# 开启后在结果顶部或底部插入独立的 "更新时间,#genre#" 分类。默认关闭以保持格式不变。
open_update_time = False
update_time_position = "top"  # top / bottom

# ---------- 归属地 / 运营商过滤 ----------
# 均为白名单关键词（子串匹配），逗号分隔填写；留空表示不启用该维度筛选。
location = []               # 例如 ["北京", "广东"]，建议填靠近使用者的归属地
isp = []                    # 例如 ["电信", "联通", "移动"]
origin_type_prefer = []     # 来源偏好顺序，如 ["whitelist", "subscribe"]；留空保持现状排序
# 轻量归属地/运营商推断映射（本项目不含 IP 地址库，默认按源数据自带字段筛选）。
# 格式：{"IP前缀或host子串": {"location": "北京-北京", "isp": "电信"}}
location_isp_map = {}

# ---------- EPG 头部声明 ----------
# 结果 m3u 首行 x-tvg-url 指向哪个 EPG：
#   self   -> 本项目合并输出的 EPG（output/epg/epg.xml 的发布地址 <pages_base_url>/epg.xml）
#   source -> 沿用 config/epg.txt 里的原始 EPG 地址（旧行为）
epg_header_mode = "self"
# 站点根地址，如 https://52liulian.github.io/Live-tv（Actions 中由 PAGES_BASE_URL 注入）。
# 留空时即使 epg_header_mode=self，也会回落到 config/epg.txt 的原始地址。
pages_base_url = "https://52liulian.github.io/Live-tv"   # 变更站点域名时同步修改
