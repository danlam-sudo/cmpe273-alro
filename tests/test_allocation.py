import numpy as np
import pytest

from allocation import allocate
from models import Depot, Order


def order(oid, lat, lon, units):
    return Order(order_id=oid, delivery_lat=lat, delivery_lon=lon, units=units)


def depot(wid, lat, lon, stock):
    return Depot(warehouse_id=wid, lat=lat, lon=lon, units_available=stock)


def test_single_depot_single_order():
    result = allocate(
        [order("O1", 37.34, -121.91, 50)],
        [depot("D1", 37.36, -121.92, 100)],
        np.array([[5.0]]),
    )
    assert len(result) == 1
    assert result[0].depot_id == "D1"
    assert result[0].parent_order_id == "O1"
    assert result[0].units == 50


def test_order_split_when_one_depot_insufficient():
    """80-unit order; each depot only has 50. LP must split across both."""
    result = allocate(
        [order("O1", 37.34, -121.91, 80)],
        [depot("D1", 37.36, -121.92, 50), depot("D2", 37.31, -121.89, 50)],
        np.array([[3.0], [7.0]]),   # D1 is closer → gets larger share
    )
    assert len(result) == 2
    assert sum(so.units for so in result) == 80
    d1_units = next(so.units for so in result if so.depot_id == "D1")
    d2_units = next(so.units for so in result if so.depot_id == "D2")
    assert d1_units > d2_units


def test_no_split_when_single_depot_covers_all():
    """One depot has enough for all orders — no splitting needed."""
    result = allocate(
        [order("O1", 37.34, -121.91, 20), order("O2", 37.32, -121.89, 15)],
        [depot("D1", 37.36, -121.92, 100), depot("D2", 37.31, -121.89, 100)],
        np.array([[3.0, 5.0], [8.0, 2.0]]),
    )
    from collections import Counter
    by_order = Counter(so.parent_order_id for so in result)
    # Each order should be served by exactly one depot
    assert all(v == 1 for v in by_order.values())


def test_infeasible_raises_value_error():
    with pytest.raises(ValueError, match="infeasible"):
        allocate(
            [order("O1", 37.34, -121.91, 200)],
            [depot("D1", 37.36, -121.92, 100)],
            np.array([[5.0]]),
        )


def test_all_orders_assigned_to_correct_depots():
    """Each sub_order's depot_id must match a real depot."""
    depots = [depot("D1", 37.36, -121.92, 100), depot("D2", 37.31, -121.89, 100)]
    orders = [order("O1", 37.34, -121.91, 30), order("O2", 37.32, -121.89, 20)]
    cost = np.array([[3.0, 5.0], [8.0, 2.0]])

    result = allocate(orders, depots, cost)
    valid_depot_ids = {d.warehouse_id for d in depots}
    assert all(so.depot_id in valid_depot_ids for so in result)
