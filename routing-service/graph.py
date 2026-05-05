import math
import asyncio
from pathlib import Path

import networkx as nx
import osmnx as ox

GRAPH_PATH = Path("/data/sj_graph.graphml")
G: nx.MultiDiGraph | None = None
_node_cache: dict[int, dict] = {}
_distance_cache: dict[tuple, list] = {}   # tuple(node_ids) → matrix; tuple preserves ordering


async def load_graph():
    global G, _node_cache
    loop = asyncio.get_running_loop()
    G = await loop.run_in_executor(None, _load_or_download)
    _node_cache = {n: {"lat": d["y"], "lon": d["x"]} for n, d in G.nodes(data=True)}


def _load_or_download() -> nx.MultiDiGraph:
    if GRAPH_PATH.exists():
        return ox.load_graphml(GRAPH_PATH)
    graph = ox.graph_from_place("San Jose, California", network_type="drive")
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, GRAPH_PATH)
    return graph


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def snap_to_node(lat: float, lon: float) -> int:
    # osmnx takes (X=lon, Y=lat) — opposite of geographic convention
    return ox.nearest_nodes(G, lon, lat)


def compute_distance_matrix(locations: list[tuple[float, float]]) -> list[list[float]]:
    """
    locations: list of (lat, lon) pairs.
    Returns N×N matrix of road distances in km.
    Uses single-source Dijkstra per node: O(N) runs vs O(N²) pairwise queries.
    """
    nodes = [snap_to_node(lat, lon) for lat, lon in locations]
    cache_key = tuple(nodes)

    if cache_key in _distance_cache:
        return _distance_cache[cache_key]

    n = len(nodes)
    matrix = [[0.0] * n for _ in range(n)]

    for i, source in enumerate(nodes):
        lengths = nx.single_source_dijkstra_path_length(G, source, weight="length")
        for j, target in enumerate(nodes):
            if i != j:
                road_m = lengths.get(target)
                if road_m is not None:
                    matrix[i][j] = road_m / 1000.0
                else:
                    # Directed graph component gap — use Haversine × detour factor
                    matrix[i][j] = _haversine_km(*locations[i], *locations[j]) * 1.4

    _distance_cache[cache_key] = matrix
    return matrix
