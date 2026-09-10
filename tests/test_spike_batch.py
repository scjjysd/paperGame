import time
from pathlib import Path

import pytest

from app.services.character_pipeline import CharacterPipeline
from app.services.annotations import NeedsCorrection

SAMPLES = sorted((Path(__file__).parent.parent / 'testdata' / 'characters').glob('*.png'))
TIMEOUT_SEC = 60


@pytest.mark.skipif(not SAMPLES, reason='samples not downloaded')
def test_spike_batch_meets_acceptance_bar():
    pipeline = CharacterPipeline(Path(__file__).parent.parent / 'out' / 'spike')
    results = []
    for img in SAMPLES:
        t0 = time.time()
        try:
            pipeline.render(img, 'run')
            pipeline.render(img, 'jump')
            results.append((img.name, 'success', round(time.time() - t0, 1)))
        except NeedsCorrection as e:
            results.append((img.name, f'needs_correction:{e.reason}', round(time.time() - t0, 1)))
        except Exception as e:
            results.append((img.name, f'failed:{type(e).__name__}', round(time.time() - t0, 1)))

    for r in results:
        print(r)
    success = [r for r in results if r[1] == 'success' and r[2] <= TIMEOUT_SEC]
    assert len(SAMPLES) >= 20, f'only {len(SAMPLES)} samples, need 20'
    assert len(success) >= 16, f'acceptance bar not met: {len(success)}/20'
