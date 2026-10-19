#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛季自动维护（GitHub Actions 侧）—— 客户端上报之外的**第二兜底**。

与 `shared/src/jvmSharedMain/kotlin/com/gimw/arco/data/season/` 下的
`SeasonCapture`（A1–A9）**同一条链路、同一份口径**，只是把执行者从玩家客户端
换成了 CI。解析规则一比一移植自 `SeasonHtmlParser.kt`：

    toPlainText        ← A5a + A5b（实体解码 → <t> 保留 inner → 剥其余标签）
    extractSeasonTimeBlock ← A5c（「〓赛季时间〓」到下一个「〓」）
    classifyEndpoint   ← A6（Absolute / OpenStart / OpenEnd，只归类不填补）
    parseWallClock     ← 年-月-日 与 年/月/日 共用一份

🔴 唯一写入口是 service_role 专用 RPC `arco_admin_upsert_season`
   （定义见 `.agent_work/season-authority.sql`），**不直接对表 INSERT**：
   好处是守卫触发器照常生效、换实现时 workflow 不用改。
🔴 幂等：同一 `season_key` 重复写 = 覆盖（ON CONFLICT DO UPDATE）。
🔴 本文件只依赖标准库（urllib / json / re / datetime），CI 上不做 pip install。

子命令：
    python season_sync.py parse --file <公告正文 html> [--list <annlist.json>]
        离线解析并打印 JSON（单测与人工核对用，不联网、不碰库）

    python season_sync.py sync
        联网全流程（CI 用）。退出码：0 = 已写入 / 本轮无需动 / 官方还没发公告；
        1 = 真异常（环境变量缺失、防御校验不过、RPC 失败、网络异常）⇒ 需要人看。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────
# 常量（逐字照抄 Kotlin 侧：ArcoConstants / SeasonCapture / AnnouncementHttp）
# ─────────────────────────────────────────────────────────────────────

#: Kotlin 侧是 `ZoneId.of("Asia/Shanghai")`（全工程唯一时区）。中国 1991 年后无夏令时，
#: ⇒ 固定 +08:00 与之等价；写死偏移是为了不依赖 zoneinfo/tzdata（Windows 与 CI 都稳）。
CST = timezone(timedelta(hours=8), "Asia/Shanghai")

TYPE_ID_GAME = 2  # 游戏公告（版本更新说明在这）
TYPE_ID_QIANXING = 26  # 千星奇域（赛季开启公告在这）

SOURCE_OFFICIAL = "OFFICIAL"  # §7.4：本期唯一取值
PARSED_BY = 1  # 与 Kotlin 侧 SeasonCapture.PARSED_BY 对齐（改了口径才 +1）

# 公告接口出口（AnnouncementHttp.kt 逐字照抄）
HOST_LIST = "https://hk4e-ann-api.mihoyo.com"
HOST_CONTENT = "https://hk4e-ann-static.mihoyo.com"
PATH_LIST = "/common/hk4e_cn/announcement/api/getAnnList"
PATH_CONTENT = "/common/hk4e_cn/announcement/api/getAnnContent"
USER_AGENT = "Arco/0.1 (Android; read-only announcement)"
BASE_QUERY = [
    ("game", "hk4e"),
    ("game_biz", "hk4e_cn"),
    ("lang", "zh-cn"),
    ("auth_appid", "announcement"),
    ("authkey_ver", "1"),
    ("bundle_id", "hk4e_cn"),
    ("channel_id", "1"),
    ("level", "60"),
    # 🔴 必须是 pc（实测 android/ios 返回 total=0），且必须带真实 uid（PITFALLS A4）
    ("platform", "pc"),
]
PLAYER_REGION = "cn_gf01"  # ArcoConstants.PLAYER_REGION（官服，不做渠道服）
GAME_UID = "261958214"  # 取证时的真实 gameUid，只做请求参数，不参与解析
RET_OK = 0

# 赛程块起止标记（SeasonHtmlParser.TIME_MARKER / MARKER_CHAR）
TIME_MARKER = "〓赛季时间〓"
MARKER_CHAR = "〓"

# 开放式端点的官方措辞（规格 §7.3 A6）
OPEN_START_TEXT = "版本更新后"
OPEN_END_TEXT = "版本结束"

