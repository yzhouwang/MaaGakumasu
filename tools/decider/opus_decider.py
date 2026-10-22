"""外部决策者：监视 agent/produce/decider.py 的文件信箱，用 Claude Code CLI（默认 Opus）逐个作答。

仅用于本地开发调试，不进入发布包。战斗请求会先用 agent/produce/analysis.py 的 OfflineVision 识别手牌名称、库效果与 HUD，
再连同截图路径交给模型；模型只返回候选 id。模型调用失败时回答原有逻辑的选择（fallback），Agent 不必等待超时。

用法（仓库根目录，需先 `claude auth login`）：
    python tools/decider/opus_decider.py --context "初 LEGEND，十王星南【ガラクタロード】，理性·好印象"
    python tools/decider/opus_decider.py --dry-run          # 不调用模型，直接回答 fallback，用于联调
    python tools/decider/opus_decider.py --ghl              # GHL 优先：GHL 的决策模型能答的由它答，其余照常问 LLM

--ghl：GHL（gakumas-headless-lab）的实况桥（scripts/maa/live.ts，配合 ghl_readings.py --follow 与决策模型服务）
把它对每个请求的选择写入信箱的 ghl/answers.jsonl。本决策者对范围内的请求先等 GHL 的选择（--ghl-wait 秒），
采用决策模型在本请求候选中的最优项；GHL 跳过、超时或没有可执行的候选时照常问 LLM（--ghl-only 时回答 fallback）。
课程中默认不采用饮料：决策模型的课程目标把饮料当作免费（--ghl-lesson-drinks 允许）。
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


# GHL 的任务 → --ghl-scope 的名称。
GHL_SCOPES = {
    "exam": ("ghl/battle-exam",),
    "lesson": ("ghl/battle-lesson",),
    "week": ("ghl/week",),
    "offer": ("ghl/card-offer", "ghl/drink-offer", "ghl/midterm-item", "ghl/start-bonus"),
}


def read_ghl_answers(path: Path) -> dict:
    """GHL 实况桥的答复（每请求一行，后写的一行取代前面的）；只读以换行结尾的行，正在写入的行下次再读。"""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return {}
    answers = {}
    for line in text.split("\n")[:-1]:
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        answers[answer.get("id")] = answer
    return answers


def ghl_choice(answer: dict, request: dict, scope: set, policy: str = "decider", lesson_drinks: bool = False):
    """
    GHL 对请求的选择：范围内、且有本请求的候选可执行时返回 {"choice", "reason"}，否则返回 (None, 原因)。

    decider 策略取决策模型在可执行候选中概率最高的一项；reference 策略取 GHL 的参照策略（战斗 B4、其余 B1v2）。
    课程中默认不采用饮料。职业课（class_option）的选项在 GHL 中是「上课:属性」，候选为属性。
    """
    task = answer.get("task")
    if answer.get("skip"):
        return None, f"GHL skipped: {answer['skip']}"
    if not any(task in GHL_SCOPES[name] for name in scope):
        return None, f"GHL task {task} not in scope"
    ids = {item["id"] for item in request["candidates"]}

    def live_of(key):
        live = (answer.get("optionLive") or {}).get(key)
        if request["kind"] == "class_option":
            return live[3:] if live and live.startswith("上课:") else None
        return live

    def allowed(live):
        return live in ids and (lesson_drinks or task != "ghl/battle-lesson" or not live.startswith("drink_"))

    texts = {option[0]: option[1] for option in answer.get("options") or []}
    if policy == "reference":
        ref = answer.get("ref") or {}
        live = live_of(ref.get("key"))
        if not allowed(live):
            return None, f"GHL reference {ref.get('policy')} chose no candidate here ({live})"
        return {"choice": live, "reason": f"GHL {ref.get('policy')}: {texts.get(ref.get('key'), '')}"[:300]}, None
    decider = answer.get("decider") or {}
    if decider.get("error") or not decider.get("probabilities"):
        return None, f"GHL decider gave no answer ({decider.get('error', 'no probabilities')})"
    scored = [(p, key, live_of(key)) for key, p in decider["probabilities"].items()]
    scored = [item for item in scored if allowed(item[2])]
    if not scored:
        return None, "GHL decider: no candidate it may choose"
    p, key, live = max(scored)
    top = decider.get("key")
    note = "" if key == top else f"; its top choice ({texts.get(top, top)}) is not offered or not allowed here"
    return {"choice": live, "reason": f"GHL decider p={p:.2f}: {texts.get(key, '')}{note}"[:300]}, None


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
        self.notes_path = self.mailbox / "notes.json"
        try:
            # 备注写盘保存，决策者重启后仍保留本轮培育的牌组与饮料记录
            self.notes = json.loads(self.notes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.notes = []
        self.reader = None
        self.ghl_scope = set(args.ghl_scope.split(",")) if getattr(args, "ghl", False) else set()
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
            lines.append("Your notes so far: " + " | ".join(self.notes[-20:]))
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

    def ghl_answer(self, request: dict):
        """等 GHL 实况桥对该请求的选择（至多 --ghl-wait 秒），返回 (答复, 未采用的原因)。"""
        if request["kind"] not in ("battle", "week", "reward", "class_option"):
            return None, f"kind {request['kind']}: GHL does not decide it"
        deadline = time.time() + self.args.ghl_wait
        while True:
            answer = read_ghl_answers(self.mailbox / "ghl" / "answers.jsonl").get(request["id"])
            if answer:
                return ghl_choice(answer, request, self.ghl_scope, self.args.ghl_policy, self.args.ghl_lesson_drinks)
            if time.time() >= deadline:
                return None, f"no GHL answer within {self.args.ghl_wait:.0f} s"
            time.sleep(0.2)

    def handle(self, path: Path):
        request = json.loads(path.read_text(encoding="utf-8"))
        ids = [item["id"] for item in request["candidates"]]
        reading = {}
        ghl_note = ""
        if self.args.ghl:
            started = time.time()
            answer, why = self.ghl_answer(request)
            if answer:
                response = {"id": request["id"], "choice": answer["choice"], "reason": answer["reason"], "source": "ghl", "meta": {"seconds": round(time.time() - started, 1)}}
                self.finish(path, request, {}, "", response)
                return
            ghl_note = why
            if self.args.ghl_only:
                response = {"id": request["id"], "choice": request.get("fallback"), "reason": f"GHL only; {why}", "source": "fallback"}
                self.finish(path, request, {}, "", response)
                return
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
                        write_atomic(self.notes_path, self.notes)
                else:
                    response["reason"] = f"model answered unknown id {answer.get('choice')!r}"
                    response["meta"] = answer.get("_meta")
            except Exception as exc:
                response["reason"] = f"decider error: {exc}"
        if ghl_note:
            response["ghl"] = ghl_note
        self.finish(path, request, reading, prompt, response)

    def finish(self, path: Path, request: dict, reading: dict, prompt: str, response: dict):
        """写入答复与决策日志。"""
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
    parser.add_argument("--ghl", action="store_true", help="GHL 优先：范围内的请求采用 GHL 实况桥的选择（ghl/answers.jsonl）")
    parser.add_argument("--ghl-scope", default="exam,lesson,week,offer", help="GHL 决定的范围：exam、lesson、week（含职业课）、offer，逗号分隔")
    parser.add_argument("--ghl-policy", choices=("decider", "reference"), default="decider", help="decider：决策模型；reference：GHL 的参照策略（B4/B1v2）")
    parser.add_argument("--ghl-wait", type=float, default=20, help="等 GHL 选择的秒数，超时则照常问 LLM")
    parser.add_argument("--ghl-lesson-drinks", action="store_true", help="允许课程中采用 GHL 选的饮料")
    parser.add_argument("--ghl-only", action="store_true", help="不问 LLM：GHL 不答的请求回答 fallback（联调用）")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    unknown = set(args.ghl_scope.split(",")) - set(GHL_SCOPES)
    if unknown:
        parser.error(f"unknown --ghl-scope: {', '.join(sorted(unknown))}")
    Decider(args).run()


if __name__ == "__main__":
    main()
