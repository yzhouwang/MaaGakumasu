"""培育决策特征测试：固定现有出牌、事件优先级、偶像卡识别与试镜门槛的行为，使用合成输入。

这些测试记录“当前代码怎么选”，不评价选择是否最优；行为有意变更时同步修改对应断言并在提交说明中写明。
需要安装 maafw（被测模块在导入时依赖 maa），不连接设备、不截图。
"""

import sys
import json
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from produce.battle import ProduceCardsAuto, card_candidates
from produce.events import ProduceChooseEventAuto, ProduceChooseNIAEventAuto
from produce.options import ProduceChooseMirrorAuto
from custom.reco.produce import ProduceChooseIdolAuto


def detection(label, score, box):
    return SimpleNamespace(label=label, score=score, box=box)


def lessons(vo_sp=False, da_sp=False, vi_sp=False):
    """周行动界面识别到课程时 _get_available_events 返回的结构。"""
    return [
        {"Vo": [190, 1000, 1, 1], "SP": vo_sp},
        {"Da": [360, 1000, 1, 1], "SP": da_sp},
        {"Vi": [530, 1000, 1, 1], "SP": vi_sp},
    ]


FULL_HEALTH = {"current": 30, "max": 30, "ratio": 1.0}


class CardSelectionTest(unittest.TestCase):
    """ProduceCardsAuto._get_card_info：只统计手牌区域 [840, 1000] 内的检测框。"""

    def test_suggestion_and_best_card_by_detector_score(self):
        results = [
            detection("cards", 0.70, [100, 900, 120, 160]),
            detection("suggestions", 0.60, [250, 900, 120, 160]),
            detection("cards", 0.90, [400, 900, 120, 160]),
            detection("suggestions", 0.80, [550, 900, 120, 160]),
            detection("useless", 0.99, [10, 900, 120, 160]),
        ]
        suggestions, useless, cards, suggestions_box, best_box = ProduceCardsAuto._get_card_info(results)
        self.assertEqual((suggestions, useless, cards), (2, 1, 2))
        self.assertEqual(suggestions_box, [550, 900, 120, 160])
        # 超时兜底取检测置信度最高的普通牌，而不是卡牌收益
        self.assertEqual(best_box, [400, 900, 120, 160])

    def test_hand_band_is_inclusive_and_filters_outliers(self):
        results = [
            detection("suggestions", 0.99, [100, 839, 120, 160]),
            detection("cards", 0.99, [100, 1001, 120, 160]),
            detection("cards", 0.50, [250, 840, 120, 160]),
            detection("cards", 0.40, [400, 1000, 120, 160]),
        ]
        suggestions, useless, cards, suggestions_box, best_box = ProduceCardsAuto._get_card_info(results)
        self.assertEqual((suggestions, useless, cards), (0, 0, 2))
        self.assertEqual(suggestions_box, [0, 0, 1, 1])
        self.assertEqual(best_box, [250, 840, 120, 160])

    def test_equal_scores_keep_first_detection(self):
        results = [detection("cards", 0.80, [100, 900, 1, 1]), detection("cards", 0.80, [400, 900, 1, 1])]
        self.assertEqual(ProduceCardsAuto._get_card_info(results)[4], [100, 900, 1, 1])

    def test_no_detections_return_placeholder_boxes(self):
        self.assertEqual(ProduceCardsAuto._get_card_info([]), (0, 0, 0, [0, 0, 1, 1], [0, 0, 1, 1]))

    def test_card_candidates_mark_region_and_availability(self):
        candidates = card_candidates(
            [detection("suggestions", 0.9, [1, 900, 2, 3]), detection("useless", 0.8, [1, 900, 2, 3]), detection("cards", 0.7, [1, 1200, 2, 3])]
        )
        self.assertEqual([item["candidate_id"] for item in candidates], ["detection_0", "detection_1", "detection_2"])
        self.assertEqual([item["in_hand_region"] for item in candidates], [True, True, False])
        self.assertEqual([item["available"] for item in candidates], [True, False, False])