# A3 的两个标题正则（SeasonCapture.SEASON_TITLE / VERSION_TITLE）
SEASON_TITLE = re.compile(r"「(.+?)」\s*赛季开启公告说明")
VERSION_TITLE = re.compile(r"「.+?」\s*(\d+\.\d+)\s*版本更新说明")

# ── 解析用正则（SeasonHtmlParser 逐字移植）──
T_TAG = re.compile(r"<t\b[^>]*>(.*?)</t>", re.DOTALL)  # A5a：高亮锚点，inner 留下
ANY_TAG = re.compile(r"<[^>]+>")  # A5b：其余标签一律剥掉
SEGMENT = re.compile(
    r"赛季(准备|游玩|展示)期：\s*(.+?)\s*~\s*(.+?)(?=\s*赛季(?:准备|游玩|展示)期：|\r?\n|$)"
)
WALL_CLOCK = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
KIND_BY_LABEL = {"准备": "prep", "游玩": "play", "展示": "show"}

# ── 防御校验阈值（与 season-authority.sql 的守卫触发器同口径）──
SEAM_TOLERANCE_SEC = 60  # 相邻赛季 whole_start == 上一季 whole_end（实测 gap=0），留 ±60s
MIN_SEASON_SPAN_SEC = 20 * 86400
MAX_SEASON_SPAN_SEC = 70 * 86400
#: 早退阈值：距当前赛季结束还早于此值 ⇒ 本轮不做任何写操作（省额度）。
#: 🔴 不是「拉早了就不再拉」——cron 每 30 分钟会再来一次，官方一发公告下一轮就抓到。
SKIP_MARGIN_SEC = 6 * 3600

HTTP_TIMEOUT_SEC = 30
HTTP_ATTEMPTS = 3

#: 仓库内的离线夹具（`parse` 子命令没给 --list 时的兜底；单测也读它）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LIST_FIXTURE = os.path.join(
    REPO_ROOT, "shared", "src", "jvmSharedTest", "resources", "annlist.json"
)


class SeasonValidationError(Exception):
    """防御校验不过 ⇒ 真异常，要人看（区别于「官方还没发公告」）。"""


# ─────────────────────────────────────────────────────────────────────
# 解析层（与 SeasonHtmlParser 一比一）
# ─────────────────────────────────────────────────────────────────────


def decode_entities(text: str) -> str:
    """A5a 前置：`&lt;` → `<` …；🔴 `&amp;` 必须**最后**替换。

    先换 `&amp;` 会把 `&amp;lt;` 二次解码成 `<`，把正文里的字面量当成标签
    （本接口正文里两类写法都真实存在）。
    """
    out = text
    for src, dst in (
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ):
        out = out.replace(src, dst)
    return out.replace("&amp;", "&")


def to_plain_text(content: str) -> str:
    """A5a + A5b：实体解码 → `<t>` 保留 inner → 剥其余标签。"""
    unwrapped = T_TAG.sub(lambda m: m.group(1), decode_entities(content))
    return ANY_TAG.sub("", unwrapped)


def extract_season_time_block(text: str, marker: str = TIME_MARKER):
    """A5c：截「〓赛季时间〓」到下一个「〓」之间；没有闭合标记时取到正文末尾。"""
    start_of = text.find(marker)
    if start_of < 0:
        return None
    start = start_of + len(marker)
    nxt = text.find(MARKER_CHAR, start)
    return text[start:] if nxt < 0 else text[start:nxt]


def parse_wall_clock(text):
    """墙上时间 → epoch 秒；空串或凑不出时间 → None。

    分隔符走 `\\D`：赛程正文里是「年/月/日」，公告 JSON 的 start_time/end_time 里是
    「年-月-日」，两者是同一种东西 ⇒ 只留这一份实现（口径一漂移就过不了对照断言）。
    """
    matched = WALL_CLOCK.search((text or "").strip())
    if matched is None:
        return None
    year, month, day = (int(matched.group(i)) for i in (1, 2, 3))
    hour, minute = int(matched.group(4)), int(matched.group(5))
    second = int(matched.group(6)) if matched.group(6) else 0
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=CST)
    except ValueError:  # 非法墙上时间（如 2026/02/31）⇒ 照实当解析不出
        return None
    return int(moment.timestamp())


