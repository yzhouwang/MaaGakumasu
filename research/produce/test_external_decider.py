"""外部决策接口回归：文件信箱的请求/答复/实际采用记录、坐标透传与超时，交互式画面循环的安全区域、结束与停止处理，
以及外部决策者的 GHL 优先模式（tools/decider/opus_decider.py --ghl）。

使用合成截图与替身决策者，不连接设备、不调用模型；临时信箱写入 debug/research/produce/tests/。
需要安装 maafw（截图编码使用 MaaFramework）。
"""

import sys
import json
import time
import tempfile
import unittest
import threading
from types import SimpleNamespace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools" / "decider")]
import custom.action.produce as produce_actions
from opus_decider import Decider, ghl_choice, read_ghl_answers
from produce.decider import ExternalDecider, run_interactive, _next_session

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
        root = ROOT / "debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="ghl_first_", dir=root)
        self.addCleanup(temporary.cleanup)
        mailbox = Path(temporary.name)
        for sub in ("requests", "responses", "ghl"):
            (mailbox / sub).mkdir()
        answers = [ghl("ghl/battle-exam", {"1": 0.9}, {"1": "card_0"}, id="a"), ghl("ghl/battle-exam", {"1": 0.9}, {"1": "card_1"}, id="b")]
        # 正在写入的最后一行不读。
        (mailbox / "ghl" / "answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers) + '{"id": "c", "ta', encoding="utf-8")
        self.assertEqual(sorted(read_ghl_answers(mailbox / "ghl" / "answers.jsonl")), ["a", "b"])
        for request_id in ("b", "z"):
            (mailbox / "requests" / f"{request_id}.json").write_text(json.dumps({**self.battle, "id": request_id, "frame": "frames/x.png"}), encoding="utf-8")
        args = SimpleNamespace(
            mailbox=str(mailbox), extra_prompt=None, context="", ghl=True, ghl_scope="exam,lesson,week,offer", ghl_policy="decider",
            ghl_wait=0.2, ghl_lesson_drinks=False, ghl_only=True, dry_run=False, no_vision=True,
        )
        decider = Decider(args)
        for request_id in ("b", "z"):
            decider.handle(mailbox / "requests" / f"{request_id}.json")
        responses = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (mailbox / "responses").glob("*.json")}
        self.assertEqual((responses["b"]["choice"], responses["b"]["source"]), ("card_1", "ghl"))
        # GHL 没有答复：--ghl-only 时回答 fallback。
        self.assertEqual((responses["z"]["choice"], responses["z"]["source"]), ("card_0", "fallback"))


if __name__ == "__main__":
    unittest.main()
