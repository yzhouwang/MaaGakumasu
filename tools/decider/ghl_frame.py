"""把一帧战斗画面的离线识别结果整理为 GHL（gakumas-headless-lab）的实况帧 LiveFrame。

GHL 的影子回放与决策模型按 LiveFrame 读取战斗状态（src/bridge/maaShadow.ts）：
- t：剩余回合（大号数字，含当前回合）；tAdd：日程外追加的回合；
- toClear：课程 CLEAR 前还差的分数；bonus：CLEAR 后的传说加成计数（分数 − CLEAR）；score：考试分数；
- st：体力；g：元气；
- b：Buff（goodImpressionTurns、motivation、halfCostTurns、doubleCostTurns、nullifyDebuff），未显示为 0；
- gib：好印象增加量 [百分比, 回合]；gie：好印象效果百分比；sb：得分提升 [百分比, 回合或 None]；
- hand：手牌名（从左到右，「+」为强化，读不出为「?」）。

读不到的字段为 None，并列入 unknown，不按常见值补齐。仅供本地开发调试的外部决策者与离线评估使用。
"""

from produce.console import card_name

# 与 GHL 引擎字段同名的 Buff，数值即层数或回合。
GHL_BUFFS = ("goodImpressionTurns", "motivation", "halfCostTurns", "doubleCostTurns", "nullifyDebuff")


def field_value(state, key):
    return ((state.get("fields") or {}).get(key) or {}).get("value")


def hand_names(cards):
    """按卡牌框从左到右给出手牌名；身份未确认的牌记为「?」。"""
    names = []
    for card in sorted(cards, key=lambda card: card["box"][0]):
        identity = card.get("identity") or {}
        names.append(card_name(identity) if identity.get("database_card_id") and identity.get("status") == "matched" else "?")
    return names


def ghl_frame(result):
    """
    Args:
        result: OfflineVision.analyze 的结果（cards 与 battle_state）。

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
    if score_buffs:
        frame["sb"] = score_buffs
    frame["hand"] = hand_names(result.get("cards") or [])
    frame["unknown"] = sorted(key for key, value in frame.items() if value is None)
    return frame
