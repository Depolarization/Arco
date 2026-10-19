#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""season_sync 的离线单测（纯标准库 unittest，**绝不联网、绝不碰库**）。

跑法（在 `.github/scripts/` 目录下）：
    python -m unittest discover
或：
    python -m unittest test_season_sync -v

夹具（2026-10-08 官方接口实证响应，与 Kotlin 侧 SeasonCaptureTest 用的是同一批）：
    ann-21900-season-raw.html  7.1「霄练赛季」开启公告正文
    annlist.json               getAnnList 响应（含版本更新说明 21894 的 start/end）
    anncontent.json            getAnnContent 响应（正文与上面那个 html 逐字相同）

🔴 ground truth 来自线上 `arco_seasons` 实测值，对不上就是解析口径漂了。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import season_sync  # noqa: E402

RESOURCES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared",
    "src",
    "jvmSharedTest",
    "resources",
)

# 🔴 夹具双位置查找（2026-10-19 线上首跑踩到）：
#    本 workflow 跑在**运维线 main** 上，而 main 里**没有源码**（只有 update.json + workflows），
#    所以 `shared/src/jvmSharedTest/resources/` 在 CI 里根本不存在 —— 首跑就是因此红的。
#    修法 = 夹具随脚本一起放到 `.github/scripts/fixtures/`（自包含，不依赖源码线）；
#    本地源码树里仍优先用 shared 那份（两边内容必须一致，由 test_fixture_copies_are_identical 兜底）。
_LOCAL_RESOURCES = RESOURCES
_BUNDLED_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name: str) -> str:
    for base in (_BUNDLED_FIXTURES, _LOCAL_RESOURCES):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        f"夹具 {name} 在 {_BUNDLED_FIXTURES} 与 {_LOCAL_RESOURCES} 都不存在"
    )


HTML_FIXTURE = _fixture("ann-21900-season-raw.html")
LIST_FIXTURE = _fixture("annlist.json")
CONTENT_FIXTURE = _fixture("anncontent.json")

# 线上 arco_seasons 实测 ground truth
GT_WHOLE_START = 1790118000  # 2026-09-23 07:00:00 +08
GT_WHOLE_END = 1793743200  # 2026-11-04 06:00:00 +08
GT_ANN_ID = 21900
GT_NAME = "霄练赛季"


