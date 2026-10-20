"""外部决策接口回归：文件信箱的请求/答复、坐标透传与超时，以及交互式画面循环的安全区域、结束与停止处理。

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
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from produce.decider import ExternalDecider, run_interactive

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

    def test_valid_answer_returns_candidate_with_point(self):
        self.answer_with({"choice": "skip", "reason": "r", "x": 10, "y": 20})
        chosen = self.decider.ask("battle", IMAGE, self.candidates, fallback="card_0", extra_images={"offer1": IMAGE})
        self.assertEqual((chosen["id"], chosen["point"], chosen["reason"]), ("skip", [10, 20], "r"))
        request = json.loads(next((self.mailbox / "requests").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(request["fallback"], "card_0")
        self.assertTrue((self.mailbox / request["frame"]).exists())
        self.assertTrue((self.mailbox / request["extra_frames"]["offer1"]).exists())

    def test_unknown_choice_is_rejected(self):
        self.answer_with({"choice": "card_9"})
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates))

    def test_timeout_and_stopping(self):
        self.decider.timeout = 0.3
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates))
        self.decider.timeout = 30
        self.assertIsNone(self.decider.ask("battle", IMAGE, self.candidates, stopping=lambda: True))


if __name__ == "__main__":
    unittest.main()
