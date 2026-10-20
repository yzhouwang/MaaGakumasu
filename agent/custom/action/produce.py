"""MaaFramework 培育动作入口及奖励领取适配。"""

import time

from utils import logger
from produce.battle import ProduceCardsAuto
from produce.events import ProduceChooseEventAuto, ProduceChooseEventBase, ProduceChooseNIAEventAuto
from produce.console import policy_name
from produce.decider import ExternalDecider, run_interactive
from produce.options import ProduceKeepDrinkAuto, ProduceChooseWorkAuto, ProduceChooseMirrorAuto, ProduceChooseOptionsAuto
from produce.rewards import RewardPreview, reward_layout
from produce.sessions import SESSIONS
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

__all__ = [
    "ProduceChooseEventBase", "ProduceChooseEventAuto", "ProduceChooseNIAEventAuto", "ProduceCardsAuto",
    "ProduceChooseWorkAuto", "ProduceChooseOptionsAuto", "ProduceChooseMirrorAuto", "ProduceKeepDrinkAuto", "ProduceRewardConfirm",
    "ProduceRewardExternalReady", "ProduceChooseRewardExternal", "ProduceCustomizeExternalReady", "ProduceCustomizeExternal",
    "ProduceConsultExternal", "ProduceDeckEditExternalReady", "ProduceDeckEditExternal",
]


@AgentServer.custom_action("ProduceConsultExternal")
class ProduceConsultExternal(CustomAction):
    """
    相谈（购买技能卡/饮料、强化、删除等）：逐步把当前画面交给外部决策者，按答复点击，直到其回到行动选择画面。

    点击只允许落在安全区域：画面中部与底部操作按钮带；顶部状态栏与最底部菜单栏（可进入放弃培育等菜单）不可点击。
    """

    def run(self, context, argv):
        decider = ExternalDecider.load()
        if not decider:
            return True
        return run_interactive(
            context, decider, "consult", is_done=lambda image: False,
            session_key=f"consult-{time.strftime('%Y%m%d-%H%M%S')}", screen_info={"type": "consult"},
        )


DECK_EDIT_ICONS = {
    "strengthen": "produce/strengthen_icon.png",
    "delete": "produce/delete_icon.png",
    "copy": "produce/copy_icon.png",
    "exchange": "produce/exchange_icon.png",
}


def deck_edit_type(context, image):
    """按左上角图标识别卡牌强化/删除/复制/变换画面，返回类型或 None。"""
    for kind, template in DECK_EDIT_ICONS.items():
        detail = context.run_recognition(
            "ProduceDeckEditIcon", image,
            pipeline_override={"ProduceDeckEditIcon": {"recognition": "TemplateMatch", "roi": [0, 28, 182, 58], "template": template}},
        )
        if detail and detail.hit:
            return kind
    return None


@AgentServer.custom_recognition("ProduceDeckEditExternalReady")
class ProduceDeckEditExternalReady(CustomRecognition):
    """外部决策启用（deck_edit）且处于卡牌强化/删除/复制/变换画面时命中，否则交给原有的固定位置节点。"""

    def analyze(self, context, argv):
        decider = ExternalDecider.load()
        if not decider or not decider.handles("deck_edit"):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "外部决策未启用"})
        kind = deck_edit_type(context, argv.image)
        if not kind:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "非卡牌编辑画面"})
        return CustomRecognition.AnalyzeResult(box=[0, 28, 182, 58], detail={"detail": kind})


@AgentServer.custom_action("ProduceDeckEditExternal")
class ProduceDeckEditExternal(CustomAction):
    """
    卡牌强化/删除/复制/变换：由外部决策者逐步选择目标卡并确认，画面左上角图标消失即视为完成。

    决策者未完成时回退原有的 ProduceChooseStrengthenFlag / ProduceChooseDeleteFlag 固定位置流程，避免主循环反复进入本节点。
    """

    def run(self, context, argv):
        decider = ExternalDecider.load()
        image = context.tasker.controller.post_screencap().wait().get()
        kind = deck_edit_type(context, image)
        if not decider or not kind:
            return True
        finished = run_interactive(
            context, decider, "deck_edit", is_done=lambda frame: deck_edit_type(context, frame) is None,
            session_key=f"deck-edit-{time.strftime('%Y%m%d-%H%M%S')}", screen_info={"type": kind},
        )
        if not finished:
            return False
        if deck_edit_type(context, context.tasker.controller.post_screencap().wait().get()):
            logger.info("卡牌编辑未由决策者完成，改用原有流程")
            context.run_task("ProduceChooseStrengthenFlag" if kind == "strengthen" else "ProduceChooseDeleteFlag")
        return True


