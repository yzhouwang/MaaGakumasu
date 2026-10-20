"""静态检查 interface、任务配置与流水线之间的引用，不依赖 MaaFramework。

MaaFramework 加载资源时会校验 next 等节点引用，但不会检查以下内容，本脚本补足：

1. interface 的 import、languages、resource.path、welcome 路径存在，且大小写与磁盘一致；
2. 每个任务的 entry 节点在该任务可用的每个资源组合中存在；
3. 任务选项与控制器选项的 pipeline_override 目标节点在适用的每个资源组合中存在；
4. 覆盖层（如 zh_CN、DMM）新增的节点至少被引用一次，避免节点名拼写错误后静默变成新节点；
5. 流水线与 pipeline_override 引用的 Custom 识别/动作名称已在 agent/ 中注册；
6. 流水线与 pipeline_override 引用的模板图片和检测模型存在，且大小写一致。

用法（仓库根目录）：python tools/ci/check_interface.py
"""

import os
import re
import ast
import sys
import json
import argparse
from pathlib import Path
from functools import cache

ROOT = Path(__file__).resolve().parents[2]
JUMP_PREFIX = re.compile(r"^\[[A-Za-z]+\]")
NODE_LIST_KEYS = ("next", "on_error", "interrupt")
CUSTOM_KEYS = ("custom_recognition", "custom_action")
MODEL_FOLDERS = {"NeuralNetworkDetect": "detect", "NeuralNetworkClassify": "classify"}


def load_jsonc(path: Path):
    """读取 JSON；MaaFramework 允许注释，读取前去除字符串外的 // 与 /* */ 注释。"""
    text = path.read_text(encoding="utf-8-sig")
    result, index, in_string = [], 0, False
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if char == "\\":
                result.append(text[index + 1])
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif text.startswith("//", index):
            index = text.find("\n", index)
            index = len(text) if index < 0 else index
            continue
        elif text.startswith("/*", index):
            index = text.index("*/", index) + 2
            continue
        else:
            result.append(char)
        index += 1
    return json.loads("".join(result))


@cache
def _listdir(folder: str) -> frozenset:
    try:
        return frozenset(os.listdir(folder))
    except OSError:
        return frozenset()


def exact_path(base: Path, relative: str) -> Path | None:
    """按磁盘上的真实大小写逐级解析路径；任意一级大小写不符即视为不存在。"""
    current = base
    for part in Path(relative.replace("\\", "/")).parts:
        if part in (".", ""):
            continue
        if part == "..":
            current = current.parent
            continue
        if part not in _listdir(str(current)):
            return None
        current = current / part
    return current


def shown(path: Path) -> str:
    """输出相对当前目录的路径，便于在 CI 日志中定位；无法相对化时输出原路径。"""
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def strip_prefix(name: str) -> str:
    return JUMP_PREFIX.sub("", name)


