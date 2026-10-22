"""牌组账本读数回归：行动选择画面的周数与成长加成、奖励画面的种类与剩余次数、候选名称、课程属性图标。

使用替身 OCR 文字，不读取真实截图；需要安装 maafw（读取器模块的导入链）。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
from ghl_frame import LESSON_ICON, lesson_attribute
from ghl_readings import offer_name, week_fields, reward_fields


def token(text, box, confidence=1.0):
    return {"text": text, "confidence": confidence, "box": box}


class WeekFieldsTest(unittest.TestCase):
    def test_week_from_the_exam_countdown(self):
        # OCR 常把标签的词序打乱、末尾多读一个字符。
        for label, week in [("9週中間まで", 1), ("中間まで7週1", 3), ("7週最終まで1", 11), ("最終まで4週i", 14), ("中間まで", None)]:
            self.assertEqual(week_fields([token(label, [40, 30, 150, 60])], [])["week"], week, label)

    def test_growth_bonus_left_to_right(self):
        bonus = [token("26.0%", [300, 745, 60, 30]), token("22.7%", [160, 745, 60, 30]), token("37.3%", [440, 745, 60, 30])]
        self.assertEqual(week_fields([], bonus)["bonus"], {"Vo": 22.7, "Da": 26.0, "Vi": 37.3})
        self.assertIsNone(week_fields([], bonus[:2])["bonus"])


class RewardFieldsTest(unittest.TestCase):
    def test_kind_and_counters(self):
        prompt, kind, counters = reward_fields(
            [token("受け取るスキルカードを選んでください。", [60, 600, 600, 40])],
            [token("あと1回", [60, 1050, 120, 40]), token("あと4回", [500, 1050, 120, 40]), token("あと3回", [300, 1050, 120, 40], 0.5)],
        )
        self.assertEqual((prompt, kind), ("受け取るスキルカードを選んでください。", "card"))
        # 左侧为卡牌移除、右侧为重抽；低置信度的读数不用。
        self.assertEqual(counters, {"removals": 1, "rerolls": 4})

    def test_transition_screen_has_no_kind(self):
        self.assertIsNone(reward_fields([token("+3★0好印象が3以上の場合、やる気", [60, 600, 600, 40])], [])[1])


class OfferNameTest(unittest.TestCase):
    def test_names_as_ghl_spells_them(self):
        self.assertEqual(offer_name({"status": "matched", "item_kind": "skill_card", "database_name": "本番前夜", "observed_plus_count": 1}), ("本番前夜+", "card"))
        # 「++」：GHL 最高只有「+」。
        self.assertEqual(offer_name({"status": "unknown", "unknown_reason": "extra_upgrade_markers", "item_kind": "skill_card", "database_name": "手拍子+", "observed_plus_count": 2}), ("手拍子+", "card"))
        self.assertEqual(offer_name({"status": "matched", "item_kind": "drink", "database_name": "初星ホエイプロテイン"}), ("初星ホエイプロテイン", "drink"))
        self.assertEqual(offer_name({"status": "unknown"}), (None, None))
        self.assertEqual(offer_name({}), (None, None))


class LessonAttributeTest(unittest.TestCase):
    def test_icon_colours(self):
        x, y, width, height = LESSON_ICON
        for bgr, attr in [((60, 190, 245), "Vi"), ((190, 90, 240), "Vo"), ((230, 140, 60), "Da"), ((100, 100, 100), None)]:
            image = np.zeros((1280, 720, 3), dtype=np.uint8)
            image[y : y + height, x : x + width] = bgr
            self.assertEqual(lesson_attribute(image), attr, bgr)


if __name__ == "__main__":
    unittest.main()
