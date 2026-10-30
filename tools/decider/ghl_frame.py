"""把一帧战斗画面的离线识别结果整理为 GHL（gakumas-headless-lab）的实况帧 LiveFrame。

GHL 的影子回放与决策模型按 LiveFrame 读取战斗状态（src/bridge/maaShadow.ts）：
- t：剩余回合（大号数字，含当前回合）；tAdd：日程外追加的回合；
- toClear：课程 CLEAR 前还差的分数；bonus：CLEAR 后的传说加成计数（分数 − CLEAR）；score：考试分数；
- st：体力；g：元气；
- b：Buff（goodImpressionTurns、motivation、halfCostTurns、doubleCostTurns、nullifyDebuff），未显示为 0；
- gib：好印象增加量 [百分比, 回合]；gie：好印象效果百分比；sb：得分提升 [百分比, 回合或 None]；
- hand：手牌名（从左到右，「+」为强化，读不出为「?」）。战斗中再次强化的「+」牌显示「++」，GHL 没有这种牌，记为「+」。
  四五张手牌叠放时，右邻的牌盖住卡名末尾与卡面右缘的「+」：认出基础名而看不到强化标记的牌记为「基础名+?」，
  是该牌或其强化版，由 GHL 按它所记的牌组判断。

另附 slots：每张手牌对应的请求候选 id（可出的牌为 card_N，不可出的为 unplayable_N），GHL 据此把手牌位置对应到候选；

另附 drinks：饮料槽中的饮料名（从左到右；没有登记图标的饮料为「?」），GHL 用来对应饮料候选；
buffsComplete：Buff 列末尾出现「⋮」（显示不下）或被提示框盖住时为 False，此时 b 中没有的 Buff 不能当作 0；
turnTypes：考试从当前回合起各剩余回合的属性（回合环色段），读到的段数与剩余回合一致时才给出；
lesson：课程的属性与 SP（右上角参数图标颜色、圆环上方的 SP 标记）；exam：考试当前回合的属性与倍率。

读不到的字段为 None，并列入 unknown，不按常见值补齐。仅供本地开发调试的外部决策者与离线评估使用。
"""

from produce.state import overlap_ratio
from produce.console import card_name

# 课程右上角参数框的属性图标（取样区域 [520, 48, 24, 24]）：Vi 橙、Vo 粉、Da 蓝。CLEAR 后该处换成
# 「レジェンドボーナス」计数框，不再有属性图标。
LESSON_ICON = (520, 48, 24, 24)
# 课程圆环上方：SP 课程有「SP」标记（OCR 多读作「LS?」「L+S」）。
LESSON_SP_ROI = [320, 30, 90, 45]

# 与 GHL 引擎字段同名的 Buff，数值即层数或回合。
GHL_BUFFS = ("goodImpressionTurns", "motivation", "halfCostTurns", "doubleCostTurns", "nullifyDebuff")


def field_value(state, key):
    return ((state.get("fields") or {}).get(key) or {}).get("value")


def hand_names(cards):
    """按卡牌框从左到右给出手牌名；身份未确认的牌记为「?」，认出基础名而看不到强化标记的牌记为「基础名+?」。"""
    names = []
    for card in sorted(cards, key=lambda card: card["box"][0]):
        identity = card.get("identity") or {}
        if identity.get("database_card_id") and identity.get("status") == "matched":
            names.append(card_name(identity))
        elif identity.get("database_card_id") and identity.get("unknown_reason") == "extra_upgrade_markers":
            # 「++」：库中最高只有「+」，按库名记。
            names.append(identity["database_name"])
        elif identity.get("database_card_id") and identity.get("status") == "base_match_variant_unresolved":
            # 叠放的手牌只露出卡名前段（upgrade_not_visible_in_overlap），或两个识别视图的强化标记不一致。
            names.append((identity.get("base_name") or identity["database_name"].rstrip("+")) + "+?")
        else:
            names.append("?")
    return names


def drink_names(state, drink_boxes=None):
    """饮料槽从左到右的饮料名；给出信箱候选的饮料框时逐框对应，框内没有登记图标的饮料记为「?」。"""
    drinks = state.get("drinks") or []
    if not drink_boxes:
        return [drink["name"] or "?" for drink in drinks]
    names = []
    for box in sorted(drink_boxes, key=lambda box: box[0]):
        hit = next((drink for drink in drinks if overlap_ratio(drink["box"], box) >= 0.45), None)
        names.append((hit or {}).get("name") or "?")
    return names


