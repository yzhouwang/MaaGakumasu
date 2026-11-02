"""从行动选择画面裁剪 P 道具图标，生成 MaaFramework 模板。

P 道具栏的图标形状不规则（星形、纸链），透明处露出背景，所以遮罩不用几何形状：取多张背景不同的帧，
图标框内各帧像素一致的位置保留，其余涂绿（green_mask）。

    python3 research/produce/build_item_templates.py --capture-root <目录>
    # <目录>/<run_id>/frames/<frame_sha256>.png 为 source.frames 列出的帧
"""

import json
import hashlib
import argparse
from pathlib import Path

from PIL import Image

# 各帧同一位置的颜色差（各通道）不超过此值时算作图标本身。
AGREEMENT = 12


def template_from(frames, box):
    left, top, width, height = box
    crops = [frame.convert("RGB").crop((left, top, left + width, top + height)) for frame in frames]
    template = crops[0].copy()
    pixels = template.load()
    others = [crop.load() for crop in crops[1:]]
    for y in range(height):
        for x in range(width):
            if any(max(abs(a - b) for a, b in zip(pixels[x, y], other[x, y])) > AGREEMENT for other in others):
                pixels[x, y] = (0, 255, 0)
    return template


def build(manifest_path, capture_root, image_root):
    manifest_path, capture_root, image_root = (Path(path).resolve() for path in (manifest_path, capture_root, image_root))
    registry = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in registry.get("items", []):
        source = entry["source"]
        frames = []
        for digest in source["frames"]:
            path = (capture_root / source["run_id"] / "frames" / (digest + ".png")).resolve()
            if not path.is_relative_to(capture_root):
                raise ValueError("Source frame escaped capture directory")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"Source checksum mismatch: {path}")
            frames.append(Image.open(path))
        target = (image_root / entry["template"]).resolve()
        if not target.is_relative_to(image_root):
            raise ValueError("Template escaped image directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        template_from(frames, source["box"]).save(target, format="PNG")
        print(entry["id"], hashlib.sha256(target.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("assets/data/battle_buff_templates.json"))
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, default=Path("assets/resource/base/image"))
    args = parser.parse_args()
    build(args.manifest, args.capture_root, args.image_root)


if __name__ == "__main__":
    main()
