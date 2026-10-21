"""使用 Maa 原生 OCR 和配置中的绿幕模板读取对局状态。"""

import re
import json
import hashlib
import unicodedata
from pathlib import Path

import numpy as np

STATE_FIELDS = (
    "turns_remaining",
    "turns_added",
    "score",
    "stamina",
    "genki",
    "attribute",
    "attribute_multiplier_percent",
    "lesson_to_clear",
    "lesson_to_perfect",
    "lesson_bonus",
)
# 考试 HUD：剩余回合环、追加回合的「+N」胶囊、体力与元气。体力区域放宽并按数字中心筛选，
# 过窄的区域会把体力心形图标的一半与数字连读（如 7 读成 27）。
EXAM_HUD = {
    "turns": ([20, 55, 80, 70], [32, 69, 60, 46]),
    "added": [95, 115, 85, 45],
    "stamina": [530, 195, 130, 64],
    "genki": ([643, 170, 72, 43], [648, 172, 67, 40]),
}
# 课程 HUD：左上「残りターン数」框（追加回合显示为「5+1」，日程结束后为「0+2」）、中央圆环
# 「CLEARまで」或 CLEAR 后的「PERFECTまで」、CLEAR 后右上角的「レジェンドボーナス」计数；
# 体力与元气比考试 HUD 高约 40 像素。
LESSON_HUD = {
    "label": [20, 28, 170, 44],
    "turns": [70, 58, 110, 58],
    "goal_label": [270, 88, 180, 34],
    "goal": [280, 108, 160, 72],
    "bonus": [500, 30, 210, 60],
    "stamina": [530, 150, 130, 70],
    "genki": [636, 126, 64, 46],
}
# 体力数字位于心形图标右侧，按识别框中心横坐标筛选，排除图标与右侧 P 道具计数。
STAMINA_CENTER_X = (560, 640)
# 课程的 Buff 列比考试高约 13 像素，第一个图标会越出模板登记的区域上沿；考试 Buff 列上方是 P 道具
# 触发预告的大图标，不放宽。
BUFF_COLUMN = {"lesson": ([0, 210, 84, 560], 250), "exam": (None, 263)}
ANOMALY_FIELDS = (
    "stance",
    "stance_stage",
    "full_power_charge",
    "next_stance",
    "transition_after_turns",
    "held_cards_count",
    "held_cards_capacity",
)
STANCE_NAMES = {"strength": "强气", "preservation": "温存", "fullPower": "全力", "none": "通常"}


def unknown_field(reason="not_observed", roi=None, raw=None):
    return {"value": None, "status": "unknown", "reason": reason, "roi": roi, "raw_ocr": raw or [], "confidence": None}


def number_field(raw, roi, *, maximum, percent=False, minimum_confidence=0.70):
    values = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        pattern = r"\d[\d,]*\s*%" if percent else r"[(), .]*\d[\d,]*[(), .]*"
        if not re.fullmatch(pattern, text) or item["confidence"] < minimum_confidence:
            continue
        value = int(re.sub(r"\D", "", text))
        if value <= maximum:
            values.append((value, item["confidence"]))
    if len({value for value, _ in values}) != 1:
        return unknown_field("missing_or_ambiguous_number", roi, raw)
    value, confidence = max(values, key=lambda item: item[1])
    return {"value": value, "status": "observed", "confidence": confidence, "roi": roi, "raw_ocr": raw}


def centered(raw, x_range):
    """只保留识别框中心横坐标落在 x_range 内的文字。"""
    return [item for item in raw if x_range[0] <= item["box"][0] + item["box"][2] / 2 <= x_range[1]]


