"""外部决策接口：通过文件信箱把培育决策交给仓库之外的决策者，超时、出错或答复无效时回退原有逻辑。

默认关闭。在 Git 忽略的 config/decider.json 中启用：

    {"enabled": true, "kinds": ["battle", "week"], "timeout_s": 600, "mailbox": "debug/decider/mailbox"}

信箱布局（相对仓库根目录）：
- requests/<id>.json：决策请求，含决策种类、候选项（id、说明、点击框）、原有逻辑的选择（fallback）与已读取的状态；
- frames/<id>.png：请求对应的原始截图；
- responses/<id>.json：决策者的答复 {"id": ..., "choice": <候选 id>, "reason": ...}。

决策者只返回候选 id，点击仍由 Agent 按候选框执行；答复不在候选中时视为无效并回退。
"""

import os
import json
import time
import itertools
from pathlib import Path

from utils import logger
from utils.image_io import encode_png

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "decider.json"
POLL_INTERVAL = 0.2


def _write_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


class ExternalDecider:
    _counter = itertools.count(1)
    _cached = None
    _cached_mtime = None

    def __init__(self, config: dict):
        self.kinds = set(config.get("kinds", ["battle", "week"]))
        self.timeout = float(config.get("timeout_s", 600))
        self.mailbox = ROOT / config.get("mailbox", "debug/decider/mailbox")
        self.session = time.strftime("%Y%m%d-%H%M%S")

    @classmethod
    def load(cls):
        """读取配置；文件修改后重新加载，未启用时返回 None。"""
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return None
        if mtime != cls._cached_mtime:
            cls._cached_mtime = mtime
            try:
                config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                cls._cached = cls(config) if config.get("enabled") is True else None
            except Exception:
                logger.exception("外部决策配置读取失败，使用原有逻辑")
                cls._cached = None
        return cls._cached

    def handles(self, kind: str) -> bool:
        return kind in self.kinds

    def ask(self, kind: str, image, candidates: list, fallback=None, state=None, context=None, stopping=None, extra_images=None):
        """提交请求并等待答复；返回选中的候选（附 reason），无有效答复时返回 None。

        extra_images 为 {名称: 截图}，例如奖励逐个预览后的详情画面，写入 frames/ 并在请求的 extra_frames 中列出。
        """
        if not candidates:
            return None
        request_id = f"{self.session}-{next(self._counter):05d}"
        frame = f"frames/{request_id}.png"
        try:
            _write_atomic(self.mailbox / frame, encode_png(image))
            extra_frames = {}
            for name, extra in (extra_images or {}).items():
                extra_frames[name] = f"frames/{request_id}-{name}.png"
                _write_atomic(self.mailbox / extra_frames[name], encode_png(extra))
            request = {
                "id": request_id,
                "kind": kind,
                "created_at": time.time(),
                "frame": frame,
                "extra_frames": extra_frames,
                "candidates": candidates,
                "fallback": fallback,
                "state": state or {},
                "context": context or {},
            }
            _write_atomic(self.mailbox / "requests" / f"{request_id}.json", json.dumps(request, ensure_ascii=False, indent=1).encode("utf-8"))
        except Exception:
            logger.exception("外部决策请求写入失败，使用原有逻辑")
            return None

        logger.info(f"外部决策：等待 {kind} 答复 {request_id}（{len(candidates)} 个候选）")
        response_path = self.mailbox / "responses" / f"{request_id}.json"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if stopping and stopping():
                return None
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(POLL_INTERVAL)
                    continue
                chosen = next((item for item in candidates if item["id"] == response.get("choice")), None)
                if chosen is None:
                    logger.warning(f"外部决策答复无效（{response.get('choice')}），使用原有逻辑")
                    return None
                logger.success(f"外部决策：{kind} 选择 {chosen['id']}（{chosen.get('label', '')}）{response.get('reason', '')[:80]}")
                result = {**chosen, "reason": response.get("reason", "")}
                if isinstance(response.get("x"), int) and isinstance(response.get("y"), int):
                    result["point"] = [response["x"], response["y"]]
                return result
            time.sleep(POLL_INTERVAL)
        logger.warning(f"外部决策超时（{self.timeout:.0f} 秒），使用原有逻辑")
        return None


INTERACTIVE_SAFE_AREAS = ([0, 150, 720, 880], [0, 1030, 720, 110])


def run_interactive(
    context, decider, kind, *, is_done, session_key, screen_info=None, safe_areas=INTERACTIVE_SAFE_AREAS, max_steps=50, step_delay=1.5
):
    """
    逐步把当前画面交给外部决策者，按答复点击/滚动/等待，直到画面离开（is_done 返回 True）或决策者表示结束。

    点击坐标只允许落在 safe_areas 内（默认不含顶部状态栏与最底部菜单栏，避免进入放弃培育等菜单）。
    多次无有效答复或步数达到上限时返回，由调用方或主循环继续处理。

    Args:
        is_done: 接收当前截图，返回该画面是否已经结束。
        screen_info: 附加给决策者的画面说明（例如强化/删除）。

    Returns:
        bool: 任务停止时返回 False，其余情况返回 True。
    """
    invalid = 0
    for step in range(max_steps):
        if context.tasker.stopping:
            return False
        image = context.tasker.controller.post_screencap().wait().get()
        if is_done(image):
            return True
        candidates = [
            {"id": "tap", "label": "点击画面中的一个位置（答复须给出 x、y；只能落在安全区域内）", "box": safe_areas[0]},
            {"id": "scroll_down", "label": "向下滚动列表", "box": safe_areas[0]},
            {"id": "scroll_up", "label": "向上滚动列表", "box": safe_areas[0]},
            {"id": "wait", "label": "等待画面变化", "box": [0, 0, 0, 0]},
            {"id": "done", "label": "本画面的操作已完成", "box": [0, 0, 0, 0]},
        ]
        chosen = decider.ask(
            kind,
            image,
            candidates,
            fallback="wait",
            state={"safe_areas": [list(area) for area in safe_areas], "screen": screen_info or {}},
            context={"session_key": session_key, "step": step},
            stopping=lambda: context.tasker.stopping,
        )
        if context.tasker.stopping:
            return False
        if not chosen:
            invalid += 1
            if invalid >= 3:
                logger.warning(f"{kind}：决策者多次无有效答复，交还原有流程")
                return True
            time.sleep(step_delay)
            continue
        action = chosen["id"]
        if action == "done":
            return True
        if action == "tap":
            x, y = chosen.get("point") or (None, None)
            if x is None or not any(ax <= x <= ax + aw and ay <= y <= ay + ah for ax, ay, aw, ah in safe_areas):
                invalid += 1
                logger.warning(f"{kind}：点击坐标不在安全区域 {chosen.get('point')}")
                if invalid >= 3:
                    return True
                continue
            context.tasker.controller.post_click(x, y).wait()
        elif action in ("scroll_down", "scroll_up"):
            begin, end = (800, 500) if action == "scroll_down" else (500, 800)
            context.tasker.controller.post_swipe(360, begin, 360, end, 500).wait()
        else:
            time.sleep(1.0)
        time.sleep(step_delay)
    logger.warning(f"{kind}：步数达到上限，交还原有流程")
    return True
