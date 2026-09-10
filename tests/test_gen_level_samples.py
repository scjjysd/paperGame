import hashlib
import json
import sys
from pathlib import Path

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[1] / 'scripts' / 'tools'
sys.path.insert(0, str(TOOLS_DIR))

from gen_level_samples import generate_dataset  # noqa: E402


SEED = 20260908


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def test_generation_is_deterministic(tmp_path):
    generate_dataset(tmp_path, seed=SEED, count=10)
    first = _hashes(tmp_path)

    generate_dataset(tmp_path, seed=SEED, count=10)
    second = _hashes(tmp_path)

    assert first == second


def test_manifest_covers_required_variants_and_truth(tmp_path):
    generate_dataset(tmp_path, seed=SEED, count=10)
    manifest = json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))

    assert manifest['seed'] == SEED
    assert manifest['generatorVersion']
    assert len(manifest['samples']) == 10
    assert {'front', 'perspective', 'rotated', 'uneven_light'} <= {
        sample['variant'] for sample in manifest['samples']
    }
    front = manifest['front']
    source = next(sample for sample in manifest['samples'] if sample['id'] == front['sourceSampleId'])
    assert source['variant'] == 'front'
    assert front['image'] == 'front.png'
    assert (tmp_path / front['image']).read_bytes() == (tmp_path / source['image']).read_bytes()
    assert front['sha256'] == hashlib.sha256((tmp_path / 'front.png').read_bytes()).hexdigest()

    for sample in manifest['samples']:
        assert sample['image']
        assert sample['truth']
        assert sample['sha256'] == hashlib.sha256(
            (tmp_path / sample['image']).read_bytes()
        ).hexdigest()
        truth = json.loads((tmp_path / sample['truth']).read_text(encoding='utf-8'))
        assert len(truth['paperCorners']) == 4
        assert truth['platforms']
        assert truth['goalRegion']
        assert truth['expectedStatus'] in {'ready', 'needs_fix', 'needs_review'}


def test_generation_rejects_invalid_count(tmp_path):
    with pytest.raises(ValueError, match='count must be positive'):
        generate_dataset(tmp_path, seed=SEED, count=0)
