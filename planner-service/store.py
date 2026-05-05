import datetime
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderRecord:
    order_id: str
    units: int
    priority: str
    delivery_address: Optional[str] = None
    delivery_lat: Optional[float] = None    # resolved by POST /data/orders via geocoding
    delivery_lon: Optional[float] = None
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())


class Store:
    def __init__(self):
        self._orders: dict[str, OrderRecord] = {}
        self._vehicles: dict[str, dict] = {}
        self._inventory: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}

    # ── Orders ──────────────────────────────────────────────────────────────────

    def add_orders(self, orders: list[dict]) -> list[str]:
        ids = []
        for o in orders:
            oid = o.get("order_id") or str(uuid.uuid4())
            fields = {k: v for k, v in o.items() if k != "order_id"}
            self._orders[oid] = OrderRecord(order_id=oid, **fields)
            ids.append(oid)
        return ids

    def get_orders(self, status: Optional[str] = None, priority: Optional[str] = None, address: Optional[str] = None) -> list[dict]:
        records = list(self._orders.values())
        if status:
            records = [r for r in records if r.status == status]
        if priority:
            records = [r for r in records if r.priority == priority]
        if address:
            records = [r for r in records if r.delivery_address and address.lower() in r.delivery_address.lower()]
        return [vars(r) for r in records]

    def patch_order(self, order_id: str, updates: dict):
        if order_id not in self._orders:
            raise KeyError(order_id)
        for k, v in updates.items():
            setattr(self._orders[order_id], k, v)

    # ── Vehicles / Inventory ─────────────────────────────────────────────────────

    def set_vehicles(self, vehicles: list[dict]):
        self._vehicles = {v["vehicle_id"]: v for v in vehicles}

    def set_inventory(self, inventory: list[dict]):
        self._inventory = {i["warehouse_id"]: i for i in inventory}

    def get_depots(self) -> list[dict]:
        return list(self._inventory.values())

    def get_vehicles(self) -> list[dict]:
        return list(self._vehicles.values())

    # ── Plans ────────────────────────────────────────────────────────────────────

    def save_plan(self, plan: dict) -> str:
        plan_id = str(uuid.uuid4())
        plan["plan_id"] = plan_id
        self._plans[plan_id] = plan
        return plan_id

    def get_plan(self, plan_id: str) -> Optional[dict]:
        return self._plans.get(plan_id)

    def get_latest_plan(self) -> Optional[dict]:
        if not self._plans:
            return None
        return self._plans[next(reversed(self._plans))]   # insertion-ordered in Python 3.7+


store = Store()
