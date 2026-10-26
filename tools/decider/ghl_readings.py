"""从决策信箱逐条读出 GHL 牌组账本所需的画面读数。

GHL（gakumas-headless-lab）按这些读数组装实况回放输入 LiveRun（src/bridge/maaLedger.ts）：起始牌组、
每场战斗、每个决策的画面状态，以及决策之间的牌组、饮料、P 道具变化。本脚本只读画面，不推断牌组。

每条决策一行 JSON：
- battle：GHL 实况帧（ghl_frame.read_battle_request），战斗编号取请求的 context.battle_id；
- reward：奖励种类（技能卡 card、P 饮料 drink、P 道具 item；开局奖励的画面与饮料奖励相同，由 GHL 按位置区分）、
  各候选的名称（候选详情截图的标题查库，与候选顺序一致，未识别为 null）、剩余的重抽与卡牌移除次数；
- week：周数（「中間まで N週」「最終試験まで N週」）、三维成长加成，以及请求中已读取的体力、P 点数与三维；
- customize（特别指导）：顶部的 P 点数；改造预览画面另读卡牌名、菜单各选项的名称、价格与改造上限（还可改造的次数或「カスタマイズ上限」）、
  选中的选项与其价格，以及这张卡还可改造的总次数（「合計あと N回」）与本次特别指导还可改造的卡牌数（「あと N枚」）；
- consult（相谈）：顶部的 P 点数、左上角的画面标签（相談、強化、削除等）；相谈主画面另读两个服务按钮（未用时的价格，或「交換済」
  与下次的价格）、「リフレッシュ」的剩余次数与 P 点数左侧正在扣除的 P；交換確認对话框另读商品名称、种类与交换前后的 P 点数，
  スキルカード強化、スキルカード削除对话框另读动作、卡牌名称（强化为强化前的卡牌）与前后的 P 点数；
- 其余种类：种类与答复。

用法（仓库根目录，python 需安装 maafw，例如 beta 的内置 Python）：
    python tools/decider/ghl_readings.py --mailbox debug/decider/mailbox --out debug/decider/mailbox/ghl_readings.jsonl
    python tools/decider/ghl_readings.py --mailbox debug/decider/mailbox --follow   # 实况：监视 requests/，逐条追加到 ghl/readings.jsonl

--follow 用于 GHL 的实况桥（src/bridge/maaLive.ts）：请求一写入就读画面并追加一行，不等决策者答复；
答复已存在时一并记入 choice，否则为 null（GHL 从 responses/ 取答复）。只读画面，不写 responses/。
"""

import re
import sys
import json
import argparse
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(Path(__file__).resolve().parent)]

