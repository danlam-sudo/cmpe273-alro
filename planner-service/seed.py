"""
Seed the in-memory store with demo data for San Jose on startup.
All coordinates are pre-resolved so geocoding is not needed at boot time.
"""
from store import store

INVENTORY = [
    {"warehouse_id": "W1", "lat": 37.3382, "lon": -121.8863, "units_available": 600},  # Downtown SJ
    {"warehouse_id": "W2", "lat": 37.4008, "lon": -121.9500, "units_available": 500},  # North SJ / Alviso
]

VEHICLES = [
    {"vehicle_id": "V1", "depot_id": "W1", "capacity_units": 200},
    {"vehicle_id": "V2", "depot_id": "W1", "capacity_units": 200},
    {"vehicle_id": "V3", "depot_id": "W2", "capacity_units": 200},
    {"vehicle_id": "V4", "depot_id": "W2", "capacity_units": 200},
]

ORDERS = [
    # West SJ
    {"delivery_address": "Santana Row, San Jose, CA",          "delivery_lat": 37.3197, "delivery_lon": -121.9475, "units": 40, "priority": "normal"},
    {"delivery_address": "Valley Fair Mall, San Jose, CA",     "delivery_lat": 37.3254, "delivery_lon": -121.9447, "units": 25, "priority": "high"},
    # South SJ
    {"delivery_address": "Willow Glen, San Jose, CA",          "delivery_lat": 37.3065, "delivery_lon": -121.8882, "units": 35, "priority": "normal"},
    {"delivery_address": "Blossom Hill, San Jose, CA",         "delivery_lat": 37.2398, "delivery_lon": -121.8623, "units": 30, "priority": "high"},
    {"delivery_address": "Almaden Valley, San Jose, CA",       "delivery_lat": 37.2611, "delivery_lon": -121.9024, "units": 20, "priority": "normal"},
    {"delivery_address": "Cambrian Park, San Jose, CA",        "delivery_lat": 37.2571, "delivery_lon": -121.9268, "units": 45, "priority": "normal"},
    # East SJ
    {"delivery_address": "Alum Rock, San Jose, CA",            "delivery_lat": 37.3727, "delivery_lon": -121.8355, "units": 30, "priority": "high"},
    {"delivery_address": "Evergreen, San Jose, CA",            "delivery_lat": 37.3106, "delivery_lon": -121.8035, "units": 25, "priority": "normal"},
    {"delivery_address": "Story Road, San Jose, CA",           "delivery_lat": 37.3353, "delivery_lon": -121.8488, "units": 20, "priority": "normal"},
    # North SJ
    {"delivery_address": "Berryessa, San Jose, CA",            "delivery_lat": 37.3849, "delivery_lon": -121.8700, "units": 35, "priority": "high"},
    {"delivery_address": "Montague Expressway, San Jose, CA",  "delivery_lat": 37.4012, "delivery_lon": -121.9073, "units": 50, "priority": "normal"},
    {"delivery_address": "Milpitas border, San Jose, CA",      "delivery_lat": 37.4154, "delivery_lon": -121.9008, "units": 15, "priority": "high"},
]


def seed():
    store.set_inventory(INVENTORY)
    store.set_vehicles(VEHICLES)
    store.add_orders(ORDERS)