def read(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class SeasonParseTest(unittest.TestCase):
    """① 7.1 公告解析出 ground truth 四个值（+ 六段子窗口，对齐 Kotlin 侧断言）。"""

    def setUp(self):
        self.list_payload = json.loads(read(LIST_FIXTURE))
        self.html = read(HTML_FIXTURE)
        self.season, self.reason = season_sync.build_season(self.list_payload, self.html)

    def test_ground_truth_whole_start(self):
        self.assertIsNotNone(self.season, "解析失败：%s" % self.reason)
        self.assertEqual(GT_WHOLE_START, self.season["whole_start"])

    def test_ground_truth_whole_end(self):
        self.assertIsNotNone(self.season, "解析失败：%s" % self.reason)
        self.assertEqual(GT_WHOLE_END, self.season["whole_end"])

    def test_ground_truth_ann_id_and_name(self):
        self.assertIsNotNone(self.season, "解析失败：%s" % self.reason)
        self.assertEqual(GT_ANN_ID, self.season["ann_id"])
        self.assertIn("霄练", self.season["name"])

    def test_identity_fields(self):
        self.assertEqual("7.1-霄练赛季", self.season["season_key"])
        self.assertEqual("7.1", self.season["version_label"])
        self.assertEqual("OFFICIAL", self.season["source"])
        self.assertEqual(1, self.season["parsed_by"])

    def test_three_segments(self):
        """A6：开放式端点被版本起止填补，绝对端点保持公告原值（秒级逐字对齐）。"""
        self.assertEqual(
            {
                "prep_start": 1790118000,  # 2026/09/23 07:00（版本开始）
                "prep_end": 1790215140,  # 2026/09/24 09:59
                "play_start": 1790215200,  # 2026/09/24 10:00
                "play_end": 1793649540,  # 2026/11/03 03:59
                "show_start": 1793649600,  # 2026/11/03 04:00
                "show_end": 1793743200,  # 2026/11/04 06:00（版本结束）
            },
            {k: self.season[k] for k in
             ("prep_start", "prep_end", "play_start", "play_end", "show_start", "show_end")},
        )

    def test_ground_truth_from_anncontent_fixture(self):
        """同一份正文走 anncontent.json 里的那个 ann_id ⇒ 结果必须一致。"""
        payload = json.loads(read(CONTENT_FIXTURE))
        body = [a for a in payload["data"]["list"] if a["ann_id"] == GT_ANN_ID][0]["content"]
        self.assertEqual(self.html, body, "两个夹具的正文应当逐字相同")
        season, reason = season_sync.build_season(self.list_payload, body)
        self.assertIsNotNone(season, "解析失败：%s" % reason)
        self.assertEqual(self.season, season)

    def test_missing_version_window_stays_null(self):
        """版本公告缺 start/end ⇒ 照实存 null（照抄 Kotlin 侧「不编造」语义），由校验层拒。"""
        payload = json.loads(read(LIST_FIXTURE))
        for group in payload["data"]["list"]:
            if group.get("type_id") == season_sync.TYPE_ID_GAME:
                group["list"][1]["start_time"] = None
                group["list"][1]["end_time"] = None
        season, _ = season_sync.build_season(payload, self.html)
        self.assertIsNone(season["whole_start"])
        self.assertIsNone(season["whole_end"])
        with self.assertRaises(season_sync.SeasonValidationError):
            season_sync.validate_season(season, None)

    def test_whole_clock_parses_both_separators(self):
        """同一份 WALL_CLOCK 正则吃「年/月/日」与「年-月-日」（口径不得分裂）。"""
        self.assertEqual(GT_WHOLE_START, season_sync.parse_wall_clock("2026-09-23 07:00:00"))
        self.assertEqual(GT_WHOLE_START, season_sync.parse_wall_clock("2026/09/23 07:00"))
        self.assertIsNone(season_sync.parse_wall_clock("版本更新后"))
        self.assertIsNone(season_sync.parse_wall_clock(None))

    def test_entity_decoding_order_is_amp_last(self):
        """🔴 `&amp;` 必须最后解码，否则 `&amp;lt;` 会变成字面量 `<` 当成标签。"""
        self.assertEqual("<b>x</b>", season_sync.decode_entities("&lt;b&gt;x&lt;/b&gt;"))
        self.assertEqual("&lt;b&gt;", season_sync.decode_entities("&amp;lt;b&amp;gt;"))

    def test_t_tag_inner_is_kept_and_other_tags_stripped(self):
        plain = season_sync.to_plain_text('<p>a</p><t class="x">2026/09/24 10:00</t><span>b</span>')
        self.assertEqual("a2026/09/24 10:00b", plain)

    def test_unrelated_announcement_is_soft_failure(self):
        """官方还没发新赛季公告 ⇒ 软失败（reason 非空、season 为 None），不抛。"""
        empty_list = {"retcode": 0, "data": {"list": [{"type_id": 26, "list": []}]}}
        season, reason = season_sync.build_season(empty_list, self.html)
        self.assertIsNone(season)
        self.assertTrue(reason)

    def test_unparsable_body_is_soft_failure(self):
        season, reason = season_sync.build_season(self.list_payload, "<p>没有赛程块</p>")
        self.assertIsNone(season)
        self.assertIn("赛程块", reason)


class AnnouncementLookupTest(unittest.TestCase):
    """② 列表 JSON 能正确取出赛季公告的 ann_id（以及版本公告的标号与起止）。"""

    def setUp(self):
        self.list_payload = json.loads(read(LIST_FIXTURE))

    def test_season_announcement_ann_id(self):
        ann, name = season_sync.find_season_announcement(self.list_payload)
        self.assertEqual(GT_ANN_ID, ann["ann_id"])
        self.assertEqual(GT_NAME, name)

    def test_version_announcement_label_and_window(self):
        ann, label = season_sync.find_version_announcement(self.list_payload)
        self.assertEqual("7.1", label)
        self.assertEqual(GT_WHOLE_START, season_sync.parse_wall_clock(ann["start_time"]))
        self.assertEqual(GT_WHOLE_END, season_sync.parse_wall_clock(ann["end_time"]))

    def test_missing_season_group(self):
        only_game = {"retcode": 0, "data": {"list": [{"type_id": 2, "list": []}]}}
        ann, name = season_sync.find_season_announcement(only_game)
        self.assertIsNone(ann)
        self.assertIsNone(name)


class ValidationTest(unittest.TestCase):
    """③④ 防御校验：衔接不成立、窗口 100 天 ⇒ 必须被拒（真异常，退出码 1）。"""

    def setUp(self):
        self.list_payload = json.loads(read(LIST_FIXTURE))
        season, reason = season_sync.build_season(self.list_payload, read(HTML_FIXTURE))
        self.assertIsNotNone(season, "夹具解析失败：%s" % reason)
        self.season = season

    def test_baseline_passes_validation(self):
        """ground truth 配上「上一季终点 == 本季起点」（实测 gap=0）必须过，否则测试本身没意义。"""
        season_sync.validate_season(self.season, self.season["whole_start"])

    def test_seam_mismatch_is_rejected(self):
        """故意让 whole_start 与云端最晚一季 whole_end 不等 ⇒ 必须抛。"""
        latest = self.season["whole_end"] - 5 * 86400  # 云端上一季还剩 5 天才结束
        tampered = dict(self.season)  # whole_start 保持 1790118000
        with self.assertRaises(season_sync.SeasonValidationError) as ctx:
            season_sync.validate_season(tampered, latest)
        self.assertIn("不衔接", str(ctx.exception))

    def test_seam_within_tolerance_is_accepted(self):
        """±60s 容差内必须放行（时钟/官方文案抖动不算异常）。"""
        season_sync.validate_season(dict(self.season), GT_WHOLE_START - 60)
        season_sync.validate_season(dict(self.season), GT_WHOLE_START + 60)

    def test_hundred_day_window_is_rejected(self):
        """故意把窗口改成 100 天（远超 70 天上限）⇒ 必须抛。"""
        tampered = dict(self.season)
        tampered["whole_end"] = tampered["whole_start"] + 100 * 86400
        tampered["show_end"] = tampered["whole_end"]
        with self.assertRaises(season_sync.SeasonValidationError) as ctx:
            season_sync.validate_season(tampered, None)
        self.assertIn("全长异常", str(ctx.exception))

    def test_twenty_day_window_is_also_rejected(self):
        """下限同样要卡：20 天整点通过，19 天拒绝（子区间跟着一起收窄，保持自洽）。"""
        def retimed(span_days):
            season = dict(self.season)
            season["whole_end"] = season["whole_start"] + span_days * 86400
            season["play_end"] = season["whole_end"]
            season["show_start"] = season["whole_end"]
            season["show_end"] = season["whole_end"]
            return season

        season_sync.validate_season(retimed(20), None)
        with self.assertRaises(season_sync.SeasonValidationError):
            season_sync.validate_season(retimed(19), None)

    def test_missing_ann_id_is_rejected(self):
        with self.assertRaises(season_sync.SeasonValidationError):
            season_sync.validate_season(dict(self.season, ann_id=None), None)

    def test_missing_segment_is_rejected(self):
        with self.assertRaises(season_sync.SeasonValidationError) as ctx:
            season_sync.validate_season(dict(self.season, show_end=None), None)
        self.assertIn("show_end", str(ctx.exception))

    def test_out_of_order_segments_are_rejected(self):
        tampered = dict(self.season, play_end=self.season["show_start"] + 1)
        with self.assertRaises(season_sync.SeasonValidationError) as ctx:
            season_sync.validate_season(tampered, None)
        self.assertIn("顺序非法", str(ctx.exception))


class SkipGateTest(unittest.TestCase):
    """早退判据：距结束还有 >6h ⇒ 跳过；到窗口 ⇒ 动手；表空 ⇒ 不跳。"""

    def test_skips_while_season_still_running(self):
        self.assertTrue(season_sync.should_skip(1793743200 - 7 * 3600, 1793743200))

    def test_runs_once_inside_window(self):
        self.assertFalse(season_sync.should_skip(1793743200 - 5 * 3600, 1793743200))
        self.assertFalse(season_sync.should_skip(1793743200 + 10 * 86400, 1793743200))

    def test_empty_table_never_skips(self):
        self.assertFalse(season_sync.should_skip(1_700_000_000, None))


class RpcPayloadTest(unittest.TestCase):
    """写库 payload：13 个参数，键名逐字等于 SQL 里的 p_* 参数名。"""

    EXPECTED_KEYS = [
        "p_season_key", "p_name", "p_version",
        "p_prep_start", "p_prep_end", "p_play_start", "p_play_end",
        "p_show_start", "p_show_end", "p_whole_start", "p_whole_end",
        "p_ann_id", "p_parsed_by",
    ]

    def test_payload_keys_match_sql_signature(self):
        captured = {}

        def fake_post(url, headers, payload):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            return '"ok:7.1-霄练赛季"'

        list_payload = json.loads(read(LIST_FIXTURE))
        season, _ = season_sync.build_season(list_payload, read(HTML_FIXTURE))

        original = season_sync.http_post_json
        season_sync.http_post_json = fake_post
        try:
            season_sync.rpc_upsert_season("https://example.supabase.co", "SECRET", season)
        finally:
            season_sync.http_post_json = original

        self.assertEqual(sorted(self.EXPECTED_KEYS), sorted(captured["payload"]))
        self.assertEqual(13, len(captured["payload"]))
        self.assertTrue(captured["url"].endswith("/rest/v1/rpc/arco_admin_upsert_season"))
        self.assertEqual("SECRET", captured["headers"]["apikey"])
        self.assertEqual("Bearer SECRET", captured["headers"]["Authorization"])
        self.assertEqual(GT_WHOLE_START, captured["payload"]["p_whole_start"])
        self.assertEqual(GT_WHOLE_END, captured["payload"]["p_whole_end"])
        self.assertEqual(GT_ANN_ID, captured["payload"]["p_ann_id"])


class CliTest(unittest.TestCase):
    """`parse` 子命令必须打印可解析 JSON，且给出 ground truth（CI 上单测跑的就是它）。"""

    def test_parse_command_prints_ground_truth_json(self):
        proc = subprocess.run(
            [
                sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "season_sync.py"),
                "parse", "--file", HTML_FIXTURE, "--list", LIST_FIXTURE,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout[proc.stdout.index("{"):])
        self.assertTrue(payload["parsed"], payload)
        season = payload["season"]
        self.assertEqual(GT_WHOLE_START, season["whole_start"])
        self.assertEqual(GT_WHOLE_END, season["whole_end"])
        self.assertEqual(GT_ANN_ID, season["ann_id"])
        self.assertIn("霄练", season["name"])

    def test_sync_without_env_exits_1_without_leaking(self):
        """缺环境变量 ⇒ exit 1；🔴 日志里不得出现密钥的任何片段。"""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "season_sync.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUPABASE_")}
        env["SUPABASE_SERVICE_ROLE_KEY"] = "super-secret-key-value"
        proc = subprocess.run(
            [sys.executable, script, "sync"], capture_output=True, text=True, env=env, encoding="utf-8"
        )
        self.assertEqual(1, proc.returncode)
        self.assertIn("SUPABASE_URL", proc.stdout)
        self.assertNotIn("super-secret-key-value", proc.stdout + proc.stderr)


class FixtureIntegrityTest(unittest.TestCase):
    """🔴 两份夹具必须逐字节一致：源码树一份、`.github/scripts/fixtures/` 一份。

    起因：workflow 跑在运维线 main 上，那里没有源码树，只能用随脚本打包的那份；
    而本地开发/源码线用 shared 那份。若只改一边，CI 用旧夹具会「测试绿但解析已坏」。
    """

    def test_fixture_copies_are_identical(self):
        if not os.path.isdir(_LOCAL_RESOURCES):
            self.skipTest("当前环境没有源码树（运维线 main），跳过一致性校验")
        for name in ("annlist.json", "anncontent.json", "ann-21900-season-raw.html"):
            src = os.path.join(_LOCAL_RESOURCES, name)
            dst = os.path.join(_BUNDLED_FIXTURES, name)
            with open(src, "rb") as f:
                a = f.read()
            with open(dst, "rb") as f:
                b = f.read()
            self.assertEqual(a, b, f"{name} 两份夹具内容不一致：请同步 .github/scripts/fixtures/")


if __name__ == "__main__":
    unittest.main()