"""MaaFramework 培育动作入口及奖励领取适配。"""

import time

import numpy as np
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
    特别指导的卡牌自定义：逐步把当前画面与一组受限操作交给外部决策者，直到画面离开特别指导。

    可选操作只有：在卡牌网格内点选（选中卡牌，不消耗 P 点）、滚动网格、点选自定义选项、カスタマイズ、
    実行する（消耗 P 点）、一覧に戻る、等待与结束（由本动作点击「終了」）；「…をカスタマイズしました」提示遮住「終了」时改为点掉提示。
    左侧为「キャンセル」的弹窗（P 点有剩余时「終了」的确认框、误开的 P 饮料详情等）打开时只提供弹窗左右两个按钮与等待：
    弹窗按钮与最底部菜单栏同高，网格点选够不到；主循环的通用按钮节点按得分会点「キャンセル」，因此弹窗都由本动作处理。
    没有有效答复时按 fallback 处理：弹窗在按过「終了」之后确认、之前关闭；「終了」可见则结束，被提示遮住则点掉提示。
    决策步数用完或三次没有有效答复（超时、无效选项或网格外坐标）后不再询问，按同样的 fallback 离开特别指导，
    不把未处理的弹窗或提示交还主循环。
    """

    GRID = [40, 625, 640, 365]
    MENU_ROIS = ([30, 470, 660, 130], [30, 775, 660, 140])
    BUTTONS = {"customize": [235, 1050, 250, 70], "execute": [372, 1050, 252, 70], "back": [90, 1050, 252, 70]}
    # 「…をカスタマイズしました」提示条的上部（提示条约在 y 945–1115，遮住「終了」）；列表画面此处为空白
    DISMISS = [330, 970, 60, 30]
    # 弹窗左下的「キャンセル」；弹窗居中，右侧按钮与之左右对称
    DIALOG_CANCEL = {"recognition": "TemplateMatch", "roi": [40, 1000, 400, 270], "template": "cancel.png", "threshold": 0.9}
    # 左上角的特别指导标题，与 ProduceGuideScreen 相同（另设节点：那个节点识别成功时会在控制台提示「事件: 特别指导」）
    GUIDE_HEADER = {"recognition": "TemplateMatch", "roi": [0, 0, 260, 170], "template": "produce/guide_header.png", "threshold": 0.85}
    MAX_STEPS, LEAVE_STEPS = 40, 15
    STEP_DELAY = 1.5
    SETTLE_S, SETTLE_POLL = 6.0, 0.5

    def run(self, context, argv):
        decider = ExternalDecider.load()
        if not decider:
            return True
        session_key = f"customize-{time.strftime('%Y%m%d-%H%M%S')}"
        invalid, exiting, leaving = 0, False, False
        for step in range(self.MAX_STEPS + self.LEAVE_STEPS):
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            # 弹窗会模糊左上角的特别指导标题，先认弹窗再判断是否已离开
            dialog = self._dialog(context, image)
            if not dialog and not self._in_guide(context, image):
                return True
            exit_box = None if dialog else self._exit_button(context, image)
            if dialog:
                candidates, fallback = self._dialog_candidates(dialog), "dialog_confirm" if exiting else "dialog_cancel"
            else:
                candidates, fallback = self._screen_candidates(context, image, exit_box), "finish" if exit_box else "dismiss"
            chosen = None
            if step < self.MAX_STEPS and invalid < 3:
                chosen = decider.ask(
                    "customize", image, candidates, fallback=fallback,
                    context={"session_key": session_key, "step": step}, stopping=lambda: context.tasker.stopping,
                )
                if context.tasker.stopping:
                    return False
                invalid += chosen is None
            elif not leaving:
                leaving = True
                logger.warning("自定义：决策步数用完或三次没有有效答复，不再询问，直接离开特别指导")
            chosen = chosen or next(c for c in candidates if c["id"] == fallback)
            action = chosen["id"]
            if action == "finish":
                self._click(context, exit_box)
                exiting = True
                # P 点有剩余时弹出确认框，否则画面直接离开特别指导
                self._settle(context, lambda frame: self._dialog(context, frame) or not self._in_guide(context, frame))
                continue
            if action in ("dialog_cancel", "dialog_confirm"):
                # 答复期间弹窗可能已在关闭：只在它仍在原处时点击，以免点到弹窗下方的饮料栏
                now = self._dialog(context, context.tasker.controller.post_screencap().wait().get())
                if not now or max(abs(a - b) for a, b in zip(now, dialog)) > 4:
                    logger.warning(f"自定义：弹窗已变化（{dialog} → {now}），不点击")
                    time.sleep(self.SETTLE_POLL)
                    continue
                self._click(context, chosen["box"])
                if action == "dialog_confirm":
                    self._settle(context, lambda frame: not self._dialog(context, frame) and not self._in_guide(context, frame))
                else:
                    exiting = False
                    self._settle(context, lambda frame: not self._dialog(context, frame))
                continue
            if action == "tap_grid":
                x, y = chosen.get("point") or (None, None)
                gx, gy, gw, gh = self.GRID
                if x is None or not (gx <= x <= gx + gw and gy <= y <= gy + gh):
                    invalid += 1
                    logger.warning(f"自定义：网格坐标无效 {chosen.get('point')}")
                    continue
                context.tasker.controller.post_click(x, y).wait()
            elif action in ("scroll_down", "scroll_up"):
                begin, end = (900, 700) if action == "scroll_down" else (700, 900)
                context.tasker.controller.post_swipe(360, begin, 360, end, 500).wait()
            elif action == "wait":
                time.sleep(1.0)
            else:
                self._click(context, chosen["box"])
            time.sleep(self.STEP_DELAY)
        logger.warning("自定义：未能离开特别指导，交还主循环")
        return True

    def _screen_candidates(self, context, image, exit_box):
        """
        特别指导画面（无弹窗）的候选：网格点选与滚动、自定义选项、底部按钮、等待，
        以及「終了」可见时结束、被提示遮住时点掉提示。
        """
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
        ]
        if exit_box:
            return candidates + [{"id": "finish", "label": "结束特别指导（按「終了」；P 点有剩余时游戏会弹出确认框）", "box": list(exit_box)}]
        return candidates + [{"id": "dismiss", "label": "点掉画面下方遮住「終了」的提示（「…をカスタマイズしました」）", "box": self.DISMISS}]

    @staticmethod
    def _dialog_candidates(cancel):
        """弹窗打开时只有左右两个按钮与等待。"""
        x, y, w, h = cancel
        return [
            {"id": "dialog_cancel", "label": "弹窗左侧的「キャンセル」（关闭弹窗，例如误开的 P 饮料详情）", "box": list(cancel)},
            {"id": "dialog_confirm", "label": "弹窗右侧的按钮（确认结束特别指导等；P 饮料详情的「使う」不要选）", "box": [720 - x - w, y, w, h]},
            {"id": "wait", "label": "等待画面变化", "box": [0, 0, 0, 0]},
        ]

    def _dialog(self, context, image):
        """左侧为「キャンセル」的弹窗打开时返回该按钮的框，否则返回 None。"""
        detail = context.run_recognition("ProduceCustomizeDialog", image, pipeline_override={"ProduceCustomizeDialog": self.DIALOG_CANCEL})
        return list(detail.box) if detail and detail.hit else None

    def _in_guide(self, context, image):
        """左上角仍是特别指导的标题。"""
        detail = context.run_recognition(
            "ProduceCustomizeGuideHeader", image, pipeline_override={"ProduceCustomizeGuideHeader": self.GUIDE_HEADER}
        )
        return bool(detail and detail.hit)

    @staticmethod
    def _exit_button(context, image):
        """「終了」按钮的框（与 ProduceGuideExit 相同的识别），没有时返回 None。"""
        detail = context.run_recognition("ProduceGuideExit", image)
        return list(detail.box) if detail and detail.hit else None

    @staticmethod
    def _click(context, box):
        bx, by, bw, bh = box
        context.tasker.controller.post_click(bx + bw // 2, by + bh // 2).wait()

    def _settle(self, context, until):
        """点击后等画面变化：until(截图) 为真或超过 SETTLE_S 秒为止。"""
        deadline = time.time() + self.SETTLE_S
        while time.time() < deadline and not context.tasker.stopping:
            time.sleep(self.SETTLE_POLL)
            if until(context.tasker.controller.post_screencap().wait().get()):
                return

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
    SETTLE_TIMEOUT = 3.0
    # 奖励详情面板（白底）区域；领取后的淡出画面与离开后的画面在此明显变暗
    PANEL_ROI = (80, 490, 560, 280)

    def run(self, context, argv):
        decider = ExternalDecider.load()
        if not decider:
            return self.fallback(context)
        image, offers = self._settled_offers(context)
        if offers is None:
            return self.fallback(context)
        if len(offers) < 2:
            # 领取后的淡出画面仍能定位到候选框，画面离开后不再点击，交回主循环重新识别
            logger.info("奖励画面已离开，不再选择")
            return True
        previews = {}
        for index, offer in enumerate(offers):
            x, y, w, h = offer["box"]
            context.tasker.controller.post_click(x + w // 2, y + h // 2).wait()
            time.sleep(self.PREVIEW_DELAY)
            previews[f"offer{index + 1}"] = context.tasker.controller.post_screencap().wait().get().copy()
            if context.tasker.stopping:
                return False
            if not self._panel_bright(previews[f"offer{index + 1}"]):
                logger.warning("奖励画面在预览时离开，不再点击")
                return True
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

    @classmethod
    def _panel_bright(cls, image) -> bool:
        x, y, w, h = cls.PANEL_ROI
        return float(np.median(image[y:y + h, x:x + w])) >= 250

    def _settled_offers(self, context):
        """
        等待奖励画面完全显示后定位候选。

        Returns:
            tuple: (截图, 候选)。候选为空表示画面已离开；为 None 表示超时后面板仍未正常显示，交给原有选择。
        """
        deadline = time.time() + self.SETTLE_TIMEOUT
        while True:
            image = context.tasker.controller.post_screencap().wait().get()
            offers = reward_layout(image)["candidates"]
            if len(offers) < 2 or context.tasker.stopping:
                return image, []
            if self._panel_bright(image):
                return image, offers
            if time.time() >= deadline:
                return image, None
            time.sleep(0.3)

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