# 奖励画面：提示语（「受け取るスキルカードを選んでください」等）、底部的剩余次数、候选详情的标题。
REWARD_PROMPT_ROI = [40, 590, 640, 60]
REWARD_COUNTER_ROI = [20, 1030, 680, 90]
OFFER_TITLE_ROI = [120, 485, 480, 55]
# 行动选择画面：左上角距考试的周数，三维下方的成长加成。
WEEK_COUNTER_ROI = [30, 25, 175, 115]
WEEK_BONUS_ROI = [150, 740, 420, 40]
# 特别指导与相谈画面顶部的 P 点数。
P_COUNTER_ROI = [330, 88, 110, 50]
# 特别指导的改造预览画面：上方为当前卡牌，下方为改造后的预览，菜单至多三个选项，选中项四角有橙色括号，
# P 点数左侧的气泡为选中项的价格（「-70」）。卡牌列表画面的同名菜单在列表上方，不作为改造读数。
CUSTOMIZE_COST_ROI = [166, 92, 108, 46]
CUSTOMIZE_TITLE_ROI = [192, 166, 470, 42]
CUSTOMIZE_MENU_ROI = [240, 756, 240, 32]
OPTION_LEFT = (38, 257, 476)
OPTION_WIDTH = 207
OPTION_LABEL_ROWS = (808, 60)
OPTION_PRICE_ROWS = (866, 36)
# 改造上限：选项上方的「あと N回」为该选项还可改造的次数，不能再改造的选项下方改为「カスタマイズ上限」（选项已到最高等级，
# 或这张卡的改造次数已用完）；「実行する」上方的「合計あと N回」为这张卡还可改造的总次数（游戏数据 maxCustomizeCount
# 减去已改造的次数，各选项的每一级都算一次），「一覧に戻る」上方的「あと N枚」为本次特别指导还可改造的卡牌数。
OPTION_BADGE_ROWS = (782, 30)
OPTION_LIMIT_ROWS = (890, 32)
CUSTOMIZE_FOOTER_ROI = [80, 1000, 560, 46]
# 选项卡上下缘之外的两条横带：只有选中项的括号是橙色。
SELECTION_BANDS = ((784, 800), (904, 919))
# 相谈（商店）：左上角的画面标签（相談；卡牌强化、删除画面为「強化」「削除」）。确认对话框随说明的长短向上伸展（强化对话框
# 列出强化前、后两张卡，伸得最高），底部的「P 交换前 → 交换后」一行位置固定；名称是提示语「Pポイントを使用して…しますか」
# 下方、图标右侧的第一行（强化对话框为强化前的卡牌）。
SCREEN_LABEL_ROI = [0, 28, 182, 58]
EXCHANGE_DIALOG_ROI = [20, 100, 680, 900]
EXCHANGE_P_ROI = [80, 990, 560, 60]
# 确认对话框的标题、提示语中的动词与动作：交換確認（交换商品）、スキルカード強化（强化卡牌）、スキルカード削除（删除卡牌）。
SHOP_DIALOGS = (("交換確認", "交換", "buy"), ("スキルカード強化", "強化", "upgrade"), ("スキルカード削除", "削除", "remove"))
# 标题在对话框左缘；卡牌说明从图标右侧（x ≥ 190）开始，其中的同样字样不算标题。
DIALOG_TITLE_RIGHT = 190
# 相谈主画面的服务按钮：左为「強化」、右为「削除」，未用时显示价格；每批商品各可用一次，用过后按钮盖上「交換済」并显示下次的价格
# （「次 125」），「リフレッシュ」换一批商品时两者恢复可用。按钮下方左侧的「リフレッシュ」上方为剩余次数（「あと1回」），用完后按钮消失。
# P 点数左侧的气泡（「-100」）为正在扣除的 P：扣款动画期间按钮可能仍是扣款前的样子。
SERVICE_ROW_ROI = [40, 930, 640, 80]
# 按钮名称、读数字段与名称左缘的 x：「交換済」的勾常盖住名称的第二个字（「強 次150」），按第一个字与位置认按钮。
SERVICE_BUTTONS = (("強化", "upgrade", 120), ("削除", "remove", 440))
REFRESH_ROI = [10, 1025, 200, 90]
P_SPENT_ROI = [150, 88, 140, 50]
REWARD_KINDS = (("スキルカード", "card"), ("ドリンク", "drink"), ("アイテム", "item"))
ITEM_KINDS = {"skill_card": "card", "drink": "drink", "p_item": "item"}


def texts(vision, array, roi):
    return [dict(item, text=unicodedata.normalize("NFKC", item["text"]).strip()) for item in vision._read_title(array, roi)]


def reward_fields(prompt_items, counter_items):
    """奖励画面的提示语与底部计数文字 → (提示语, 奖励种类或 None, 剩余次数)。"""
    prompt = "".join(item["text"] for item in prompt_items)
    kind = next((value for word, value in REWARD_KINDS if word in prompt), None)
    counters = {}
    for item in counter_items:
        match = re.fullmatch(r"あと(\d+)回", item["text"])
        if match and item["confidence"] >= 0.7:
            # 左侧为卡牌移除，右侧为重抽。
            counters["removals" if item["box"][0] + item["box"][2] / 2 < 360 else "rerolls"] = int(match.group(1))
    return prompt, kind, counters


def offer_name(identity):
    """候选详情标题的查库结果 → (GHL 名称, 种类)；技能卡最多记一个「+」（GHL 没有「++」），未识别为 (None, None)。"""
    if not (identity.get("status") == "matched" or identity.get("unknown_reason") == "extra_upgrade_markers"):
        return None, None
    name = identity["database_name"]
    if identity.get("item_kind") == "skill_card":
        name = name.rstrip("+") + "+" * min(1, identity.get("observed_plus_count", 0))
    return name, ITEM_KINDS.get(identity.get("item_kind"))


