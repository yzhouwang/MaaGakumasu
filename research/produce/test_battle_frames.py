"""战斗画面识别回归：课程 HUD、考试追加回合与元气、体力数字筛选、同形 Buff 图标区分、控制台 HUD 文案与 GHL 实况帧整理。

使用替身 OCR（按识别区域返回预设文字），不读取真实截图、不加载模板；需要安装 maafw（读取器模块的导入链）。
"""

import sys
import json
import math
import shutil
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
from ghl_frame import ghl_frame
from produce.state import (
    EXAM_HUD,
    TURN_RING,
    LESSON_HUD,
    BattleStateReader,
    turn_ring,
    turn_fields,
    buff_overflow,
    majority_color,
    added_turns_field,
    exam_genki_field,
    masked_correlation,
    buff_column_occluded,
)
from produce.console import state_lines

IMAGE = np.zeros((1280, 720, 3), dtype=np.uint8)


def token(text, box, confidence=1.0):
    return {"text": text, "confidence": confidence, "box": box}


def reader(screen):
    """screen 为 {识别区域: 文字列表}；未登记的区域没有文字。模板登记表不存在，Buff 一律不读。"""
    return BattleStateReader(Path("missing_registry.json"), lambda array, roi, only_rec=False: screen.get(tuple(roi), []))


def lesson_screen(goal_label, goal, bonus=()):
    return {
        tuple(LESSON_HUD["label"]): [token("残りターン数", [36, 37, 126, 26])],
        tuple(LESSON_HUD["turns"]): [token("5+1", [83, 64, 76, 45])],
        tuple(LESSON_HUD["goal_label"]): [token(goal_label, [290, 95, 136, 23])],
        tuple(LESSON_HUD["goal"]): [token(goal, [301, 117, 115, 56])],
        tuple(LESSON_HUD["bonus"]): list(bonus) or [token("433", [600, 41, 80, 39])],
        # 心形图标被识别为单独的文字时按中心横坐标排除。
        tuple(LESSON_HUD["stamina"]): [token("2", [540, 172, 20, 30], 0.8), token("26", [566, 168, 56, 39])],
        tuple(LESSON_HUD["genki"]): [token("12", [646, 135, 43, 32])],
    }