def turn_fields(raw, roi):
    """解析剩余回合与追加回合：「8」「5+1」「0+2」或分开识别的「5」「+1」。"""
    tokens = [item for item in sorted(raw, key=lambda item: item["box"][0]) if item["confidence"] >= 0.70]
    text = " ".join(unicodedata.normalize("NFKC", item["text"]).strip() for item in tokens)
    match = re.fullmatch(r"(\d{1,2})\s*(?:\+\s*(\d{1,2}))?", text)
    if not match:
        return unknown_field("missing_or_ambiguous_turns", roi, raw), unknown_field("missing_or_ambiguous_turns", roi, raw)
    confidence = min(item["confidence"] for item in tokens)
    turns = {"value": int(match.group(1)), "status": "observed", "confidence": confidence, "roi": roi, "raw_ocr": raw}
    added = {"value": int(match.group(2) or 0), "status": "observed" if match.group(2) else "not_shown", "confidence": confidence, "roi": roi, "raw_ocr": raw}
    return turns, added


def added_turns_field(raw, roi):
    """考试剩余回合环下方的「+N」胶囊；未显示时为 0（not_shown）。"""
    values = []
    for item in raw:
        match = re.fullmatch(r"\+\s*(\d{1,2})", unicodedata.normalize("NFKC", item["text"]).strip())
        if match and item["confidence"] >= 0.70:
            values.append((int(match.group(1)), item["confidence"]))
    if len({value for value, _ in values}) > 1:
        return unknown_field("ambiguous_added_turns", roi, raw)
    if not values:
        return {"value": 0, "status": "not_shown", "confidence": None, "roi": roi, "raw_ocr": raw}
    value, confidence = max(values, key=lambda item: item[1])
    return {"value": value, "status": "observed", "confidence": confidence, "roi": roi, "raw_ocr": raw}


def buff_number(raw, roi, format_name):
    if format_name in ("integer", "percent"):
        return number_field(raw, roi, maximum=9999, percent=format_name == "percent")
    # Maa 偶尔将开头的片假名「タ」误识为形近的「夕」。
    suffix = {"turns": r"(?:ターン|夕ーン|回合)", "times": r"(?:回|次)", "turns_after": r"(?:ターン|夕ーン)後|回合后|回合後"}[format_name]
    accepted = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        match = re.fullmatch(r"(\d{1,4})\s*(?:" + suffix + ")", text)
        if match:
            accepted.append({**item, "text": match.group(1)})
    result = number_field(accepted, roi, maximum=9999)
    result["raw_ocr"] = raw
    return result


def number_views(views):
    """合并可读数值视图，存在分歧时保留未知。"""
    values = {view["value"] for view in views if view["value"] is not None}
    if len(values) == 1:
        reading = max((view for view in views if view["value"] is not None), key=lambda view: view["confidence"])
        return {**reading, "value_views": views}
    reason = "conflicting_number_views" if values else "missing_or_ambiguous_number"
    reading = unknown_field(reason, views[0]["roi"], [item for view in views for item in view["raw_ocr"]])
    return {**reading, "value_views": views}