def reward_reading(vision, rewards, mailbox, request):
    """奖励：种类、候选名称（与候选顺序一致，读不到为 None）与剩余次数。"""
    from utils.image_io import read_image
    from produce.analysis import normalized_image

    array = normalized_image(read_image(mailbox / request["frame"]))
    prompt, kind, counters = reward_fields(texts(vision, array, REWARD_PROMPT_ROI), texts(vision, array, REWARD_COUNTER_ROI))
    candidates = [candidate["id"] for candidate in request["candidates"] if candidate["id"].startswith("reward_")]
    names, kinds = [], []
    for candidate in candidates:
        frame = (request.get("extra_frames") or {}).get("offer" + candidate.removeprefix("reward_"))
        title = [item for item in texts(vision, normalized_image(read_image(mailbox / frame)), OFFER_TITLE_ROI) if item["confidence"] >= 0.6] if frame else []
        name, item_kind = offer_name(rewards.match_tokens(title) if title else {})
        names.append(name)
        kinds.append(item_kind)
    # 提示语读不到（画面过渡）而候选同属一种时，按名称所属种类补全。
    if kind is None and names and all(kinds) and len(set(kinds)) == 1:
        kind = kinds[0]
    return {"kind": kind, "prompt": prompt, "names": names, "candidates": candidates, "counters": counters}


def exchange_fields(dialog_items, p_items):
    """
    相谈确认对话框的文字 → 动作（buy 交换商品、upgrade 强化卡牌、remove 删除卡牌）、名称的文字与交换前、后的 P 点数；
    不是这些对话框时为 None。动作按提示语中的动词（「交換しますか」等），提示语读不到时按左缘的标题。
    """
    prompts = ((item, action) for _, verb, action in SHOP_DIALOGS for item in dialog_items if verb + "しますか" in item["text"])
    left = [item for item in dialog_items if item["box"][0] < DIALOG_TITLE_RIGHT]
    titles = ((item, action) for title, _, action in SHOP_DIALOGS for item in left if title in item["text"])
    found = next(prompts, None) or next(titles, None)
    if found is None:
        return None
    anchor, action = found
    below = anchor["box"][1] + anchor["box"][3]
    # 强化对话框中强化后的卡牌在下方，取第一行即强化前的卡牌。
    names = sorted(
        (item for item in dialog_items if 190 <= item["box"][0] <= 230 and item["box"][1] > below and item["confidence"] >= 0.6),
        key=lambda item: item["box"][1],
    )
    numbers = [int(value) for item in sorted(p_items, key=lambda item: item["box"][0]) if item["confidence"] >= 0.6 for value in re.findall(r"\d+", item["text"])]
    return {
        "action": action,
        "name": names[0] if names else None,
        "before": numbers[0] if len(numbers) >= 2 else None,
        "after": numbers[1] if len(numbers) >= 2 else None,
    }


def service_fields(row_items, refresh_items, spent_items):
    """
    相谈主画面的文字 → 两个服务按钮 upgrade（強化）与 remove（削除）：used 为是否盖着「交換済」，price 为下一次的价格（未用时为
    按钮上的价格，用过后为「次 N」，读不到为 None）；两个按钮的名称都读到才给出，否则为 None（提示「…を強化しました」或获得
    动画盖住了按钮）。refreshes 为「リフレッシュ」的剩余次数（「あと N回」）：按钮读到而次数读不到、或该处有别的文字时为 None，
    服务按钮读到而该处没有文字时为 0（按钮已消失）。spent 为 P 点数左侧气泡中正在扣除的 P，没有气泡为 None。
    """
    row = [item for item in row_items if item["confidence"] >= 0.6]
    buttons = {"upgrade": None, "remove": None}
    if not any("しました" in item["text"] for item in row):
        halves = [[item for item in row if (item["box"][0] + item["box"][2] / 2 < 360) == (side == 0)] for side in (0, 1)]
        found = [any(item["text"].startswith(word[0]) and abs(item["box"][0] - x) <= 30 for item in half) for half, (word, _, x) in zip(halves, SERVICE_BUTTONS)]
        if all(found):
            for half, (_, key, _) in zip(halves, SERVICE_BUTTONS):
                # 各段之间留空格：P 图标偶尔读成「0」，不与价格连成一个数。
                text = " ".join(item["text"] for item in sorted(half, key=lambda item: item["box"][0]))
                # 用过的按钮价格前有「次」；未用的按钮价格在最右（P 图标在价格左边）。
                used = "交換済" in text or "次" in text
                numbers = re.findall(r"\d+", text.split("次")[-1] if "次" in text else text)
                buttons[key] = {"used": used, "price": int(numbers[-1]) if numbers else None}
    refresh = [item for item in refresh_items if item["confidence"] >= 0.6 and item["text"]]
    refreshes = count_in(refresh, r"あと(\d+)回")
    if refreshes is None and not refresh and buttons["upgrade"] is not None:
        refreshes = 0
    # 气泡在 P 图标左边（中心 x < 275；图标被区域切开时可能读成别的字），数字前有负号：离开相谈时背后的窗框会读成「1」。
    bubbles = [re.fullmatch(r"[-－ー一](\d+)", item["text"]) for item in spent_items if item["confidence"] >= 0.6 and item["box"][0] + item["box"][2] / 2 < 275]
    spent = next((int(match.group(1)) for match in bubbles if match), None)
    return dict(buttons, refreshes=refreshes, spent=spent)