class EventPriorityTest(unittest.TestCase):
    """ProduceChooseEventAuto._choose_best_event（初）：第一属性 Vi、第二属性 Da。"""

    def setUp(self):
        self.chooser = ProduceChooseEventAuto()
        self.chooser.first, self.chooser.second = "Vi", "Da"

    def choose(self, events, suggestion="", health=FULL_HEALTH, points=0, score=None, allow_rest=True):
        score = score or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1000}
        return self.chooser._choose_best_event(suggestion, health, points, score, events, allow_rest=allow_rest)

    def test_low_health_prefers_outing_when_points_allow(self):
        events = [{"外出": [1, 2, 3, 4]}, {"活动": [5, 6, 7, 8]}]
        low = {"current": 7, "max": 30, "ratio": 7 / 30}
        self.assertEqual(self.choose(events, health=low, points=100)["name"], "外出")
        self.assertEqual(self.choose(events, health=low, points=99), {"name": "rest", "box": [0, 0, 0, 0], "run_task": "ProduceChooseRest"})

    def test_low_health_ratio_triggers_rest_even_with_high_value(self):
        low_ratio = {"current": 9, "max": 50, "ratio": 0.18}
        self.assertEqual(self.choose(lessons(), health=low_ratio)["name"], "rest")

    def test_rest_unavailable_falls_through_to_lessons(self):
        low = {"current": 3, "max": 30, "ratio": 0.1}
        self.assertEqual(self.choose(lessons(), health=low, allow_rest=False)["name"], "Vi")

    def test_teacher_suggestion_overrides_attribute_preference(self):
        self.assertEqual(self.choose(lessons(vi_sp=True), suggestion="ボーカルレッスンがおすすめ")["name"], "Vo")

    def test_sp_of_first_then_second_attribute(self):
        self.assertEqual(self.choose(lessons(vi_sp=True, da_sp=True))["name"], "Vi_SP")
        self.assertEqual(self.choose(lessons(da_sp=True))["name"], "Da_SP")

    def test_first_attribute_stops_at_eighty_percent(self):
        score = {"Vo": 0, "Da": 100, "Vi": 800, "max": 1000}
        self.assertEqual(self.choose(lessons(vi_sp=True), score=score)["name"], "Da")
        score = {"Vo": 0, "Da": 100, "Vi": 799, "max": 1000}
        self.assertEqual(self.choose(lessons(), score=score)["name"], "Vi")

    def test_both_stopped_uses_points_then_activity(self):
        score = {"Vo": 0, "Da": 900, "Vi": 900, "max": 1000}
        events = [{"交谈": [1, 1, 1, 1]}, {"活动": [2, 2, 2, 2]}]
        self.assertEqual(
            self.choose(events, points=100, score=score), {"name": "交谈", "box": [1, 1, 1, 1], "run_task": "ProduceShoppingEntry"}
        )
        self.assertEqual(self.choose(events, points=99, score=score)["name"], "活动")

    def test_both_stopped_with_only_lessons_takes_third_attribute_sp_or_first_event(self):
        score = {"Vo": 0, "Da": 900, "Vi": 900, "max": 1000}
        self.assertEqual(self.choose(lessons(vo_sp=True), score=score)["name"], "Vo_SP")
        # 所有优先级未命中时保底选列表中的第一个事件
        self.assertEqual(self.choose(lessons(), score=score)["name"], "Vo")

    def test_unknown_event_is_fallback_and_empty_list_returns_none(self):
        self.assertEqual(self.choose([{"未知": [9, 9, 9, 9]}])["name"], "未知")
        self.assertIsNone(self.choose([]))


class NIAEventPriorityTest(unittest.TestCase):
    """ProduceChooseNIAEventAuto：营业在 SP 之后、属性课程之前；指导在外出之后。"""

    def setUp(self):
        self.chooser = ProduceChooseNIAEventAuto()
        self.chooser.first, self.chooser.second = "Vi", "Da"

    def choose(self, events, points=0):
        return self.chooser._choose_best_event("", FULL_HEALTH, points, {"Vo": 0, "Da": 0, "Vi": 0, "max": 1000}, events)

    def test_work_after_sp_before_lessons(self):
        work = [{"工作": [7, 7, 7, 7]}]
        self.assertEqual(self.choose(lessons() + work), {"name": "工作", "box": [7, 7, 7, 7], "run_task": "ProduceWorkEntry"})
        self.assertEqual(self.choose(lessons(vi_sp=True) + work)["name"], "Vi_SP")

    def test_guide_after_outing(self):
        guide = {"指导": [8, 8, 8, 8]}
        self.assertEqual(self.choose([guide, {"外出": [1, 1, 1, 1]}])["name"], "外出")
        self.assertEqual(self.choose([guide]), {"name": "指导", "box": [8, 8, 8, 8], "run_task": "ProduceGuideEntry"})


