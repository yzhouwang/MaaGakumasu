"""战斗画面识别回归：课程 HUD、考试追加回合、体力数字筛选、同形 Buff 图标区分、控制台 HUD 文案与 GHL 实况帧整理。

使用替身 OCR（按识别区域返回预设文字），不读取真实截图、不加载模板；需要安装 maafw（读取器模块的导入链）。
"""

import sys
import json
import shutil
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
from ghl_frame import ghl_frame
from produce.state import EXAM_HUD, LESSON_HUD, BattleStateReader, turn_fields, added_turns_field, masked_correlation
from produce.console import state_lines

IMAGE = np.zeros((1280, 720, 3), dtype=np.uint8)


def token(text, box, confidence=1.0):
    return {"text": text, "confidence": confidence, "box": box}


def reader(screen):
    """screen 为 {识别区域: 文字列表}；未登记的区域没有文字。模板登记表不存在，Buff 一律不读。"""
    return BattleStateReader(Path("missing_registry.json"), lambda array, roi: screen.get(tuple(roi), []))


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

    def test_exam_added_turn_pill(self):
        self.assertEqual(added_turns_field([token("+1", [99, 120, 53, 34])], EXAM_HUD["added"])["value"], 1)
        absent = added_turns_field([], EXAM_HUD["added"])
        self.assertEqual((absent["value"], absent["status"]), (0, "not_shown"))
        self.assertIsNone(added_turns_field([token("+1", [99, 120, 53, 34]), token("+2", [99, 120, 53, 34])], EXAM_HUD["added"])["value"])


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
            {"box": [265, 884, 189, 249], "identity": {"status": "unknown", "database_card_id": None}},
            {"box": [49, 884, 187, 250], "identity": {"status": "matched", "database_card_id": 7, "database_name": "ファンサの基本", "observed_plus_count": 0}},
        ]
        frame = ghl_frame({"battle_state": state, "cards": cards})
        self.assertEqual((frame["layout"], frame["t"], frame["tAdd"], frame["bonus"], frame["st"], frame["g"]), ("lesson", 5, 1, 41, 26, 12))
        self.assertNotIn("toClear", frame)
        self.assertEqual(frame["b"], {"goodImpressionTurns": 23})
        self.assertEqual(frame["sb"], [[59, 1]])
        self.assertEqual(frame["hand"], ["ファンサの基本", "?"])
        self.assertEqual(frame["unknown"], [])

    def test_missing_fields_are_listed(self):
        frame = ghl_frame({"battle_state": {"hud_present": False, "fields": {}}, "cards": []})
        self.assertIsNone(frame["layout"])
        self.assertEqual(frame["unknown"], ["g", "layout", "score", "st", "t"])


if __name__ == "__main__":
    unittest.main()