def consult_reading(vision, rewards, mailbox, request):
    """
    相谈：P 点数与画面标签；相谈主画面（标签「相談」，没有对话框）另有两个服务按钮 upgrade、remove（是否「交換済」与下一次的价格）、
    「リフレッシュ」的剩余次数 refreshes 与正在扣除的 P spent（见 service_fields）；交換確認对话框另有 exchange：商品名称
    （查库，读不到为 None）、种类与交换前后的 P 点数；スキルカード強化、スキルカード削除对话框另有 service：动作（upgrade、remove）、
    卡牌名称（强化为强化前的卡牌，查库，读不到或不是技能卡为 None）与前后的 P 点数。卡牌另记一栏，只认 exchange 的读取方不会把
    强化、删除当成购买。
    """
    from utils.image_io import read_image
    from produce.analysis import normalized_image

    array = normalized_image(read_image(mailbox / request["frame"]))
    label = "".join(item["text"] for item in texts(vision, array, SCREEN_LABEL_ROI) if item["confidence"] >= 0.6)
    fields = {"p": first_number(texts(vision, array, P_COUNTER_ROI)), "label": label}
    dialog = exchange_fields(texts(vision, array, EXCHANGE_DIALOG_ROI), texts(vision, array, EXCHANGE_P_ROI))
    if dialog is None:
        if "相談" in label:
            fields.update(service_fields(texts(vision, array, SERVICE_ROW_ROI), texts(vision, array, REFRESH_ROI), texts(vision, array, P_SPENT_ROI)))
        return fields
    name, kind = offer_name(rewards.match_tokens([dialog["name"]]) if dialog["name"] else {})
    if dialog["action"] == "buy":
        fields["exchange"] = {"name": name, "kind": kind, "before": dialog["before"], "after": dialog["after"]}
    else:
        name = name if kind == "card" else None
        fields["service"] = {"action": dialog["action"], "name": name, "before": dialog["before"], "after": dialog["after"]}
    return fields


def week_fields(counter_items, bonus_items):
    """行动选择画面的周数标签与成长加成文字 → 周数（中间考试前为第 10 − N 周，之后为第 18 − N 周）、标签与三维成长加成。"""
    label = "".join(item["text"] for item in counter_items)
    # 整个数字：「107週」不能只取后两位当作 7 周
    match = re.search(r"(\d+)週", label)
    exam_week = 18 if "最終" in label else 10 if "中間" in label else None
    week = exam_week - int(match.group(1)) if match and exam_week else None
    # 读错的数字（「中間まで7週」读成「中間まで77週1」得 −67 周）：不是考试前的周时记为未读，标签照记
    if week is not None and not (1 if exam_week == 10 else 11) <= week < exam_week:
        week = None
    bonus = []
    for item in sorted(bonus_items, key=lambda item: item["box"][0]):
        match = re.search(r"(\d+(?:\.\d)?)%", item["text"])
        if match:
            bonus.append(float(match.group(1)))
    return {"week": week, "counter": label, "bonus": dict(zip(("Vo", "Da", "Vi"), bonus)) if len(bonus) == 3 else None}


