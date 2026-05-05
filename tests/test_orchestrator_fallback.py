from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


async def test_returns_road_matrix_when_routing_available():
    mock_matrix = [[0.0, 5.2], [5.2, 0.0]]

    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        # httpx Response methods (raise_for_status, json) are synchronous — use MagicMock
        resp = MagicMock()
        resp.json.return_value = {"matrix": mock_matrix}
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)

        from orchestrator import get_distance_matrix
        matrix, is_fallback = await get_distance_matrix([(37.36, -121.92), (37.31, -121.89)])

    assert matrix == mock_matrix
    assert is_fallback is False


async def test_falls_back_to_haversine_on_connect_error():
    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("routing-service unreachable")
        )

        from orchestrator import get_distance_matrix
        locations = [(37.36, -121.92), (37.31, -121.89)]
        matrix, is_fallback = await get_distance_matrix(locations)

    assert is_fallback is True
    assert matrix[0][0] == pytest.approx(0.0)
    assert matrix[0][1] > 0.0
    assert matrix[0][1] == pytest.approx(matrix[1][0], rel=0.01)   # symmetric


async def test_fallback_matrix_dimensions_match_input():
    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("")
        )

        from orchestrator import get_distance_matrix
        locations = [(37.36, -121.92), (37.34, -121.91), (37.31, -121.89)]
        matrix, _ = await get_distance_matrix(locations)

    assert len(matrix) == 3
    assert all(len(row) == 3 for row in matrix)


async def test_geometry_falls_back_to_straight_lines_on_error():
    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("")
        )

        from orchestrator import get_route_geometries
        segments = [
            ((37.36, -121.92), (37.34, -121.91)),
            ((37.34, -121.91), (37.31, -121.89)),
        ]
        paths = await get_route_geometries(segments)

    assert len(paths) == 2
    # Each fallback path is the two endpoints
    assert paths[0] == [(37.36, -121.92), (37.34, -121.91)]
    assert paths[1] == [(37.34, -121.91), (37.31, -121.89)]