class TurnParsingTest(unittest.TestCase):
    def test_remaining_and_added_turns(self):
        for raw, expected in [
            ([token("8", [84, 66, 37, 43])], (8, 0)),
            ([token("5+1", [83, 64, 76, 45])], (5, 1)),
            ([token("0+2", [83, 64, 76, 45])], (0, 2)),
            ([token("+1", [120, 64, 40, 45]), token("5", [83, 64, 37, 45])], (5, 1)),
        ]:
            turns, added = turn_fields(raw, [0, 0, 1, 1])
            self.assertEqual((turns["value"], added["value"]), expected)
        turns, added = turn_fields([token("ターン", [0, 0, 1, 1])], [0, 0, 1, 1])
        self.assertIsNone(turns["value"])
        self.assertIsNone(added["value"])

    def test_buff_overflow_marker(self):
        self.assertTrue(buff_overflow([token("doo", [39, 727, 7, 21], 0.56)]))
        self.assertTrue(buff_overflow([token("000", [36, 725, 14, 26], 0.93)]))
        # 普通数值（宽）不是「⋮」。
        self.assertFalse(buff_overflow([token("000", [36, 725, 40, 26])]))
        self.assertFalse(buff_overflow([token("—", [0, 640, 120, 200])]))

    def test_buff_column_occlusion(self):
        self.assertTrue(buff_column_occluded([token("好印象が15以上の場", [110, 187, 210, 26])]))
        values = [token("1ターン", [62, 300, 80, 24]), token("ターン内1回", [62, 540, 120, 24]), token("夕ーン内1回", [56, 631, 120, 30])]
        values += [token("レッスン中1回", [116, 737, 119, 22]), token("65%", [62, 330, 60, 24])]
        self.assertFalse(buff_column_occluded(values))

    def test_exam_added_turn_pill(self):
        self.assertEqual(added_turns_field([token("+1", [99, 120, 53, 34])], EXAM_HUD["added"])["value"], 1)
        absent = added_turns_field([], EXAM_HUD["added"])
        self.assertEqual((absent["value"], absent["status"]), (0, "not_shown"))
        self.assertIsNone(added_turns_field([token("+1", [99, 120, 53, 34]), token("+2", [99, 120, 53, 34])], EXAM_HUD["added"])["value"])

    def test_exam_genki_is_the_whole_number(self):
        # 窄框只见后两位：508 读作 8、194 读作 94、1138 读作 138；整数框读出整个数，窄框读出的须是它的末几位。
        roi = EXAM_HUD["genki_whole"]

        def genki(text, narrow, confidence=1.0):
            seen = {"value": narrow, "confidence": 1.0 if narrow is not None else None, "roi": EXAM_HUD["genki"][0], "raw_ocr": []}
            return exam_genki_field([token(text, roi, confidence)] if text is not None else [], roi, seen)["value"]

        for text, narrow, expected in [("508", 8, 508), ("194", 94, 194), ("1138", 138, 1138), ("207", 7, 207), ("81", None, 81)]:
            self.assertEqual(genki(text, narrow), expected)
        # 一两位数时露出的体力条末端读成数字前的「D」或「0」，盾形右缘读成数字后的「-」；②经 NFKC 为 2。
        for text, narrow, expected in [("D45", 45, 45), ("045", 45, 45), ("477-", 77, 477), ("D②", 2, 2)]:
            self.assertEqual(genki(text, narrow, 0.9), expected)
        # 置信度低的读数须与窄框相同。
        self.assertEqual(genki("0", 0, 0.52), 0)
        self.assertIsNone(genki("2", None, 0.53))
        # 与窄框矛盾、读不出数字或没有读数时为未知，不退回窄框。
        self.assertIsNone(genki("407", 9))
        self.assertIsNone(genki("回", 0, 0.48))
        self.assertIsNone(genki(None, 8))

    def test_exam_reader_takes_genki_from_the_whole_number_box(self):
        screen = {tuple(roi): [token("8", [690, 178, 20, 32])] for roi in EXAM_HUD["genki"]}
        screen[tuple(EXAM_HUD["genki_whole"])] = [token("508", EXAM_HUD["genki_whole"])]
        # 回合与体力也读得出，HUD 才算在场。
        screen.update({tuple(roi): [token("3", [52, 72, 24, 40])] for roi in EXAM_HUD["turns"]})
        screen[tuple(EXAM_HUD["stamina"])] = [token("26", [566, 210, 56, 39])]
        state = reader(screen).read(IMAGE)
        self.assertEqual((state["layout"], state["fields"]["genki"]["value"]), ("exam", 508))


class LessonHudTest(unittest.TestCase):
    def test_before_clear(self):
        state = reader(lesson_screen("CLEARまで", "90")).read(IMAGE)
        fields = state["fields"]
        self.assertEqual(state["layout"], "lesson")
        self.assertTrue(state["hud_present"])
        self.assertEqual((fields["turns_remaining"]["value"], fields["turns_added"]["value"]), (5, 1))
        self.assertEqual(fields["lesson_to_clear"]["value"], 90)
        self.assertIsNone(fields["lesson_bonus"]["value"])
        self.assertEqual((fields["stamina"]["value"], fields["genki"]["value"]), (26, 12))
        self.assertIsNone(fields["score"]["value"])
        self.assertEqual(state_lines(state)[:2], ["状态：剩 5+1 回合｜体 26｜元 12", "课程：距 CLEAR 90"])

    def test_after_clear(self):
        bonus = [token("レジェンド", [511, 38, 100, 23]), token("ボーナス", [517, 60, 84, 24]), token("41", [625, 43, 54, 39])]
        state = reader(lesson_screen("PERFECTまで", "109", bonus)).read(IMAGE)
        fields = state["fields"]
        self.assertEqual((fields["lesson_to_perfect"]["value"], fields["lesson_bonus"]["value"]), (109, 41))
        self.assertIsNone(fields["lesson_to_clear"]["value"])
        self.assertEqual(state_lines(state)[1], "课程：已 CLEAR｜距 PERFECT 109｜传说加成 41")

    def test_exam_screen_is_not_a_lesson(self):
        screen = {tuple(LESSON_HUD["label"]): [token("残りターン", [14, 10, 110, 24])]}
        self.assertNotEqual(reader(screen).read(IMAGE)["layout"], "lesson")