def first_number(items, confidence=0.6):
    """读数中第一个整数（忽略负号），读不到为 None。"""
    for item in sorted(items, key=lambda item: item["box"][0]):
        match = re.search(r"\d+", item["text"])
        if match and item["confidence"] >= confidence:
            return int(match.group())
    return None


def option_rois(slot):
    left = OPTION_LEFT[slot]
    return [left, OPTION_LABEL_ROWS[0], OPTION_WIDTH, OPTION_LABEL_ROWS[1]], [left + 55, OPTION_PRICE_ROWS[0], 130, OPTION_PRICE_ROWS[1]]


def limit_rois(slot):
    """选项上方「あと N回」与下方「カスタマイズ上限」的区域。"""
    left = OPTION_LEFT[slot]
    return [left, OPTION_BADGE_ROWS[0], OPTION_WIDTH, OPTION_BADGE_ROWS[1]], [left, OPTION_LIMIT_ROWS[0], OPTION_WIDTH, OPTION_LIMIT_ROWS[1]]


def count_in(items, pattern, confidence=0.6):
    """读数从左到右拼成一行后第一个匹配 pattern（一个数字分组）的次数，读不到为 None；OCR 常把「合計あと2回」拆成几段。"""
    text = "".join(item["text"] for item in sorted(items, key=lambda item: item["box"][0]) if item["confidence"] >= confidence)
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def customize_fields(p_items, cost_items, title_items, menu_items, slots, limits=None, footer_items=None):
    """
    特别指导画面的文字 → P 点数；改造预览画面另有卡牌名文字、各选项（名称与价格）与选中项的价格。
    给出 limits 时各选项另有 left（「あと N回」，读不到为 None）与 capped（「カスタマイズ上限」）；给出 footer_items 时另有
    total_left（「合計あと N回」）与 cards_left（「あと N枚」），画面上没有或读不到为 None。

    Args:
        slots: 每个选项位置的 (名称文字, 价格文字)，从左到右；名称为空的位置没有选项。
        limits: 每个选项位置的 (上方「あと N回」的文字, 下方「カスタマイズ上限」的文字)，与 slots 对应。
        footer_items: 「一覧に戻る」与「実行する」上方的文字。
    """
    fields = {"p": first_number(p_items)}
    if not any("カスタマイズ" in item["text"] for item in menu_items):
        return fields
    options = []
    for slot, (label_items, price_items) in enumerate(slots):
        # 两行的名称按行拼接；Maa 偶尔将片假名「タ」误识为形近的「夕」。
        label = "".join(item["text"] for item in sorted(label_items, key=lambda item: (item["box"][1], item["box"][0])) if item["confidence"] >= 0.6)
        if not label:
            break
        option = {"label": label.replace("夕", "タ"), "price": first_number(price_items)}
        if limits is not None:
            badge_items, limit_items = limits[slot]
            # 「カスタマイズ上限」常被读成「カス夕マイズ上限」等，认「上限」二字。
            option.update(left=count_in(badge_items, r"あと(\d+)回"), capped=any("上限" in item["text"] and item["confidence"] >= 0.6 for item in limit_items))
        options.append(option)
    title = max(title_items, key=lambda item: item["confidence"], default=None)
    fields = dict(fields, screen="preview", title=title, options=options, cost=first_number(cost_items))
    if footer_items is not None:
        # 「一覧に戻る」上方（画面左半）为「あと N枚」，「実行する」上方（右半）为「合計あと N回」；「合計」可能读不到。
        left = [item for item in footer_items if item["box"][0] + item["box"][2] / 2 < 360]
        right = [item for item in footer_items if item["box"][0] + item["box"][2] / 2 >= 360]
        fields.update(total_left=count_in(right, r"あと(\d+)回"), cards_left=count_in(left, r"あと(\d+)枚"))
    return fields


