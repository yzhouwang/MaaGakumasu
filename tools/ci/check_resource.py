import sys
import json

from typing import List
from pathlib import Path

from maa.resource import Resource
from maa.tasker import Tasker, LoggingLevelEnum


def check(dirs: List[Path]) -> bool:
    resource = Resource()

    print(f"Checking {len(dirs)} directories...")

    for dir in dirs:
        print(f"Checking {dir}...")
        status = resource.post_bundle(dir).wait().status
        if not status.succeeded:
            print(f"Failed to check {dir}.")
            return False

    print("All directories checked.")
    return True


def check_interface(interface: Path) -> bool:
    """按 interface.json 声明的每个资源组合依次加载，覆盖层与基础资源一起校验。"""
    with open(interface, "r", encoding="utf-8") as f:
        resources = json.load(f)["resource"]

    failed = []
    for item in resources:
        print(f"Checking resource {item['name']}...")
        if not check([interface.parent / path for path in item["path"]]):
            failed.append(item["name"])

    if failed:
        print(f"Failed resources: {', '.join(failed)}")
    return not failed


def main():
    if len(sys.argv) < 2:
        print("Usage: python check_resource.py <directory> [<directory> ...]")
        print("       python check_resource.py --interface <interface.json>")
        sys.exit(1)

    Tasker.set_stdout_level(LoggingLevelEnum.All)

    if sys.argv[1] == "--interface":
        # 资源名含中文；Windows 下重定向输出默认使用 cp1252 等本地编码，会抛出 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8")
        if len(sys.argv) != 3 or not check_interface(Path(sys.argv[2])):
            sys.exit(1)
        return

    dirs = [Path(arg) for arg in sys.argv[1:]]
    if not check(dirs):
        sys.exit(1)


if __name__ == "__main__":
    main()
