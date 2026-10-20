"""无界面运行 MaaGakumasu 任务：读取 interface.json，按选项拼出 pipeline_override，启动 Agent 并在 ADB 设备上执行。

与 MFAAvalonia 的行为保持一致：任务与选项（含嵌套 case 的子选项）的 pipeline_override 按出现顺序深度合并；
未指定的选项使用 default_case。仅用于本地开发调试，不进入发布包。

示例（仓库根目录）：
    python tools/headless/run.py --task 启动游戏
    python tools/headless/run.py --task 开始培育 --option 培育难度=初 --option 「初」难度=LEGEND --option 培育偶像=十王星南
    python tools/headless/run.py --list-options 开始培育
"""

import os
import sys
import json
import time
import argparse
import subprocess
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "ci"))
from check_interface import exact_path, load_jsonc  # noqa: E402


def deep_merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def fill_inputs(value, inputs: dict):
    """把 input 类选项的 {name} 占位符替换为实际值；整串为占位符时按 pipeline_type 转换类型。"""
    if isinstance(value, dict):
        return {key: fill_inputs(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [fill_inputs(item, inputs) for item in value]
    if isinstance(value, str):
        for name, (raw, kind) in inputs.items():
            if value == "{" + name + "}":
                return int(raw) if kind == "int" else (raw.lower() == "true" if kind == "bool" else raw)
            value = value.replace("{" + name + "}", str(raw))
    return value


class Interface:
    def __init__(self, path: Path):
        self.path = path
        self.base = path.parent
        self.data = load_jsonc(path)
        self.tasks = list(self.data.get("task", []))
        self.options = dict(self.data.get("option", {}))
        for relative in self.data.get("import", []):
            imported = load_jsonc(exact_path(self.base, relative))
            self.tasks += imported.get("task", [])
            self.options.update(imported.get("option", {}))

    def task(self, name: str) -> dict:
        for task in self.tasks:
            if name in (task["name"], task.get("entry")):
                return task
        raise SystemExit(f"未找到任务 {name}；可用任务：{', '.join(t['name'] for t in self.tasks)}")

    def resource_paths(self, name: str) -> list:
        for item in self.data["resource"]:
            if item["name"] == name:
                return [exact_path(self.base, folder) for folder in item["path"]]
        raise SystemExit(f"未找到资源 {name}")

    def override_for(self, task: dict, choices: dict, inputs: dict, controller: str | None = None) -> tuple[dict, list]:
        """返回 (pipeline_override, 实际选中的 [(选项, case)])，顺序与 MFA 一致。"""
        override, chosen = deepcopy(task.get("pipeline_override", {})), []

        def visit(name: str):
            option = self.options[name]
            kind = option.get("type", "select")
            if kind == "input":
                values = {}
                for item in option.get("inputs", []):
                    key = f"{name}.{item['name']}"
                    values[item["name"]] = (inputs.get(key, item.get("default", "")), item.get("pipeline_type", "string"))
                deep_merge(override, fill_inputs(option.get("pipeline_override", {}), values))
                chosen.append((name, {k: v[0] for k, v in values.items()}))
                return
            if option.get("pipeline_override"):
                deep_merge(override, option["pipeline_override"])
            cases = option.get("cases", [])
            if not cases:
                return
            if kind == "checkbox":
                # 多选：命令行用逗号分隔，未指定时使用 default_case 列表
                wanted = choices[name].split(",") if name in choices else list(option.get("default_case", []))
                chosen.append((name, wanted))
                for case in cases:
                    if case["name"] in wanted:
                        deep_merge(override, case.get("pipeline_override", {}))
                        for child in case.get("option", []):
                            visit(child)
                return
            wanted = choices.get(name, option.get("default_case", cases[0]["name"]))
            case = next((c for c in cases if c["name"] == wanted), None)
            if case is None:
                raise SystemExit(f"选项「{name}」没有 case「{wanted}」；可选：{', '.join(c['name'] for c in cases)}")
            chosen.append((name, wanted))
            deep_merge(override, case.get("pipeline_override", {}))
            for child in case.get("option", []):
                visit(child)

        if controller:
            for item in self.data.get("controller", []):
                if item["name"] == controller:
                    for name in item.get("option", []):
                        visit(name)
        for name in task.get("option", []):
            visit(name)
        return override, chosen

    def describe(self, task: dict):
        def show(name: str, depth: int):
            option = self.options[name]
            cases = [c["name"] for c in option.get("cases", [])]
            print("  " * depth + f"- {name} ({option.get('type', 'select')}; 默认 {option.get('default_case', '-')}): {', '.join(cases)}")
            for case in option.get("cases", []):
                for child in case.get("option", []):
                    show(child, depth + 1)

        for name in task.get("option", []):
            show(name, 0)


def find_device(serial: str):
    from maa.toolkit import Toolkit

    for device in Toolkit.find_adb_devices():
        if device.address == serial:
            return device
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interface", type=Path, default=ROOT / "assets" / "interface.json")
    parser.add_argument("--task", help="任务名或 entry，例如 开始培育 / Produce")
    parser.add_argument("--resource", default="官服")
    parser.add_argument("--option", action="append", default=[], help="选项=case，可重复")
    parser.add_argument("--input", action="append", default=[], help="选项.字段=值，可重复（input 类选项）")
    parser.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    parser.add_argument("--serial", default="127.0.0.1:16384")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "debug" / "headless")
    parser.add_argument("--list-options", metavar="TASK")
    parser.add_argument("--dry-run", action="store_true", help="只打印 pipeline_override，不连接设备")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    interface = Interface(args.interface.resolve())
    if args.list_options:
        interface.describe(interface.task(args.list_options))
        return
    if not args.task:
        parser.error("需要 --task")

    task = interface.task(args.task)
    choices = dict(item.split("=", 1) for item in args.option)
    inputs = dict(item.split("=", 1) for item in args.input)
    override, chosen = interface.override_for(task, choices, inputs)
    print(f"任务 {task['name']} (entry {task['entry']})，资源 {args.resource}")
    for name, case in chosen:
        print(f"  {name} = {case}")
    if args.dry_run:
        print(json.dumps(override, ensure_ascii=False, indent=2))
        return

    from maa.tasker import Tasker
    from maa.toolkit import Toolkit
    from maa.resource import Resource
    from maa.controller import AdbController
    from maa.agent_client import AgentClient

    args.log_dir.mkdir(parents=True, exist_ok=True)
    Toolkit.init_option(str(args.log_dir))

    device = find_device(args.serial)
    if device:
        controller = AdbController(device.adb_path, device.address, device.screencap_methods, device.input_methods, device.config)
    else:
        controller = AdbController(args.adb, args.serial)
    if not controller.post_connection().wait().succeeded:
        raise SystemExit(f"无法连接设备 {args.serial}")

    resource = Resource()
    for path in interface.resource_paths(args.resource):
        if not resource.post_bundle(path).wait().succeeded:
            raise SystemExit(f"资源加载失败：{path}")

    client = AgentClient()
    client.bind(resource)
    agent = subprocess.Popen([sys.executable, "-u", str(ROOT / "agent" / "main.py"), client.identifier], cwd=ROOT)
    try:
        if not client.connect():
            raise SystemExit("Agent 连接失败")
        tasker = Tasker()
        tasker.bind(resource, controller)
        if not tasker.inited:
            raise SystemExit("Tasker 初始化失败")
        started = time.time()
        job = tasker.post_task(task["entry"], override)
        try:
            # 轮询而不是阻塞在原生 wait() 中，保证 Ctrl+C / SIGINT 能被及时处理
            while not job.done:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("收到中断，停止任务…", flush=True)
            tasker.post_stop().wait()
            raise
        detail = job.get()
        status = "成功" if job.succeeded else "失败"
        print(f"任务{status}，用时 {time.time() - started:.0f} 秒，最后节点：{detail.nodes[-1].name if detail and detail.nodes else '-'}")
        sys.exit(0 if job.succeeded else 1)
    finally:
        client.disconnect()
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()


if __name__ == "__main__":
    main()
