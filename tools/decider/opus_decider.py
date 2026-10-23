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

Agent 只等 config/decider.json 的 timeout_s 秒（--agent-timeout 可覆盖）。Agent 已放弃的请求（consumed/ 中有超时或停止的记录，
或已超过该时间）不再作答，只记一行日志：否则决策者中断或重启后，积压的旧请求各要等 GHL、问模型，正在等待的请求也会超时。
GHL 的等待从请求提出时算起，模型调用不超过 Agent 剩余的等待时间（留出余量），答复不会在 Agent 放弃之后才到。

模型的会话与备注不知道 GHL 和 fallback 作出的决策：这些决策按顺序编号记录（周、奖励与职业课等各一行，战斗每场一行：
行动数与使用的饮料，有新的行动时以新的编号移到末尾），问模型时附上该会话尚未见过的部分。每个会话键记下它见过的编号，
作答有效后更新；另开新会话（首次调用、满 --session-turns 次或上次调用失败后）时没有对话记录，附上最近的全部决策。
决策者重启后所有会话都是新会话，决策记录从 decisions.jsonl 恢复。

相谈（商店）与特别指导是逐步点击的交互画面，GHL 不逐步作答：实况桥在每次访问的第一步按其规则写下计划
（ghl/plans.jsonl，按 session_key），本决策者把计划附在这次访问各步的提示中，模型只负责执行（第一步等计划至多
--ghl-wait 秒）。GHL 不作计划（状态不可靠等）时照常由模型决定。
"""

import os
import re
import sys
import json
import time
import uuid
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
CONFIG_PATH = ROOT / "config" / "decider.json"

# 答复至少在 Agent 放弃前这么多秒写入；剩余时间不足 MIN_MODEL_S 秒时不再问模型，直接回答 fallback。
AGENT_MARGIN_S = 5
MIN_MODEL_S = 10
# 提示中的决策摘要（Decider.handoff_text）最多列出的行数与每行的字数，以及内存中保留的决策记录条数。
HANDOFF_LINES = 30
HANDOFF_LINE_CHARS = 160
HANDOFF_KEEP = 500
HANDOFF_HEADER = (
    "Decided without you and not yet in this conversation, oldest first (GHL: GHL's decider model; fallback: the old rule-based choice):"
)
# GHL 答复的理由为「GHL <策略>: <选项说明>」（见 ghl_choice），摘要只取选项说明；战斗中饮料的选项说明为「use drink <名称>」。
GHL_REASON_PREFIX = re.compile(r"^GHL [^:]*: ")
GHL_DRINK = re.compile(r"use drink ([^;]+)")

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


def read_ghl_plans(path: Path) -> dict:
    """GHL 实况桥为相谈、特别指导各次访问写下的计划（按 session_key，后写的一行取代前面的）；只读以换行结尾的行。"""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return {}
    plans = {}
    for line in text.split("\n")[:-1]:
        try:
            plan = json.loads(line)
        except ValueError:
            continue
        if isinstance(plan, dict) and plan.get("session_key"):
            plans[plan["session_key"]] = plan
    return plans


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


def agent_timeout(path: Path = CONFIG_PATH) -> float:
    """Agent 等待答复的秒数：config/decider.json 的 timeout_s；文件或字段缺失时与 agent/produce/decider.py 一致取 600。"""
    try:
        return float(json.loads(path.read_text(encoding="utf-8-sig")).get("timeout_s", 600))
    except (OSError, ValueError, TypeError, AttributeError):
        return 600.0


def created_at(request: dict, path: Path) -> float:
    """请求提出的时间（epoch 秒）；没有 created_at 的旧请求取文件的修改时间。"""
    try:
        return float(request.get("created_at") or path.stat().st_mtime)
    except (OSError, TypeError, ValueError):
        return time.time()


def clip(text: str, limit: int) -> str:
    """压成一行，超过 limit 个字符时截断。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def handoff_count(entry: dict) -> int:
    """一条决策记录包含的决策数：战斗为其行动数，其余为 1。"""
    return sum(entry["sources"].values()) if "battle" in entry else 1


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
        self.agent_timeout = agent_timeout() if args.agent_timeout is None else args.agent_timeout
        # Agent 已放弃、不再作答的请求（文件名），每个只记一次日志
        self.skipped = set()
        # GHL 与 fallback 的决策记录（见 note_handoff），按编号排序；handoff_seen 为各会话键已见到的编号。
        # 超出 HANDOFF_KEEP 条时丢弃最早的记录，handoff_dropped 与 handoff_floor 为丢弃的决策数与最后丢弃的编号。
        self.handoff, self.handoff_seq, self.handoff_seen = [], 0, {}
        self.handoff_dropped, self.handoff_floor = 0, 0
        self.load_handoff()

    def pending(self) -> list:
        """未答复、Agent 仍在等待的请求（按 id 排序）。"""
        requests = sorted((self.mailbox / "requests").glob("*.json"))
        return [
            path
            for path in requests
            if path.name not in self.skipped and not (self.mailbox / "responses" / path.name).exists() and not self.skip_stale(path)
        ]

    def skip_stale(self, path: Path, request: dict | None = None) -> bool:
        """
        Agent 已不再等待的请求不作答，记一行日志后返回 True：consumed/ 中已有记录（超时或停止），或已超过 Agent 的等待时间。

        否则决策者中断或重启后，积压的旧请求各要等 GHL、问模型，Agent 正在等待的请求也会超时。跳过的请求不写入
        decisions.jsonl：其中每行对应一个写出的答复，读取方把 response.choice 当作决策者的答复；Agent 实际的操作见 consumed/。
        """
        if request is None:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
        consumed = self.mailbox / "consumed" / path.name
        if consumed.exists():
            try:
                source = json.loads(consumed.read_text(encoding="utf-8")).get("source")
            except (OSError, ValueError, AttributeError):
                source = None
            why = f"Agent 已放弃等待（consumed: {source or '?'}）"
        else:
            age = time.time() - created_at(request, path)
            if age <= self.agent_timeout:
                return False
            why = f"Agent 只等 {self.agent_timeout:.0f} 秒，请求已提出 {age:.0f} 秒"
        self.skipped.add(path.name)
        print(f"[{path.stem}] {request.get('kind', '?')} 跳过：{why}", flush=True)
        return True

    def prompt_for(self, request: dict, reading: dict, mark: int | None = None, plan: dict | None = None) -> str:
        """提示正文；mark 为该会话已见到的决策编号（见 handoff_text），plan 为 GHL 对本次相谈或特别指导的计划。"""
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
        handoff = self.handoff_text(mark)
        if handoff:
            lines.append(handoff)
        if plan:
            lines.append(
                "GHL's plan for this visit (GHL tracks the deck, drinks and P from the screens and decides this visit; you carry it out):\n"
                + str(plan.get("text", "")).strip()
                + "\nCarry out the plan's steps in order. Do not buy, upgrade, delete or customize anything the plan does not name."
                " If a step cannot be done on screen (the card or item is not there, P is short), skip it and say so in your reason."
                " When the plan is done, leave the screen."
            )
        return "\n".join(lines)

    def note_handoff(self, request: dict, response: dict):
        """
        记下模型以外（GHL、fallback）的决策：以新的编号排在末尾，问模型时列出该会话尚未见过的部分（见 handoff_text）。

        战斗每场一条记录，有新的行动时更新并以新的编号移到末尾，见过旧版本的会话会再次看到整行。模型自己的决策不记录。
        """
        source = response.get("source")
        if source == "model":
            return
        who = "GHL" if source == "ghl" else str(source)
        choice = response.get("choice")
        reason = response.get("reason") or ""
        if request.get("kind") == "battle":
            # 各来源的行动数与使用的饮料（GHL 的选项说明中有饮料名称）
            battle = (request.get("context") or {}).get("battle_id") or "?"
            entry = next((item for item in reversed(self.handoff) if item.get("battle") == battle), None)
            if entry:
                self.handoff.remove(entry)
            else:
                entry = {"battle": battle, "sources": {}, "drinks": []}
            entry["sources"][who] = entry["sources"].get(who, 0) + 1
            if str(choice).startswith("drink_"):
                drink = GHL_DRINK.search(reason) if source == "ghl" else None
                entry["drinks"].append(drink.group(1).strip() if drink else choice)
        else:
            if source == "ghl":
                detail = GHL_REASON_PREFIX.sub("", reason)
            else:
                label = next((item.get("label", "") for item in request.get("candidates") or [] if item.get("id") == choice), "")
                detail = "; ".join(text for text in (label, reason) if text)
            line = f"{request.get('kind')}: {choice or 'none'} by {who}"
            entry = {"line": f"{line} ({detail})" if detail else line}
        self.handoff_seq += 1
        entry["seq"] = self.handoff_seq
        self.handoff.append(entry)
        if len(self.handoff) > HANDOFF_KEEP:
            dropped = self.handoff.pop(0)
            self.handoff_dropped += handoff_count(dropped)
            self.handoff_floor = dropped["seq"]

    def handoff_text(self, mark: int | None = None) -> str:
        """
        提示中的决策摘要：编号大于 mark 的决策记录（mark 为 None 时为全部，给没有对话记录的新会话），由旧到新，
        至多列出最近的 HANDOFF_LINES 条，其余只记省略的决策数；没有时为空。周、奖励与职业课等各一行，战斗每场一行。
        """
        mark = mark or 0
        unseen = [entry for entry in self.handoff if entry["seq"] > mark]
        if not unseen:
            return ""
        # mark 早于内存中最早的记录时，已丢弃的决策都算作省略（可能多算 mark 之前丢弃的部分）
        omitted = sum(handoff_count(entry) for entry in unseen[:-HANDOFF_LINES]) + (self.handoff_dropped if mark < self.handoff_floor else 0)
        lines = [HANDOFF_HEADER]
        if omitted:
            lines.append(f"- ({omitted} earlier decisions omitted)")
        for entry in unseen[-HANDOFF_LINES:]:
            if "battle" in entry:
                actions = ", ".join(f"{who} {count}" for who, count in entry["sources"].items())
                drinks = ("drinks used: " + ", ".join(entry["drinks"])) if entry["drinks"] else "no drinks used"
                count = handoff_count(entry)
                line = f"battle {entry['battle']}: {count} action{'' if count == 1 else 's'} ({actions}); {drinks}"
            else:
                line = entry["line"]
            lines.append("- " + clip(line, HANDOFF_LINE_CHARS))
        return "\n".join(lines)

    def load_handoff(self):
        """从决策日志恢复决策记录（重启后所有会话都是新会话，各自附上最近的全部决策）；只读以换行结尾的行。"""
        try:
            text = (self.mailbox / "decisions.jsonl").read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return
        for line in text.split("\n")[:-1]:
            try:
                row = json.loads(line)
                self.note_handoff(row["request"], row["response"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue

    def resumes(self, key: str) -> bool:
        """下次调用是否续接 key 的会话；否则另开新会话（首次调用、满 --session-turns 次，或上次调用失败后已丢弃）。"""
        session = self.sessions.get(key)
        return bool(session) and session["turns"] < self.args.session_turns

    def call_model(self, key: str, prompt: str, timeout: float) -> dict:
        session = self.sessions.get(key)
        command = ["claude", "-p", prompt, "--model", self.args.model, "--output-format", "json", "--json-schema", json.dumps(ANSWER_SCHEMA)]
        command += ["--tools", "Read", "--strict-mcp-config", "--setting-sources", ""]
        if self.args.effort:
            command += ["--effort", self.args.effort]
        if self.resumes(key):
            command += ["--resume", session["id"]]
            session["turns"] += 1
        else:
            session = {"id": str(uuid.uuid4()), "turns": 1}
            self.sessions[key] = session
            command += ["--session-id", session["id"], "--system-prompt", self.system_prompt]
        started = time.time()
        try:
            try:
                completed = subprocess.run(command, cwd=self.mailbox, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                # 超时异常的说明含整条命令（提示词与系统提示），答复与日志中只记时限
                raise RuntimeError(f"claude did not answer within {timeout:.0f} s") from None
            output = json.loads(completed.stdout or "{}")
            if output.get("is_error") or completed.returncode != 0:
                raise RuntimeError(f"claude failed ({completed.returncode}): {str(output.get('result') or completed.stderr)[:300]}")
        except Exception:
            # 失败的调用可能没有建立会话（例如首次调用被中止），之后的续接会一直失败：下次另开新会话，附上备注与最近的全部决策
            self.sessions.pop(key, None)
            raise
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

    def ghl_plan(self, request: dict, until: float):
        """
        GHL 对本次相谈或特别指导的计划（按请求的 session_key）；没有或 GHL 不作计划（skip）时为 None。

        计划在访问的第一步（step 0）由实况桥写下：第一步等到 until（请求提出后 --ghl-wait 秒，不晚于 Agent 放弃），之后各步只读一次。
        之后各步只沿用第一步已采用的计划（第一步的答复记有 ghl_plan）：第一步没有等到计划时模型已自行开始，晚到的计划
        是按开始前的状态算的，不再接手。
        """
        context = request.get("context") or {}
        key = context.get("session_key")
        if request["kind"] not in ("consult", "customize") or not key:
            return None
        while True:
            plan = read_ghl_plans(self.mailbox / "ghl" / "plans.jsonl").get(key)
            if plan or context.get("step") != 0 or time.time() >= until:
                break
            time.sleep(0.2)
        if not plan or plan.get("skip"):
            return None
        if context.get("step") != 0:
            try:
                first = json.loads((self.mailbox / "responses" / f"{plan.get('id')}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                first = {}
            if first.get("ghl_plan") != key:
                return None
        return plan

    def ghl_answer(self, request: dict, until: float):
        """等 GHL 实况桥对该请求的选择，至多等到 until（请求提出后 --ghl-wait 秒，且不晚于 Agent 放弃），返回 (答复, 未采用的原因)。"""
        if request["kind"] not in ("battle", "week", "reward", "class_option"):
            return None, f"kind {request['kind']}: GHL does not decide it"
        while True:
            answer = read_ghl_answers(self.mailbox / "ghl" / "answers.jsonl").get(request["id"])
            if answer:
                return ghl_choice(answer, request, self.ghl_scope, self.args.ghl_policy, self.args.ghl_lesson_drinks)
            if time.time() >= until:
                return None, f"no GHL answer within {self.args.ghl_wait:.0f} s of the request"
            time.sleep(0.2)

    def handle(self, path: Path):
        request = json.loads(path.read_text(encoding="utf-8"))
        # 处理前一个请求期间，本请求可能已被 Agent 放弃
        if self.skip_stale(path, request):
            return
        ids = [item["id"] for item in request["candidates"]]
        created = created_at(request, path)
        # Agent 等到请求提出后 agent_timeout 秒为止：GHL 的等待与模型调用都在此之前结束，并留出余量让 Agent 读到答复
        deadline = created + self.agent_timeout - AGENT_MARGIN_S
        reading = {}
        ghl_note = ""
        if self.args.ghl:
            started = time.time()
            answer, why = self.ghl_answer(request, min(created + self.args.ghl_wait, deadline))
            if answer:
                meta = {"seconds": round(time.time() - started, 1)}
                response = {"id": request["id"], "choice": answer["choice"], "reason": answer["reason"], "source": "ghl", "meta": meta}
                self.finish(path, request, {}, "", response)
                return
            ghl_note = why
            if self.args.ghl_only:
                response = {"id": request["id"], "choice": request.get("fallback"), "reason": f"GHL only; {why}", "source": "fallback"}
                self.finish(path, request, {}, "", response)
                return
        plan = self.ghl_plan(request, min(created + self.args.ghl_wait, deadline)) if self.args.ghl else None
        if request["kind"] == "battle" and not self.args.no_vision:
            try:
                if self.reader is None:
                    self.reader = BattleReader(self.mailbox / "vision_log")
                reading = self.reader.read(self.mailbox / request["frame"], request["candidates"])
            except Exception as exc:
                reading = {"error": f"vision failed: {exc}"}
        context = request.get("context") or {}
        key = context.get("session_key") or (context.get("battle_id") if request["kind"] == "battle" else None) or "campaign"
        # 续接的会话只附上它尚未见过的决策；另开的新会话没有对话记录，附上最近的全部决策
        prompt = self.prompt_for(request, reading, self.handoff_seen.get(key) if self.resumes(key) else None, plan)
        seen = self.handoff_seq
        response = {"id": request["id"], "choice": request.get("fallback"), "reason": "", "source": "fallback"}
        left = deadline - time.time()
        if self.args.dry_run:
            response["reason"] = "dry run"
        elif left < MIN_MODEL_S:
            response["reason"] = f"only {max(left, 0):.0f} s left before the agent stops waiting; the model was not asked"
        else:
            try:
                answer = self.call_model(key, prompt, min(self.args.call_timeout, left))
                if answer.get("choice") in ids:
                    response.update(choice=answer["choice"], reason=answer.get("reason", ""), source="model", meta=answer["_meta"])
                    # 作答有效：该会话已见到编号 seen 及之前的决策
                    self.handoff_seen[key] = seen
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
        if plan:
            response["ghl_plan"] = plan["session_key"]
        self.finish(path, request, reading, prompt, response)

    def finish(self, path: Path, request: dict, reading: dict, prompt: str, response: dict):
        """写入答复与决策日志，并记入交给模型的决策摘要。"""
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
        self.note_handoff(request, response)
        meta = response.get("meta") or {}
        print(
            f"[{request['id']}] {request['kind']} -> {response['choice']} ({response['source']}, {meta.get('seconds', '-')}s) {response['reason'][:100]}",
            flush=True,
        )

    def run(self):
        (self.mailbox / "requests").mkdir(parents=True, exist_ok=True)
        (self.mailbox / "responses").mkdir(parents=True, exist_ok=True)
        print(
            f"监视信箱 {self.mailbox}（模型 {self.args.model}，Agent 等待 {self.agent_timeout:.0f} 秒{'，dry-run' if self.args.dry_run else ''}）",
            flush=True,
        )
        while True:
            for path in self.pending():
                self.handle(path)
            if self.args.once:
                return
            time.sleep(0.3)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mailbox", default="debug/decider/mailbox")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--effort", default=None, help="传给 claude --effort，例如 high")
    parser.add_argument("--context", default="", help="本轮培育的简要说明（剧本、偶像、流派）")
    parser.add_argument("--extra-prompt", action="append", help="追加到系统提示词的文件，可重复")
    parser.add_argument("--session-turns", type=int, default=40, help="同一会话最多连续作答次数，超过后开新会话")
    parser.add_argument("--call-timeout", type=int, default=420, help="单次模型调用的秒数上限；另不超过 Agent 剩余的等待时间")
    parser.add_argument("--agent-timeout", type=float, default=None, help="Agent 等待答复的秒数，默认取 config/decider.json 的 timeout_s（缺省 600）")
    parser.add_argument("--no-vision", action="store_true", help="跳过手牌/HUD 离线识别")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="处理当前积压的请求后退出")
    parser.add_argument("--ghl", action="store_true", help="GHL 优先：范围内的请求采用 GHL 实况桥的选择（ghl/answers.jsonl）")
    parser.add_argument("--ghl-scope", default="exam,lesson,week,offer", help="GHL 决定的范围：exam、lesson、week（含职业课）、offer，逗号分隔")
    parser.add_argument("--ghl-policy", choices=("decider", "reference"), default="decider", help="decider：决策模型；reference：GHL 的参照策略（B4/B1v2）")
    parser.add_argument("--ghl-wait", type=float, default=20, help="等 GHL 选择的秒数，超时则照常问 LLM")
    parser.add_argument("--ghl-lesson-drinks", action="store_true", help="允许课程中采用 GHL 选的饮料")
    parser.add_argument("--ghl-only", action="store_true", help="不问 LLM：GHL 不答的请求回答 fallback（联调用）")
    args = parser.parse_args(argv)
    unknown = set(args.ghl_scope.split(",")) - set(GHL_SCOPES)
    if unknown:
        parser.error(f"unknown --ghl-scope: {', '.join(sorted(unknown))}")
    return args


def main():
    args = parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    Decider(args).run()


if __name__ == "__main__":
    main()