def selected_option(array):
    """选中的选项序号：按两条横带中橙色括号的中点对应选项位置，读不到为 None。"""
    columns = []
    for top, bottom in SELECTION_BANDS:
        band = array[top:bottom].astype(int)
        blue, green, red = band[..., 0], band[..., 1], band[..., 2]
        # 按色比判断：提示（「スキルカードカスタマイズ時…」等）弹出时整个画面变暗，括号由 (255, 156, 70) 变为约 (205, 127, 61)（RGB）。
        orange = (red > 170) & (green > 0.45 * red) & (green < 0.75 * red) & (blue < 0.4 * red)
        columns.append(orange.any(axis=0).nonzero()[0])
    if any(len(found) == 0 for found in columns):
        return None
    left, right = int(columns[0].min()), int(columns[0].max())
    # 一对括号的外缘相距约 230 像素。
    if not 200 <= right - left <= 260:
        return None
    middle = (left + right) / 2
    slot = min(range(len(OPTION_LEFT)), key=lambda index: abs(OPTION_LEFT[index] + OPTION_WIDTH / 2 - middle))
    return slot if abs(OPTION_LEFT[slot] + OPTION_WIDTH / 2 - middle) <= 30 else None


def customize_reading(vision, catalog, mailbox, request):
    """特别指导：P 点数；改造预览画面另有卡牌名（查库，未识别为 None）、菜单选项及其改造上限、选中项及其价格、这张卡还可改造的总次数与本次还可改造的卡牌数。"""
    from utils.image_io import read_image
    from produce.console import card_name
    from produce.analysis import normalized_image

    array = normalized_image(read_image(mailbox / request["frame"]))
    slots = [tuple(texts(vision, array, roi) for roi in option_rois(slot)) for slot in range(len(OPTION_LEFT))]
    limits = [tuple(texts(vision, array, roi) for roi in limit_rois(slot)) for slot in range(len(OPTION_LEFT))]
    fields = customize_fields(
        texts(vision, array, P_COUNTER_ROI),
        texts(vision, array, CUSTOMIZE_COST_ROI),
        texts(vision, array, CUSTOMIZE_TITLE_ROI),
        texts(vision, array, CUSTOMIZE_MENU_ROI),
        slots,
        limits,
        texts(vision, array, CUSTOMIZE_FOOTER_ROI),
    )
    if fields.get("screen") != "preview":
        return fields
    title = fields.pop("title")
    identity = catalog.match(title["text"], title["confidence"]) if title else {}
    if identity.get("database_card_id") and identity.get("status") == "matched":
        fields["card"] = card_name(identity)
    elif identity.get("database_card_id") and identity.get("unknown_reason") == "extra_upgrade_markers":
        fields["card"] = identity["database_name"]
    else:
        fields["card"] = None
    fields["selected"] = selected_option(array)
    return fields


def week_reading(vision, mailbox, request):
    """行动选择画面：周数与三维成长加成（Vo、Da、Vi），以及请求中已读取的体力、P 点数与三维。"""
    from utils.image_io import read_image
    from produce.analysis import normalized_image

    array = normalized_image(read_image(mailbox / request["frame"]))
    fields = week_fields(texts(vision, array, WEEK_COUNTER_ROI), texts(vision, array, WEEK_BONUS_ROI))
    return dict(fields, resources=request.get("state") or {})


class Reader:
    """按请求读画面；离线读整个信箱与实况逐条读共用。"""

    def __init__(self, mailbox, log_dir):
        from produce.state import BattleStateReader
        from produce.catalog import RewardCatalog, SkillCardCatalog
        from produce.analysis import OfflineVision

        resource = ROOT / "assets" / "resource" / "base"
        database = ROOT / "assets" / "data" / "produce_skill_cards.json"
        self.mailbox = mailbox
        self.catalog = SkillCardCatalog(database, ROOT / "assets" / "data" / "skill_card_aliases.json")
        self.rewards = RewardCatalog(self.catalog, database.with_name("produce_reward_items.json"))
        self.vision = OfflineVision(resource, log_dir)
        self.vision.state_reader = BattleStateReader(
            database.with_name("battle_buff_templates.json"), self.vision._read_title, self.vision.match_buff_templates, resource / "image"
        )

    def read(self, request, choice=None):
        from ghl_frame import read_battle_request
        from utils.image_io import read_image
        from produce.analysis import normalized_image

        row = {"id": request["id"], "kind": request["kind"], "choice": choice}
        try:
            if request["kind"] == "battle":
                _, row["frame"] = read_battle_request(self.vision, self.catalog, read_image(self.mailbox / request["frame"]), request)
                row["battle_id"] = (request.get("context") or {}).get("battle_id")
            elif request["kind"] == "reward":
                row["reward"] = reward_reading(self.vision, self.rewards, self.mailbox, request)
            elif request["kind"] == "week":
                row["week"] = week_reading(self.vision, self.mailbox, request)
            elif request["kind"] == "customize":
                row["customize"] = customize_reading(self.vision, self.catalog, self.mailbox, request)
            elif request["kind"] == "consult":
                row["consult"] = consult_reading(self.vision, self.rewards, self.mailbox, request)
        except Exception as exc:
            row["error"] = str(exc)
        return row


