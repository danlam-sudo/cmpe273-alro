import asyncio
import math

import httpx

ROUTING_URL = "http://routing-service:8002"
SOLVER_URL  = "http://solver-service:8003"


async def get_distance_matrix(
    locations: list[tuple[float, float]],
) -> tuple[list[list[float]], bool]:
    """
    Returns (matrix, is_fallback).
    Falls back to Haversine on timeout or connection error so planning
    continues even when routing-service is unavailable.
    """
    try:
        async with asyncio.timeout(2.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/distances",
                    json={"locations": locations},
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json()["matrix"], False
    except (asyncio.TimeoutError, httpx.ConnectError, httpx.TimeoutException):
        return _haversine_matrix(locations), True


async def get_route_geometries(
    segments: list[tuple],
) -> list[list[tuple]]:
    """
    Returns road-following polylines for each segment.
    Falls back to straight two-point lines on any failure.
    """
    try:
        async with asyncio.timeout(5.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/geometry",
                    json={"segments": segments},
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json()["paths"]
    except Exception:
        return [[origin, destination] for origin, destination in segments]


async def geocode(address: str) -> tuple[float, float]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ROUTING_URL}/geocode",
            json={"address": address},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data["lat"], data["lon"]


async def run_solve(
    orders, depots, vehicles, distance_matrix, locations,
) -> tuple[dict, bool]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SOLVER_URL}/solve",
            json={
                "orders": orders,
                "depots": depots,
                "vehicles": vehicles,
                "distance_matrix": distance_matrix,
                "locations": locations,
                "time_limit_seconds": 5,
            },
            timeout=12.0,   # longer than the solver budget to avoid premature client timeout
        )
        r.raise_for_status()
        data = r.json()
        return data, data.get("partial", False)


def _haversine_matrix(locations: list[tuple[float, float]]) -> list[list[float]]:
    n = len(locations)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                matrix[i][j] = _haversine(*locations[i], *locations[j])
    return matrix


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
