"""关卡全栈 compose 与冒烟脚本的静态契约。"""
from pathlib import Path

import yaml


SERVER = Path(__file__).resolve().parents[1]


def test_compose_has_level_worker():
    compose = yaml.safe_load((SERVER / 'docker-compose.yml').read_text())
    service = compose['services']['level-worker']
    assert service['command'] == ['python', '-m', 'app.workers.level_worker']
    assert service['environment']['OUT_ROOT'] == '/data/out'
    assert service['volumes'] == ['./out:/data/out']


def test_smoke_script_checks_contract_artifacts():
    text = (SERVER / 'scripts/smoke/smoke_levels_e2e.sh').read_text()
    assert 'SMOKE_LEVELS_E2E_PASS' in text
    assert 'rectifiedImageUrl' in text
    assert 'levelJsonUrl' in text
    assert 'analysisJsonUrl' in text
