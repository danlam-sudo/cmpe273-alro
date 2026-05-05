from dataclasses import dataclass


@dataclass
class Order:
    order_id: str
    delivery_lat: float
    delivery_lon: float
    units: int
    priority: str = "normal"


@dataclass
class Depot:
    warehouse_id: str
    lat: float
    lon: float
    units_available: int


@dataclass
class Vehicle:
    vehicle_id: str
    depot_id: str
    capacity_units: int


@dataclass
class SubOrder:
    sub_order_id: str
    parent_order_id: str
    depot_id: str
    delivery_lat: float
    delivery_lon: float
    units: int


@dataclass
class Stop:
    sub_order_id: str
    lat: float
    lon: float
    road_geometry: list[tuple[float, float]]


@dataclass
class Route:
    vehicle_id: str
    depot_id: str
    stops: list[Stop]
    total_distance_km: float
    utilization_pct: float