def classify_endpoint(raw: str):
    """A6：端点归类（只标 Open，不填值）；空或解析不出 → None。

    返回 `("absolute", epoch)` / `("open_start", None)` / `("open_end", None)`。
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if OPEN_START_TEXT in trimmed:
        return ("open_start", None)
    if OPEN_END_TEXT in trimmed:
        return ("open_end", None)
    epoch_sec = parse_wall_clock(trimmed)
    if epoch_sec is None:
        return None
    return ("absolute", epoch_sec)


def parse_schedule(content: str):
    """公告正文（HTML）→ 三段赛程；解析不到赛程块或三段全缺 → None。

    每段形如 `{"start": <classify 结果 或 None>, "end": ...}`，键为 prep/play/show。
    """
    block = extract_season_time_block(to_plain_text(content))
    if block is None:
        return None
    segments = {}
    for match in SEGMENT.finditer(block):
        kind = KIND_BY_LABEL[match.group(1)]
        segments[kind] = {
            "start": classify_endpoint(match.group(2)),
            "end": classify_endpoint(match.group(3)),
        }
    if not segments:
        return None
    return {
        kind: segments.get(kind) for kind in ("prep", "play", "show")
    }


def _resolve(endpoint, version_start, version_end):
    """A6 填补：开放端点用版本公告 start/end 填；填不出 → None（照实存，不编造）。"""
    if endpoint is None:
        return None
    kind, value = endpoint
    if kind == "absolute":
        return value
    if kind == "open_start":
        return version_start
    return version_end


# ─────────────────────────────────────────────────────────────────────
# 列表 / 详情层（与 SeasonCapture A1–A5 一致）
# ─────────────────────────────────────────────────────────────────────


def _ann_groups(list_payload):
    return (list_payload.get("data") or {}).get("list") or []


def find_season_announcement(list_payload):
    """A2 + A3：`type_id=26` 分组里第一条 `「X」赛季开启公告说明` → (ann, "X")。"""
    for group in _ann_groups(list_payload):
        if group.get("type_id") != TYPE_ID_QIANXING:
            continue
        for ann in group.get("list") or []:
            found = SEASON_TITLE.search(ann.get("title") or "")
            if found:
                return ann, found.group(1)
    return None, None


def find_version_announcement(list_payload):
    """A3：`type_id=2` 分组里第一条 `「X」7.1版本更新说明` → (ann, "7.1")。"""
    for group in _ann_groups(list_payload):
        if group.get("type_id") != TYPE_ID_GAME:
            continue
        for ann in group.get("list") or []:
            found = VERSION_TITLE.search(ann.get("title") or "")
            if found:
                return ann, found.group(1)
    return None, None


def build_season(list_payload, content_html):
    """公告列表 + 赛季公告正文 → 落库用的赛季字典。

    🔴 返回 `(season, reason)`：解析不出赛季时 season=None、reason 是给人看的说明，
    属于「官方还没发 / 页面结构变了」的**软失败**，调用方应当 exit 0 而不是刷红。

    字段与 `arco_admin_upsert_season` 的 13 个参数一一对应（source/captured_at
    由 RPC 自己填，不在参数里）。
    """
    season_ann, season_name = find_season_announcement(list_payload)
    if season_ann is None:
        return None, "千星奇域分组（type_id=26）内没有赛季开启公告"

    version_ann, version_label = find_version_announcement(list_payload)
    if version_ann is None:
        return None, "游戏公告分组（type_id=2）内没有版本更新说明"
    version_start = parse_wall_clock(version_ann.get("start_time"))
    version_end = parse_wall_clock(version_ann.get("end_time"))

    schedule = parse_schedule(content_html)
    if schedule is None:
        return None, "annId=%s 解析不出「〓赛季时间〓」赛程块" % season_ann.get("ann_id")

    season = {
        "season_key": "%s-%s" % (version_label, season_name),
        "name": season_name,
        "version_label": version_label,
        "prep_start": _resolve(schedule["prep"]["start"] if schedule["prep"] else None, version_start, version_end),
        "prep_end": _resolve(schedule["prep"]["end"] if schedule["prep"] else None, version_start, version_end),
        "play_start": _resolve(schedule["play"]["start"] if schedule["play"] else None, version_start, version_end),
        "play_end": _resolve(schedule["play"]["end"] if schedule["play"] else None, version_start, version_end),
        "show_start": _resolve(schedule["show"]["start"] if schedule["show"] else None, version_start, version_end),
        "show_end": _resolve(schedule["show"]["end"] if schedule["show"] else None, version_start, version_end),
        # A7：整段赛季 = 版本公告起止
        "whole_start": version_start,
        "whole_end": version_end,
        "ann_id": season_ann.get("ann_id"),
        "source": SOURCE_OFFICIAL,
        "parsed_by": PARSED_BY,
    }
    return season, None


# ─────────────────────────────────────────────────────────────────────
# 防御校验（服务端触发器也会拦，这里先拦一遍 ⇒ 报错信息更清楚）
# ─────────────────────────────────────────────────────────────────────


def should_skip(now: int, latest_whole_end) -> bool:
    """③ 早退判据：`now < latest.whole_end - 6h` ⇒ 本轮跳过（省额度，不写库）。

    表为空（latest 为 None）时不早退，直接尝试捕获首季。
    判据写成纯函数是为了能在单测里卡边界，不联网也能验。
    """
    if latest_whole_end is None:
        return False
    return now < latest_whole_end - SKIP_MARGIN_SEC


def validate_season(season, latest_whole_end):
    """逐条对照 season-authority.sql 的守卫；任何一条不过 ⇒ 抛 SeasonValidationError。

    `latest_whole_end` 为 None 表示表里还没有任何赛季 ⇒ 跳过「无缝衔接」那一条。
    """
    if not season.get("ann_id"):
        raise SeasonValidationError("ann_id 为空（source=OFFICIAL 必须带公告 id）")

    whole_start = season.get("whole_start")
    whole_end = season.get("whole_end")
    if whole_start is None or whole_end is None:
        raise SeasonValidationError(
            "whole_start / whole_end 为空（版本公告缺 start_time/end_time 或解析不出）"
        )

    if latest_whole_end is not None:
        gap = whole_start - latest_whole_end
        if abs(gap) > SEAM_TOLERANCE_SEC:
            raise SeasonValidationError(
                "整段起点与上一季终点不衔接：解析出的 whole_start=%d，云端最晚一季 whole_end=%d，差 %d 秒"
                "（实测赛季无缝衔接，容差 ±%d 秒）" % (whole_start, latest_whole_end, gap, SEAM_TOLERANCE_SEC)
            )

    span = whole_end - whole_start
    if not (MIN_SEASON_SPAN_SEC <= span <= MAX_SEASON_SPAN_SEC):
        raise SeasonValidationError(
            "赛季全长异常：%d 秒（%.1f 天），应落在 %d~%d 秒（20~70 天）之间"
            % (span, span / 86400.0, MIN_SEASON_SPAN_SEC, MAX_SEASON_SPAN_SEC)
        )

    keys = ("prep_start", "prep_end", "play_start", "play_end", "show_start", "show_end")
    missing = [k for k in keys if season.get(k) is None]
    if missing:
        raise SeasonValidationError(
            "prep/play/show 三段存在空值：%s（守卫要求三段均不可为空）" % ", ".join(missing)
        )

    chain = [whole_start] + [season[k] for k in keys] + [whole_end]
    if any(a > b for a, b in zip(chain, chain[1:])):
        raise SeasonValidationError(
            "子区间顺序非法：须 whole_start ≤ prep ≤ play ≤ show ≤ whole_end，实测 %s" % chain
        )


# ─────────────────────────────────────────────────────────────────────
# HTTP（标准库 urllib）
# ─────────────────────────────────────────────────────────────────────


def http_get(url, headers=None, attempts=HTTP_ATTEMPTS):
    """带线性退避的 GET（海外 runner 到官方域名偶发超时，见 heat-sample.yml 的同类处置）。

    🔴 官方接口失败一律抛，绝不吞成空响应——吞空会被上层误判成「官方没发赛季公告」。
    """
    last = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:  # 含 socket 超时
            last = exc
            if attempt < attempts:
                print(
                    "::warning::GET 第 %d/%d 次失败：%s，退避 %ds 重试"
                    % (attempt, attempts, exc.__class__.__name__, attempt * 10)
                )
                time.sleep(attempt * 10)
    raise RuntimeError("GET 失败（已重试 %d 次）：%s" % (attempts, url.split("?")[0]))


def http_post_json(url, headers, payload):
    """POST JSON，返回响应文本。HTTP 层错误一律抛（含 PostgREST 的错误体，便于排查）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("POST %s HTTP %s：%s" % (url, exc.code, detail)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("POST %s 失败：%s" % (url, exc.__class__.__name__)) from exc


def _ann_url(host: str, path: str) -> str:
    query = BASE_QUERY + [
        ("region", PLAYER_REGION),
        ("sdk_presentation_style", "fullscreen"),
        ("sdk_screen_transparent", "true"),
        ("sign_type", "2"),
        ("uid", GAME_UID),
    ]
    return host + path + "?" + urllib.parse.urlencode(query)


def _decode_ann(api: str, text: str):
    """解码 + retcode 门禁：非 0 直接抛。"""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("%s 响应解析失败（非 JSON）" % api) from exc
    if payload.get("retcode") != RET_OK:
        raise RuntimeError("%s retcode=%s（message=%s）" % (api, payload.get("retcode"), payload.get("message")))
    return payload


def fetch_ann_list() -> dict:
    text = http_get(_ann_url(HOST_LIST, PATH_LIST), {"User-Agent": USER_AGENT})
    return _decode_ann("getAnnList", text)


def fetch_ann_content() -> dict:
    text = http_get(_ann_url(HOST_CONTENT, PATH_CONTENT), {"User-Agent": USER_AGENT})
    return _decode_ann("getAnnContent", text)


# ─────────────────────────────────────────────────────────────────────
# Supabase 侧
# ─────────────────────────────────────────────────────────────────────


def supabase_headers(key: str) -> dict:
    """service_role 的 apikey 与 Authorization 都用它；🔴 调用方不许打印本字典。"""
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }


