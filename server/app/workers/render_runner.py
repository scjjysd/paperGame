"""子进程入口：执行 render_character，产物搬运到契约布局，写 result.json。

退出码协议：0 = 业务终态已写入 result.json（ready/needs_correction/failed:ASSET_MISSING）；
非 0 = 基础设施故障（由主循环重试）。chdir 污染与 GLFW 崩溃被隔离在本子进程内。
"""
import json
import shutil
import sys
import traceback
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
MOTIONS = ('run', 'jump')


def relocate_artifacts(job_dir: Path) -> None:
    """render_character 产物在 work/input/** 下，搬运到 job_dir 根的契约布局。"""
    work_char = job_dir / 'work' / 'input'
    for m in MOTIONS:
        src = work_char / m / f'{m}.png'
        if src.exists():
            shutil.move(str(src), str(job_dir / f'{m}.png'))
    anno_src = work_char / 'anno'
    if anno_src.exists():
        anno_dst = job_dir / 'anno'
        if anno_dst.exists():
            shutil.rmtree(anno_dst)
        shutil.move(str(anno_src), str(anno_dst))
    shutil.rmtree(job_dir / 'work', ignore_errors=True)


def _load_joints(char_cfg: Path):
    import yaml
    if not char_cfg.exists():
        return []
    cfg = yaml.safe_load(char_cfg.read_text())
    return [{'name': j['name'], 'loc': [int(x) for x in j['loc']], 'parent': j['parent']}
            for j in cfg['skeleton']]


def run(job_dir: Path) -> int:
    job_id = job_dir.name
    from app.services.annotations import NeedsCorrection
    from app.services.character_pipeline import CharacterPipeline

    pipeline = CharacterPipeline(job_dir / 'work')
    try:
        result = pipeline.render_character(job_dir / 'input.png')
    except NeedsCorrection as e:
        relocate_artifacts(job_dir)
        payload = {'status': 'needs_correction', 'reason': e.reason,
                   'maskUrl': f'/artifacts/{job_id}/anno/mask.png',
                   'joints': _load_joints(job_dir / 'anno' / 'char_cfg.yaml')}
    except FileNotFoundError as e:
        payload = {'status': 'failed', 'code': 'ASSET_MISSING'}
    except Exception:
        traceback.print_exc()
        return 2
    else:
        relocate_artifacts(job_dir)
        animations = {m: {**result['animations'][m],
                          'spriteSheetUrl': f'/artifacts/{job_id}/{m}.png'} for m in MOTIONS}
        payload = {'status': 'ready', 'characterId': job_id, 'animations': animations}

    (job_dir / 'result.json').write_text(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1]).resolve()))
