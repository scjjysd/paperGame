import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services.motion_2d import FPS, synth_motion
from app.services.render_scene import render_animation, render_animations
from app.services.sprite_sheet import build_sprite_sheet


ROOT = Path(__file__).resolve().parents[1]
ANNO = ROOT / 'vendor' / 'AnimatedDrawings' / 'examples' / 'characters' / 'char3'


def _gif_frames(path):
    image = Image.open(path)
    frames, durations = [], []
    try:
        while True:
            frames.append(np.asarray(image.convert('RGBA')).copy())
            durations.append(image.info.get('duration'))
            image.seek(image.tell() + 1)
    except EOFError:
        return frames, durations


@pytest.mark.skipif(os.environ.get('RUN_RENDER_INTEGRATION') != '1',
                    reason='set RUN_RENDER_INTEGRATION=1 inside Mesa image')
def test_batch_render_matches_two_single_renders_pixel_for_pixel(tmp_path):
    specs, baseline, batch = [], {}, {}
    for motion in ('run', 'jump'):
        motion_dir = tmp_path / motion
        cfg = synth_motion(ANNO / 'char_cfg.yaml', motion, motion_dir)
        baseline[motion] = tmp_path / 'baseline' / (motion + '.gif')
        batch[motion] = tmp_path / 'batch' / motion / (motion + '.gif')
        render_animation(ANNO, cfg, baseline[motion], use_mesa=True)
        specs.append((motion, cfg, batch[motion]))

    rendered = render_animations(ANNO, specs, use_mesa=True)

    assert rendered == batch
    for motion in ('run', 'jump'):
        old_frames, old_duration = _gif_frames(baseline[motion])
        new_frames, new_duration = _gif_frames(batch[motion])
        assert old_duration == new_duration
        assert len(old_frames) == len(new_frames)
        for old, new in zip(old_frames, new_frames):
            np.testing.assert_array_equal(old, new)

        old_sheet = tmp_path / 'old' / (motion + '.png')
        new_sheet = tmp_path / 'new' / (motion + '.png')
        old_sheet.parent.mkdir(parents=True, exist_ok=True)
        new_sheet.parent.mkdir(parents=True, exist_ok=True)
        old_meta = build_sprite_sheet(baseline[motion], old_sheet, fps=FPS)
        new_meta = build_sprite_sheet(batch[motion], new_sheet, fps=FPS)
        assert old_meta == new_meta
        np.testing.assert_array_equal(np.asarray(Image.open(old_sheet)),
                                      np.asarray(Image.open(new_sheet)))