def fetch_latest_whole_end(base_url: str, key: str):
    """查最晚一季的 whole_end（order=whole_end.desc&limit=1）；表空 ⇒ None。"""
    url = "%s/rest/v1/arco_seasons?select=season_key,whole_end&order=whole_end.desc&limit=1" % base_url.rstrip("/")
    rows = json.loads(http_get(url, supabase_headers(key)))
    if not rows:
        return None
    return rows[0].get("whole_end")


def rpc_upsert_season(base_url: str, key: str, season: dict) -> str:
    """调 service_role 专用 RPC `arco_admin_upsert_season`（参数名逐字照抄 SQL）。

    🔴 13 个参数，键名 = SQL 里的 `p_*` 参数名（PostgREST 的 RPC body 用带前缀的原名）。
    """
    payload = {
        "p_season_key": season["season_key"],
        "p_name": season["name"],
        "p_version": season["version_label"],
        "p_prep_start": season["prep_start"],
        "p_prep_end": season["prep_end"],
        "p_play_start": season["play_start"],
        "p_play_end": season["play_end"],
        "p_show_start": season["show_start"],
        "p_show_end": season["show_end"],
        "p_whole_start": season["whole_start"],
        "p_whole_end": season["whole_end"],
        "p_ann_id": season["ann_id"],
        "p_parsed_by": season["parsed_by"],
    }
    url = "%s/rest/v1/rpc/arco_admin_upsert_season" % base_url.rstrip("/")
    return http_post_json(url, supabase_headers(key), payload)


