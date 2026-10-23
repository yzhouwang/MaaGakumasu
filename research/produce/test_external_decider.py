"""外部决策接口回归：文件信箱的请求/答复/实际采用记录、坐标透传与超时，交互式画面循环的安全区域、结束与停止处理，
以及外部决策者（tools/decider/opus_decider.py）的 GHL 优先模式、Agent 已放弃请求的跳过与等待时限、交给模型的决策摘要。

使用合成截图与替身决策者，不连接设备、不调用模型（claude CLI 与模型调用均由替身代替）；临时信箱写入 debug/research/produce/tests/。
需要安装 maafw（截图编码使用 MaaFramework）。
"""

import io
import os
import re
import sys
import json
import time
import tempfile
import unittest
import threading
import contextlib
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
import custom.action.produce as produce_actions
from opus_decider import HANDOFF_KEEP, HANDOFF_LINES, HANDOFF_LINE_CHARS, Decider, ghl_choice, parse_args, read_ghl_plans, read_ghl_answers
from produce.decider import ExternalDecider, _next_session, run_interactive

IMAGE = np.zeros((1280, 720, 3), dtype=np.uint8)


class FakeController:
    def __init__(self):
        self.clicks, self.swipes = [], []

    def post_screencap(self):
        return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: IMAGE))

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return SimpleNamespace(wait=lambda: None)

    def post_swipe(self, x1, y1, x2, y2, duration):
        self.swipes.append((x1, y1, x2, y2))
        return SimpleNamespace(wait=lambda: None)


def fake_context(stopping=False):
    controller = FakeController()
    return SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=stopping)), controller


