import networkx as nx


def get_route_geometry(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> list[tuple[float, float]]:
    """
    Returns ordered (lat, lon) waypoints following actual road edges.
    Falls back to a straight two-point line if no road path exists.
    """
    from graph import G, snap_to_node

    o_node = snap_to_node(*origin)
    d_node = snap_to_node(*destination)

    try:
        route_nodes = nx.shortest_path(G, o_node, d_node, weight="length")
    except nx.NetworkXNoPath:
        return [origin, destination]

    return [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in route_nodes]