# ─────────────────────────────────────────────────────────────────────
# 子命令
# ─────────────────────────────────────────────────────────────────────


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def cmd_parse(args) -> int:
    """离线解析：正文 HTML（+ 公告列表，用于取版本公告起止）→ 打印 JSON。不联网、不碰库。"""
    html = _read_text(args.file)
    if args.list:
        list_path = args.list
        list_source = "--list"
    elif os.path.exists(DEFAULT_LIST_FIXTURE):
        list_path = DEFAULT_LIST_FIXTURE
        list_source = "仓库夹具"
    else:
        print(
            "::warning::未给 --list 且找不到仓库夹具 %s ⇒ 无版本公告起止，whole_* 将为 null"
            % DEFAULT_LIST_FIXTURE
        )
        list_path, list_source = None, None

    if list_path is None:
        season, reason = None, "缺少公告列表，无法定位版本公告"
    else:
        print("公告列表来源：%s（%s）" % (list_source, list_path))
        season, reason = build_season(json.loads(_read_text(list_path)), html)
    if season is None:
        print(json.dumps({"parsed": False, "reason": reason}, ensure_ascii=False, indent=2))
        return 0

    # 🔴 只打印解析出的字段，绝不打印正文 HTML（会刷爆日志）
    print(json.dumps({"parsed": True, "season": season}, ensure_ascii=False, indent=2))
    return 0


