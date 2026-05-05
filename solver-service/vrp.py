from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from models import Depot, Route, Stop, SubOrder, Vehicle


def solve(
    sub_orders: list[SubOrder],
    depots: list[Depot],                    # depots in the same order as in locations
    vehicles: list[Vehicle],
    distance_matrix: list[list[float]],    # km, indexed by vrp locations list
    locations: list[tuple[float, float]],  # depots first, then one entry per sub_order
    time_limit_seconds: int = 5,
) -> tuple[list[Route], bool]:
    """
    Solves MDVRP. Each vehicle starts and ends at its assigned depot.
    locations must be: [depot_0, ..., depot_k, sub_order_0, ..., sub_order_m]
    The caller (main.py) builds this list and the matching distance matrix.
    Split orders produce separate OR-Tools nodes so multiple vehicles can
    each make a partial delivery to the same address.
    """
    if not sub_orders:
        return [], False

    n_depots = len(depots)
    n_nodes = len(locations)
    n_vehicles = len(vehicles)

    # depots occupy indices 0..n_depots-1 (they come first in locations)
    depot_node_map: dict[str, int] = {d.warehouse_id: i for i, d in enumerate(depots)}

    # sub_orders occupy indices n_depots..n_depots+len(sub_orders)-1
    so_node_map: dict[str, int] = {
        so.sub_order_id: n_depots + i for i, so in enumerate(sub_orders)
    }

    # OR-Tools requires integer arc costs
    int_matrix = [[int(d * 1000) for d in row] for row in distance_matrix]

    starts = [depot_node_map[v.depot_id] for v in vehicles]
    ends   = [depot_node_map[v.depot_id] for v in vehicles]

    manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_idx, to_idx):
        return int_matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_idx = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    def demand_callback(from_idx):
        node = manager.IndexToNode(from_idx)
        sub_order_idx = node - n_depots
        if 0 <= sub_order_idx < len(sub_orders):
            return sub_orders[sub_order_idx].units
        return 0   # depot nodes carry zero demand

    demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx,
        0,
        [v.capacity_units for v in vehicles],
        True,
        "Capacity",
    )

    # Depot assignment: a sub_order allocated to depot A may only be served
    # by vehicles based at depot A.
    for so in sub_orders:
        node_idx = so_node_map[so.sub_order_id]
        allowed = [i for i, v in enumerate(vehicles) if v.depot_id == so.depot_id]
        routing_idx = manager.NodeToIndex(node_idx)
        routing.VehicleVar(routing_idx).SetValues(allowed)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = time_limit_seconds

    solution = routing.SolveWithParameters(params)

    if solution is None:
        raise ValueError(
            f"OR-Tools returned no solution (status={routing.status()}). "
            f"Check vehicle capacities and depot constraints."
        )

    # status 2 = ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED (time limit hit, not optimal)
    partial = routing.status() == 2
    so_by_node_idx = {n_depots + i: so for i, so in enumerate(sub_orders)}

    routes = []
    for v_idx, vehicle in enumerate(vehicles):
        stops = []
        total_distance_m = 0
        total_demand = 0

        index = routing.Start(v_idx)
        while not routing.IsEnd(index):
            next_index = solution.Value(routing.NextVar(index))
            total_distance_m += int_matrix[manager.IndexToNode(index)][manager.IndexToNode(next_index)]
            node = manager.IndexToNode(index)
            if so := so_by_node_idx.get(node):
                stops.append(Stop(
                    sub_order_id=so.sub_order_id,
                    lat=so.delivery_lat,
                    lon=so.delivery_lon,
                    road_geometry=[],   # filled in by planner-service after solve
                ))
                total_demand += so.units
            index = next_index

        if stops:
            routes.append(Route(
                vehicle_id=vehicle.vehicle_id,
                depot_id=vehicle.depot_id,
                stops=stops,
                total_distance_km=round(total_distance_m / 1000.0, 3),
                utilization_pct=round(total_demand / vehicle.capacity_units * 100, 1),
            ))

    return routes, partial
