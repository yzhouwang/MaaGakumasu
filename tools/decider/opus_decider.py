"""外部决策者：监视 agent/produce/decider.py 的文件信箱，用 Claude Code CLI（默认 Opus）逐个作答。

仅用于本地开发调试，不进入发布包。战斗请求会先用 agent/produce/analysis.py 的 OfflineVision 识别手牌名称、库效果与 HUD，
再连同截图路径交给模型；模型只返回候选 id。模型调用失败时回答原有逻辑的选择（fallback），Agent 不必等待超时。

用法（仓库根目录，需先 `claude auth login`）：
    python tools/decider/opus_decider.py --context "初 LEGEND，十王星南【ガラクタロード】，理性·好印象"
    python tools/decider/opus_decider.py --dry-run          # 不调用模型，直接回答 fallback，用于联调
"""

import os
import sys
import json
import time
import uuid
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string"},
        "reason": {"type": "string"},
        "notes": {"type": "string"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
    },
    "required": ["choice", "reason"],
}


def write_atomic(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


class BattleReader:
    """复用采集分支的离线识别：手牌名称/库效果与战斗 HUD。"""

    def __init__(self, log_dir: Path):
        from produce.state import BattleStateReader
        from produce.catalog import SkillCardCatalog
        from produce.analysis import OfflineVision

        resource = ROOT / "assets" / "resource" / "base"
        database = ROOT / "assets" / "data" / "produce_skill_cards.json"
        self.catalog = SkillCardCatalog(database, ROOT / "assets" / "data" / "skill_card_aliases.json")
        self.vision = OfflineVision(resource, log_dir)
        templates = database.with_name("battle_buff_templates.json")
        self.vision.state_reader = BattleStateReader(templates, self.vision._read_title, self.vision.match_buff_templates, resource / "image")

    def read(self, frame: Path, candidates: list) -> dict:
        from utils.image_io import read_image
        from produce.console import card_name, state_lines, catalog_lines

        analysis_candidates = [
            {"candidate_id": item["id"], "box": item["box"], "label": item.get("detector_label"), "in_hand_region": True}
            for item in candidates
            if item["id"].startswith("card_")
        ]
        result = self.vision.analyze(read_image(frame), self.catalog, analysis_candidates)
        cards = {}
        for card in result.get("cards", []):
            identity = card.get("identity") or {}
            cards[card["candidate_id"]] = {
                "name": card_name(identity) if identity.get("status") else None,
                "effect": catalog_lines(identity) if identity.get("database_card_id") else [],
                "identity_status": identity.get("status"),
            }
        state = result.get("battle_state") or {}
        return {"cards": cards, "hud": state_lines(state) if state.get("hud_present") else [], "battle_state": state}


class Decider:
    def __init__(self, args):
        self.args = args
        self.mailbox = (ROOT / args.mailbox).resolve()
        self.sessions = {}
        self.notes = []
        self.reader = None
        self.system_prompt = (Path(__file__).with_name("system_prompt.md")).read_text(encoding="utf-8")
        for extra in args.extra_prompt or []:
            self.system_prompt += "\n\n" + Path(extra).read_text(encoding="utf-8")
        if args.context:
            self.system_prompt += f"\n\nThis produce: {args.context}"

    def pending(self) -> list:
        requests = sorted((self.mailbox / "requests").glob("*.json"))
        return [path for path in requests if not (self.mailbox / "responses" / path.name).exists()]

    def prompt_for(self, request: dict, reading: dict) -> str:
        lines = [f"Decision kind: {request['kind']}", f"Screenshot: {request['frame']}"]
        for name, frame in (request.get("extra_frames") or {}).items():
            lines.append(f"Detail screenshot {name}: {frame}")
        if request.get("context"):
            lines.append("Context: " + json.dumps(request["context"], ensure_ascii=False))
        if request.get("state"):
            lines.append("Screen readings: " + json.dumps(request["state"], ensure_ascii=False))
        if reading.get("hud"):
            lines.append("Battle HUD (OCR): " + "; ".join(reading["hud"]))
        lines.append("Candidates:")
        for item in request["candidates"]:
            extra = reading.get("cards", {}).get(item["id"], {})
            description = f"- {item['id']}: {item.get('label', '')}"
            if extra.get("name"):
                description += f" | {extra['name']}"
            if extra.get("effect"):
                description += " | " + " / ".join(extra["effect"])
            for key in ("sp", "detector_label"):
                if key in item and item[key] is not None:
                    description += f" | {key}={item[key]}"
            lines.append(description)
        lines.append(f"Fallback (old rule-based choice): {request.get('fallback')}")
        if self.notes:
            lines.append("Your notes so far: " + " | ".join(self.notes[-12:]))
        return "\n".join(lines)

    def call_model(self, key: str, prompt: str) -> dict:
        session = self.sessions.get(key)
        command = ["claude", "-p", prompt, "--model", self.args.model, "--output-format", "json", "--json-schema", json.dumps(ANSWER_SCHEMA)]
        command += ["--tools", "Read", "--strict-mcp-config", "--setting-sources", ""]
        if self.args.effort:
            command += ["--effort", self.args.effort]
        if session and session["turns"] < self.args.session_turns:
            command += ["--resume", session["id"]]
            session["turns"] += 1
        else:
            session = {"id": str(uuid.uuid4()), "turns": 1}
            self.sessions[key] = session
            command += ["--session-id", session["id"], "--system-prompt", self.system_prompt]
        started = time.time()
        completed = subprocess.run(command, cwd=self.mailbox, capture_output=True, text=True, timeout=self.args.call_timeout)
        output = json.loads(completed.stdout or "{}")
        if output.get("is_error") or completed.returncode != 0:
            raise RuntimeError(f"claude failed ({completed.returncode}): {str(output.get('result') or completed.stderr)[:300]}")
        answer = output.get("structured_output")
        if not isinstance(answer, dict):
            text = str(output.get("result", ""))
            answer = json.loads(text[text.index("{") : text.rindex("}") + 1])
        answer["_meta"] = {
            "seconds": round(time.time() - started, 1),
            "cost_usd": output.get("total_cost_usd"),
            "models": list((output.get("modelUsage") or {}).keys()),
            "session": session["id"],
        }
        return answer

    def handle(self, path: Path):
        request = json.loads(path.read_text(encoding="utf-8"))
        ids = [item["id"] for item in request["candidates"]]
        reading = {}
        if request["kind"] == "battle" and not self.args.no_vision:
            try:
                if self.reader is None:
                    self.reader = BattleReader(self.mailbox / "vision_log")
                reading = self.reader.read(self.mailbox / request["frame"], request["candidates"])
            except Exception as exc:
                reading = {"error": f"vision failed: {exc}"}
        prompt = self.prompt_for(request, reading)
        response = {"id": request["id"], "choice": request.get("fallback"), "reason": "", "source": "fallback"}
        if self.args.dry_run:
            response["reason"] = "dry run"
        else:
            context = request.get("context", {})
            key = context.get("session_key") or (context.get("battle_id") if request["kind"] == "battle" else None) or "campaign"
            try:
                answer = self.call_model(key, prompt)
                if answer.get("choice") in ids:
                    response.update(choice=answer["choice"], reason=answer.get("reason", ""), source="model", meta=answer["_meta"])
                    for axis in ("x", "y"):
                        if isinstance(answer.get(axis), int):
                            response[axis] = answer[axis]
                    if answer.get("notes"):
                        self.notes.append(answer["notes"])
                else:
                    response["reason"] = f"model answered unknown id {answer.get('choice')!r}"
                    response["meta"] = answer.get("_meta")
            except Exception as exc:
                response["reason"] = f"decider error: {exc}"
        write_atomic(self.mailbox / "responses" / path.name, response)
        with open(self.mailbox / "decisions.jsonl", "a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "request": request,
                        "reading": {k: v for k, v in reading.items() if k != "battle_state"},
                        "prompt": prompt,
                        "response": response,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        meta = response.get("meta") or {}
        print(
            f"[{request['id']}] {request['kind']} -> {response['choice']} ({response['source']}, {meta.get('seconds', '-')}s) {response['reason'][:100]}",
            flush=True,
        )

    def run(self):
        (self.mailbox / "requests").mkdir(parents=True, exist_ok=True)
        (self.mailbox / "responses").mkdir(parents=True, exist_ok=True)
        print(f"监视信箱 {self.mailbox}（模型 {self.args.model}{'，dry-run' if self.args.dry_run else ''}）", flush=True)
        while True:
            for path in self.pending():
                self.handle(path)
            if self.args.once:
                return
            time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mailbox", default="debug/decider/mailbox")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--effort", default=None, help="传给 claude --effort，例如 high")
    parser.add_argument("--context", default="", help="本轮培育的简要说明（剧本、偶像、流派）")
    parser.add_argument("--extra-prompt", action="append", help="追加到系统提示词的文件，可重复")
    parser.add_argument("--session-turns", type=int, default=40, help="同一会话最多连续作答次数，超过后开新会话")
    parser.add_argument("--call-timeout", type=int, default=420)
    parser.add_argument("--no-vision", action="store_true", help="跳过手牌/HUD 离线识别")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="处理当前积压的请求后退出")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    Decider(args).run()


if __name__ == "__main__":
    main()