class IdolMatchTest(unittest.TestCase):
    """ProduceChooseIdolAuto：NFKC 归一化后按 SequenceMatcher 比较，阈值 0.7。"""

    def test_symbol_variants_normalize_to_same_text(self):
        self.assertEqual(ProduceChooseIdolAuto.normalize_text("36℃ U･B･U"), ProduceChooseIdolAuto.normalize_text("36°C U・B・U"))
        self.assertEqual(ProduceChooseIdolAuto.similarity_ratio("36℃ U･B･U", "36°C U・B・U"), 1.0)

    def test_single_character_ocr_error_still_matches(self):
        self.assertEqual(ProduceChooseIdolAuto.SIMILARITY_THRESHOLD, 0.7)
        self.assertGreaterEqual(ProduceChooseIdolAuto.similarity_ratio("ガラクタロ一ド", "ガラクタロード"), 0.7)
        self.assertLess(ProduceChooseIdolAuto.similarity_ratio("ENDLESS DANCE", "ガラクタロード"), 0.7)


class MirrorThresholdTest(unittest.TestCase):
    """ProduceChooseMirrorAuto.run：选满足投票数的最高门槛，按配置降档，遇锁定继续降档。"""

    MIRROR = {"0": [360, 1050, 1, 1], "10000": [100, 600, 200, 40], "30000": [100, 500, 200, 40], "60000": [100, 400, 200, 40]}

    def run_mirror(self, vote, level=None, locked=()):
        chooser = ProduceChooseMirrorAuto()
        clicks = []
        chooser._get_current_vote = lambda context, image: vote
        chooser._get_current_mirror = lambda context, image: dict(self.MIRROR)
        chooser._check_lock = lambda context, image, box: box in [self.MIRROR[key] for key in locked]
        chooser._get_focus_mirror_index = lambda context, image: 0
        chooser._double_click = lambda context, x, y: clicks.append((x, y))
        screencap = SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: None))
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(post_screencap=lambda: screencap)))
        argv = SimpleNamespace(custom_action_param=json.dumps({"level": level} if level else {}))
        self.assertTrue(chooser.run(context, argv))
        return clicks

    @staticmethod
    def center(key):
        box = MirrorThresholdTest.MIRROR[key]
        return (box[0] + box[2] // 2, box[1] + box[3] // 2 - 20)

    def test_highest_threshold_not_above_vote(self):
        self.assertEqual(self.run_mirror(25000), [self.center("10000")])
        self.assertEqual(self.run_mirror(60000), [self.center("60000")])

    def test_lowering_steps_and_clamp(self):
        self.assertEqual(self.run_mirror(60000, level=2), [self.center("10000")])
        self.assertEqual(self.run_mirror(25000, level=5), [self.center("0")])

    def test_locked_threshold_steps_down(self):
        self.assertEqual(self.run_mirror(60000, locked=("60000", "30000")), [self.center("10000")])

    def test_unreadable_vote_falls_back_to_zero_threshold(self):
        self.assertEqual(self.run_mirror(None), [self.center("0")])

    def test_mirror_ocr_parsing(self):
        results = [SimpleNamespace(box=[1, 2, 3, 4], text="30,000"), SimpleNamespace(box=[5, 6, 7, 8], text="10,000")]
        detail = SimpleNamespace(hit=True, filtered_results=results)
        context = SimpleNamespace(run_recognition=lambda name, image: detail)
        mirror = ProduceChooseMirrorAuto._get_current_mirror(context, None)
        self.assertEqual(list(mirror), ["0", "10000", "30000"])
        self.assertEqual(mirror["30000"], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
