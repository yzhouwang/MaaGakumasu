"""牌组账本读数回归：行动选择画面的周数与成长加成、奖励画面的种类与剩余次数、候选名称、课程属性图标、特别指导的改造预览（含改造上限）、
相谈的确认对话框（交換確認、スキルカード強化、スキルカード削除）与主画面的服务按钮、「リフレッシュ」次数和扣款气泡，以及实况读取的断行恢复与失败重试。

使用替身 OCR 文字，不读取真实截图；需要安装 maafw（读取器模块的导入链）。临时文件写入 debug/research/produce/tests/。
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
from ghl_frame import LESSON_ICON, lesson_attribute
from ghl_readings import (
    OPTION_LEFT,
    P_SPENT_ROI,
    REFRESH_ROI,
    OPTION_WIDTH,
    P_COUNTER_ROI,
    REFRESH_BADGE,
    EXCHANGE_P_ROI,
    SELECTION_BANDS,
    SERVICE_ROW_ROI,
    SCREEN_LABEL_ROI,
    CUSTOMIZE_MENU_ROI,
    CUSTOMIZE_TITLE_ROI,
    EXCHANGE_DIALOG_ROI,
    CUSTOMIZE_FOOTER_ROI,
    limit_rois,
    offer_name,
    option_rois,
    week_fields,
    completed_ids,
    refresh_badge,
    reward_fields,
    service_fields,
    consult_reading,
    exchange_fields,
    read_with_retry,
    selected_option,
    customize_fields,
    customize_reading,
)


def token(text, box, confidence=1.0):
    return {"text": text, "confidence": confidence, "box": box}


class WeekFieldsTest(unittest.TestCase):
    def test_week_from_the_exam_countdown(self):
        # OCR 常把标签的词序打乱、末尾多读一个字符。
        for label, week in [("9週中間まで", 1), ("中間まで7週1", 3), ("7週最終まで1", 11), ("最終まで4週i", 14), ("中間まで", None)]:
            self.assertEqual(week_fields([token(label, [40, 30, 150, 60])], [])["week"], week, label)

    def test_a_countdown_no_week_can_have_is_not_read(self):
        # 实况中「中間まで7週」读成「中間まで77週1」：10 − 77 = −67 周。标签照记，周数为未读。
        for label in ("中間まで77週1", "最終まで12週", "中間まで0週", "中間まで107週", "最終まで101週"):
            fields = week_fields([token(label, [40, 30, 150, 60])], [])
            self.assertEqual((fields["week"], fields["counter"]), (None, label), label)

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


class CustomizeFieldsTest(unittest.TestCase):
    def test_preview_screen(self):
        fields = customize_fields(
            [token("410", [372, 98, 50, 30])],
            [token("-70", [180, 100, 60, 30])],
            [token("思い出し笑い+", [196, 170, 180, 34])],
            [token("カスタマイズメニュー", [255, 762, 200, 22])],
            [
                ([token("好印象+", [90, 820, 100, 30])], [token("40", [140, 872, 30, 24])]),
                ([token("やる気+", [310, 820, 100, 30])], [token("40", [360, 872, 30, 24])]),
                # 两行的名称按行拼接，「夕」更正为「タ」。
                ([token("夕追加", [560, 840, 60, 26]), token("好印象分パラメー", [500, 812, 170, 26])], [token("70", [580, 872, 30, 24])]),
            ],
        )
        self.assertEqual((fields["p"], fields["screen"], fields["cost"], fields["title"]["text"]), (410, "preview", 70, "思い出し笑い+"))
        self.assertEqual(fields["options"], [{"label": "好印象+", "price": 40}, {"label": "やる気+", "price": 40}, {"label": "好印象分パラメータ追加", "price": 70}])

    def test_two_options(self):
        slots = [([token("開始時手札に入る", [50, 820, 190, 30])], [token("70", [140, 872, 30, 24])]), ([token("元気追加", [310, 820, 100, 30])], [token("70", [360, 872, 30, 24])]), ([], [])]
        fields = customize_fields([token("260", [372, 98, 50, 30])], [], [token("ティーパーティ+", [196, 170, 180, 34])], [token("カスタマイズメニュー", [255, 762, 200, 22])], slots)
        self.assertEqual([option["label"] for option in fields["options"]], ["開始時手札に入る", "元気追加"])

    def test_other_screens_give_the_p_counter_only(self):
        # 卡牌列表画面的菜单在上方，预览位置没有「カスタマイズメニュー」。
        fields = customize_fields([token("410", [372, 98, 50, 30])], [], [token("紅葉ランニング+", [196, 180, 200, 34])], [token("十", [300, 760, 20, 20], 0.61)], [([], [])] * 3)
        self.assertEqual(fields, {"p": 410})

    MENU = [token("カスタマイズメニュー", [255, 762, 200, 22])]
    WINK = [
        ([token("好印象+", [90, 820, 100, 30])], [token("40", [140, 872, 30, 24])]),
        ([token("好印象分パラメータ+", [270, 812, 180, 26])], [token("40", [360, 872, 30, 24])]),
        ([token("コスト値-", [520, 820, 120, 30])], [token("20", [580, 872, 30, 24])]),
    ]

    def test_what_each_option_and_the_card_have_left(self):
        # 未到上限：选项上方「あと N回」，「実行する」上方「合計あと N回」（ラブリーウインク+ 共可改造 2 次）。
        limits = [([token("あと2回", [105, 788, 70, 20])], []), ([token("あと1回", [325, 788, 70, 20])], []), ([token("あと1回", [545, 788, 70, 20])], [])]
        footer = [token("合計あと1回", [440, 1012, 110, 22])]
        fields = customize_fields([token("70", [372, 98, 50, 30])], [], [], self.MENU, self.WINK, limits, footer)
        self.assertEqual([(option["left"], option["capped"]) for option in fields["options"]], [(2, False), (1, False), (1, False)])
        self.assertEqual((fields["total_left"], fields["cards_left"]), (1, None))

    def test_a_card_at_its_limit(self):
        # 改造次数用完：各选项下方「カスタマイズ上限」（读成「カス夕マイズ上限」也算），「一覧に戻る」上方为本次还可改造的卡牌数。
        limits = [([], [token("カスタマイズ上限", [55, 895, 175, 22])]), ([], [token("カス夕マイズ上限", [275, 895, 175, 22])]), ([], [token("カスタマイズ上限", [492, 895, 170, 22])])]
        fields = customize_fields([token("0", [400, 98, 30, 30])], [], [], self.MENU, self.WINK, limits, [token("あと1枚", [170, 1012, 90, 22])])
        self.assertEqual([(option["left"], option["capped"]) for option in fields["options"]], [(None, True)] * 3)
        self.assertEqual((fields["total_left"], fields["cards_left"]), (None, 1))
        # 低置信度的「上限」不算。
        limits[0] = ([], [token("カスタマイズ上限", [55, 895, 175, 22], 0.4)])
        self.assertFalse(customize_fields([], [], [], self.MENU, self.WINK, limits, [])["options"][0]["capped"])

    def test_counts_read_as_several_texts(self):
        # OCR 把徽章拆成几段：按从左到右拼接后再读；画面右半（「実行する」上方）的「あと N回」才是总次数。
        footer = [token("あと1枚", [170, 1012, 90, 22]), token("合計", [440, 1012, 40, 22]), token("あと2回", [482, 1012, 70, 22])]
        fields = customize_fields([], [], [], self.MENU, self.WINK, None, footer)
        self.assertEqual((fields["total_left"], fields["cards_left"]), (2, 1))
        self.assertNotIn("left", fields["options"][0])
        footer = [token("回", [540, 1012, 20, 22]), token("合計", [440, 1012, 40, 22]), token("あと", [482, 1012, 40, 22]), token("2", [524, 1012, 14, 22])]
        footer += [token("あと", [170, 1012, 40, 22]), token("1枚", [212, 1012, 40, 22])]
        fields = customize_fields([], [], [], self.MENU, self.WINK, None, footer)
        self.assertEqual((fields["total_left"], fields["cards_left"]), (2, 1))
        limits = [([token("あと", [105, 788, 40, 20]), token("2回", [147, 788, 30, 20])], []), ([], []), ([], [])]
        self.assertEqual(customize_fields([], [], [], self.MENU, self.WINK, limits, [])["options"][0]["left"], 2)
        # 画面左半的「あと N回」不是总次数。
        self.assertIsNone(customize_fields([], [], [], self.MENU, self.WINK, None, [token("あと2回", [170, 1012, 90, 22])])["total_left"])


class SelectedOptionTest(unittest.TestCase):
    ORANGE = (50, 150, 255)  # BGR

    def bracket_image(self, slot, orange=ORANGE, background=(200, 210, 220)):
        image = np.full((1280, 720, 3), background, dtype=np.uint8)
        left = OPTION_LEFT[slot]
        for top, bottom in SELECTION_BANDS:
            for x in (left - 12, left + OPTION_WIDTH - 26):
                image[top + 4 : bottom - 2, x : x + 38] = orange
        return image

    def test_brackets_mark_the_option(self):
        for slot in range(len(OPTION_LEFT)):
            self.assertEqual(selected_option(self.bracket_image(slot)), slot)

    def test_brackets_on_a_dimmed_screen(self):
        # 提示弹出时画面变暗：括号约为 (61, 127, 205)（BGR），背景同样变暗。
        for slot in range(len(OPTION_LEFT)):
            self.assertEqual(selected_option(self.bracket_image(slot, (61, 127, 205), (160, 168, 176))), slot)

    def test_no_brackets(self):
        self.assertIsNone(selected_option(np.full((1280, 720, 3), (200, 210, 220), dtype=np.uint8)))

    def test_an_orange_option_card_is_not_a_selection(self):
        # 选项卡本身可以是橙色（パラメータ追加），但不越出上下缘之外的横带。
        image = np.full((1280, 720, 3), (200, 210, 220), dtype=np.uint8)
        image[803:902, OPTION_LEFT[0] : OPTION_LEFT[0] + OPTION_WIDTH] = self.ORANGE
        self.assertIsNone(selected_option(image))


P_LINE = [
    token("520", [320, 1005, 50, 30]),
    token("P", [100, 1000, 40, 40]),
    token("→", [400, 1005, 20, 30]),
    token("420", [470, 1005, 50, 30]),
]
REMOVAL_DIALOG = [
    token("強化確認", [63, 273, 87, 28]),
    token("スキルカード削除", [58, 634, 257, 33]),
    token("Pポイントを使用してスキルカードを削除しますか?", [75, 719, 563, 24]),
    token("ファンサの基本", [206, 791, 205, 31]),
    token("昇", [203, 876, 28, 28]),
]
UPGRADE_DIALOG = [
    token("スキルカード強化", [56, 260, 259, 32]),
    token("Pポイントを使用してスキルカードを強化しますか?", [75, 344, 562, 23]),
    token("輝くキミへ", [204, 415, 148, 32], 0.96),
    token("やる気消費4", [250, 465, 140, 26]),
    token("輝くキミへ+", [204, 719, 168, 31], 0.92),
]


class ExchangeFieldsTest(unittest.TestCase):
    def test_dialog_name_and_p(self):
        # 对话框随商品说明向上伸展：名称取提示语下方、图标右侧的第一行；P 点数为交换前、后。
        dialog = [
            token("交換確認", [51, 451, 136, 37]),
            token("Pポイントを使用して交換しますか?", [159, 539, 395, 24]),
            token("やる気消費5", [243, 659, 148, 28]),
            token("私が最強!", [203, 611, 141, 32], 0.97),
            token("昇", [201, 802, 31, 31]),
        ]
        p_line = [token("193", [320, 1005, 50, 30]), token("P", [100, 1000, 40, 40]), token("→", [400, 1005, 20, 30]), token("93", [470, 1005, 40, 30])]
        fields = exchange_fields(dialog, p_line)
        self.assertEqual((fields["action"], fields["name"]["text"], fields["before"], fields["after"]), ("buy", "私が最強!", 193, 93))

    def test_not_a_dialog(self):
        self.assertIsNone(exchange_fields([token("Pポイントと交換するものを選んでください", [120, 300, 470, 30])], []))
        # 卡牌列表画面：上方卡牌详情的「強化確認」按钮不是对话框。
        self.assertIsNone(exchange_fields([token("強化確認", [63, 273, 87, 28]), token("ファンサの基本", [193, 105, 205, 32])], []))

    def test_removal_dialog(self):
        fields = exchange_fields(REMOVAL_DIALOG, P_LINE)
        self.assertEqual((fields["action"], fields["name"]["text"], fields["before"], fields["after"]), ("remove", "ファンサの基本", 520, 420))

    def test_upgrade_dialog_names_the_card_before_the_upgrade(self):
        fields = exchange_fields(UPGRADE_DIALOG, P_LINE)
        self.assertEqual((fields["action"], fields["name"]["text"]), ("upgrade", "輝くキミへ"))

    def test_without_the_prompt_the_title_at_the_left_edge_decides(self):
        # 提示语读不到：按左缘的标题；卡牌说明里的同样字样（x ≥ 190）不算标题。
        dialog = [item for item in REMOVAL_DIALOG if "しますか" not in item["text"]] + [token("スキルカード強化追加", [296, 840, 250, 27])]
        fields = exchange_fields(dialog, P_LINE)
        self.assertEqual((fields["action"], fields["name"]["text"]), ("remove", "ファンサの基本"))
        self.assertIsNone(exchange_fields([token("スキルカード強化追加", [296, 840, 250, 27]), token("ファンサの基本", [206, 900, 205, 31])], P_LINE))


# 相谈主画面的按钮行：两个服务都用过（「交換済」，下次 125），未用的按钮只有名称与价格。
USED_ROW = [
    token("強化❤ 次 125", [123, 950, 191, 35], 0.85),
    token("交換済", [167, 978, 66, 28]),
    token("削除次125", [437, 947, 195, 42]),
    token("交換済", [483, 977, 66, 29]),
]
OPEN_ROW = [token("強化", [124, 950, 67, 36]), token("150", [241, 954, 57, 28]), token("削除", [440, 950, 65, 36]), token("125", [560, 954, 53, 27])]
REFRESH = [token("あと1回", [69, 1033, 79, 26]), token("リフレッシュ", [41, 1072, 143, 25])]


class ServiceFieldsTest(unittest.TestCase):
    def test_used_services_show_the_next_price(self):
        self.assertEqual(
            service_fields(USED_ROW, REFRESH, [token("P", [283, 97, 38, 35])]),
            {"upgrade": {"used": True, "price": 125}, "remove": {"used": True, "price": 125}, "refreshes": 1, "spent": None},
        )

    def test_open_services_and_no_refresh_button(self):
        # 「リフレッシュ」用完后按钮消失：服务按钮读到而该处没有文字，剩余 0 次。
        fields = service_fields(OPEN_ROW, [], [])
        self.assertEqual(fields, {"upgrade": {"used": False, "price": 150}, "remove": {"used": False, "price": 125}, "refreshes": 0, "spent": None})
        # 一个用过、一个未用。
        row = USED_ROW[:2] + [token("削除", [440, 950, 65, 36]), token("100", [558, 954, 57, 28])]
        self.assertEqual(service_fields(row, REFRESH, [])["remove"], {"used": False, "price": 100})

    def test_split_and_misread_texts(self):
        # 「交換済」的勾盖住名称的第二个字；P 图标读成「0」，在价格左边，不与价格连成一个数；各段分开读到。
        row = [token("強 次150", [117, 947, 202, 41], 0.9), token("交換済", [166, 979, 69, 27]), token("削除0次125", [439, 950, 191, 37], 0.94), token("交換済", [484, 979, 65, 27])]
        fields = service_fields(row, REFRESH, [])
        self.assertEqual((fields["upgrade"], fields["remove"]), ({"used": True, "price": 150}, {"used": True, "price": 125}))
        row = [token("強化", [124, 950, 67, 36]), token("0", [205, 955, 20, 25]), token("100", [245, 954, 52, 27])]
        row += [token("削除", [440, 950, 65, 36]), token("次", [520, 954, 20, 27]), token("交換済", [483, 977, 66, 29]), token("150", [560, 954, 53, 27])]
        fields = service_fields(row, [token("あと", [69, 1033, 40, 26]), token("1回", [110, 1033, 38, 26])], [])
        self.assertEqual((fields["upgrade"], fields["remove"], fields["refreshes"]), ({"used": False, "price": 100}, {"used": True, "price": 150}, 1))
        # 价格读不到。
        self.assertEqual(service_fields([token("強化", [124, 950, 67, 36]), token("削除", [440, 950, 65, 36])], REFRESH, [])["upgrade"], {"used": False, "price": None})

    def test_covered_buttons_are_not_read(self):
        # 提示「…を強化しました」盖住按钮行（透过它还能读到扣款前的按钮）；获得动画的说明框盖住「強化」；名称置信度低；
        # 说明框里的「強」不在按钮名称的位置。这时「リフレッシュ」处没有文字也不算按钮消失。
        for row in (
            OPEN_ROW + [token("輝くキミへを強化しました", [116, 1000, 323, 30])],
            [token("やる気+6", [177, 949, 111, 31]), token("削除100", [442, 954, 171, 28])],
            [token("強化", [124, 950, 67, 36], 0.5)] + OPEN_ROW[1:],
            [token("強気+2", [178, 949, 110, 31])] + OPEN_ROW[2:],
        ):
            self.assertEqual(service_fields(row, [], []), {"upgrade": None, "remove": None, "refreshes": None, "spent": None}, row)

    def test_refresh_count_not_read(self):
        # 按钮在而次数读不到，或该处有别的文字（说明框）：不知道。
        self.assertIsNone(service_fields(OPEN_ROW, [token("リフレッシュ", [41, 1072, 143, 25])], [])["refreshes"])
        self.assertIsNone(service_fields(OPEN_ROW, [token("重複不可レッスン中1回", [85, 1024, 260, 24])], [])["refreshes"])
        # 只有置信度低的文字：红色标记在则按钮在（次数读不到），不在才是按钮消失；扣款动画中也不判定按钮消失。
        low = [token("1", [10, 1025, 200, 90], 0.16)]
        self.assertIsNone(service_fields(OPEN_ROW, low, [], badge=True)["refreshes"])
        self.assertEqual(service_fields(OPEN_ROW, low, [])["refreshes"], 0)
        self.assertIsNone(service_fields(OPEN_ROW, [], [token("-100", [179, 97, 78, 34])])["refreshes"])

    def test_the_bubble_of_a_payment(self):
        # 扣款时 P 点数左侧的气泡：「-100」，或刚出现时的小气泡；没有负号的数（离开相谈时背后的窗框）、P 图标都不算。
        self.assertEqual(service_fields(OPEN_ROW, REFRESH, [token("-100", [179, 97, 78, 34])])["spent"], 100)
        self.assertEqual(service_fields(OPEN_ROW, REFRESH, [token("-150", [239, 103, 44, 21])])["spent"], 150)
        for items in ([token("1", [150, 88, 140, 50], 0.65)], [token("P", [283, 97, 38, 35])], [token("-5", [282, 97, 30, 30])], [token("-100", [179, 97, 78, 34], 0.4)]):
            self.assertIsNone(service_fields(OPEN_ROW, REFRESH, items)["spent"], items)
        # 盖住按钮的画面也读气泡。
        self.assertEqual(service_fields([], [], [token("-125", [179, 97, 78, 34])])["spent"], 125)


class RefreshBadgeTest(unittest.TestCase):
    def frame(self, bgr, size=(110, 36), background=(200, 220, 235)):
        x, y, _, _ = REFRESH_BADGE
        image = np.full((1280, 720, 3), background, dtype=np.uint8)
        image[y : y + size[1], x : x + size[0]] = bgr
        return image

    def test_the_red_count_badge(self):
        # 「あと1回」的红色标记（BGR），扣款动画中变暗的也算；按钮消失后该处是笔记本的底色；零星的红色不算。
        self.assertTrue(refresh_badge(self.frame((90, 60, 230))))
        self.assertTrue(refresh_badge(self.frame((60, 40, 150))))
        self.assertFalse(refresh_badge(self.frame((200, 220, 235))))
        self.assertFalse(refresh_badge(self.frame((90, 60, 230), size=(10, 10))))


class FakeVision:
    """按 ROI 返回替身 OCR 文字。"""

    def __init__(self, by_roi):
        self.by_roi = {tuple(roi): items for roi, items in by_roi}

    def _read_title(self, array, roi):
        return self.by_roi.get(tuple(roi), [])


class FakeRewards:
    """名称查库：替身文字按表给出种类，「+」记为强化次数。"""

    KINDS = {"ファンサの基本": "skill_card", "輝くキミへ": "skill_card", "初星水": "drink"}

    def match_tokens(self, tokens):
        text = tokens[0]["text"]
        kind = self.KINDS.get(text.rstrip("+"))
        if kind is None:
            return {"status": "unknown"}
        return {"status": "matched", "item_kind": kind, "database_name": text.rstrip("+"), "observed_plus_count": text.count("+")}


class ConsultReadingTest(unittest.TestCase):
    def setUp(self):
        from utils.image_io import encode_png

        root = ROOT / "debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="consult_", dir=root)
        self.addCleanup(temporary.cleanup)
        self.mailbox = Path(temporary.name)
        (self.mailbox / "frame.png").write_bytes(encode_png(np.zeros((1280, 720, 3), dtype=np.uint8)))

    def read(self, label, dialog, p_line, counter=(), row=(), refresh=(), spent=()):
        vision = FakeVision(
            [
                (SCREEN_LABEL_ROI, [token(label, [59, 39, 63, 36])]),
                (P_COUNTER_ROI, list(counter)),
                (EXCHANGE_DIALOG_ROI, dialog),
                (EXCHANGE_P_ROI, p_line),
                (SERVICE_ROW_ROI, list(row)),
                (REFRESH_ROI, list(refresh)),
                (P_SPENT_ROI, list(spent)),
            ]
        )
        return consult_reading(vision, FakeRewards(), self.mailbox, {"frame": "frame.png"})

    def test_card_dialogs_are_a_service_not_an_exchange(self):
        # 只认 exchange 的读取方不会把强化、删除当成购买。
        self.assertEqual(
            self.read("削除", REMOVAL_DIALOG, P_LINE),
            {"p": None, "label": "削除", "service": {"action": "remove", "name": "ファンサの基本", "before": 520, "after": 420}},
        )
        upgrade = self.read("強化", UPGRADE_DIALOG, P_LINE)["service"]
        self.assertEqual(upgrade, {"action": "upgrade", "name": "輝くキミへ", "before": 520, "after": 420})

    def test_exchange_unchanged(self):
        dialog = [token("交換確認", [52, 595, 135, 38]), token("Pポイントを使用して交換しますか?", [159, 683, 395, 24]), token("初星水", [204, 755, 177, 31])]
        exchange = {"name": "初星水", "kind": "drink", "before": 520, "after": 420}
        self.assertEqual(self.read("", dialog, P_LINE), {"p": None, "label": "", "exchange": exchange})

    def test_a_service_name_that_is_not_a_skill_card_is_not_read(self):
        dialog = [item if item["text"] != "ファンサの基本" else token("初星水", item["box"]) for item in REMOVAL_DIALOG]
        self.assertIsNone(self.read("削除", dialog, P_LINE)["service"]["name"])

    def test_the_main_screen_shows_the_services(self):
        fields = self.read("相談", [], [], [token("420", [372, 98, 50, 30])], USED_ROW, REFRESH)
        services = {"upgrade": {"used": True, "price": 125}, "remove": {"used": True, "price": 125}, "refreshes": 1, "spent": None}
        self.assertEqual(fields, {"p": 420, "label": "相談", **services})
        # 扣款动画中：按钮可能仍是扣款前的样子，照读，另记气泡。
        fields = self.read("相談", [], [], [token("520", [372, 98, 50, 30])], OPEN_ROW, REFRESH, [token("-100", [179, 97, 78, 34])])
        self.assertEqual((fields["upgrade"], fields["spent"]), ({"used": False, "price": 150}, 100))
        # 按钮读不到：都为 None。
        none = {"upgrade": None, "remove": None, "refreshes": None, "spent": None}
        self.assertEqual(self.read("相談", [], [], [token("520", [372, 98, 50, 30])]), {"p": 520, "label": "相談", **none})

    def test_other_screens(self):
        # 卡牌列表（强化、删除）与对话框不读服务按钮。
        self.assertEqual(self.read("強化", [], [], [], USED_ROW, REFRESH), {"p": None, "label": "強化"})
        self.assertNotIn("upgrade", self.read("相談", REMOVAL_DIALOG, P_LINE, (), USED_ROW, REFRESH))


class FakeCatalog:
    """卡牌名查库：都查不到。"""

    def match(self, text, confidence):
        return {}


class CustomizeReadingTest(unittest.TestCase):
    def setUp(self):
        from utils.image_io import encode_png

        root = ROOT / "debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="customize_", dir=root)
        self.addCleanup(temporary.cleanup)
        self.mailbox = Path(temporary.name)
        (self.mailbox / "frame.png").write_bytes(encode_png(np.zeros((1280, 720, 3), dtype=np.uint8)))

    def test_limits_are_read_where_the_menu_shows_them(self):
        label, price = option_rois(0)
        badge, limit = limit_rois(1)
        vision = FakeVision(
            [
                (P_COUNTER_ROI, [token("170", [372, 98, 50, 30])]),
                (CUSTOMIZE_MENU_ROI, [token("カスタマイズメニュー", [255, 762, 200, 22])]),
                (CUSTOMIZE_TITLE_ROI, [token("夢色リップ+", [196, 170, 180, 34])]),
                (label, [token("条件変更", [90, 820, 100, 30])]),
                (price, [token("70", [140, 872, 30, 24])]),
                (option_rois(1)[0], [token("元気追加", [310, 820, 100, 30])]),
                (option_rois(1)[1], [token("70", [360, 872, 30, 24])]),
                (limit_rois(0)[1], [token("カスタマイズ上限", [55, 895, 175, 22])]),
                (badge, []),
                (limit, [token("カスタマイズ上限", [275, 895, 175, 22])]),
                (CUSTOMIZE_FOOTER_ROI, [token("あと1枚", [170, 1012, 90, 22])]),
            ]
        )
        fields = customize_reading(vision, FakeCatalog(), self.mailbox, {"frame": "frame.png"})
        self.assertEqual(
            fields["options"],
            [{"label": "条件変更", "price": 70, "left": None, "capped": True}, {"label": "元気追加", "price": 70, "left": None, "capped": True}],
        )
        self.assertEqual((fields["p"], fields["card"], fields["total_left"], fields["cards_left"]), (170, None, None, 1))


class FollowTest(unittest.TestCase):
    def setUp(self):
        root = ROOT / "debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="readings_", dir=root)
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name) / "readings.jsonl"

    def test_torn_last_line_and_failed_rows_are_not_done(self):
        # 上次写到一半中断：残行补上换行，之后追加的一行自成一行；读取失败的行重启后重读。
        self.out.write_text('{"id": "a"}\n{"id": "b", "error": "x"}\n{"id": "c", "fra', encoding="utf-8")
        self.assertEqual(completed_ids(self.out), {"a"})
        with self.out.open("a", encoding="utf-8") as stream:
            stream.write('{"id": "c"}\n')
        self.assertEqual(completed_ids(self.out), {"a", "c"})
        # 断在多字节字符中间的残行。
        self.out.write_bytes('{"id": "a"}\n{"id": "d", "hand": "'.encode("utf-8") + "初".encode("utf-8")[:2])
        self.assertEqual(completed_ids(self.out), {"a"})

    def test_failed_reads_are_retried(self):
        rows = [{"id": "x", "error": "frame not written yet"}, {"id": "x"}]
        waits = []
        self.assertEqual(read_with_retry(lambda request, choice: rows.pop(0), {"id": "x"}, None, sleep=waits.append), {"id": "x"})
        self.assertEqual(waits, [1])
        # 一直失败：重试之后写入带 error 的一行。
        row = read_with_retry(lambda request, choice: {"id": "x", "error": "e"}, {"id": "x"}, None, sleep=waits.append)
        self.assertEqual((row["error"], waits), ("e", [1, 1, 2]))


if __name__ == "__main__":
    unittest.main()