class BadgeDiscriminationTest(unittest.TestCase):
    """好印象效果与好印象增加量整体得分几乎相同，只按右上角标记区域区分。"""

    @staticmethod
    def badge(mark):
        icon = np.full((56, 56, 3), 200, dtype=np.uint8)
        icon[:, 42:] = (0, 255, 0)  # 遮罩
        icon[4:24, 26:42] = 120
        if mark == "chevron":
            icon[6:10, 28:40] = 255
            icon[14:18, 28:40] = 255
        else:
            icon[6:22, 32:36] = 255
        return icon

    def setUp(self):
        self.reader = BattleStateReader(Path("missing_registry.json"), lambda array, roi: [], image_root=Path("."))
        self.reader._template_pixels = {"effect": self.badge("chevron"), "gain": self.badge("percent")}
        rule = {"group": "good_impression_badge", "region": [26, 4, 16, 20]}
        self.effect = {"definition": {"id": "effect", "key": "goodImpressionEffectPercent", "discriminator": rule}, "box": [16, 600, 56, 56], "score": 0.99}
        self.gain = {"definition": {"id": "gain", "key": "goodImpressionGainPercent", "discriminator": rule}, "box": [16, 600, 56, 56], "score": 0.985}
        self.plain = {"definition": {"id": "plain", "key": "goodImpressionTurns"}, "box": [16, 600, 56, 56], "score": 0.92}

    def frame_with(self, mark):
        frame = np.zeros((1280, 720, 3), dtype=np.uint8)
        icon = self.badge(mark)
        icon[:, 42:] = 30  # 画面上该处是数值文字
        frame[600:656, 16:72] = icon
        return frame

    def test_correlation_ignores_mask(self):
        icon = self.badge("chevron")
        self.assertAlmostEqual(masked_correlation(icon.copy(), icon), 1.0)
        noisy = icon.copy()
        noisy[:, 42:] = 7
        self.assertAlmostEqual(masked_correlation(noisy, icon), 1.0)

    def test_mark_decides_between_kin(self):
        group, evidence = self.reader._discriminated(self.frame_with("percent"), [self.effect, self.gain, self.plain])
        self.assertEqual([hit["definition"]["id"] for hit in group], ["gain", "plain"])
        self.assertEqual(evidence["correlations"][0]["id"], "gain")
        group, _ = self.reader._discriminated(self.frame_with("chevron"), [self.gain, self.effect])
        self.assertEqual(group[0]["definition"]["id"], "effect")

    def test_unclear_mark_stays_ambiguous(self):
        frame = self.frame_with("chevron")
        frame[604:624, 42:58] = 120  # 标记区域被遮挡
        group, evidence = self.reader._discriminated(frame, [self.effect, self.gain])
        self.assertEqual(len(group), 2)
        self.assertIsNotNone(evidence)


