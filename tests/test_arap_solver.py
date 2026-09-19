import logging
from pathlib import Path
import sys

import numpy as np
import pytest
import scipy.sparse as sp

VENDOR = Path(__file__).resolve().parents[1] / 'vendor' / 'AnimatedDrawings'
sys.path.insert(0, str(VENDOR))

from animated_drawings.model import arap


def test_regularized_solver_stabilizes_without_determinant_or_dense_identity(monkeypatch, caplog):
    """regularized 模式不得退回超大稠密 det/identity 路径。"""
    monkeypatch.delenv('RENDER_ARAP_SOLVER', raising=False)
    monkeypatch.setattr(np.linalg, 'det', lambda *_: pytest.fail('regularized must not call det'))
    monkeypatch.setattr(np, 'identity', lambda *_args, **_kwargs:
                        pytest.fail('regularized must not allocate dense identity'))
    caplog.set_level(logging.INFO)

    singular = np.array([[1.0, -1.0], [-1.0, 1.0]], dtype=np.float32)
    result, solve = arap._prepare_normal_system(singular, 'A1')

    assert sp.isspmatrix_csr(result)
    assert result.dtype == np.float64
    assert np.allclose(result.diagonal(), [1.00000001, 1.00000001], rtol=0, atol=1e-12)
    assert np.isfinite(solve(np.array([1.0, 0.0]))).all()
    assert 'float32_stabilization=1e-08' in caplog.text
    assert 'float64_fallback=true' in caplog.text


def test_vendor_solver_keeps_determinant_perturbation_path(monkeypatch):
    """vendor 回退必须保留原有奇异检测语义，方便线上快速止损。"""
    monkeypatch.setenv('RENDER_ARAP_SOLVER', 'vendor')
    determinants = iter((0.0, 1.0))
    calls = []

    def determinant(matrix):
        calls.append(matrix.copy())
        return next(determinants)

    monkeypatch.setattr(np.linalg, 'det', determinant)
    result, solve = arap._prepare_normal_system(
        np.zeros((2, 2), dtype=np.float32), 'A2')

    assert len(calls) == 2
    assert np.allclose(result.diagonal(), [1e-8, 1e-8], rtol=0, atol=1e-12)
    assert np.isfinite(solve(np.array([1.0, 0.0]))).all()


def test_regularized_nonsingular_system_keeps_upstream_spsolve_path(monkeypatch):
    """LU 仅用于探测奇异；正常逐帧求解必须保持上游 spsolve 数值路径。"""
    monkeypatch.setenv('RENDER_ARAP_SOLVER', 'regularized')
    calls = []
    original = arap.spla.spsolve

    def record_spsolve(matrix, rhs):
        calls.append((matrix, rhs))
        return original(matrix, rhs)

    monkeypatch.setattr(arap.spla, 'spsolve', record_spsolve)
    matrix = np.array([[2.0, -1.0], [-1.0, 2.0]], dtype=np.float32)
    prepared, solve = arap._prepare_normal_system(matrix, 'A1')
    result = solve(np.array([1.0, 0.0]))

    assert len(calls) == 1
    assert calls[0][0] is prepared
    assert np.allclose(result, [2.0 / 3.0, 1.0 / 3.0])


def test_arap_solver_rejects_unknown_mode(monkeypatch):
    monkeypatch.setenv('RENDER_ARAP_SOLVER', 'fast-ish')

    with pytest.raises(ValueError, match='RENDER_ARAP_SOLVER'):
        arap._prepare_normal_system(np.eye(2, dtype=np.float32), 'A1')