def overlap_ratio(left, right):
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[0] + left[2], right[0] + right[2]), min(left[1] + left[3], right[1] + right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    union = left[2] * left[3] + right[2] * right[3] - intersection
    return intersection / union if union else 0


def player_score_roi(rgb):
    """通过宽白色排名框定位 NIA 玩家，不依赖颜色。

    对手也可能使用彩色或相似头像；仅接受唯一的一对边框，
    不通过颜色和角色头像判断玩家位置。
    """
    frame = rgb[10:156, 216:720].astype(int)
    white = (frame.min(axis=2) > 225) & (frame.max(axis=2) - frame.min(axis=2) < 30)
    columns = np.flatnonzero(white.sum(axis=0) >= 120) + 216
    groups = np.split(columns, np.flatnonzero(np.diff(columns) > 2) + 1) if len(columns) else []
    pairs = [
        (int(left[0]), int(right[0])) for index, left in enumerate(groups) for right in groups[index + 1 :] if 116 <= right[0] - left[0] <= 126
    ]
    if len(pairs) == 1:
        left, right = pairs[0]
        left, right = max(216, left - 4), min(720, right + 8)
        return [left, 124, right - left, 49]
    return None


def held_card_fields(raw, roi):
    """只解析明确显示的保留卡计数，未显示时不推断为零。"""
    pairs = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        match = re.fullmatch(r"保留\s*(\d{1,2})\s*/\s*(\d{1,2})", text)
        if match and item["confidence"] >= 0.70:
            count, capacity = map(int, match.groups())
            if 0 <= count <= capacity <= 99:
                pairs.append((count, capacity, item["confidence"]))
    if len({(count, capacity) for count, capacity, _ in pairs}) != 1:
        return [unknown_field("missing_or_ambiguous_held_counter", roi, raw) for _ in range(2)]
    count, capacity, confidence = max(pairs, key=lambda pair: pair[2])
    return [{"value": value, "status": "observed", "roi": roi, "raw_ocr": raw, "confidence": confidence} for value in (count, capacity)]


class BattleStateReader:
    def __init__(self, registry: Path, read_ocr, match_templates=None, image_root=None):
        self.read_ocr = read_ocr
        self.match_templates = match_templates
        self.template_sha256 = None
        self.template_error = None
        self.templates = []
        self.ambiguity_margin = 0.025
        try:
            content = registry.read_bytes()
            data = json.loads(content)
            self.ambiguity_margin = float(data.get("ambiguity_margin", 0.025))
            digest = hashlib.sha256(content)
            problems = []
            entries = [
                (kind, entry)
                for group, kind in (("buffs", "buff"), ("stances", "stance"), ("stance_previews", "stance_preview"))
                for entry in data.get(group, [])
            ]
            for kind, definition in entries:
                entry = {**definition, "kind": kind}
                if entry.get("enabled") is False:
                    continue
                if entry.get("reading", {}).get("format", "integer") not in ("integer", "percent", "turns", "times", "turns_after"):
                    raise ValueError("Unsupported Buff value format")
                if image_root:
                    root = Path(image_root).resolve()
                    template = (root / entry["template"]).resolve()
                    if not template.is_relative_to(root):
                        raise ValueError("Template escaped image directory")
                    if not template.is_file():
                        problems.append("Missing template: " + entry["template"])
                        continue
                    raw = template.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != entry["template_sha256"]:
                        problems.append("Template checksum mismatch: " + entry["template"])
                        continue
                    digest.update(raw)
                self.templates.append(entry)
            self.template_sha256 = digest.hexdigest()
            if problems:
                self.template_error = "; ".join(problems)
            if not match_templates:
                self.template_error = "Native TemplateMatch callback unavailable"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.templates = []
            self.template_error = str(exc)

    def _ocr(self, array, roi, errors):
        try:
            return self.read_ocr(array, roi)
        except Exception as exc:
            errors.append({"roi": roi, "error": str(exc)})
            return []

    def _reading(self, array, box, spec, errors):
        dx, dy, width, height = spec["roi_offset"]
        left, top = max(0, box[0] + dx), max(0, box[1] + dy)
        roi = [left, top, min(width, 720 - left), min(height, 1280 - top)]
        raw = self._ocr(array, roi, errors)
        return buff_number(raw, roi, spec["format"])

    def _matches(self, array, kind, errors, roi=None):
        hits = []
        if self.match_templates:
            for entry in self.templates:
                if entry["kind"] != kind:
                    continue
                try:
                    for hit in self.match_templates(array, {**entry, "roi": roi} if roi else entry):
                        if hit["score"] >= entry["threshold"] - self.ambiguity_margin:
                            hits.append({**hit, "definition": entry})
                except Exception as exc:
                    errors.append({"template": entry["template"], "error": str(exc)})
        groups = []
        for hit in sorted(hits, key=lambda item: -item["score"]):
            group = next((g for g in groups if overlap_ratio(g[0]["box"], hit["box"]) >= 0.45), None)
            if group is None:
                groups.append([hit])
            else:
                group.append(hit)
        return sorted(groups, key=lambda g: g[0]["box"][1])

    @staticmethod
    def _icon_evidence(best, group):
        entry = best["definition"]
        return {
            "backend": "MaaFramework.TemplateMatch",
            "template_id": entry["id"],
            "template": entry["template"],
            "score": best["score"],
            "threshold": entry["threshold"],
            "method": entry["method"],
            "green_mask": True,
            "candidates": [{"id": hit["definition"]["id"], "score": hit["score"]} for hit in group[:3]],
        }

    def _buffs(self, array, rgb, errors, layout="exam"):
        buffs, unknown = [], []
        roi, first_center = BUFF_COLUMN[layout]
        for group in self._matches(array, "buff", errors, roi):
            original_group = group
            unit_evidence = None
            family = group[0]["definition"].get("value_family")
            relatives = [hit for hit in group if family and hit["definition"].get("value_family") == family]
            # 只有明确登记为同源的图标才能通过数值单位区分，
            # 其他不同图标仍按匹配分数竞争。
            if len({hit["definition"]["reading"]["format"] for hit in relatives}) > 1:
                readings = []
                for hit in relatives:
                    spec = hit["definition"]["reading"]
                    views = [self._reading(array, hit["box"], spec, errors)]
                    if spec.get("second_roi_offset"):
                        views.append(self._reading(array, hit["box"], {**spec, "roi_offset": spec["second_roi_offset"]}, errors))
                    readings.extend({"format": spec["format"], "reading": view} for view in views)
                observed_formats = {view["format"] for view in readings if view["reading"]["value"] is not None}
                if len(observed_formats) == 1:
                    value_format = observed_formats.pop()
                    group = [
                        hit
                        for hit in group
                        if hit["definition"].get("value_family") != family or hit["definition"]["reading"]["format"] == value_format
                    ]
                    unit_evidence = {"family": family, "format": value_format, "value_views": readings}
            best = group[0]
            entry, box = best["definition"], best["box"]
            if best["score"] < entry["threshold"]:
                continue
            rival = next((hit for hit in group if hit["definition"]["key"] != entry["key"]), None)
            evidence = self._icon_evidence(best, original_group)
            if unit_evidence:
                evidence["value_format_disambiguation"] = unit_evidence
            margin = (
                max(entry.get("ambiguity_margin", self.ambiguity_margin), rival["definition"].get("ambiguity_margin", self.ambiguity_margin))
                if rival
                else self.ambiguity_margin
            )
            if rival and best["score"] - rival["score"] < margin:
                unknown.append({"box": box, "icon_evidence": evidence, "value": None, "status": "unknown", "reason": "ambiguous_templates"})
                continue
            reading = self._reading(array, box, entry["reading"], errors)
            views = [reading]
            second_offset = entry["reading"].get("second_roi_offset")
            if second_offset:
                views.append(self._reading(array, box, {**entry["reading"], "roi_offset": second_offset}, errors))
            observed_values = {view["value"] for view in views if view["value"] is not None}
            incompatible_unit = {
                "integer": r"\d\s*(?:%|ターン|夕ーン|回合|回|次)",
                "turns": r"\d\s*%",
            }.get(entry["reading"]["format"])
            if incompatible_unit and any(
                re.search(incompatible_unit, unicodedata.normalize("NFKC", item["text"])) for view in views for item in view["raw_ocr"]
            ):
                # 效果修正可能复用基础图标；出现明确的修正单位时，
                # 不能将其解释为基础层数或持续回合。
                unknown.append(
                    {
                        "box": box,
                        "icon_evidence": evidence,
                        "candidate_key": entry["key"],
                        "value": None,
                        "status": "unknown",
                        "reason": "value_unit_conflict",
                        "value_views": views,
                    }
                )
                continue
            conflict = len(observed_values) > 1
            if not conflict:
                reading = next((view for view in views if view["value"] is not None), reading)
            value = reading["value"] if not conflict else None
            duration = value if entry["unit"] == "turns" else None
            duration_evidence = None
            if entry.get("duration_reading"):
                duration_evidence = self._reading(array, box, entry["duration_reading"], errors)
                duration = duration_evidence["value"]
            buffs.append(
                {
                    "box": box,
                    "key": entry["key"],
                    "name": entry["name"],
                    "unit": entry["unit"],
                    "value": value,
                    "duration": duration,
                    "duration_evidence": duration_evidence,
                    "status": "observed" if value is not None else "icon_only",
                    "unknown_reason": "conflicting_value_views" if conflict else ("value_not_observed" if value is None else None),
                    "icon_evidence": evidence,
                    "value_views": views,
                    "raw_ocr": reading["raw_ocr"],
                    "ocr_confidence": reading["confidence"],
                }
            )
        # 保留未分类区域以便补充新机制素材，
        # 不认定每个采样区域都包含 Buff。
        for slot in range(8):
            cy = round(first_center + slot * 61.4)
            box = [14, cy - 30, 60, 60]
            if not any(overlap_ratio(box, b["box"]) >= 0.45 for b in [*buffs, *unknown]):
                unknown.append({"box": box, "status": "candidate_region", "reason": "no_registered_template", "value": None})
        return buffs, unknown

    def _single_icon(self, array, kind, errors):
        groups = self._matches(array, kind, errors)
        qualified = [group for group in groups if group[0]["score"] >= group[0]["definition"]["threshold"]]
        if len(qualified) != 1:
            return None, unknown_field("no_unique_" + kind + "_icon")
        group = qualified[0]
        best = group[0]
        evidence = self._icon_evidence(best, group)
        rival = next((hit for hit in group if hit["definition"]["id"] != best["definition"]["id"]), None)
        if rival and best["score"] - rival["score"] < self.ambiguity_margin:
            return None, {**unknown_field("ambiguous_templates"), "icon_evidence": evidence}
        return best, {"value": best["definition"]["key"], "status": "observed", "box": best["box"], "icon_evidence": evidence}

    def _numeric_views(self, array, box, spec, errors):
        views = [self._reading(array, box, spec, errors)]
        if spec.get("second_roi_offset"):
            views.append(self._reading(array, box, {**spec, "roi_offset": spec["second_roi_offset"]}, errors))
        return number_views(views)

    def _anomaly(self, array, errors, buffs=()):
        result = {key: unknown_field() for key in ANOMALY_FIELDS}
        held_views = [held_card_fields(self._ocr(array, roi, errors), roi) for roi in ([503, 1140, 124, 35], [514, 1146, 112, 29])]
        result["held_cards_count"] = number_views([view[0] for view in held_views])
        result["held_cards_capacity"] = number_views([view[1] for view in held_views])
        result["pointer_condition"] = {"value": None, "status": "unknown", "raw_ocr": []}
        current, result["stance"] = self._single_icon(array, "stance", errors)
        result["hud_present"] = current is not None
        if current is None:
            return result
        entry, box = current["definition"], current["box"]
        competing = next(
            (
                buff
                for buff in buffs
                if overlap_ratio(box, buff["box"]) >= 0.45 and buff["icon_evidence"]["score"] >= current["score"] - self.ambiguity_margin
            ),
            None,
        )
        if competing:
            result["stance"] = {
                **unknown_field("conflicts_with_buff_icon"),
                "icon_evidence": result["stance"]["icon_evidence"],
                "competing_buff": competing["icon_evidence"],
            }
            result["hud_present"] = False
            return result
        upcoming, result["next_stance"] = self._single_icon(array, "stance_preview", errors)
        if entry["key"] == "none" and upcoming is None:
            # 单独的灰色通常姿态图标可能与变暗的普通 Buff 相似。
            # 先核验非凡流派独立的姿态预告，再确认该布局。
            result["stance"] = {**unknown_field("neutral_icon_without_anomaly_context"), "icon_evidence": result["stance"]["icon_evidence"]}
            result["hud_present"] = False
            return result
        if entry.get("reading"):
            stage = self._numeric_views(array, box, entry["reading"], errors)
            result["stance_stage"] = stage if stage["value"] in (1, 2) else {**stage, "value": None, "status": "unknown"}
        result["full_power_charge"] = self._numeric_views(array, box, entry["charge_reading"], errors)
        if upcoming:
            result["transition_after_turns"] = self._numeric_views(array, upcoming["box"], upcoming["definition"]["reading"], errors)
        # 该行只描述指针触发条件或阈值，
        # 不表示实际累计全力值或姿态切换次数。
        roi = [104, 201, 210, 42]
        raw = self._ocr(array, roi, errors)
        texts = [item["text"] for item in raw if item["confidence"] >= 0.70 and re.search(r"変更|变更|變更|累計|累计|累計", item["text"])]
        result["pointer_condition"] = {
            "value": " ".join(texts) or None,
            "status": "observed" if texts else "unknown",
            "roi": roi,
            "raw_ocr": raw,
            "interpretation": "visible_condition_text_only",
        }
        return result

    def _lesson_fields(self, array, errors):
        """读取课程 HUD；画面上既没有「残りターン数」框也没有圆环标签时返回 None。"""
        label = "".join(item["text"] for item in self._ocr(array, LESSON_HUD["label"], errors) if item["confidence"] >= 0.70)
        goal_label = "".join(item["text"] for item in self._ocr(array, LESSON_HUD["goal_label"], errors) if item["confidence"] >= 0.70)
        goal_label = unicodedata.normalize("NFKC", goal_label).upper()
        stage = "perfect" if "PERFECT" in goal_label else "clear" if "CLEAR" in goal_label else None
        if "残りターン数" not in unicodedata.normalize("NFKC", label) and stage is None:
            return None
        fields = {key: unknown_field() for key in STATE_FIELDS}
        roi = LESSON_HUD["turns"]
        fields["turns_remaining"], fields["turns_added"] = turn_fields(self._ocr(array, roi, errors), roi)
        roi = LESSON_HUD["goal"]
        goal = number_field(self._ocr(array, roi, errors), roi, maximum=999999)
        if stage:
            fields["lesson_to_perfect" if stage == "perfect" else "lesson_to_clear"] = goal
        roi = LESSON_HUD["bonus"]
        raw = self._ocr(array, roi, errors)
        # CLEAR 前右上角显示本课属性的当前参数，CLEAR 后才换成「レジェンドボーナス」计数。
        if any(re.search("レジェンド|ボーナス", unicodedata.normalize("NFKC", item["text"])) for item in raw):
            fields["lesson_bonus"] = number_field(centered(raw, (600, 720)), roi, maximum=999999)
        else:
            fields["lesson_bonus"] = unknown_field("not_shown", roi, raw)
        roi = LESSON_HUD["stamina"]
        fields["stamina"] = number_field(centered(self._ocr(array, roi, errors), STAMINA_CENTER_X), roi, maximum=999)
        roi = LESSON_HUD["genki"]
        fields["genki"] = number_field(self._ocr(array, roi, errors), roi, maximum=99999)
        return fields

    def read(self, array):
        errors = []
        rgb = array[..., ::-1]
        lesson = self._lesson_fields(array, errors)
        if lesson:
            fields = lesson
            present = sum(fields[key]["value"] is not None for key in ("turns_remaining", "stamina", "genki")) >= 2
            return self._finish(array, rgb, fields, present, "lesson", errors)
        fields = {key: unknown_field() for key in STATE_FIELDS}
        fields["turns_remaining"] = number_views([number_field(self._ocr(array, roi, errors), roi, maximum=99) for roi in EXAM_HUD["turns"]])
        roi = EXAM_HUD["added"]
        fields["turns_added"] = added_turns_field(self._ocr(array, roi, errors), roi)
        roi = EXAM_HUD["stamina"]
        fields["stamina"] = number_field(centered(self._ocr(array, roi, errors), STAMINA_CENTER_X), roi, maximum=999)
        fields["genki"] = number_views([number_field(self._ocr(array, roi, errors), roi, maximum=99999) for roi in EXAM_HUD["genki"]])
        roi = [106, 44, 115, 81]
        raw = self._ocr(array, roi, errors)
        fields["attribute_multiplier_percent"] = number_field(raw, roi, maximum=99999, percent=True)
        attributes = []
        for item in raw:
            text = unicodedata.normalize("NFKC", item["text"]).casefold()
            for value, tokens in {
                "Vo": ("ボーカル", "声乐", "聲樂", "vocal"),
                "Da": ("ダンス", "舞蹈", "dance"),
                "Vi": ("ビジュアル", "形象", "visual"),
            }.items():
                if item["confidence"] >= 0.70 and any(token in text for token in tokens):
                    attributes.append((value, item["confidence"]))
        if len({value for value, _ in attributes}) == 1:
            value, confidence = max(attributes, key=lambda item: item[1])
            fields["attribute"] = {"value": value, "status": "observed", "roi": roi, "raw_ocr": raw, "confidence": confidence}
        present = sum(fields[key]["value"] is not None for key in ("turns_remaining", "stamina", "genki", "attribute_multiplier_percent")) >= 3
        return self._finish(array, rgb, fields, present, "exam", errors)

    def _finish(self, array, rgb, fields, present, layout, errors):
        state = {
            "schema_version": 1,
            "hud_present": present,
            "layout": layout if present else None,
            "fields": fields,
            "buffs": [],
            "unknown_buffs": [],
            "buffs_complete": False,
            "turn_number": None,
            "card_uses_remaining": None,
            "stance": None,
            "errors": errors,
            "icon_templates_sha256": self.template_sha256,
        }
        if self.template_error:
            state["template_error"] = self.template_error
        if not present:
            state["fields"] = {key: unknown_field("battle_hud_not_confirmed", field["roi"], field["raw_ocr"]) for key, field in fields.items()}
            return state
        score_roi = player_score_roi(rgb) if layout == "exam" else None
        if layout == "lesson":
            state["fields"]["score"] = unknown_field("lesson_shows_goal_counters")
        elif score_roi:
            state["fields"]["score"] = number_field(self._ocr(array, score_roi, errors), score_roi, maximum=999999999)
            state["score_locator"] = "unique_wide_white_player_rank_frame"
        else:
            state["fields"]["score"] = unknown_field("unsupported_or_ambiguous_score_layout")
        state["buffs"], state["unknown_buffs"] = self._buffs(array, rgb, errors, layout)
        state["anomaly"] = self._anomaly(array, errors, state["buffs"])
        state["stance"] = state["anomaly"]["stance"]["value"]
        # 侧边列表可能滚动或省略，未出现的状态不自动补零。
        state["buffs_incomplete_reason"] = "registered_visible_templates_only; hidden, faded and unregistered icons may be unparsed"
        return state


def state_changes(before, after):
    changes = {}
    if not before or not after or not before.get("hud_present") or not after.get("hud_present"):
        return changes
    for key in STATE_FIELDS:
        old, new = before["fields"][key]["value"], after["fields"][key]["value"]
        if old is not None and new is not None and old != new:
            changes[key] = {"before": old, "after": new}
    for key in ANOMALY_FIELDS:
        old = before.get("anomaly", {}).get(key, {}).get("value")
        new = after.get("anomaly", {}).get(key, {}).get("value")
        if old is not None and new is not None and old != new:
            changes["anomaly_" + key] = {"before": old, "after": new}
    old_buffs = {b["key"]: b["value"] for b in before["buffs"] if sum(other["key"] == b["key"] for other in before["buffs"]) == 1}
    for buff in after["buffs"]:
        if sum(other["key"] == buff["key"] for other in after["buffs"]) != 1:
            continue
        old = old_buffs.get(buff["key"])
        if old is not None and buff["value"] is not None and old != buff["value"]:
            changes[buff["key"]] = {"before": old, "after": buff["value"]}
    return changes