def lesson_attribute(array):
    """课程属性：右上角参数图标的平均颜色（BGR）。"""
    x, y, width, height = LESSON_ICON
    blue, green, red = (float(value) for value in array[y : y + height, x : x + width].reshape(-1, 3).mean(axis=0))
    if blue > red:
        return "Da"
    if red > 200 and green > 180:
        return "Vi"
    if red > 200 and blue > 160:
        return "Vo"
    return None


def read_battle_request(vision, catalog, image, request):
    """
    读一条战斗决策请求：可出与不可出的牌合起来是整手牌，饮料按请求的饮料候选框对应。

    Returns:
        tuple: (OfflineVision.analyze 的结果, GHL 实况帧；课程帧另附 lesson，考试帧另附 exam)
    """
    from produce.analysis import normalized_image

    candidates = [
        {"candidate_id": item["id"], "box": item["box"], "label": item.get("detector_label"), "in_hand_region": True}
        for item in request["candidates"]
        if item["id"].startswith("card_")
    ]
    candidates += [
        {"candidate_id": f"unplayable_{index}", "box": box, "label": "useless", "in_hand_region": True}
        for index, box in enumerate((request.get("state") or {}).get("unplayable_card_boxes") or [])
    ]
    result = vision.analyze(image, catalog, candidates)
    frame = ghl_frame(result, [item["box"] for item in request["candidates"] if item["id"].startswith("drink_")])
    if frame["layout"] == "lesson":
        array = normalized_image(image)
        sp = any("S" in item["text"] for item in vision._read_title(array, LESSON_SP_ROI))
        frame["lesson"] = {"attr": lesson_attribute(array) if frame.get("bonus") is None else None, "sp": sp}
    elif frame["layout"] == "exam":
        state = result.get("battle_state") or {}
        frame["exam"] = {"attr": field_value(state, "attribute"), "mult": field_value(state, "attribute_multiplier_percent")}
    return result, frame


def ghl_frame(result, drink_boxes=None):
    """
    Args:
        result: OfflineVision.analyze 的结果（cards 与 battle_state）。
        drink_boxes: 信箱请求中饮料候选的框，可选；给出时没有登记图标的饮料也占位为「?」。

    Returns:
        dict: LiveFrame 字段，外加 layout（lesson / exam / None）与 unknown（读不到的字段名）。
    """
    state = result.get("battle_state") or {}
    layout = state.get("layout") or ("exam" if state.get("hud_present") else None)
    frame = {"layout": layout, "t": field_value(state, "turns_remaining"), "st": field_value(state, "stamina"), "g": field_value(state, "genki")}
    added = field_value(state, "turns_added")
    if added:
        frame["tAdd"] = added
    if layout == "lesson":
        if field_value(state, "lesson_bonus") is not None:
            frame["bonus"] = field_value(state, "lesson_bonus")
        else:
            frame["toClear"] = field_value(state, "lesson_to_clear")
    else:
        frame["score"] = field_value(state, "score")
    buffs, score_buffs = {}, []
    for buff in state.get("buffs") or []:
        if buff["key"] in GHL_BUFFS:
            buffs[buff["key"]] = buff["value"]
        elif buff["key"] == "scoreBuffPercent":
            score_buffs.append([buff["value"], buff.get("duration")])
        elif buff["key"] == "goodImpressionGainPercent":
            frame.setdefault("gib", []).append([buff["value"], buff.get("duration")])
        elif buff["key"] == "goodImpressionEffectPercent":
            frame["gie"] = buff["value"]
    frame["b"] = buffs
    frame["buffsComplete"] = not (state.get("buffs_overflow") or state.get("buffs_occluded"))
    if score_buffs:
        frame["sb"] = score_buffs
    cards = sorted(result.get("cards") or [], key=lambda card: card["box"][0])
    frame["hand"] = hand_names(cards)
    frame["slots"] = [card.get("candidate_id") for card in cards]
    frame["drinks"] = drink_names(state, drink_boxes)
    ring = state.get("turn_ring") or {}
    if ring.get("status") == "observed":
        frame["turnTypes"] = ring["remaining"]
    frame["unknown"] = sorted(key for key, value in frame.items() if value is None)
    return frame
