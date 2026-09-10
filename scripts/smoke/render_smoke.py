"""容器内 Mesa 渲染尖刺：go/no-go 判据。用法（容器内）：python scripts/render_smoke.py <input_png> <out_dir>"""
import sys
from pathlib import Path

from PIL import Image

from app.services.annotations import analyze
from app.services.render_scene import VENDOR, render_animation


def main() -> int:
    input_png, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    analyze(input_png, out_dir / 'anno')
    gif = render_animation(out_dir / 'anno',
                           Path('/app/assets/motions/run.yaml'),
                           out_dir / 'smoke.gif',
                           use_mesa=True)
    img = Image.open(gif).convert('RGBA')
    corner = img.getpixel((0, 0))
    assert corner[3] == 0, f'GIF 角落不透明: {corner}'
    n_frames = getattr(img, 'n_frames', 1)
    print(f'MESA_SMOKE_PASS frames={n_frames} size={img.size} gif_bytes={gif.stat().st_size}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
