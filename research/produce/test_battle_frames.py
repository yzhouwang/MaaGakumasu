"""战斗画面识别回归：课程 HUD、考试追加回合、体力数字筛选、控制台 HUD 文案与 GHL 实况帧整理。

使用替身 OCR（按识别区域返回预设文字），不读取真实截图、不加载模板；需要安装 maafw（读取器模块的导入链）。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
from ghl_frame import ghl_frame
from produce.state import EXAM_HUD, LESSON_HUD, BattleStateReader, turn_fields, added_turns_field
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