@AgentServer.custom_recognition("ProduceCustomizeExternalReady")
class ProduceCustomizeExternalReady(CustomRecognition):
    """外部决策启用（customize）时命中特别指导的卡牌自定义画面，否则交给原有的「终了」节点。"""

    def analyze(self, context, argv):
        decider = ExternalDecider.load()
        if not decider or not decider.handles("customize"):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "外部决策未启用"})
        return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "特别指导自定义"})


@AgentServer.custom_action("ProduceCustomizeExternal")
class ProduceCustomizeExternal(CustomAction):
    """
    特别指导的卡牌自定义：逐步把当前画面与一组受限操作交给外部决策者，直到其选择结束。

    可选操作只有：在卡牌网格内点选（选中卡牌，不消耗 P 点）、滚动网格、点选自定义选项、カスタマイズ、
    実行する（消耗 P 点）、一覧に戻る、等待与结束。结束后由后续的 ProduceGuideExit 点击「終了」。
    """

    GRID = [40, 625, 640, 365]
    MENU_ROIS = ([30, 470, 660, 130], [30, 775, 660, 140])
    BUTTONS = {"customize": [235, 1050, 250, 70], "execute": [372, 1050, 252, 70], "back": [90, 1050, 252, 70]}
    MAX_STEPS = 40
    STEP_DELAY = 1.5

    def run(self, context, argv):
        decider = ExternalDecider.load()
        if not decider:
            return True
        session_key = f"customize-{time.strftime('%Y%m%d-%H%M%S')}"
        invalid = 0
        for step in range(self.MAX_STEPS):
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            candidates = [
                {"id": "tap_grid", "label": "点选卡牌网格中的一张卡（答复须给出 x、y，限网格区域内；只选中，不消耗 P 点）", "box": self.GRID},
                {"id": "scroll_down", "label": "向下滚动卡牌网格", "box": self.GRID},
                {"id": "scroll_up", "label": "向上滚动卡牌网格", "box": self.GRID},
            ]
            candidates += self._menu_options(context, image)
            candidates += [
                {"id": "customize", "label": "カスタマイズ（进入确认画面）", "box": self.BUTTONS["customize"]},
                {"id": "execute", "label": "実行する（确认执行，消耗 P 点）", "box": self.BUTTONS["execute"]},
                {"id": "back", "label": "一覧に戻る（取消并返回列表）", "box": self.BUTTONS["back"]},
                {"id": "wait", "label": "等待画面变化", "box": [0, 0, 0, 0]},
                {"id": "finish", "label": "结束特别指导（按「終了」）", "box": [0, 0, 0, 0]},
            ]
            chosen = decider.ask(
                "customize", image, candidates, fallback="finish",
                context={"session_key": session_key, "step": step}, stopping=lambda: context.tasker.stopping,
            )
            if context.tasker.stopping:
                return False
            if not chosen or chosen["id"] == "finish":
                return True
            action = chosen["id"]
            if action == "tap_grid":
                x, y = chosen.get("point") or (None, None)
                gx, gy, gw, gh = self.GRID
                if x is None or not (gx <= x <= gx + gw and gy <= y <= gy + gh):
                    invalid += 1
                    logger.warning(f"自定义：网格坐标无效 {chosen.get('point')}")
                    if invalid >= 3:
                        return True
                    continue
                context.tasker.controller.post_click(x, y).wait()
            elif action in ("scroll_down", "scroll_up"):
                begin, end = (900, 700) if action == "scroll_down" else (700, 900)
                context.tasker.controller.post_swipe(360, begin, 360, end, 500).wait()
            elif action == "wait":
                time.sleep(1.0)
            else:
                bx, by, bw, bh = chosen["box"]
                context.tasker.controller.post_click(bx + bw // 2, by + bh // 2).wait()
            time.sleep(self.STEP_DELAY)
        logger.warning("自定义步数达到上限，结束特别指导")
        return True

    def _menu_options(self, context, image):
        """OCR 自定义菜单（列表画面与确认画面两处），按文字位置生成选项候选，附带 P 点花费。"""
        options = []
        for roi in self.MENU_ROIS:
            detail = context.run_recognition(
                "ProduceCustomizeMenuText", image, pipeline_override={"ProduceCustomizeMenuText": {"recognition": "OCR", "roi": roi}}
            )
            results = detail.all_results if detail else []
            labels = [r for r in results if not r.text.strip().isdigit()]
            costs = [r for r in results if r.text.strip().isdigit()]
            for label in labels:
                x, y, w, h = label.box
                cost = next((c.text for c in costs if abs((c.box[0] + c.box[2] / 2) - (x + w / 2)) < 120 and c.box[1] > y), "?")
                options.append({"id": f"option_{len(options) + 1}", "label": f"自定义选项：{label.text}（{cost} P）", "box": [x - 15, y - 15, w + 30, h + 60]})
        return options


@AgentServer.custom_recognition("ProduceRewardExternalReady")
class ProduceRewardExternalReady(CustomRecognition):
    """外部决策启用（reward）且能定位到 2–4 个奖励候选时命中，否则交给原有选择节点。"""

    def analyze(self, context, argv):
        decider = ExternalDecider.load()
        if not decider or not decider.handles("reward"):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "外部决策未启用"})
        layout = reward_layout(argv.image)
        if len(layout["candidates"]) < 2:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": f"奖励布局未就绪：{layout['status']}"})
        return CustomRecognition.AnalyzeResult(box=layout["candidates"][0]["box"], detail={"detail": layout["status"]})