def completed_ids(out):
    """输出中已读成功的请求 id。上次写到一半中断的残行先补上换行，使下一行不与它拼接；
    读不出的行与读取失败的行不算完成（重启后重读，后写的一行取代前面的）。"""
    if not out.exists():
        return set()
    # 写到一半中断的行可能断在多字节字符中间：按替换字符解码，该行读不出即跳过。
    text = out.read_bytes().decode("utf-8", errors="replace")
    if text and not text.endswith("\n"):
        with out.open("a", encoding="utf-8") as stream:
            stream.write("\n")
    done = set()
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not row.get("error"):
            done.add(row["id"])
    return done


def read_with_retry(read, request, choice, waits=(1, 2), sleep=None):
    """读取失败（截图尚未写完、识别出错等）按 waits 的间隔重试，仍失败则返回带 error 的一行。"""
    import time

    row = read(request, choice)
    for wait in waits:
        if not row.get("error"):
            break
        (sleep or time.sleep)(wait)
        row = read(request, choice)
    return row


def lock_output(out):
    """同一输出只允许一个读取进程写入：锁文件记录进程号，进程已不在时视为残留并接管。"""
    import os
    import atexit

    lock = out.with_name(out.name + ".lock")
    try:
        os.kill(int(lock.read_text().strip()), 0)
        raise SystemExit(f"另一个读取进程（{lock.read_text().strip()}）正在写 {out}")
    except (OSError, ValueError) as exc:
        if isinstance(exc, PermissionError):
            raise SystemExit(f"另一个读取进程正在写 {out}") from exc
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True))


def follow(reader, out, poll=0.3, retry_after=30, attempts=5):
    """
    监视 requests/：每个新请求读一次画面并追加一行（按请求 id 的顺序，即提出的顺序）。
    读取失败的请求每隔 retry_after 秒再读，至多 attempts 轮；读成功时追加的一行取代失败的一行。
    """
    import time

    out.parent.mkdir(parents=True, exist_ok=True)
    lock_output(out)
    done = completed_ids(out)
    failed = {}
    print(f"监视 {reader.mailbox / 'requests'} → {out}（已有 {len(done)} 条）", flush=True)
    while True:
        for path in sorted((reader.mailbox / "requests").glob("*.json")):
            if path.stem in done or failed.get(path.stem, (0, 0))[1] > time.time():
                continue
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            response = reader.mailbox / "responses" / path.name
            try:
                choice = json.loads(response.read_text(encoding="utf-8")).get("choice") if response.exists() else None
            except (OSError, ValueError):
                choice = None
            row = read_with_retry(reader.read, request, choice)
            with out.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            rounds = failed.get(path.stem, (0, 0))[0] + 1
            if not row.get("error") or rounds >= attempts:
                done.add(path.stem)
                failed.pop(path.stem, None)
            else:
                failed[path.stem] = (rounds, time.time() + retry_after)
            print(f"[{row['id']}] {row['kind']}{' 读取失败：' + row['error'] if row.get('error') else ''}", flush=True)
        time.sleep(poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mailbox", type=Path, required=True, help="决策信箱目录（含 decisions.jsonl 与 frames/）")
    parser.add_argument("--out", type=Path, help="输出 JSONL；--follow 时默认 <信箱>/ghl/readings.jsonl")
    parser.add_argument("--follow", action="store_true", help="实况：监视 requests/，逐条追加")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = args.out or (args.mailbox / "ghl" / "readings.jsonl" if args.follow else None)
    if out is None:
        parser.error("--out is required without --follow")
    reader = Reader(args.mailbox, out.parent / "vision_log")
    if args.follow:
        follow(reader, out)
        return
    rows = []
    for line in (args.mailbox / "decisions.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        rows.append(reader.read(record["request"], (record.get("response") or {}).get("choice")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"{len(rows)} 条决策 → {out}")


if __name__ == "__main__":
    main()