class ScriptedDecider:
    """按顺序返回预设答复；None 表示没有有效答复。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def ask(self, kind, image, candidates, **kwargs):
        self.requests.append((kind, [c["id"] for c in candidates], kwargs.get("state")))
        answer = self.answers.pop(0) if self.answers else {"id": "done"}
        if answer is None:
            return None
        chosen = next(c for c in candidates if c["id"] == answer["id"])
        return {**chosen, **({"point": answer["point"]} if "point" in answer else {})}


class InteractiveLoopTest(unittest.TestCase):
    def run_loop(self, answers, done_after=None, stopping=False):
        context, controller = fake_context(stopping)
        decider = ScriptedDecider(answers)
        calls = {"n": 0}

        def is_done(image):
            calls["n"] += 1
            return done_after is not None and calls["n"] > done_after

        result = run_interactive(context, decider, "deck_edit", is_done=is_done, session_key="t", screen_info={"type": "delete"}, step_delay=0)
        return result, controller, decider

    def test_screen_already_closed_asks_nothing(self):
        result, controller, decider = self.run_loop([], done_after=0)
        self.assertTrue(result)
        self.assertEqual(decider.requests, [])

    def test_taps_inside_safe_area_and_stops_when_screen_closes(self):
        result, controller, decider = self.run_loop([{"id": "tap", "point": [360, 700]}, {"id": "tap", "point": [500, 1085]}], done_after=2)
        self.assertTrue(result)
        self.assertEqual(controller.clicks, [(360, 700), (500, 1085)])
        self.assertEqual(decider.requests[0][0], "deck_edit")
        self.assertEqual(decider.requests[0][2]["screen"], {"type": "delete"})

    def test_taps_outside_safe_area_are_refused(self):
        # 顶部状态栏与最底部菜单栏不可点击；连续三次无效后交还原有流程
        answers = [{"id": "tap", "point": [360, 60]}, {"id": "tap", "point": [650, 1210]}, {"id": "tap"}]
        result, controller, decider = self.run_loop(answers)
        self.assertTrue(result)
        self.assertEqual(controller.clicks, [])
        self.assertEqual(len(decider.requests), 3)

    def test_scroll_wait_and_done(self):
        result, controller, decider = self.run_loop([{"id": "scroll_down"}, {"id": "scroll_up"}, {"id": "done"}])
        self.assertTrue(result)
        self.assertEqual(controller.swipes, [(360, 800, 360, 500), (360, 500, 360, 800)])

    def test_no_answer_three_times_hands_back(self):
        result, controller, decider = self.run_loop([None, None, None])
        self.assertTrue(result)
        self.assertEqual(len(decider.requests), 3)

    def test_stopping_returns_false_without_asking(self):
        result, controller, decider = self.run_loop([{"id": "done"}], stopping=True)
        self.assertFalse(result)
        self.assertEqual(decider.requests, [])


def reward_frame(brightness, offers=3):
    """合成奖励画面：详情面板亮度与可定位的候选数（候选定位由测试替换）。"""
    image = np.full((1280, 720, 3), brightness, dtype=np.uint8)
    image[0, 0, 0] = offers
    return image


class SequenceController(FakeController):
    """按顺序返回截图，最后一张重复使用。"""

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    def post_screencap(self):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: frame))


class RewardScreenTest(unittest.TestCase):
    """领取后的淡出画面仍能定位到候选框：等待面板正常显示，画面离开后不再点击或询问。"""

    def setUp(self):
        layout = lambda image: {"candidates": [{"candidate_id": f"reward_{i + 1}", "box": [100 + 160 * i, 820, 130, 130]} for i in range(image[0, 0, 0])]}
        for name, value in (("reward_layout", layout), ("ExternalDecider", SimpleNamespace(load=lambda: self.decider))):
            original = getattr(produce_actions, name)
            setattr(produce_actions, name, value)
            self.addCleanup(setattr, produce_actions, name, original)
        self.decider = ScriptedDecider([{"id": "reward_2"}])
        self.action = produce_actions.ProduceChooseRewardExternal()
        self.action.SETTLE_TIMEOUT, self.action.PREVIEW_DELAY = 0.5, 0

    def context(self, frames):
        controller = SequenceController(frames)
        return SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False)), controller

    def test_fading_screen_that_leaves_is_not_tapped(self):
        context, controller = self.context([reward_frame(190), reward_frame(120, offers=0)])
        self.assertTrue(self.action.run(context, None))
        self.assertEqual((controller.clicks, self.decider.requests), ([], []))

    def test_waits_for_next_reward_to_appear(self):
        context, controller = self.context([reward_frame(190), reward_frame(255)])
        image, offers = self.action._settled_offers(context)
        self.assertEqual(len(offers), 3)
        self.assertEqual(int(image[600, 300, 0]), 255)

    def test_dim_panel_past_timeout_hands_back_to_old_choice(self):
        context, controller = self.context([reward_frame(190)])
        self.assertIsNone(self.action._settled_offers(context)[1])

    def test_screen_leaving_during_preview_stops_tapping(self):
        context, controller = self.context([reward_frame(255), reward_frame(255), reward_frame(120, offers=0)])
        self.assertTrue(self.action.run(context, None))
        self.assertEqual(len(controller.clicks), 2)
        self.assertEqual(self.decider.requests, [])


class MailboxTest(unittest.TestCase):
    def setUp(self):
        root = ROOT / "debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="decider_", dir=root)
        self.addCleanup(temporary.cleanup)
        self.mailbox = Path(temporary.name)
        self.decider = ExternalDecider({"kinds": ["battle"], "timeout_s": 3, "mailbox": str(self.mailbox.relative_to(ROOT))})
        self.candidates = [{"id": "card_0", "label": "出牌", "box": [1, 2, 3, 4]}, {"id": "skip", "label": "跳过回合", "box": [5, 6, 7, 8]}]

    def answer_with(self, response):
        """模拟外部决策者：等待请求出现后写入答复。"""

        def responder():
            for _ in range(100):
                requests = list((self.mailbox / "requests").glob("*.json"))
                if requests:
                    request = json.loads(requests[0].read_text(encoding="utf-8"))
                    (self.mailbox / "responses").mkdir(exist_ok=True)
                    (self.mailbox / "responses" / requests[0].name).write_text(json.dumps({"id": request["id"], **response}), encoding="utf-8")
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=responder)
        thread.start()
        self.addCleanup(thread.join)

    def consumed(self):
        """consumed/ 中的实际采用记录（按请求顺序），只取 choice 与 source。"""
        return [
            (record["choice"], record["source"])
            for record in (json.loads(path.read_text(encoding="utf-8")) for path in sorted((self.mailbox / "consumed").glob("*.json")))
        ]

    def test_valid_answer_returns_candidate_with_point(self):
        self.answer_with({"choice": "skip", "reason": "r", "x": 10, "y": 20})
        chosen = self.decider.ask("battle", IMAGE, self.candidates, fallback="card_0", extra_images={"offer1": IMAGE})
        self.assertEqual((chosen["id"], chosen["point"], chosen["reason"]), ("skip", [10, 20], "r"))
        request = json.loads(next((self.mailbox / "requests").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(request["fallback"], "card_0")
        self.assertTrue((self.mailbox / request["frame"]).exists())
        self.assertTrue((self.mailbox / request["extra_frames"]["offer1"]).exists())
        self.assertEqual(self.consumed(), [("skip", "decider")])

    def test_unknown_choice_is_rejected(self):
        self.answer_with({"choice": "card_9"})
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates))
        # 答复不在候选中：Agent 回退原有逻辑，记录为 invalid。
        self.assertEqual(self.consumed(), [(None, "invalid")])

    def test_session_ids_keep_the_request_order(self):
        # 时钟回拨后重新载入：会话号仍晚于信箱中已有的请求。
        (self.mailbox / "requests").mkdir(exist_ok=True)
        (self.mailbox / "requests" / "20991231-235959-00007.json").write_text("{}", encoding="utf-8")
        self.assertEqual(_next_session(self.mailbox), "21000101-000000")
        session = ExternalDecider({"kinds": ["battle"], "mailbox": str(self.mailbox.relative_to(ROOT))}).session
        self.assertGreater(session, "20991231-235959")

    def test_timeout_and_stopping(self):
        self.decider.timeout = 0.3
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates))
        self.decider.timeout = 30
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates, stopping=lambda: True))
        self.assertEqual(self.consumed(), [(None, "timeout"), (None, "stopped")])


def ghl(task, probabilities, option_live, **extra):
    """GHL 实况桥的一条答复。"""
    options = [[key, f"option {key}"] for key in option_live]
    return {"id": "r1", "task": task, "options": options, "optionLive": option_live, "decider": {"key": max(probabilities, key=probabilities.get), "probabilities": probabilities}, **extra}


def ghl_pick(task, *options):
    """GHL 的答复：options 为 (候选, 选项说明)，决策模型选第一项。"""
    keys = [str(index + 1) for index in range(len(options))]
    probabilities = {key: 0.8 if key == "1" else 0.2 / (len(options) - 1) for key in keys}
    texts = [[key, text] for key, (_, text) in zip(keys, options)]
    return ghl(task, probabilities, {key: live for key, (live, _) in zip(keys, options)}, options=texts)


def temporary_mailbox(test, prefix):
    """debug/research/produce/tests/ 下的临时信箱，测试结束后删除。"""
    root = ROOT / "debug/research/produce/tests"
    root.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=root)
    test.addCleanup(temporary.cleanup)
    mailbox = Path(temporary.name)
    for sub in ("requests", "responses", "consumed", "ghl"):
        (mailbox / sub).mkdir()
    return mailbox


def decider_args(mailbox, *flags):
    """外部决策者的命令行参数：临时信箱，Agent 等待 600 秒（不读 config/decider.json）。"""
    return parse_args(["--mailbox", str(mailbox), "--agent-timeout", "600", *flags])


def write_request(mailbox, request_id, kind, candidates, fallback, age=0.0, **extra):
    """写入 age 秒前提出的请求；age 为 None 时没有 created_at（旧请求）。"""
    request = {"id": request_id, "kind": kind, "frame": f"frames/{request_id}.png", "candidates": candidates, "fallback": fallback, **extra}
    if age is not None:
        request["created_at"] = time.time() - age
    path = mailbox / "requests" / f"{request_id}.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def summary_lines(text):
    """提示中「Decided without you」一段的各行，没有时为空。"""
    _, _, tail = text.partition("Decided without you")
    return tail.splitlines()[1:]


class GhlFirstTest(unittest.TestCase):
    battle = {"id": "r1", "kind": "battle", "candidates": [{"id": "card_0"}, {"id": "card_1"}, {"id": "drink_0"}, {"id": "skip"}], "fallback": "card_0"}
    scope = {"exam", "lesson", "week", "offer"}

    def test_best_candidate_the_harness_offers(self):
        # 决策模型的首选（重抽）没有候选：取可执行候选中概率最高的一项。
        answer = ghl("ghl/battle-exam", {"1": 0.5, "2": 0.3, "3": 0.2}, {"1": None, "2": "card_1", "3": "skip"})
        choice, _ = ghl_choice(answer, self.battle, self.scope)
        self.assertEqual(choice["choice"], "card_1")
        self.assertIn("not offered", choice["reason"])

    def test_no_drinks_in_lessons(self):
        answer = ghl("ghl/battle-lesson", {"1": 0.6, "2": 0.3}, {"1": "drink_0", "2": "card_0"})
        self.assertEqual(ghl_choice(answer, self.battle, self.scope)[0]["choice"], "card_0")
        self.assertEqual(ghl_choice(answer, self.battle, self.scope, lesson_drinks=True)[0]["choice"], "drink_0")
        # 考试中照常采用饮料。
        answer["task"] = "ghl/battle-exam"
        self.assertEqual(ghl_choice(answer, self.battle, self.scope)[0]["choice"], "drink_0")

    def test_scope_skip_and_errors_go_to_the_llm(self):
        answer = ghl("ghl/battle-lesson", {"1": 1.0}, {"1": "card_0"})
        self.assertIsNone(ghl_choice(answer, self.battle, {"exam"})[0])
        self.assertIsNone(ghl_choice({"id": "r1", "skip": "kind customize: not rebuilt"}, self.battle, self.scope)[0])
        self.assertIsNone(ghl_choice({"id": "r1", "task": "ghl/battle-exam", "decider": {"error": "timeout"}}, self.battle, self.scope)[0])

    def test_class_answer_and_reference_policy(self):
        request = {"id": "r1", "kind": "class_option", "candidates": [{"id": "Vo"}, {"id": "Vi"}], "fallback": "Vo"}
        answer = ghl("ghl/week", {"1": 0.2, "2": 0.7, "3": 0.1}, {"1": "上课:Vo", "2": "上课:Vi", "3": "rest"}, ref={"policy": "B1v2", "key": "1"})
        self.assertEqual(ghl_choice(answer, request, self.scope)[0]["choice"], "Vi")
        self.assertEqual(ghl_choice(answer, request, self.scope, policy="reference")[0]["choice"], "Vo")

    def test_answers_file_and_ghl_only_response(self):
        mailbox = temporary_mailbox(self, "ghl_first_")
        answers =[ghl("ghl/battle-exam", {"1": 0.9}, {"1": "card_0"}, id="a"), ghl("ghl/battle-exam", {"1": 0.9}, {"1": "card_1"}, id="b")]
        # 正在写入的最后一行不读。
        (mailbox / "ghl" / "answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers) + '{"id": "c", "ta', encoding="utf-8")
        self.assertEqual(sorted(read_ghl_answers(mailbox / "ghl" / "answers.jsonl")), ["a", "b"])
        for request_id in ("b", "z"):
            (mailbox / "requests" / f"{request_id}.json").write_text(json.dumps({**self.battle, "id": request_id, "frame": "frames/x.png"}), encoding="utf-8")
        decider = Decider(decider_args(mailbox, "--ghl", "--ghl-only", "--ghl-wait", "0.2", "--no-vision"))
        for request_id in ("b", "z"):
            decider.handle(mailbox / "requests" / f"{request_id}.json")
        responses = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (mailbox / "responses").glob("*.json")}
        self.assertEqual((responses["b"]["choice"], responses["b"]["source"]), ("card_1", "ghl"))
        # GHL 没有答复：--ghl-only 时回答 fallback。
        self.assertEqual((responses["z"]["choice"], responses["z"]["source"]), ("card_0", "fallback"))


class DeciderDeadlineTest(unittest.TestCase):
    """Agent 已放弃的请求不再作答；GHL 的等待从请求提出时算起；模型调用不超过 Agent 剩余的等待时间。"""

    candidates = [{"id": "Vo", "label": "Vo"}, {"id": "rest", "label": "休息"}]

    def setUp(self):
        self.mailbox = temporary_mailbox(self, "decider_deadline_")

    def request(self, request_id, age=0.0):
        return write_request(self.mailbox, request_id, "week", self.candidates, "rest", age)

    def handle(self, decider, path):
        with contextlib.redirect_stdout(io.StringIO()):
            decider.handle(path)

    def responses(self):
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (self.mailbox / "responses").glob("*.json")}

    def test_pending_skips_requests_the_agent_gave_up(self):
        decider = Decider(decider_args(self.mailbox, "--agent-timeout", "60"))
        self.request("a")
        (self.mailbox / "consumed" / "a.json").write_text(json.dumps({"id": "a", "choice": None, "source": "timeout"}), encoding="utf-8")
        self.request("b", age=61)
        self.request("c", age=10)
        self.request("d")
        (self.mailbox / "responses" / "d.json").write_text("{}", encoding="utf-8")
        # 没有 created_at 的旧请求按文件修改时间计。
        old = time.time() - 120
        os.utime(self.request("e", age=None), (old, old))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual([path.stem for path in decider.pending()], ["c"])
            self.assertEqual([path.stem for path in decider.pending()], ["c"])
        # 每个跳过的请求只记一行日志，不写答复，也不写决策日志。
        lines = output.getvalue().splitlines()
        self.assertEqual([line.split("]")[0][1:] for line in lines], ["a", "b", "e"])
        self.assertIn("consumed: timeout", lines[0])
        self.assertEqual(sorted(self.responses()), ["d"])
        self.assertFalse((self.mailbox / "decisions.jsonl").exists())

    def test_ghl_wait_counts_from_the_request(self):
        decider = Decider(decider_args(self.mailbox, "--ghl", "--ghl-only", "--ghl-wait", "1"))
        answer = ghl("ghl/week", {"1": 0.9, "2": 0.1}, {"1": "Vo", "2": "rest"}, id="answered")
        (self.mailbox / "ghl" / "answers.jsonl").write_text(json.dumps(answer) + "\n", encoding="utf-8")
        seconds = {}
        for request_id, age in (("late", 5), ("answered", 30), ("fresh", 0)):
            path = self.request(request_id, age)
            started = time.time()
            self.handle(decider, path)
            seconds[request_id] = time.time() - started
        responses = self.responses()
        # 提出 5 秒后才处理：GHL 的等待（1 秒）已过，读一次 answers.jsonl 就回答。
        self.assertLess(seconds["late"], 0.5)
        self.assertEqual((responses["late"]["choice"], responses["late"]["source"]), ("rest", "fallback"))
        self.assertIn("no GHL answer within 1 s of the request", responses["late"]["reason"])
        # GHL 已作答的请求照常采用。
        self.assertEqual((responses["answered"]["choice"], responses["answered"]["source"]), ("Vo", "ghl"))
        # 刚提出的请求等满 1 秒。
        self.assertGreaterEqual(seconds["fresh"], 0.9)

    def test_model_call_is_capped_by_the_agent_wait(self):
        decider = Decider(decider_args(self.mailbox, "--agent-timeout", "100", "--call-timeout", "420"))
        timeouts = []

        def run(command, **kwargs):
            # 替身 claude CLI：记下时限，第二次调用超时
            timeouts.append(kwargs["timeout"])
            if len(timeouts) == 2:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {"choice": "Vo", "reason": "r"}}), stderr="")

        with mock.patch.object(subprocess, "run", run):
            for request_id, age in (("a", 40), ("b", 40), ("c", 92)):
                self.handle(decider, self.request(request_id, age))
        responses = self.responses()
        # 提出 40 秒后：Agent 还等 60 秒，模型调用以 55 秒为限（留 5 秒余量）。
        self.assertEqual(len(timeouts), 2)
        self.assertTrue(50 < timeouts[0] <= 55, timeouts[0])
        self.assertEqual((responses["a"]["choice"], responses["a"]["source"]), ("Vo", "model"))
        # 调用超时：回答 fallback，理由只记时限，不含整条命令。
        self.assertEqual((responses["b"]["choice"], responses["b"]["source"]), ("rest", "fallback"))
        self.assertRegex(responses["b"]["reason"], r"^decider error: claude did not answer within 5\d s$")
        # 只剩 3 秒：不问模型，直接回答 fallback。
        self.assertEqual((responses["c"]["choice"], responses["c"]["source"]), ("rest", "fallback"))
        self.assertIn("the model was not asked", responses["c"]["reason"])

    def test_failed_call_starts_a_new_session(self):
        decider = Decider(decider_args(self.mailbox))
        commands = []

        def run(command, **kwargs):
            # 替身 claude CLI：第一次调用超时（会话可能没有建立），之后正常作答
            commands.append(command)
            if len(commands) == 1:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {"choice": "Vo", "reason": "r"}}), stderr="")

        with mock.patch.object(subprocess, "run", run):
            for request_id in ("a", "b", "c"):
                self.handle(decider, self.request(request_id))
        # 超时的会话不再续接：第二次调用另开新会话，第三次续接第二次的会话。
        self.assertIn("--session-id", commands[1])
        self.assertNotIn("--resume", commands[1])
        self.assertEqual(commands[2][commands[2].index("--resume") + 1], commands[1][commands[1].index("--session-id") + 1])


class HandoffTest(unittest.TestCase):
    """
    GHL 与 fallback 的决策附在问模型的提示中：续接的会话只列它尚未见过的决策，新会话（首次调用、满 --session-turns 次、
    调用失败后）列最近的全部决策；决策者重启后从决策日志恢复。claude CLI 由替身代替，会话照常续接或另开。
    """

    week = [{"id": "Vi", "label": "Vi", "sp": True}, {"id": "rest", "label": "休息"}]
    battle = [{"id": "card_0", "label": "出牌"}, {"id": "drink_0", "label": "使用饮料"}, {"id": "skip", "label": "跳过回合"}]
    offers = [{"id": "reward_1", "label": "第 1 个奖励"}, {"id": "reward_2", "label": "第 2 个奖励"}]
    classes = [{"id": "Vo", "label": "Vo 选项"}, {"id": "Vi", "label": "Vi 选项"}]
    consult = [{"id": "tap", "label": "点击画面中的一个位置"}, {"id": "wait", "label": "等待画面变化"}, {"id": "done", "label": "本画面的操作已完成"}]
    reward = "- reward: reward_2 by GHL (take 輝くキミへ+)"

    def setUp(self):
        self.mailbox = temporary_mailbox(self, "decider_handoff_")
        self.calls, self.answers = [], []
        patcher = mock.patch.object(subprocess, "run", self.claude)
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude(self, command, **kwargs):
        """替身 claude CLI：记下提示与是否续接会话；按顺序取预设的选择（None 为调用超时），没有时选第一个候选。"""
        prompt = command[command.index("-p") + 1]
        self.calls.append({"prompt": prompt, "resume": "--resume" in command})
        choice = self.answers.pop(0) if self.answers else re.search(r"^- ([^:]+):", prompt.split("Candidates:", 1)[1], re.M).group(1)
        if choice is None:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {"choice": choice, "reason": "r"}}), stderr="")

    def decider(self, *flags):
        return Decider(decider_args(self.mailbox, "--ghl", "--ghl-wait", "0", "--no-vision", *flags))

    def summaries(self):
        """各次调用的提示中「Decided without you」一段的各行。"""
        return [summary_lines(call["prompt"]) for call in self.calls]

    def take_reward(self, decider, request_id):
        """GHL 选第 2 个奖励。"""
        answer = ghl_pick("ghl/card-offer", ("reward_2", "take 輝くキミへ+"), ("reward_1", "take 笑顔の基本"))
        self.ask(decider, request_id, "reward", self.offers, "reward_1", answer)

    def ask(self, decider, request_id, kind, candidates, fallback, answer=None, **extra):
        """提出请求并处理；answer 为 GHL 对它的答复（先写入 ghl/answers.jsonl）。"""
        if answer:
            with open(self.mailbox / "ghl" / "answers.jsonl", "a", encoding="utf-8") as file:
                file.write(json.dumps({**answer, "id": request_id}, ensure_ascii=False) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            decider.handle(write_request(self.mailbox, request_id, kind, candidates, fallback, **extra))

    def test_summary_reaches_the_next_model_prompt(self):
        decider = self.decider()
        self.ask(decider, "001", "week", self.week, "rest", ghl_pick("ghl/week", ("Vi", "Legend lesson Vi (SP): 8 turns, clear 85"), ("rest", "rest")))
        for request_id, option in (("002", ("card_0", "play 先へ進む姿+")), ("003", ("drink_0", "use drink ミックススムージー"))):
            answer = ghl_pick("ghl/battle-exam", option, ("skip", "end the turn without playing"))
            self.ask(decider, request_id, "battle", self.battle, "card_0", answer, context={"battle_id": "b1"})
        self.take_reward(decider, "004")
        self.assertEqual(self.calls, [])
        # 决策者重启：决策记录从决策日志恢复，新会话（相谈）附上最近的全部决策。
        decider = self.decider()
        self.ask(decider, "005", "consult", self.consult, "wait", context={"session_key": "consult-1"})
        expected = [
            "- week: Vi by GHL (Legend lesson Vi (SP): 8 turns, clear 85)",
            "- battle b1: 2 actions (GHL 2); drinks used: ミックススムージー",
            self.reward,
        ]
        self.assertEqual(summary_lines(self.calls[0]["prompt"]), expected)
        # 续接的会话只列它尚未见过的决策，包括模型答复无效时的 fallback。
        self.answers = ["zzz"]
        self.ask(decider, "006", "consult", self.consult, "wait", context={"session_key": "consult-1"})
        self.ask(decider, "007", "class_option", self.classes, "Vo", ghl_pick("ghl/week", ("上课:Vi", "class main (Vi): +150 Vi"), ("上课:Vo", "class main (Vo)")))
        self.ask(decider, "008", "consult", self.consult, "wait", context={"session_key": "consult-1"})
        self.ask(decider, "009", "consult", self.consult, "wait", context={"session_key": "consult-1"})
        expected = [
            "- consult: wait by fallback (等待画面变化; model answered unknown id 'zzz')",
            "- class_option: Vi by GHL (class main (Vi): +150 Vi)",
        ]
        self.assertEqual(self.summaries()[1:], [[], expected, []])
        self.assertEqual([call["resume"] for call in self.calls], [False, True, True, True])

    def test_each_session_sees_what_it_missed(self):
        # GHL 选了奖励，随后 GHL 没有作答的一手战斗问了模型（战斗会话）；之后续接的 campaign 会话仍看到这个奖励。
        decider = self.decider()
        self.ask(decider, "001", "week", self.week, "rest")
        self.take_reward(decider, "002")
        self.ask(decider, "003", "battle", self.battle, "card_0", context={"battle_id": "b1"})
        self.ask(decider, "004", "week", self.week, "rest")
        self.assertEqual(self.summaries(), [[], [self.reward], [self.reward]])
        self.assertEqual([call["resume"] for call in self.calls], [False, False, True])
        # 战斗有新的行动时整行以新的编号重新列出，见过旧版本的会话再次看到它。
        play = ghl_pick("ghl/battle-exam", ("card_0", "play 先へ進む姿+"), ("skip", "end the turn without playing"))
        for battle_id, week_id in (("005", "006"), ("007", "008")):
            self.ask(decider, battle_id, "battle", self.battle, "card_0", play, context={"battle_id": "b2"})
            self.ask(decider, week_id, "week", self.week, "rest")
        battles = [["- battle b2: 1 action (GHL 1); no drinks used"], ["- battle b2: 2 actions (GHL 2); no drinks used"]]
        self.assertEqual(self.summaries()[3:], battles)

    def test_new_session_gets_the_full_list(self):
        # 满 --session-turns 次或调用失败后另开新会话：没有对话记录，附上最近的全部决策。
        decider = self.decider("--session-turns", "2")
        self.take_reward(decider, "001")
        self.answers = ["wait", "wait", "wait", None]
        for request_id in ("002", "003", "004", "005", "006"):
            self.ask(decider, request_id, "consult", self.consult, "wait", context={"session_key": "consult-1"})
        failed = "- consult: wait by fallback (等待画面变化; decider error: claude did not answer within 420 s)"
        self.assertEqual([call["resume"] for call in self.calls], [False, True, False, True, False])
        self.assertEqual(self.summaries(), [[self.reward], [], [self.reward], [], [self.reward, failed]])

    def test_summary_is_bounded(self):
        decider = self.decider()
        request = {"id": "x", "kind": "week", "candidates": self.week}
        for _ in range(HANDOFF_KEEP + 5):
            decider.note_handoff(request, {"choice": "Vi", "reason": "GHL decider p=0.80: " + "x" * 400, "source": "ghl"})
        # 内存中只留 HANDOFF_KEEP 条；新会话只列最近的 HANDOFF_LINES 条，其余（含已丢弃的）记为省略。
        self.assertEqual(len(decider.handoff), HANDOFF_KEEP)
        lines = summary_lines(decider.handoff_text())
        self.assertEqual(len(lines), HANDOFF_LINES + 1)
        self.assertEqual(lines[0], f"- ({HANDOFF_KEEP + 5 - HANDOFF_LINES} earlier decisions omitted)")
        self.assertTrue(all(len(line) == len("- ") + HANDOFF_LINE_CHARS for line in lines[1:]))
        # 见过前面各条的会话只列其后的决策。
        self.assertEqual(len(summary_lines(decider.handoff_text(HANDOFF_KEEP + 2))), 3)


class GhlPlanTest(HandoffTest):
    """相谈、特别指导的访问附上 GHL 的计划：按 session_key 取 ghl/plans.jsonl，第一步等计划写下，不作计划（skip）时照常。"""

    plan_text = "Upgrade 先へ進む姿 (100 P). Delete ファンサの基本 (100 P). Then leave."

    def write_plan(self, key, **extra):
        with open(self.mailbox / "ghl" / "plans.jsonl", "a", encoding="utf-8") as file:
            file.write(json.dumps({"session_key": key, "kind": "consult", "text": self.plan_text, **extra}, ensure_ascii=False) + "\n")

    def responses(self):
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (self.mailbox / "responses").glob("*.json")}

    def test_plan_reaches_every_step_of_its_visit(self):
        self.write_plan("consult-1", id="001")
        decider = self.decider()
        for request_id, step in (("001", 0), ("002", 1)):
            self.ask(decider, request_id, "consult", self.consult, "done", context={"session_key": "consult-1", "step": step})
        self.assertTrue(all("GHL's plan for this visit" in call["prompt"] and self.plan_text in call["prompt"] for call in self.calls))
        self.assertEqual({key: value.get("ghl_plan") for key, value in self.responses().items()}, {"001": "consult-1", "002": "consult-1"})
        # Another visit has no plan.
        self.ask(decider, "003", "consult", self.consult, "done", context={"session_key": "consult-2", "step": 3})
        self.assertNotIn("GHL's plan for this visit", self.calls[-1]["prompt"])

    def test_first_step_waits_for_the_plan(self):
        decider = self.decider("--ghl-wait", "5")
        writer = threading.Timer(0.4, self.write_plan, args=("customize-1",))
        writer.start()
        self.addCleanup(writer.cancel)
        self.ask(decider, "001", "customize", self.consult, "done", context={"session_key": "customize-1", "step": 0})
        self.assertIn(self.plan_text, self.calls[-1]["prompt"])

    def test_a_plan_written_after_the_first_step_does_not_take_over(self):
        # The first step had no plan in time: the model began on its own, so a later plan (computed before that) is not used.
        decider = self.decider()
        self.ask(decider, "001", "consult", self.consult, "done", context={"session_key": "consult-1", "step": 0})
        self.write_plan("consult-1", id="001")
        self.ask(decider, "002", "consult", self.consult, "done", context={"session_key": "consult-1", "step": 1})
        self.assertTrue(all("GHL's plan for this visit" not in call["prompt"] for call in self.calls))
        self.assertNotIn("ghl_plan", self.responses()["002"])

    def test_no_plan_when_ghl_skips_and_none_for_other_kinds(self):
        self.write_plan("consult-1", skip="replay state unreliable")
        decider = self.decider()
        self.ask(decider, "001", "consult", self.consult, "done", context={"session_key": "consult-1", "step": 0})
        self.ask(decider, "002", "week", self.week, "rest", context={"session_key": "consult-1"})
        self.assertTrue(all("GHL's plan for this visit" not in call["prompt"] for call in self.calls))

    def test_plans_file_last_complete_row_wins(self):
        path = self.mailbox / "ghl" / "plans.jsonl"
        path.write_text(json.dumps({"session_key": "a", "text": "old"}) + "\n" + json.dumps({"session_key": "a", "text": "new"}) + "\n" + '{"session_key": "b", "te', encoding="utf-8")
        self.assertEqual({key: plan["text"] for key, plan in read_ghl_plans(path).items()}, {"a": "new"})


if __name__ == "__main__":
    unittest.main()