@AgentServer.custom_action("ProduceChooseRewardExternal")
class ProduceChooseRewardExternal(CustomAction):
    """
    逐个点选奖励候选并截取详情画面，连同可用的「不领取」交给外部决策者，按答复选择后领取。

    点选只改变选中项，不会领取；没有有效答复时禁用本节点重新执行原有选择流程。
    """

    PREVIEW_DELAY = 0.8

    def run(self, context, argv):
        decider = ExternalDecider.load()
        image = context.tasker.controller.post_screencap().wait().get()
        offers = reward_layout(image)["candidates"]
        if not decider or len(offers) < 2:
            return self.fallback(context)
        previews = {}
        for index, offer in enumerate(offers):
            x, y, w, h = offer["box"]
            context.tasker.controller.post_click(x + w // 2, y + h // 2).wait()
            time.sleep(self.PREVIEW_DELAY)
            previews[f"offer{index + 1}"] = context.tasker.controller.post_screencap().wait().get().copy()
            if context.tasker.stopping:
                return False
        candidates = [
            {"id": offer["candidate_id"], "label": f"第 {index + 1} 个奖励（详情见 offer{index + 1} 截图）", "box": offer["box"]}
            for index, offer in enumerate(offers)
        ]
        skip = context.run_recognition("ProduceChooseSkip", image)
        if skip and skip.hit:
            candidates.append({"id": "skip", "label": "不领取", "box": [int(v) for v in skip.best_result.box]})
            if self._drinks_full(context, previews[f"offer{len(offers)}"]):
                # 饮料已达持有上限时「受け取る」不可用，只能选择「受け取らない」，无需询问决策者
                logger.info("P 饮料已达持有上限，只能不领取")
                return self._take(context, candidates[-1])
        recommended = context.run_recognition("ProduceChooseRecommend", image)
        fallback = "skip" if skip and skip.hit else candidates[0]["id"]
        if not (skip and skip.hit) and recommended and recommended.hit:
            rx, ry, rw, rh = recommended.best_result.box
            point = (rx + rw // 2, ry + rh // 2 - 80)
            fallback = next((c["id"] for c in candidates if c["box"][0] <= point[0] <= c["box"][0] + c["box"][2]), fallback)
        chosen = decider.ask(
            "reward", image, candidates, fallback=fallback, extra_images=previews,
            state={"game_recommended": bool(recommended and recommended.hit)}, stopping=lambda: context.tasker.stopping,
        )
        if context.tasker.stopping:
            return False
        if not chosen:
            return self.fallback(context)
        if chosen["id"] != "skip" and skip and skip.hit and self._drinks_full(context, previews[f"offer{len(offers)}"]):
            logger.info("P 饮料已达持有上限，改为不领取")
            chosen = candidates[-1]
        return self._take(context, chosen)

    def _take(self, context, chosen):
        x, y, w, h = chosen["box"]
        context.tasker.controller.post_click(x + w // 2, y + h // 2).wait()
        time.sleep(self.PREVIEW_DELAY)
        context.run_task("ProduceChooseConfirm")
        return True

    @staticmethod
    def _drinks_full(context, image):
        """「受け取る」按钮上出现「所持上限」时，说明 P 饮料已满。"""
        detail = context.run_recognition(
            "ProduceRewardDrinkFull", image,
            pipeline_override={"ProduceRewardDrinkFull": {"recognition": "OCR", "roi": [200, 1095, 320, 60], "expected": "所持上限"}},
        )
        return bool(detail and detail.hit)

    @staticmethod
    def fallback(context):
        logger.info("奖励改用原有选择")
        context.run_task("ProduceChooseGetFlag", pipeline_override={"ProduceChooseRewardExternal": {"enabled": False}})
        return True


@AgentServer.custom_action("ProduceRewardConfirm")
class ProduceRewardConfirm(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        session = SESSIONS.get(context)
        if session:
            session.pop("reward_receive_action", None)
        capture = session["capture"] if session else None
        if not capture or not capture.reward_preview_enabled:
            return self.receive(context, argv.box, session)
        # 跳过无法领取的饮料属于原有策略动作，
        # 不作为候选预览，也不进入奖励选择流程。
        choice = session.get("reward_choice") or {}
        if choice.get("policy_node") == "ProduceChooseSkip":
            return self.receive(context, argv.box, session)

        def receive_visible(image):
            detail = context.run_recognition("ProduceRewardReceive", image)
            return bool(detail and detail.hit)

        def screenshot():
            image = context.tasker.controller.post_screencap().wait().get()
            return image.copy()

        preview = RewardPreview(
            capture, screenshot,
            lambda x, y: context.tasker.controller.post_click(x, y).wait().status.succeeded,
            receive_visible, lambda: context.tasker.stopping,
        )
        try:
            result = preview.collect(choice, session.get("reward_confirmation_id"))
        except Exception:
            logger.exception("奖励详情采集未启动，继续原选择")
            # collect 会处理输入后的异常并尝试恢复原选择。
            # 超出该保护范围的异常不得确认已经变化的选项。
            result = {"safe_to_receive": not preview.changed_selection, "status": "skipped"}
        if context.tasker.stopping:
            return False
        if not result["safe_to_receive"]:
            logger.warning("奖励原选择未恢复或任务已停止，暂停领取；请检查当前选中项后继续任务")
            context.tasker.post_stop()
            return False
        if preview.last_image is not None:
            # 恢复原页面后重新识别领取按钮，
            # 不复用预览候选之前的识别结果。
            detail = context.run_recognition("ProduceRewardReceive", preview.last_image)
            if not detail or not detail.hit:
                logger.warning("奖励领取按钮未确认，暂停任务")
                context.tasker.post_stop()
                return False
            box = detail.box
        else:
            box = argv.box
        success = self.receive(context, box, session, log_result=False)
        action = session.get("reward_receive_action")
        receive_event_id = preview.record(
            "reward_receive_input", confirmation_id=session.get("reward_confirmation_id"),
            selected_candidate_id=(f"reward_{preview.layout['selected_index'] + 1}" if preview.group_id else None),
            actual_box=list(box), framework_action_succeeded=success, game_action_confirmed=None,
            actual_point=list(action.result.point) if action and getattr(action.result, "point", None) else None,
        )
        if receive_event_id and preview.group_id and capture.merged_console_enabled:
            emitted = capture.wait_for_console(receive_event_id, cancelled=lambda: context.tasker.stopping)
            if not emitted and not context.tasker.stopping:
                self.log_receive(success, choice, pending=True)
        else:
            self.log_receive(success, choice)
        return success

    @staticmethod
    def receive(context, box, session=None, log_result=True):
        if context.tasker.stopping:
            return False
        action = context.run_action("ProduceRewardReceive", box=box)
        if session:
            session["reward_receive_action"] = action
        success = bool(action and action.success)
        if log_result:
            ProduceRewardConfirm.log_receive(success, (session or {}).get("reward_choice"))
        return success

    @staticmethod
    def log_receive(success, choice, pending=False):
        message = "已点击领取奖励" if success else "奖励领取按钮点击失败"
        if (choice or {}).get("policy_node") == "ProduceChooseSkip":
            message = "已点击跳过奖励确认" if success else "跳过奖励确认点击失败"
        message += f"｜{policy_name(choice)}"
        if pending:
            message += "｜候选摘要仍在后台处理，完成后补发"
        (logger.info if success else logger.warning)(message)
