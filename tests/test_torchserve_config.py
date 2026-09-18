import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docker' / 'torchserve-entrypoint.sh'


def _run(tmp_path, workers):
    base = tmp_path / 'base.properties'
    runtime = tmp_path / 'runtime.properties'
    captured = tmp_path / 'captured.properties'
    base.write_text('load_models=all\n')
    fake = tmp_path / 'torchserve'
    fake.write_text(
        '#!/bin/sh\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--ts-config" ]; then cp "$2" "$CAPTURE_CONFIG"; exit 0; fi\n'
        '  shift\n'
        'done\n'
        'exit 9\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ,
           'TORCHSERVE_BIN': str(fake),
           'TORCHSERVE_BASE_CONFIG': str(base),
           'TORCHSERVE_RUNTIME_CONFIG': str(runtime),
           'CAPTURE_CONFIG': str(captured),
           'TORCHSERVE_WORKERS_PER_MODEL': workers}
    result = subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True)
    return result, captured.read_text() if captured.exists() else ''


@pytest.mark.parametrize('workers', ['', '1', '2', '08'])
def test_entrypoint_accepts_empty_or_positive_integer(tmp_path, workers):
    result, config = _run(tmp_path, workers)
    assert result.returncode == 0, result.stderr
    expected = '' if workers == '' else 'default_workers_per_model={}\n'.format(workers)
    assert config == 'load_models=all\n' + expected


@pytest.mark.parametrize('workers', ['0', '-1', '1.5', 'two', ' 2'])
def test_entrypoint_rejects_invalid_worker_count(tmp_path, workers):
    result, config = _run(tmp_path, workers)
    assert result.returncode != 0
    assert 'TORCHSERVE_WORKERS_PER_MODEL' in result.stderr
    assert config == ''


def test_compose_wires_optional_worker_count_and_readonly_entrypoint():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    service = compose['services']['torchserve']
    assert service['entrypoint'] == ['/torchserve-entrypoint.sh']
    assert service['environment']['TORCHSERVE_WORKERS_PER_MODEL'] == '${TORCHSERVE_WORKERS_PER_MODEL:-}'
    assert './docker/torchserve-entrypoint.sh:/torchserve-entrypoint.sh:ro' in service['volumes']
