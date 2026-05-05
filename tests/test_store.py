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


def test_vehicles_and_inventory_roundtrip(store):
    store.set_vehicles([{"vehicle_id": "V1", "depot_id": "D1", "capacity_units": 100}])
    store.set_inventory([{"warehouse_id": "D1", "lat": 37.36, "lon": -121.92, "units_available": 500}])

    assert len(store.get_vehicles()) == 1
    assert len(store.get_depots()) == 1
    assert store.get_depots()[0]["warehouse_id"] == "D1"
