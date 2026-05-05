import pytest

from models import Depot, SubOrder, Vehicle
from vrp import solve


def sub_order(sid, parent, depot_id, lat, lon, units):
    return SubOrder(
        sub_order_id=sid, parent_order_id=parent,
        depot_id=depot_id, delivery_lat=lat, delivery_lon=lon, units=units,
    )


def depot(wid, lat, lon):
    return Depot(warehouse_id=wid, lat=lat, lon=lon, units_available=999)


def vehicle(vid, depot_id, cap):
    return Vehicle(vehicle_id=vid, depot_id=depot_id, capacity_units=cap)


def mat(n, off=10.0):
    return [[0.0 if i == j else off for j in range(n)] for i in range(n)]


def test_empty_sub_orders_returns_empty():
    routes, partial = solve([], [], [], [], [])
    assert routes == []
    assert partial is False


def test_single_route_correct_stop():
    depots  = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 50)]
    locs = [(37.36, -121.92), (37.34, -121.91)]

    routes, _ = solve(sub_orders, depots, vehicles, mat(2), locs)

    assert len(routes) == 1
    assert routes[0].vehicle_id == "V1"
    assert routes[0].depot_id == "D1"
    assert len(routes[0].stops) == 1
    assert routes[0].stops[0].sub_order_id == "SO1"


def test_depot_assignment_enforced():
    """V1 is from D1 and V2 is from D2; each must only serve its own sub_orders."""
    depots  = [depot("D1", 37.36, -121.92), depot("D2", 37.31, -121.89)]
    vehicles = [vehicle("V1", "D1", 100), vehicle("V2", "D2", 100)]
    sub_orders = [
        sub_order("SO1", "O1", "D1", 37.34, -121.91, 30),
        sub_order("SO2", "O2", "D2", 37.32, -121.88, 30),
    ]
    locs = [
        (37.36, -121.92),   # D1
        (37.31, -121.89),   # D2
        (37.34, -121.91),   # SO1
        (37.32, -121.88),   # SO2
    ]

    routes, _ = solve(sub_orders, depots, vehicles, mat(4), locs)

    so_lookup = {so.sub_order_id: so for so in sub_orders}
    for route in routes:
        for stop in route.stops:
            assert so_lookup[stop.sub_order_id].depot_id == route.depot_id, (
                f"Vehicle {route.vehicle_id} (depot {route.depot_id}) served "
                f"sub_order from depot {so_lookup[stop.sub_order_id].depot_id}"
            )


def test_per_route_distance_not_divided_by_n_vehicles():
    """With two vehicles but only one active route, distance must not be plan_total/2."""
    depots  = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100), vehicle("V2", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 10)]
    locs = [(37.36, -121.92), (37.34, -121.91)]
    m = [[0.0, 8.0], [8.0, 0.0]]   # exactly 8 km each way

    routes, _ = solve(sub_orders, depots, vehicles, m, locs)

    assert len(routes) == 1
    # Route: depot → stop → depot = 8 + 8 = 16 km
    assert routes[0].total_distance_km == pytest.approx(16.0, abs=0.5)


def test_utilization_pct():
    depots  = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 75)]
    locs = [(37.36, -121.92), (37.34, -121.91)]

    routes, _ = solve(sub_orders, depots, vehicles, mat(2), locs)

    assert routes[0].utilization_pct == pytest.approx(75.0)


def test_capacity_constraint_respected():
    """Two sub_orders totalling 90 units cannot fit in a 50-unit vehicle."""
    depots  = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 50), vehicle("V2", "D1", 50)]
    sub_orders = [
        sub_order("SO1", "O1", "D1", 37.34, -121.91, 45),
        sub_order("SO2", "O2", "D1", 37.32, -121.89, 45),
    ]
    locs = [(37.36, -121.92), (37.34, -121.91), (37.32, -121.89)]

    routes, _ = solve(sub_orders, depots, vehicles, mat(3), locs)

    # Each vehicle must carry at most 50 units
    for route in routes:
        total = sum(
            next(so.units for so in sub_orders if so.sub_order_id == s.sub_order_id)
            for s in route.stops
        )
        assert total <= 50