def cmd_sync(args) -> int:
    """联网全流程。退出码语义见模块 docstring。"""
    base_url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    missing = [n for n, v in (("SUPABASE_URL", base_url), ("SUPABASE_SERVICE_ROLE_KEY", key)) if not v]
    if missing:
        # 🔴 只报变量名，绝不报值（连长度都不打）
        print("::error::缺少环境变量：%s（service_role 密钥必须走 GitHub Secrets）" % ", ".join(missing))
        return 1

    # ② 最晚一季
    latest_whole_end = fetch_latest_whole_end(base_url, key)
    if latest_whole_end is None:
        print("云端 arco_seasons 为空 ⇒ 无早退判据，直接尝试捕获首季")
    else:
        print("云端最晚一季 whole_end=%d（%s）" % (latest_whole_end, _fmt_epoch(latest_whole_end)))

    # ③ 早退判据
    now = int(time.time())
    if should_skip(now, latest_whole_end):
        to_end = (latest_whole_end - now) / 3600.0
        to_gate = (latest_whole_end - SKIP_MARGIN_SEC - now) / 3600.0
        print("距赛季结束还有 %.1f 小时，距可写入窗口还有 %.1f 小时，本轮跳过" % (to_end, to_gate))
        print("（不是『拉早了就不再拉』：cron 每 30 分钟再来一次，官方一发公告下一轮就抓到）")
        return 0

    # ④ 抓列表 → 定位赛季公告 → 抓详情 → 解析
    list_payload = fetch_ann_list()
    season_ann, season_name = find_season_announcement(list_payload)
    if season_ann is None:
        print("公告尚未更新，等待下一轮（type_id=26 分组内没有赛季开启公告）")
        return 0
    print("定位到赛季公告：ann_id=%s 名称=%s" % (season_ann.get("ann_id"), season_name))

    content_payload = fetch_ann_content()  # 🔴 服务端忽略 ann_id，一次返回全部
    body = None
    for item in (content_payload.get("data") or {}).get("list") or []:
        if item.get("ann_id") == season_ann.get("ann_id"):
            body = item.get("content")
            break
    if body is None:
        print("公告尚未更新，等待下一轮（正文里缺少 annId=%s）" % season_ann.get("ann_id"))
        return 0

    season, reason = build_season(list_payload, body)
    if season is None:
        print("公告尚未更新，等待下一轮（%s）" % reason)
        return 0

    # ⑤ 防御校验
    print("解析结果：%s" % json.dumps(season, ensure_ascii=False))
    try:
        validate_season(season, latest_whole_end)
    except SeasonValidationError as exc:
        print("::error::防御校验不通过：%s" % exc)
        print("::error::解析出的全部字段：%s" % json.dumps(season, ensure_ascii=False))
        return 1
    print("防御校验通过（衔接 / 20~70 天 / 三段有序 / ann_id 非空）")

    # ⑥⑦ 写库
    result = rpc_upsert_season(base_url, key, season)
    print("arco_admin_upsert_season 返回：%s" % (result.strip() or "(空响应)"))
    print("::notice::赛季已写入：season_key=%s" % season["season_key"])
    return 0


def _fmt_epoch(epoch_sec: int) -> str:
    return datetime.fromtimestamp(epoch_sec, CST).strftime("%Y-%m-%d %H:%M:%S +08")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Arco 赛季自动维护（GitHub Actions 侧）")
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="离线解析公告正文 HTML，打印 JSON")
    parse_cmd.add_argument("--file", required=True, help="赛季公告正文 HTML 文件")
    parse_cmd.add_argument("--list", help="公告列表 JSON（不传则用仓库夹具 annlist.json）")
    parse_cmd.set_defaults(func=cmd_parse)

    sync_cmd = sub.add_parser("sync", help="联网全流程：抓公告 → 校验 → 写 arco_seasons")
    sync_cmd.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, OSError) as exc:
        print("::error::%s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())