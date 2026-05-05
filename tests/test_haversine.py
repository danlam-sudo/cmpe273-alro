import pytest

from orchestrator import _haversine


def test_zero_same_point():
    assert _haversine(37.33, -121.89, 37.33, -121.89) == pytest.approx(0.0)


def test_symmetric():
    a = _haversine(37.36, -121.92, 37.31, -121.89)
    b = _haversine(37.31, -121.89, 37.36, -121.92)
    assert a == pytest.approx(b)


def test_positive_for_distinct_points():
    assert _haversine(37.36, -121.92, 37.31, -121.89) > 0.0


def test_sjc_to_sap_center():
    # SJC airport (37.3639, -121.9289) → SAP Center (37.3328, -121.9008): ~4.3 km straight-line
    dist = _haversine(37.3639, -121.9289, 37.3328, -121.9008)
    assert 3.5 < dist < 6.0


def test_matrix_diagonal_is_zero():
    from orchestrator import _haversine_matrix
    locs = [(37.36, -121.92), (37.34, -121.91), (37.31, -121.89)]
    m = _haversine_matrix(locs)
    for i in range(len(locs)):
        assert m[i][i] == pytest.approx(0.0)


def test_matrix_is_symmetric():
    from orchestrator import _haversine_matrix
    locs = [(37.36, -121.92), (37.34, -121.91), (37.31, -121.89)]
    m = _haversine_matrix(locs)
    for i in range(len(locs)):
        for j in range(len(locs)):
            assert m[i][j] == pytest.approx(m[j][i])
