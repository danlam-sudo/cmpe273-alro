import pytest

from store import Store


@pytest.fixture
def store():
    return Store()


def test_add_orders_returns_ids(store):
    ids = store.add_orders([{"units": 10, "priority": "normal"}])
    assert len(ids) == 1
    assert store.get_orders()[0]["order_id"] == ids[0]


def test_address_only_order_stores_null_coords(store):
    """Orders from AI chat arrive with address but no lat/lon — must not crash."""
    store.add_orders([{
        "delivery_address": "SAP Center, San Jose",
        "units": 5,
        "priority": "high",
    }])
    o = store.get_orders()[0]
    assert o["delivery_lat"] is None
    assert o["delivery_address"] == "SAP Center, San Jose"


def test_order_with_explicit_coords_preserved(store):
    store.add_orders([{
        "delivery_lat": 37.34,
        "delivery_lon": -121.91,
        "units": 10,
        "priority": "normal",
    }])
    o = store.get_orders()[0]
    assert o["delivery_lat"] == 37.34
    assert o["delivery_lon"] == -121.91


def test_status_filter(store):
    ids = store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 5, "priority": "normal"},
    ])
    store.patch_order(ids[0], {"status": "assigned"})

    assert len(store.get_orders(status="pending")) == 1
    assert len(store.get_orders(status="assigned")) == 1


def test_priority_filter(store):
    store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 5, "priority": "high"},
    ])
    assert len(store.get_orders(priority="high")) == 1
    assert len(store.get_orders(priority="normal")) == 1


def test_patch_missing_order_raises(store):
    with pytest.raises(KeyError):
        store.patch_order("does-not-exist", {"status": "assigned"})


def test_get_latest_plan_empty_returns_none(store):
    assert store.get_latest_plan() is None


def test_get_latest_plan_returns_most_recent(store):
    store.save_plan({"version": 1})
    store.save_plan({"version": 2})
    assert store.get_latest_plan()["version"] == 2


def test_get_plan_by_id(store):
    plan_id = store.save_plan({"data": "test"})
    plan = store.get_plan(plan_id)
    assert plan is not None
    assert plan["plan_id"] == plan_id


def test_units_min_filter(store):
    store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 50, "priority": "normal"},
        {"units": 100, "priority": "high"},
    ])
    assert len(store.get_orders(units_min=50)) == 2
    assert len(store.get_orders(units_min=101)) == 0


def test_units_max_filter(store):
    store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 50, "priority": "normal"},
        {"units": 100, "priority": "high"},
    ])
    assert len(store.get_orders(units_max=50)) == 2
    assert len(store.get_orders(units_max=9)) == 0


def test_units_range_filter(store):
    store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 50, "priority": "normal"},
        {"units": 100, "priority": "high"},
    ])
    result = store.get_orders(units_min=20, units_max=80)
    assert len(result) == 1
    assert result[0]["units"] == 50


def test_sort_by_units_asc(store):
    store.add_orders([
        {"units": 80, "priority": "normal"},
        {"units": 10, "priority": "normal"},
        {"units": 40, "priority": "normal"},
    ])
    result = store.get_orders(sort_by="units", sort_order="asc")
    assert [r["units"] for r in result] == [10, 40, 80]


def test_sort_by_units_desc(store):
    store.add_orders([
        {"units": 80, "priority": "normal"},
        {"units": 10, "priority": "normal"},
        {"units": 40, "priority": "normal"},
    ])
    result = store.get_orders(sort_by="units", sort_order="desc")
    assert [r["units"] for r in result] == [80, 40, 10]


def test_sort_by_priority_puts_high_first(store):
    store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 20, "priority": "high"},
        {"units": 30, "priority": "normal"},
    ])
    result = store.get_orders(sort_by="priority", sort_order="asc")
    assert result[0]["priority"] == "high"


def test_limit(store):
    store.add_orders([{"units": i, "priority": "normal"} for i in range(10)])
    assert len(store.get_orders(limit=3)) == 3


def test_depot_id_filter_no_plan_raises(store):
    store.add_orders([{"units": 10, "priority": "normal"}])
    with pytest.raises(LookupError, match="no_plan"):
        store.get_orders(depot_id="W1")


def test_depot_id_filter_uses_latest_plan(store):
    ids = store.add_orders([
        {"units": 10, "priority": "normal"},
        {"units": 20, "priority": "normal"},
    ])
    # Simulate a completed plan where only the first order was assigned to W1
    store.save_plan({
        "routes": [],
        "sub_orders": [
            {"sub_order_id": "SO1", "parent_order_id": ids[0], "depot_id": "W1",
             "delivery_lat": 37.34, "delivery_lon": -121.91, "units": 10},
        ],
        "kpis": {},
    })
    result = store.get_orders(depot_id="W1")
    assert len(result) == 1
    assert result[0]["order_id"] == ids[0]


def test_depot_id_filter_includes_split_orders(store):
    """An order split across two depots should appear under both depot filters."""
    ids = store.add_orders([{"units": 80, "priority": "normal"}])
    store.save_plan({
        "routes": [],
        "sub_orders": [
            {"sub_order_id": "SO1", "parent_order_id": ids[0], "depot_id": "W1",
             "delivery_lat": 37.34, "delivery_lon": -121.91, "units": 50},
            {"sub_order_id": "SO2", "parent_order_id": ids[0], "depot_id": "W2",
             "delivery_lat": 37.34, "delivery_lon": -121.91, "units": 30},
        ],
        "kpis": {},
    })
    assert len(store.get_orders(depot_id="W1")) == 1
    assert len(store.get_orders(depot_id="W2")) == 1


def test_created_after_filter(store):
    store.add_orders([{"units": 10, "priority": "normal"}])
    # Future timestamp — should exclude all existing orders
    future = "2099-01-01T00:00:00+00:00"
    assert store.get_orders(created_after=future) == []
    # Past timestamp — should include all
    past = "2000-01-01T00:00:00+00:00"
    assert len(store.get_orders(created_after=past)) == 1


def test_vehicles_and_inventory_roundtrip(store):
    store.set_vehicles([{"vehicle_id": "V1", "depot_id": "D1", "capacity_units": 100}])
    store.set_inventory([{"warehouse_id": "D1", "lat": 37.36, "lon": -121.92, "units_available": 500}])

    assert len(store.get_vehicles()) == 1
    assert len(store.get_depots()) == 1
    assert store.get_depots()[0]["warehouse_id"] == "D1"