def walk(value):
    """深度遍历 JSON 值，产出所有 dict。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def node_references(node: dict) -> set:
    names = set()
    for key in NODE_LIST_KEYS:
        value = node.get(key)
        for item in [value] if isinstance(value, str) else value or []:
            if isinstance(item, str):
                names.add(strip_prefix(item))
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
    return names


def templates_of(value) -> list:
    found = []
    for item in walk(value):
        template = item.get("template")
        for name in [template] if isinstance(template, str) else template or []:
            if isinstance(name, str) and "{" not in name:
                found.append(name)
    return found


def models_of(value) -> list:
    found = []
    for item in walk(value):
        # v2 写法为 {"type": ..., "param": {...}}，v1 写法为节点内平铺的 "recognition": "..."
        kind = item.get("type") if isinstance(item.get("type"), str) else item.get("recognition")
        param = item.get("param") if isinstance(item.get("param"), dict) else item
        if isinstance(kind, str) and kind in MODEL_FOLDERS and isinstance(param.get("model"), str):
            found.append((MODEL_FOLDERS[kind], param["model"]))
    return found


def customs_of(value) -> list:
    return [(key, item[key]) for item in walk(value) for key in CUSTOM_KEYS if isinstance(item.get(key), str)]


def registered_customs(agent_dir: Path) -> set:
    """扫描 AgentServer.custom_recognition/custom_action("名称") 的调用（含装饰器），不导入 maa。"""
    names = set()
    for path in agent_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in CUSTOM_KEYS
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                names.add((call.func.attr, call.args[0].value))
    return names


def python_strings(agent_dir: Path) -> set:
    strings = set()
    for path in agent_dir.rglob("*.py"):
        for item in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                strings.add(item.value)
    return strings


class Checker:
    def __init__(self, interface_path: Path, agent_dir: Path):
        self.interface_path = interface_path
        self.base = interface_path.parent
        self.agent_dir = agent_dir
        self.errors = []
        self.interface = load_jsonc(interface_path)
        self.tasks = list(self.interface.get("task", []))
        self.options = dict(self.interface.get("option", {}))
        self._load_imports()
        self.resources = {item["name"]: item for item in self.interface.get("resource", [])}
        self.layers = {}
        for name, item in self.resources.items():
            layers = []
            for folder in item.get("path", []):
                path = exact_path(self.base, folder)
                if path is None:
                    self.error(f"资源「{name}」的路径 {folder} 不存在或大小写不一致")
                else:
                    layers.append(path)
            self.layers[name] = layers

    def error(self, message: str):
        self.errors.append(message)

    def _load_imports(self):
        for relative in self.interface.get("import", []):
            path = exact_path(self.base, relative)
            if path is None:
                self.error(f"interface 的 import 路径 {relative} 不存在或大小写不一致")
                continue
            imported = load_jsonc(path)
            self.tasks += imported.get("task", [])
            self.options.update(imported.get("option", {}))

    @cache
    def _pipeline(self, layer: Path) -> dict:
        nodes = {}
        for path in sorted((layer / "pipeline").rglob("*.json")):
            for name, node in load_jsonc(path).items():
                nodes[name] = (node, path)
        return nodes

    def nodes(self, resource: str) -> dict:
        merged = {}
        for layer in self.layers[resource]:
            merged.update(self._pipeline(layer))
        return merged

    def resources_for(self, item: dict, allowed: set) -> set:
        """按 resource / controller 字段收窄适用的资源组合。"""
        result = set(allowed)
        if item.get("resource"):
            result &= set(item["resource"])
        if item.get("controller"):
            controllers = set(item["controller"])
            result = {name for name in result if controllers & set(self.resources[name].get("controller", []))}
        return result

    def check_paths(self):
        for key in ("welcome",):
            if self.interface.get(key) and exact_path(self.base, self.interface[key]) is None:
                self.error(f"interface 的 {key} 路径 {self.interface[key]} 不存在或大小写不一致")
        for language, relative in self.interface.get("languages", {}).items():
            if exact_path(self.base, relative) is None:
                self.error(f"语言 {language} 的路径 {relative} 不存在或大小写不一致")

    def collect_overrides(self) -> list:
        """返回 (来源说明, pipeline_override, 适用资源组合)。"""
        result, seen = [], set()

        def visit(option_name: str, allowed: set, origin: str):
            option = self.options.get(option_name)
            if option is None:
                self.error(f"{origin} 引用了不存在的选项「{option_name}」")
                return
            allowed = self.resources_for(option, allowed)
            key = (option_name, frozenset(allowed))
            if key in seen or not allowed:
                return
            seen.add(key)
            if option.get("pipeline_override"):
                result.append((f"选项「{option_name}」", option["pipeline_override"], allowed))
            for case in option.get("cases", []):
                label = f"选项「{option_name}」的「{case.get('name')}」"
                if case.get("pipeline_override"):
                    result.append((label, case["pipeline_override"], allowed))
                for child in case.get("option", []):
                    visit(child, allowed, label)

        all_resources = set(self.resources)
        for task in self.tasks:
            allowed = self.resources_for(task, all_resources)
            if task.get("pipeline_override"):
                result.append((f"任务「{task['name']}」", task["pipeline_override"], allowed))
            for name in task.get("option", []):
                visit(name, allowed, f"任务「{task['name']}」")
        for controller in self.interface.get("controller", []):
            allowed = {name for name in all_resources if controller["name"] in self.resources[name].get("controller", [])}
            for name in controller.get("option", []):
                visit(name, allowed, f"控制器「{controller['name']}」")
        return result

    def check_nodes(self, overrides: list):
        for task in self.tasks:
            for resource in sorted(self.resources_for(task, set(self.resources))):
                if task.get("entry") not in self.nodes(resource):
                    self.error(f"任务「{task['name']}」的入口 {task.get('entry')} 在资源「{resource}」中不存在")
        for origin, override, allowed in overrides:
            for resource in sorted(allowed):
                missing = sorted(name for name in override if name not in self.nodes(resource))
                for name in missing:
                    self.error(f"{origin} 覆盖的节点 {name} 在资源「{resource}」中不存在")

    def check_overlay_nodes(self, overrides: list):
        referenced = {task.get("entry") for task in self.tasks}
        referenced |= {name for _, override, _ in overrides for name in override}
        referenced |= python_strings(self.agent_dir)
        for resource in self.resources:
            nodes = self.nodes(resource)
            for node, _ in nodes.values():
                referenced |= node_references(node)
        reported = set()
        for resource, layers in self.layers.items():
            known = set()
            for layer in layers:
                for name, (_, path) in self._pipeline(layer).items():
                    if known and name not in known and name not in referenced and (name, path) not in reported:
                        reported.add((name, path))
                        self.error(f"{shown(path)} 新增的节点 {name} 不存在于前序资源层且未被引用，可能是节点名拼写错误")
                known |= set(self._pipeline(layer))

    def check_customs_and_files(self, overrides: list):
        registered = registered_customs(self.agent_dir)
        sources = []
        for resource in self.resources:
            for name, (node, path) in self.nodes(resource).items():
                sources.append((resource, f"{shown(path)} 的节点 {name}", node))
        for origin, override, allowed in overrides:
            for resource in allowed:
                sources.append((resource, origin, override))
        reported = set()
        for resource, origin, value in sources:
            problems = []
            for key, name in customs_of(value):
                if (key, name) not in registered:
                    problems.append(f"{key} {name} 未在 {shown(self.agent_dir)}/ 中注册")
            for template in templates_of(value):
                if not any(exact_path(layer / "image", template) for layer in self.layers[resource]):
                    problems.append(f"模板 {template} 在资源「{resource}」的 image 目录中不存在或大小写不一致")
            for folder, model in models_of(value):
                if not any(exact_path(layer / "model" / folder, model) for layer in self.layers[resource]):
                    problems.append(f"模型 model/{folder}/{model} 在资源「{resource}」中不存在或大小写不一致")
            for problem in problems:
                if (origin, problem) not in reported:
                    reported.add((origin, problem))
                    self.error(f"{origin}：{problem}")

    def run(self) -> int:
        self.check_paths()
        overrides = self.collect_overrides()
        self.check_nodes(overrides)
        self.check_overlay_nodes(overrides)
        self.check_customs_and_files(overrides)
        print(
            f"已检查 {len(self.tasks)} 个任务、{len(self.options)} 个选项、{len(self.resources)} 个资源组合、{len(overrides)} 处 pipeline_override"
        )
        for message in self.errors:
            print(f"error: {message}")
        print("检查通过" if not self.errors else f"共 {len(self.errors)} 个问题")
        return 1 if self.errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interface", type=Path, default=ROOT / "assets" / "interface.json")
    parser.add_argument("--agent", type=Path, default=ROOT / "agent")
    args = parser.parse_args()
    # 输出含中文；Windows 下重定向输出默认使用 cp1252 等本地编码，会抛出 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(Checker(args.interface.resolve(), args.agent.resolve()).run())


if __name__ == "__main__":
    main()