class FadedFallbackTest(unittest.TestCase):
    """褪色模板（fallback）只在同一位置没有其他 Buff 的常规模板达到阈值、且读到数值时采信。"""

    TEMP = ROOT / "debug" / "research" / "produce" / "tests" / "battle_frames"

    @staticmethod
    def entry(template_id, key, fallback=False):
        entry = {
            "id": template_id,
            "key": key,
            "name": key,
            "unit": "turns",
            "template": f"produce/buffs/{template_id}.png",
            "threshold": 0.94,
            "method": 10001,
            "roi": [0, 232, 84, 538],
            "reading": {"format": "turns", "roi_offset": [36, 23, 125, 45]},
        }
        return {**entry, "fallback": True} if fallback else entry

    def setUp(self):
        self.TEMP.mkdir(parents=True, exist_ok=True)
        self.registry = self.TEMP / "registry.json"
        entries = [self.entry("cost", "halfCostTurns"), self.entry("condition", "goodConditionTurns"), self.entry("cost_faded", "halfCostTurns", True)]
        entries.append(self.entry("score_faded", "scoreBuffPercent", True))
        self.registry.write_text(json.dumps({"buffs": entries}), encoding="utf-8")

    def tearDown(self):
        target = self.TEMP.resolve()
        if target.is_relative_to((ROOT / "debug").resolve()):
            shutil.rmtree(target)

    def read(self, scores, value="1ターン"):
        box = [16, 481, 56, 56]
        screen = lesson_screen("CLEARまで", "90")

        def ocr(array, roi):
            if tuple(roi) in screen:
                return screen[tuple(roi)]
            return [token(value, [roi[0] + 2, roi[1] + 2, 60, 30])] if value and roi[0] < 200 and roi[1] >= 200 else []

        def match(array, definition):
            return [{"score": scores[definition["id"]], "box": box}] if definition["id"] in scores else []

        return BattleStateReader(self.registry, ocr, match).read(IMAGE)

    def test_faded_of_another_buff_cannot_shadow_a_regular_match(self):
        state = self.read({"cost": 0.99, "score_faded": 0.985})
        self.assertEqual([(buff["key"], buff["value"]) for buff in state["buffs"]], [("halfCostTurns", 1)])

    def test_faded_of_the_same_buff_breaks_a_near_tie(self):
        state = self.read({"cost_faded": 1.0, "cost": 0.952, "condition": 0.939})
        self.assertEqual([buff["key"] for buff in state["buffs"]], ["halfCostTurns"])

    def test_faded_without_a_value_is_not_a_buff(self):
        state = self.read({"score_faded": 0.97}, value=None)
        self.assertEqual(state["buffs"], [])
        self.assertIn("fallback_without_value", [item.get("reason") for item in state["unknown_buffs"]])


class TurnRingTest(unittest.TestCase):
    """考试回合环：自正上方标记处起逆时针各段的颜色，已过的回合原位变暗。"""

    COLORS = {"Vi": (40, 170, 240), "Vo": (140, 60, 230), "Da": (240, 140, 30)}  # BGR

    def ring_image(self, types, elapsed=0):
        image = np.full((1280, 720, 3), (235, 215, 190), dtype=np.uint8)  # 浅色背景
        (cx, cy), radius, count = TURN_RING["center"], TURN_RING["radius"], len(types)
        for y in range(cy - radius - 8, cy + radius + 9):
            for x in range(cx - radius - 8, cx + radius + 9):
                distance = math.hypot(x - cx, y - cy)
                if distance > radius + 6:
                    continue
                image[y, x] = (60, 45, 40)  # 环底色（暗）
                if abs(distance - radius) > 4:
                    continue
                # 逆时针自正上方起的角度；段之间留缝。
                angle = math.degrees(math.atan2(cx - x, cy - y)) % 360
                index, within = divmod(angle, 360 / count)
                if 4 < within < 360 / count - 4 and int(index) >= elapsed:
                    image[y, x] = self.COLORS[types[int(index)]]
        return image

    def test_full_ring_at_the_start(self):
        types = ["Vi", "Vo", "Vi", "Vo", "Vi", "Da", "Vi", "Da", "Vo", "Vi"]
        self.assertEqual(turn_ring(self.ring_image(types), 10), {"segments": 10, "remaining": types, "status": "observed"})

    def test_elapsed_turns_go_dark_in_place(self):
        types = ["Vi", "Da", "Vo", "Vo", "Da", "Vi", "Vo", "Vi", "Vi", "Da", "Vo", "Vi"]
        ring = turn_ring(self.ring_image(types, elapsed=5), 7)
        self.assertEqual((ring["segments"], ring["remaining"], ring["status"]), (12, types[5:], "observed"))
        # 剩余回合与色段数不一致：不作为已确认的回合属性。
        self.assertEqual(turn_ring(self.ring_image(types, elapsed=5), 8)["status"], "partial")

    def test_tied_votes_are_settled_the_same_way_every_run(self):
        # 标记盖住当前回合段时属性色与「?」常同票；决胜不随集合遍历顺序（Python 的哈希种子）变化。
        for votes, expected in [
            (["Vi", "?", "Vi", "?", None], "Vi"),
            ([None, "Da", None, "Da", "?"], "Da"),
            (["Vi", "Vo", "Vi", "Vo"], "?"),
            (["?", None, "?", None, "Vi"], None),
            (["Vo", "Vo", "?"], "Vo"),
        ]:
            for order in (votes, votes[::-1]):
                self.assertEqual(majority_color(order), expected, order)


