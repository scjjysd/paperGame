"""生成固定 seed 的合成关卡照片与对应真值。"""
import hashlib
import json
import math
import random
import shutil
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

GENERATOR_VERSION = "level-synthetic-1.0.0"
CANVAS_SIZE = (1280, 900)
PAPER_SIZE = (900, 560)
Point = Tuple[float, float]


def _solve(matrix: List[List[float]], values: List[float]) -> List[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, values)]
    size = len(augmented)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("degenerate perspective quadrilateral")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def _perspective_coefficients(source: Sequence[Point], target: Sequence[Point]) -> Tuple[float, ...]:
    matrix: List[List[float]] = []
    values: List[float] = []
    for (tx, ty), (sx, sy) in zip(target, source):
        matrix.append([tx, ty, 1.0, 0.0, 0.0, 0.0, -sx * tx, -sx * ty])
        values.append(sx)
        matrix.append([0.0, 0.0, 0.0, tx, ty, 1.0, -sy * tx, -sy * ty])
        values.append(sy)
    return tuple(_solve(matrix, values))


def _draw_paper(rng: random.Random, number: int) -> Tuple[Image.Image, Dict[str, object]]:
    width, height = PAPER_SIZE
    paper = Image.new("RGB", PAPER_SIZE, (249, 248, 241))
    draw = ImageDraw.Draw(paper)
    for _ in range(180):
        x, y = rng.randrange(width), rng.randrange(height)
        shade = rng.randrange(238, 250)
        draw.point((x, y), fill=(shade, shade, shade - 2))

    margin = 72 + (number % 3) * 8
    platform_y = [height - 90, height - 205, height - 320, height - 435]
    platforms = []
    for index, y in enumerate(platform_y):
        left = margin + (index * 47) % 100
        right = min(width - margin, left + 220 + ((number * 31 + index * 53) % 220))
        draw.line((left, y, right, y), fill=(20, 25, 30), width=10)
        if index % 2:
            draw.line((left + 8, y - 4, right - 8, y - 4), fill=(32, 93, 180), width=3)
        platforms.append({"id": "platform-%02d" % index,
                          "start": {"x": int(left), "y": int(y)},
                          "end": {"x": int(right), "y": int(y)},
                          "confidence": 0.99})

    start = {"x": int(margin + 34), "y": int(platform_y[0] - 44)}
    draw.ellipse((start["x"] - 12, start["y"] - 12,
                  start["x"] + 12, start["y"] + 12), fill=(35, 125, 55))
    goal = {"x": width - margin - 72, "y": platform_y[-1] - 92,
            "width": 52, "height": 76, "confidence": 0.98}
    draw.rectangle((goal["x"], goal["y"], goal["x"] + goal["width"],
                   goal["y"] + goal["height"]), outline=(215, 35, 35), width=5)
    flag_x = goal["x"] + goal["width"] // 2
    draw.line((flag_x, goal["y"] - 38, flag_x, goal["y"] + goal["height"]), fill=(150, 30, 30), width=4)
    draw.polygon([(flag_x, goal["y"] - 38), (flag_x + 38, goal["y"] - 25),
                  (flag_x, goal["y"] - 12)], fill=(218, 35, 35))
    return paper, {"sampleId": "synthetic-%02d" % number, "variant": "",
                   "rectifiedSize": {"width": width, "height": height},
                   "paperCorners": [], "playerStart": {**start, "source": "synthetic", "confidence": 1.0},
                   "platforms": platforms, "goalRegion": goal, "expectedStatus": "ready"}


def _paste_paper(photo: Image.Image, paper: Image.Image, corners: Sequence[Point]) -> Image.Image:
    source = [(0.0, 0.0), (float(PAPER_SIZE[0] - 1), 0.0),
              (float(PAPER_SIZE[0] - 1), float(PAPER_SIZE[1] - 1)), (0.0, float(PAPER_SIZE[1] - 1))]
    transformed = paper.transform(photo.size, Image.Transform.PERSPECTIVE,
                                  _perspective_coefficients(source, corners),
                                  resample=Image.Resampling.BICUBIC)
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).polygon([(int(x), int(y)) for x, y in corners], fill=255)
    return Image.composite(transformed, photo, mask)


def _rotate_point(point: Point, angle: float, center: Point) -> Point:
    radians = math.radians(angle)
    x, y = point[0] - center[0], point[1] - center[1]
    return (x * math.cos(radians) - y * math.sin(radians) + center[0],
            x * math.sin(radians) + y * math.cos(radians) + center[1])


def _sample(seed: int, number: int) -> Tuple[Image.Image, Dict[str, object]]:
    rng = random.Random(seed + number * 1009)
    paper, truth = _draw_paper(rng, number)
    photo = Image.new("RGB", CANVAS_SIZE, (182, 180, 174))
    draw = ImageDraw.Draw(photo)
    variant = ("front", "perspective", "rotated", "uneven_light")[number % 4]
    if variant == "front" or variant in ("rotated", "uneven_light"):
        corners = [(190.0, 150.0), (1090.0, 150.0), (1090.0, 710.0), (190.0, 710.0)]
    else:
        corners = [(180.0, 190.0), (1100.0, 112.0), (1140.0, 728.0), (135.0, 670.0)]
    if variant == "uneven_light":
        for y in range(CANVAS_SIZE[1]):
            shade = int(158 + 42 * y / CANVAS_SIZE[1])
            draw.line((0, y, CANVAS_SIZE[0], y), fill=(shade, shade, shade - 3))
    photo = _paste_paper(photo, paper, corners)
    if variant == "rotated":
        angle = 90 if number % 2 else -90
        photo = photo.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
        center = (CANVAS_SIZE[0] / 2.0, CANVAS_SIZE[1] / 2.0)
        corners = [_rotate_point(point, angle, center) for point in corners]
        truth["orientation"] = "portrait"
    else:
        truth["orientation"] = "landscape"
    truth["variant"] = variant
    truth["paperCorners"] = [{"x": round(x, 2), "y": round(y, 2)} for x, y in corners]
    truth["seed"] = seed
    return photo, truth


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_dataset(output_dir: Path, seed: int = 20260908, count: int = 10) -> None:
    if count <= 0:
        raise ValueError("count must be positive")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    samples = []
    for number in range(count):
        image_name, truth_name = "sample-%02d.png" % number, "sample-%02d.json" % number
        image, truth = _sample(seed, number)
        image_path = output_dir / image_name
        image.save(image_path, format="PNG", optimize=False, compress_level=9)
        _write_json(output_dir / truth_name, truth)
        samples.append({"id": truth["sampleId"], "variant": truth["variant"],
                        "image": image_name, "truth": truth_name,
                        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                        "paperCorners": truth["paperCorners"],
                        "platforms": truth["platforms"],
                        "goalRegion": truth["goalRegion"]})
    front = next(sample for sample in samples if sample["variant"] == "front")
    front_bytes = (output_dir / front["image"]).read_bytes()
    (output_dir / "front.png").write_bytes(front_bytes)
    _write_json(output_dir / "manifest.json", {"schemaVersion": "1.0",
        "generatorVersion": GENERATOR_VERSION, "seed": seed, "count": count,
        "imageSize": {"width": CANVAS_SIZE[0], "height": CANVAS_SIZE[1]},
        "front": {"image": "front.png", "sourceSampleId": front["id"],
                  "sha256": hashlib.sha256(front_bytes).hexdigest()},
        "samples": samples})


if __name__ == "__main__":
    generate_dataset(Path(__file__).resolve().parents[2] / "testdata/levels/synthetic", seed=20260908, count=10)
