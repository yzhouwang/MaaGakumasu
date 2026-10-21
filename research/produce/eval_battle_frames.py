"""战斗画面识别的离线评估：用人工读数（GHL 实况夹具）核对 tools/decider/ghl_frame.py 的每个字段。

人工读数来自 GHL（gakumas-headless-lab）的 fixtures/maa-live/<run>.json：每个决策的 f 为该截图的 LiveFrame，
mailbox 为采集信箱相对采集仓库根目录的路径。按信箱 decisions.jsonl 中请求的候选框识别手牌，与实况决策者所见一致。

用法（仓库根目录，python 需安装 maafw，例如 beta 的内置 Python）：
    python research/produce/eval_battle_frames.py --fixture <ghl>/fixtures/maa-live/sonnet-2026-10-21.json \\
        --capture-root <采集仓库根目录> --output debug/research/produce/frame_reader

输出 report.json（按字段与画面类型统计）与 mismatches.jsonl（逐帧不一致），控制台打印汇总表。
"""

import sys
import json
import time
import argparse
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]

GHL_BUFFS = ("goodImpressionTurns", "motivation", "halfCostTurns", "doubleCostTurns", "nullifyDebuff")


def displayed_turns(label, layout):
    """人工读数按 LiveFrame 记日程外回合（t=0、tAdd=N）；考试画面此时只显示大号 N、没有「+N」，按画面所示比较。"""
    if layout == "exam" and label["t"] == 0:
        return label.get("tAdd", 0), 0
    return label["t"], label.get("tAdd", 0)


def displayed_score_buffs(buffs):
    """画面把得分提升合并为一个图标：百分比之和与最短的剩余回合（全部永续时为 None）。"""
    if not buffs:
        return []
    turns = [turn for _, turn in buffs if turn is not None]
    return [[sum(percent for percent, _ in buffs), min(turns) if turns else None]]


def label_fields(label, read, layout):
    """逐字段比较人工读数与识别结果，返回 {字段: (人工, 识别)}；Buff 未显示按 0 计。"""
    pairs = {
        "turns": (displayed_turns(label, layout), (read["t"], read.get("tAdd", 0))),
        "stamina": (label["st"], read["st"]),
        "genki": (label["g"], read["g"]),
    }
    for key in ("toClear", "bonus", "score"):
        if key in label:
            pairs["progress"] = ((key, label[key]), (key, read.get(key)))
    for key in GHL_BUFFS:
        pairs[key] = ((label.get("b") or {}).get(key, 0), (read.get("b") or {}).get(key, 0))
    pairs["gib"] = (label.get("gib") or [], read.get("gib") or [])
    pairs["gie"] = (label.get("gie", 0), read.get("gie") or 0)
    pairs["scoreBuffs"] = (displayed_score_buffs(label.get("sb")), read.get("sb") or [])
    hand = [name.rstrip("!") for name in label["hand"]]
    if hand and "?" not in hand:
        pairs["hand"] = (hand, read["hand"])
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", action="append", required=True, help="GHL fixtures/maa-live/<run>.json，可重复")
    parser.add_argument("--capture-root", type=Path, default=ROOT, help="夹具中 mailbox 路径的根目录")
    parser.add_argument("--output", type=Path, default=ROOT / "debug" / "research" / "produce" / "frame_reader")
    parser.add_argument("--limit", type=int, default=0, help="每个夹具最多评估的帧数，0 为全部")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    from ghl_frame import ghl_frame
    from produce.state import BattleStateReader
    from utils.image_io import read_image
    from produce.catalog import SkillCardCatalog
    from produce.analysis import OfflineVision

    resource = ROOT / "assets" / "resource" / "base"
    database = ROOT / "assets" / "data" / "produce_skill_cards.json"
    args.output.mkdir(parents=True, exist_ok=True)
    catalog = SkillCardCatalog(database, ROOT / "assets" / "data" / "skill_card_aliases.json")
    vision = OfflineVision(resource, args.output / "vision_log")
    vision.state_reader = BattleStateReader(database.with_name("battle_buff_templates.json"), vision._read_title, vision.match_buff_templates, resource / "image")

    right, total = defaultdict(Counter), defaultdict(Counter)
    mismatches = []
    started = time.time()
    frames = 0
    for fixture_path in args.fixture:
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        mailbox = args.capture_root / fixture["mailbox"]
        requests = {}
        for line in (mailbox / "decisions.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            requests[record["request"]["id"]] = record["request"]
        labelled = [decision for decision in fixture["decisions"] if decision.get("f") and decision["id"] in requests]
        for decision in labelled[: args.limit or None]:
            request = requests[decision["id"]]
            candidates = [
                {"candidate_id": item["id"], "box": item["box"], "label": item.get("detector_label"), "in_hand_region": True}
                for item in request["candidates"]
                if item["id"].startswith("card_")
            ]
            result = vision.analyze(read_image(mailbox / request["frame"]), catalog, candidates)
            read = ghl_frame(result)
            kind = fixture["battles"][decision["battle"]]["kind"]
            layout = "lesson" if kind == "lesson" else "exam"
            frames += 1
            wrong = {}
            for field, (want, got) in label_fields(decision["f"], read, layout).items():
                total[layout][field] += 1
                if want == got:
                    right[layout][field] += 1
                else:
                    wrong[field] = {"label": want, "read": got}
            if wrong:
                mismatches.append({"run": fixture["run"], "id": decision["id"], "battle": decision["battle"], "frame": request["frame"], "wrong": wrong})
    report = {
        "frames": frames,
        "seconds": round(time.time() - started, 1),
        "accuracy": {layout: {field: [right[layout][field], total[layout][field]] for field in total[layout]} for layout in total},
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    with (args.output / "mismatches.jsonl").open("w", encoding="utf-8") as stream:
        for item in mismatches:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"{frames} 帧，{report['seconds']} 秒")
    for layout, fields in report["accuracy"].items():
        print(f"[{layout}] " + "  ".join(f"{field} {ok}/{n}" for field, (ok, n) in fields.items()))


if __name__ == "__main__":
    main()