class GhlFrameTest(unittest.TestCase):
    def test_lesson_frame(self):
        bonus = [token("レジェンド", [511, 38, 100, 23]), token("ボーナス", [517, 60, 84, 24]), token("41", [625, 43, 54, 39])]
        state = reader(lesson_screen("PERFECTまで", "109", bonus)).read(IMAGE)
        state["buffs"] = [
            {"key": "goodImpressionTurns", "value": 23, "duration": None},
            {"key": "scoreBuffPercent", "value": 59, "duration": 1},
            {"key": "concentration", "value": 3, "duration": None},
        ]
        cards = [
            {"candidate_id": "unplayable_0", "box": [265, 884, 189, 249], "identity": {"status": "unknown", "database_card_id": None}},
            {
                "candidate_id": "card_0",
                "box": [49, 884, 187, 250],
                "identity": {"status": "matched", "database_card_id": 7, "database_name": "ファンサの基本", "observed_plus_count": 0},
            },
        ]
        frame = ghl_frame({"battle_state": state, "cards": cards})
        self.assertEqual((frame["layout"], frame["t"], frame["tAdd"], frame["bonus"], frame["st"], frame["g"]), ("lesson", 5, 1, 41, 26, 12))
        self.assertNotIn("toClear", frame)
        self.assertEqual(frame["b"], {"goodImpressionTurns": 23})
        self.assertEqual(frame["sb"], [[59, 1]])
        self.assertEqual(frame["hand"], ["ファンサの基本", "?"])
        # 每个手牌位置对应的请求候选，GHL 据此把手牌对应到候选。
        self.assertEqual(frame["slots"], ["card_0", "unplayable_0"])
        self.assertEqual(frame["unknown"], [])

    def test_overlapped_cards_keep_their_base_name(self):
        # 四五张手牌叠放：右邻的牌盖住卡名末尾的「+」，基础名认出而强化与否未定，记为「基础名+?」，由 GHL 按牌组判断。
        def card(x, identity):
            return {"candidate_id": f"card_{x}", "box": [x, 883, 187, 249], "identity": identity}

        overlapped = {"status": "base_match_variant_unresolved", "database_card_id": "skill_card_0001", "database_name": "届いて！",
                      "base_name": "届いて！", "unknown_reason": "upgrade_not_visible_in_overlap", "observed_plus_count": 0}
        conflicting = {**overlapped, "database_name": "きらきら紙吹雪+", "base_name": "きらきら紙吹雪", "unknown_reason": "inconsistent_upgrade_visibility"}
        unread = {"status": "unknown", "database_card_id": None, "unknown_reason": "shared_visible_prefix"}
        shown = {"status": "matched", "database_card_id": "skill_card_0002", "database_name": "輝きの到達点", "observed_plus_count": 0}
        cards = [card(142, overlapped), card(388, conflicting), card(266, unread), card(513, shown)]
        frame = ghl_frame({"battle_state": {"hud_present": False, "fields": {}}, "cards": cards})
        self.assertEqual(frame["hand"], ["届いて！+?", "?", "きらきら紙吹雪+?", "輝きの到達点"])

    def test_drinks_follow_the_candidate_slots(self):
        state = {
            "hud_present": False,
            "fields": {},
            "drinks": [
                {"box": [26, 1165, 70, 70], "name": "初星ホエイプロテイン"},
                {"box": [197, 1165, 70, 70], "name": None},
            ],
        }
        self.assertEqual(ghl_frame({"battle_state": state, "cards": []})["drinks"], ["初星ホエイプロテイン", "?"])
        boxes = [[197, 1165, 70, 70], [112, 1165, 70, 70], [26, 1165, 70, 70]]
        # 中间的饮料没有登记图标：按候选框占位为「?」。
        self.assertEqual(ghl_frame({"battle_state": state, "cards": []}, boxes)["drinks"], ["初星ホエイプロテイン", "?", "?"])

    def test_missing_fields_are_listed(self):
        frame = ghl_frame({"battle_state": {"hud_present": False, "fields": {}}, "cards": []})
        self.assertIsNone(frame["layout"])
        self.assertEqual(frame["unknown"], ["g", "layout", "score", "st", "t"])


if __name__ == "__main__":
    unittest.main()
