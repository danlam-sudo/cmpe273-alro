# Autonomous Logistics & Routing Optimizer
## Technical Specification — v2

*Implementation Guide: Methods, Patterns, and Integration*
Distributed Systems Course Project · ALRO v2.0

---

## 0. Repository Structure

```
alro/
├── routing-service/
│   ├── main.py
│   ├── graph.py          # OSMnx graph load, cache, Dijkstra
│   ├── geocoding.py      # address → snapped lat/lon
│   ├── geometry.py       # route polyline extraction
│   ├── Dockerfile
│   └── requirements.txt
├── solver-service/
│   ├── main.py
│   ├── allocation.py     # scipy LP Transportation Problem
│   ├── vrp.py            # OR-Tools MDVRP
│   ├── models.py         # shared data classes
│   ├── Dockerfile
│   └── requirements.txt
├── planner-service/
│   ├── main.py
│   ├── store.py          # in-memory data store
│   ├── orchestrator.py   # coordinate downstream calls + fallbacks
│   ├── haversine.py      # fallback distance matrix
│   ├── Dockerfile
│   └── requirements.txt
├── ai-service/
│   ├── main.py
│   ├── tools.py          # tool schemas + dispatch logic
│   ├── session.py        # conversation history store
│   ├── Dockerfile
│   └── requirements.txt
├── dashboard/
│   ├── app.py
│   ├── map_render.py     # Folium map construction
│   ├── api_client.py     # typed wrappers around planner + ai endpoints
│   └── requirements.txt
├── tests/
│   ├── conftest.py
│   ├── test_allocation.py
│   ├── test_vrp.py
│   ├── test_store.py
│   ├── test_haversine.py
│   └── test_orchestrator_fallback.py
├── infra/
│   ├── otel-collector-config.yaml
│   └── prometheus.yml
└── docker-compose.yml
```

---

## 1. Shared Conventions

Every service implements the same scaffold before any business logic is written.
This section defines the exact pattern used identically across all four services.

### 1.1 Dependencies (all services)

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
opentelemetry-sdk>=1.24.0
opentelemetry-exporter-otlp-proto-grpc>=1.24.0
opentelemetry-instrumentation-fastapi>=0.45b0
opentelemetry-instrumentation-httpx>=0.45b0
prometheus-fastapi-instrumentator>=6.1.0
structlog>=24.1.0
```

### 1.2 FastAPI app with lifespan, health, and metrics

Every `main.py` follows this structure:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
import structlog
import os

log = structlog.get_logger()

def setup_telemetry(service_name: str):
    provider = TracerProvider()
    exporter = OTLPSpanExporter(
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"),
        insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()   # propagates trace context in outbound httpx calls

is_ready = False   # set to True in lifespan after service-specific init

@asynccontextmanager
async def lifespan(app: FastAPI):
    global is_ready
    # --- service-specific init goes here ---
    is_ready = True
    log.info("service_ready")
    yield
    # --- cleanup goes here ---

app = FastAPI(lifespan=lifespan)
setup_telemetry("routing-service")           # pass this service's name
FastAPIInstrumentor.instrument_app(app)      # auto-instruments all endpoints
Instrumentator().instrument(app).expose(app) # exposes GET /metrics

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ready")
def ready():
    if not is_ready:
        from fastapi import Response
        return Response(status_code=503, content="not ready")
    return {"status": "ready"}
```

### 1.3 Structured logging with trace context

```python
import structlog
from opentelemetry import trace

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)

def get_logger(name: str):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    return structlog.get_logger().bind(
        service=name,
        trace_id=format(ctx.trace_id, "032x") if ctx.is_valid else None,
        span_id=format(ctx.span_id, "016x") if ctx.is_valid else None,
    )
```

Call `get_logger(__name__).info("event", key=value)` throughout. Every log line
carries `trace_id` and `span_id` for Jaeger correlation.

### 1.4 Dockerfile (identical pattern, port varies)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
```

---

## 2. routing-service (Port 8002)

### 2.1 Dependencies

```
osmnx>=1.9.0
networkx>=3.3
shapely>=2.0
numpy>=1.26
```

### 2.2 Graph lifecycle (`graph.py`)

```python
import osmnx as ox
import networkx as nx
from pathlib import Path
import asyncio

GRAPH_PATH = Path("/data/sj_graph.graphml")
G: nx.MultiDiGraph | None = None
_node_cache: dict[int, dict] = {}           # node_id → {lat, lon}
_distance_cache: dict[tuple, list] = {}     # tuple(node_ids) → matrix (ordered — frozenset loses ordering)

async def load_graph():
    global G, _node_cache
    loop = asyncio.get_running_loop()    # get_event_loop() is deprecated in 3.10+
    # Run blocking I/O off the event loop
    G = await loop.run_in_executor(None, _load_or_download)
    _node_cache = {n: {"lat": d["y"], "lon": d["x"]} for n, d in G.nodes(data=True)}

def _load_or_download() -> nx.MultiDiGraph:
    if GRAPH_PATH.exists():
        return ox.load_graphml(GRAPH_PATH)
    graph = ox.graph_from_place("San Jose, California", network_type="drive")
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, GRAPH_PATH)
    return graph
```

Call `await load_graph()` inside the lifespan function before setting `is_ready = True`.

### 2.3 Distance matrix (`graph.py`)

The naive all-pairs approach (one query per location pair) is O(N²) and too slow for
a live demo. Single-source Dijkstra from each location is O(N) Dijkstra runs over a
graph of fixed size — fast enough for 30–50 locations on San Jose scale (2–5 seconds).

```python
def snap_to_node(lat: float, lon: float) -> int:
    return ox.nearest_nodes(G, lon, lat)   # note: osmnx takes (lon, lat)

def compute_distance_matrix(locations: list[tuple[float, float]]) -> list[list[float]]:
    """
    locations: list of (lat, lon) pairs
    returns: N×N matrix of road distances in km
    """
    nodes = [snap_to_node(lat, lon) for lat, lon in locations]
    cache_key = tuple(nodes)   # tuple preserves row/column ordering; frozenset would not

    if cache_key in _distance_cache:
        return _distance_cache[cache_key]

    n = len(nodes)
    node_set = set(nodes)
    matrix = [[0.0] * n for _ in range(n)]

    for i, source in enumerate(nodes):
        # Single Dijkstra from source — returns dict {node: distance_in_meters}
        lengths = nx.single_source_dijkstra_path_length(G, source, weight="length")
        for j, target in enumerate(nodes):
            if i != j:
                matrix[i][j] = lengths.get(target, float("inf")) / 1000.0  # → km

    _distance_cache[cache_key] = matrix
    return matrix
```

**Important:** `nx.single_source_dijkstra_path_length` traverses the full graph from
source and returns distances to ALL reachable nodes. Extract only the distances to the
target nodes afterwards — do not call it pairwise.

### 2.4 Route geometry (`geometry.py`)

```python
import networkx as nx

def get_route_geometry(
    origin: tuple[float, float],
    destination: tuple[float, float]
) -> list[tuple[float, float]]:
    """
    Returns ordered list of (lat, lon) waypoints following actual roads.
    """
    from graph import G, snap_to_node

    o_node = snap_to_node(*origin)
    d_node = snap_to_node(*destination)

    try:
        route_nodes = nx.shortest_path(G, o_node, d_node, weight="length")
    except nx.NetworkXNoPath:
        # Straight line fallback if no road path found
        return [origin, destination]

    return [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in route_nodes]
```

### 2.5 Geocoding (`geocoding.py`)

```python
import osmnx as ox
from graph import G, snap_to_node

def geocode_address(address: str) -> tuple[float, float]:
    """
    Converts address string to snapped road-graph coordinates.
    Appends ", San Jose, CA" if not already present to bias results.
    """
    if "san jose" not in address.lower() and "ca" not in address.lower():
        address = f"{address}, San Jose, CA"

    lat, lon = ox.geocode(address)    # Nominatim via OSMnx
    node = snap_to_node(lat, lon)
    return G.nodes[node]["y"], G.nodes[node]["x"]
```

`ox.geocode` is synchronous and makes an HTTP call to Nominatim. Run it in an
executor to avoid blocking the event loop:

```python
import asyncio

async def geocode_async(address: str) -> tuple[float, float]:
    loop = asyncio.get_running_loop()    # get_event_loop() is deprecated in 3.10+
    return await loop.run_in_executor(None, geocode_address, address)
```

### 2.6 Endpoints (`main.py`)

```python
from pydantic import BaseModel

class DistanceRequest(BaseModel):
    locations: list[tuple[float, float]]   # [(lat, lon), ...]

class GeometryRequest(BaseModel):
    segments: list[tuple[tuple[float, float], tuple[float, float]]]

class GeocodeRequest(BaseModel):
    address: str

@app.post("/distances")
async def distances(req: DistanceRequest):
    from graph import compute_distance_matrix
    import asyncio
    loop = asyncio.get_running_loop()
    matrix = await loop.run_in_executor(None, compute_distance_matrix, req.locations)
    return {"matrix": matrix}

@app.post("/geometry")
async def geometry(req: GeometryRequest):
    from geometry import get_route_geometry
    import asyncio
    loop = asyncio.get_running_loop()
    results = []
    for origin, destination in req.segments:
        path = await loop.run_in_executor(None, get_route_geometry, origin, destination)
        results.append(path)
    return {"paths": results}

@app.post("/geocode")
async def geocode(req: GeocodeRequest):
    from geocoding import geocode_async
    lat, lon = await geocode_async(req.address)
    return {"lat": lat, "lon": lon}
```

All three endpoints wrap synchronous NetworkX/OSMnx calls in `run_in_executor`
to keep the FastAPI event loop unblocked.

---

## 3. solver-service (Port 8003)

### 3.1 Dependencies

```
ortools>=9.10.4067
scipy>=1.13.0
numpy>=1.26.0
```

### 3.2 Data models (`models.py`)

```python
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
```

### 3.3 Phase 1 — Inventory Allocation (`allocation.py`)

```python
import numpy as np
from scipy.optimize import linprog
from models import Order, Depot, SubOrder
import uuid, math

def allocate(
    orders: list[Order],
    depots: list[Depot],
    cost_matrix: np.ndarray      # shape (n_depots, n_orders), Haversine km
) -> list[SubOrder]:
    """
    Solves Transportation LP to assign depot inventory to orders.
    Automatically splits orders that exceed any single depot's stock.

    Variables: x[i,j] = units shipped from depot i to order j
    Minimize:  sum(cost[i,j] * x[i,j])
    Subject to:
      sum_i(x[i,j]) == order[j].units   for all j  (demand met exactly)
      sum_j(x[i,j]) <= depot[i].stock   for all i  (supply not exceeded)
      x[i,j] >= 0
    """
    n_d, n_o = len(depots), len(orders)

    # Flatten cost matrix as objective vector
    c = cost_matrix.flatten()

    # Demand equality: for each order j, sum over depots == units
    A_eq = np.zeros((n_o, n_d * n_o))
    b_eq = np.array([o.units for o in orders], dtype=float)
    for j in range(n_o):
        for i in range(n_d):
            A_eq[j, i * n_o + j] = 1.0

    # Supply inequality: for each depot i, sum over orders <= stock
    A_ub = np.zeros((n_d, n_d * n_o))
    b_ub = np.array([d.units_available for d in depots], dtype=float)
    for i in range(n_d):
        for j in range(n_o):
            A_ub[i, i * n_o + j] = 1.0

    bounds = [(0.0, None)] * (n_d * n_o)

    result = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                     bounds=bounds, method="highs")

    if not result.success:
        raise ValueError(f"Inventory allocation infeasible: {result.message}. "
                         f"Total demand: {sum(o.units for o in orders)}, "
                         f"Total supply: {sum(d.units_available for d in depots)}")

    allocation = result.x.reshape(n_d, n_o)
    THRESHOLD = 0.5   # ignore allocations below half a unit (numerical noise)

    sub_orders = []
    for i, depot in enumerate(depots):
        for j, order in enumerate(orders):
            qty = allocation[i, j]
            if qty >= THRESHOLD:
                sub_orders.append(SubOrder(
                    sub_order_id=str(uuid.uuid4()),
                    parent_order_id=order.order_id,
                    depot_id=depot.warehouse_id,
                    delivery_lat=order.delivery_lat,
                    delivery_lon=order.delivery_lon,
                    units=round(qty)
                ))

    return sub_orders
```

**Pre-flight check:** Before calling `linprog`, verify
`sum(orders.units) <= sum(depots.stock)`. If not, return a 422 immediately with
a clear message — `linprog` will fail with an obscure message otherwise.

### 3.4 Phase 2 — MDVRP Solver (`vrp.py`)

```python
from ortools.constraint_solver import routing_enums_pb2, pywrapcp
from models import SubOrder, Depot, Vehicle, Route, Stop

def solve(
    sub_orders: list[SubOrder],
    depots: list[Depot],                   # depots in the same order as in locations
    vehicles: list[Vehicle],
    distance_matrix: list[list[float]],   # km, indexed by vrp locations list
    locations: list[tuple[float, float]], # depots first, then one entry per sub_order
    time_limit_seconds: int = 5
) -> tuple[list[Route], bool]:
    """
    Solves MDVRP. Each vehicle starts and ends at its assigned depot.
    locations must be: [depot_0, ..., depot_k, sub_order_0, ..., sub_order_m]
    The caller builds this list and the matching distance matrix.
    """
    if not sub_orders:
        return [], False

    n_depots = len(depots)
    n_nodes = len(locations)
    n_vehicles = len(vehicles)

    # depots sit at indices 0..n_depots-1; straightforward since they come first
    depot_node_map: dict[str, int] = {d.warehouse_id: i for i, d in enumerate(depots)}

    # sub_orders sit at indices n_depots..n_depots+len(sub_orders)-1
    so_node_map: dict[str, int] = {
        so.sub_order_id: n_depots + i for i, so in enumerate(sub_orders)
    }

    # Convert km to integer meters (OR-Tools requires integers)
    int_matrix = [
        [int(d * 1000) for d in row]
        for row in distance_matrix
    ]

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
        return 0   # depot nodes have zero demand

    demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx,
        0,
        [v.capacity_units for v in vehicles],
        True,
        "Capacity"
    )

    # Depot assignment: a sub-order allocated to depot A may only be served by vehicles from depot A
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

    # routing.status() values: 0=not solved, 1=success, 2=fail, 3=partial, 4=invalid
    if solution is None or routing.status() not in (1, 3):
        raise ValueError(
            f"OR-Tools could not find a feasible solution "
            f"(status={routing.status()}). Check capacity and depot constraints."
        )

    partial = routing.status() == 3
    so_by_node_idx = {n_depots + i: so for i, so in enumerate(sub_orders)}

    routes = []
    for v_idx, vehicle in enumerate(vehicles):
        stops = []
        total_distance_m = 0
        total_demand = 0

        index = routing.Start(v_idx)
        while not routing.IsEnd(index):
            next_index = solution.Value(routing.NextVar(index))
            # Accumulate arc distance for this vehicle's actual route
            total_distance_m += int_matrix[manager.IndexToNode(index)][manager.IndexToNode(next_index)]
            node = manager.IndexToNode(index)
            if so := so_by_node_idx.get(node):
                stops.append(Stop(
                    sub_order_id=so.sub_order_id,
                    lat=so.delivery_lat,
                    lon=so.delivery_lon,
                    road_geometry=[]   # filled in by planner-service after solve
                ))
                total_demand += so.units
            index = next_index

        if stops:
            routes.append(Route(
                vehicle_id=vehicle.vehicle_id,
                depot_id=vehicle.depot_id,
                stops=stops,
                total_distance_km=round(total_distance_m / 1000.0, 3),
                utilization_pct=round(total_demand / vehicle.capacity_units * 100, 1)
            ))

    return routes, partial
```

### 3.5 Solver endpoint (`main.py`)

```python
from pydantic import BaseModel
import math, asyncio

class SolveRequest(BaseModel):
    orders: list[dict]
    depots: list[dict]
    vehicles: list[dict]
    distance_matrix: list[list[float]]   # depots+orders indexed, from routing-service
    locations: list[tuple[float, float]] # depots first, then orders (same order)
    time_limit_seconds: int = 5

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@app.post("/solve")
async def solve_endpoint(req: SolveRequest):
    from models import Order, Depot, Vehicle
    from allocation import allocate
    from vrp import solve
    import numpy as np

    orders   = [Order(**o) for o in req.orders]
    depots   = [Depot(**d) for d in req.depots]
    vehicles = [Vehicle(**v) for v in req.vehicles]

    # Phase 1: Haversine cost matrix for LP (routing-service matrix not yet available
    # at allocation time; Haversine is accurate enough for inventory assignment)
    n_d, n_o = len(depots), len(orders)
    cost_matrix = np.zeros((n_d, n_o))
    for i, depot in enumerate(depots):
        for j, order in enumerate(orders):
            cost_matrix[i, j] = _haversine(
                depot.lat, depot.lon, order.delivery_lat, order.delivery_lon
            )

    loop = asyncio.get_running_loop()
    sub_orders = await loop.run_in_executor(None, allocate, orders, depots, cost_matrix)

    # Build OR-Tools location list: depots first, then one node per sub_order.
    # Split orders produce multiple sub_orders at the same lat/lon — they must be
    # separate OR-Tools nodes so different vehicles can each make a partial delivery.
    n_depots = len(depots)
    order_id_to_orig_idx = {o.order_id: n_depots + j for j, o in enumerate(orders)}

    vrp_locations = (
        [(d.lat, d.lon) for d in depots] +
        [(so.delivery_lat, so.delivery_lon) for so in sub_orders]
    )

    # Remap the original distance matrix (depots×orders) to the vrp matrix (depots×sub_orders).
    # Sub_orders for the same parent order share the original order's distance row.
    n_vrp = len(vrp_locations)
    vrp_matrix = [[0.0] * n_vrp for _ in range(n_vrp)]
    for i in range(n_vrp):
        orig_i = i if i < n_depots else order_id_to_orig_idx[sub_orders[i - n_depots].parent_order_id]
        for j in range(n_vrp):
            orig_j = j if j < n_depots else order_id_to_orig_idx[sub_orders[j - n_depots].parent_order_id]
            vrp_matrix[i][j] = req.distance_matrix[orig_i][orig_j]

    # Phase 2: OR-Tools MDVRP (pass depots so vrp.py can build depot_node_map)
    routes, partial = await loop.run_in_executor(
        None, solve, sub_orders, depots, vehicles,
        vrp_matrix, vrp_locations, req.time_limit_seconds
    )

    def route_to_dict(r):
        d = vars(r)
        d["stops"] = [vars(s) for s in d["stops"]]  # Stop dataclasses need explicit serialization
        return d

    return {
        "sub_orders": [vars(so) for so in sub_orders],
        "routes": [route_to_dict(r) for r in routes],
        "partial": partial
    }
```

---

## 4. planner-service (Port 8001)

### 4.1 Dependencies

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
```

### 4.2 In-memory data store (`store.py`)

```python
from dataclasses import dataclass, field
from typing import Optional
import uuid, datetime

@dataclass
class OrderRecord:
    order_id: str
    units: int
    priority: str
    delivery_address: Optional[str] = None
    delivery_lat: Optional[float] = None   # resolved by POST /data/orders via geocoding
    delivery_lon: Optional[float] = None
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())

class Store:
    def __init__(self):
        self._orders: dict[str, OrderRecord] = {}
        self._vehicles: dict[str, dict] = {}
        self._inventory: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}

    # Orders
    def add_orders(self, orders: list[dict]) -> list[str]:
        ids = []
        for o in orders:
            oid = o.get("order_id", str(uuid.uuid4()))
            self._orders[oid] = OrderRecord(order_id=oid, **{k: v for k, v in o.items() if k != "order_id"})
            ids.append(oid)
        return ids

    def get_orders(self, status=None, priority=None) -> list[dict]:
        records = list(self._orders.values())
        if status:
            records = [r for r in records if r.status == status]
        if priority:
            records = [r for r in records if r.priority == priority]
        return [vars(r) for r in records]

    def patch_order(self, order_id: str, updates: dict):
        if order_id not in self._orders:
            raise KeyError(order_id)
        for k, v in updates.items():
            setattr(self._orders[order_id], k, v)

    # Vehicles / Inventory
    def set_vehicles(self, vehicles: list[dict]):
        self._vehicles = {v["vehicle_id"]: v for v in vehicles}

    def set_inventory(self, inventory: list[dict]):
        self._inventory = {i["warehouse_id"]: i for i in inventory}

    def get_depots(self) -> list[dict]:
        return list(self._inventory.values())

    def get_vehicles(self) -> list[dict]:
        return list(self._vehicles.values())

    # Plans
    def save_plan(self, plan: dict) -> str:
        plan_id = str(uuid.uuid4())
        plan["plan_id"] = plan_id
        self._plans[plan_id] = plan
        return plan_id

    def get_plan(self, plan_id: str) -> dict:
        return self._plans.get(plan_id)

    def get_latest_plan(self) -> dict | None:
        if not self._plans:
            return None
        return self._plans[next(reversed(self._plans))]   # insertion-ordered in Python 3.7+

store = Store()
```

### 4.3 Orchestrator with fallbacks (`orchestrator.py`)

```python
import asyncio
import httpx
import math
from typing import Optional

ROUTING_URL = "http://routing-service:8002"
SOLVER_URL  = "http://solver-service:8003"

async def get_distance_matrix(
    locations: list[tuple[float, float]]
) -> tuple[list[list[float]], bool]:
    """
    Returns (matrix, is_fallback).
    Falls back to Haversine on timeout or connection error.
    """
    try:
        async with asyncio.timeout(2.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/distances",
                    json={"locations": locations},
                    timeout=10.0
                )
                r.raise_for_status()
                return r.json()["matrix"], False
    except (asyncio.TimeoutError, httpx.ConnectError, httpx.TimeoutException):
        return _haversine_matrix(locations), True

async def get_route_geometries(
    segments: list[tuple]
) -> list[list[tuple]]:
    """
    Returns road-following polylines. Returns straight-line segments on failure.
    """
    try:
        async with asyncio.timeout(5.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/geometry",
                    json={"segments": segments},
                    timeout=10.0
                )
                r.raise_for_status()
                return r.json()["paths"]
    except Exception:
        return [[origin, destination] for origin, destination in segments]

async def geocode(address: str) -> tuple[float, float]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ROUTING_URL}/geocode",
            json={"address": address},
            timeout=10.0
        )
        r.raise_for_status()
        return r.json()["lat"], r.json()["lon"]

async def run_solve(
    orders, depots, vehicles, distance_matrix, locations
) -> tuple[dict, bool]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SOLVER_URL}/solve",
            json={
                "orders": orders,
                "depots": depots,
                "vehicles": vehicles,
                "distance_matrix": distance_matrix,
                "locations": locations,
                "time_limit_seconds": 5
            },
            timeout=12.0   # longer than solver budget to avoid premature client timeout
        )
        r.raise_for_status()
        data = r.json()
        return data, data.get("partial", False)

def _haversine_matrix(locations):
    n = len(locations)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                matrix[i][j] = _haversine(*locations[i], *locations[j])
    return matrix

def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
```

### 4.4 Data endpoints and plan endpoint (`main.py`)

All planner endpoints are in one file. Data endpoints are synchronous (pure in-memory
reads/writes); only the geocoding call inside `POST /data/orders` is async.

```python
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from store import store
from orchestrator import get_distance_matrix, get_route_geometries, run_solve, geocode
from opentelemetry import trace

# ── Request models ────────────────────────────────────────────────────────────

class OrderInput(BaseModel):
    order_id: Optional[str] = None
    delivery_address: Optional[str] = None
    delivery_lat: Optional[float] = None
    delivery_lon: Optional[float] = None
    units: int
    priority: str = "normal"

class OrdersUpload(BaseModel):
    orders: list[OrderInput]

class OrderUpdate(BaseModel):
    priority: Optional[str] = None
    status: Optional[str] = None

class VehiclesUpload(BaseModel):
    vehicles: list[dict]

class InventoryUpload(BaseModel):
    inventory: list[dict]

# ── Data endpoints ────────────────────────────────────────────────────────────

@app.post("/data/orders")
async def create_orders(req: OrdersUpload):
    """
    Accepts orders with either explicit lat/lon or a delivery_address string.
    If only delivery_address is provided, geocodes via routing-service before storing.
    """
    resolved = []
    for o in req.orders:
        d = o.dict()
        if d.get("delivery_address") and not (d.get("delivery_lat") and d.get("delivery_lon")):
            lat, lon = await geocode(d["delivery_address"])
            d["delivery_lat"] = lat
            d["delivery_lon"] = lon
        resolved.append(d)
    ids = store.add_orders(resolved)
    return {"order_ids": ids, "count": len(ids)}

@app.get("/data/orders")
def list_orders(status: Optional[str] = None, priority: Optional[str] = None):
    return store.get_orders(status=status, priority=priority)

@app.patch("/data/orders/{order_id}")
def update_order(order_id: str, updates: OrderUpdate):
    try:
        store.patch_order(order_id, {k: v for k, v in updates.dict().items() if v is not None})
        return {"status": "updated"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

@app.post("/data/vehicles")
def upload_vehicles(req: VehiclesUpload):
    store.set_vehicles(req.vehicles)
    return {"count": len(req.vehicles)}

@app.post("/data/inventory")
def upload_inventory(req: InventoryUpload):
    store.set_inventory(req.inventory)
    return {"count": len(req.inventory)}

@app.get("/data/inventory")
def list_inventory():
    return store.get_depots()

@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    plan = store.get_latest_plan() if plan_id == "latest" else store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan

# ── Plan endpoint ─────────────────────────────────────────────────────────────

@app.post("/plan")
async def plan():
    tracer = trace.get_tracer(__name__)

    orders   = store.get_orders()
    depots   = store.get_depots()
    vehicles = store.get_vehicles()

    if not orders or not depots or not vehicles:
        return JSONResponse(status_code=422, content={"detail": "Orders, depots, and vehicles must all be loaded before planning."})

    # Exclude orders that were never geocoded
    orders = [o for o in orders if o.get("delivery_lat") and o.get("delivery_lon")]
    if not orders:
        return JSONResponse(status_code=422, content={"detail": "No orders with resolved coordinates."})

    # Validate supply covers demand
    total_demand = sum(o["units"] for o in orders)
    total_supply = sum(d["units_available"] for d in depots)
    if total_demand > total_supply:
        return JSONResponse(status_code=422, content={
            "detail": f"Total demand ({total_demand}) exceeds total supply ({total_supply}). "
                      f"Adjust orders or inventory before planning."
        })

    # Build combined location list: depots first, then delivery locations
    # This ordering must stay consistent with solver-service expectations
    locations = (
        [(d["lat"], d["lon"]) for d in depots] +
        [(o["delivery_lat"], o["delivery_lon"]) for o in orders]
    )

    with tracer.start_as_current_span("get_distance_matrix"):
        matrix, routing_fallback = await get_distance_matrix(locations)

    with tracer.start_as_current_span("run_solve"):
        solve_result, partial = await run_solve(orders, depots, vehicles, matrix, locations)

    # Enrich routes with road geometry
    routes = solve_result["routes"]
    with tracer.start_as_current_span("get_route_geometries"):
        for route in routes:
            segments = []
            prev = None
            depot = next(d for d in depots if d["warehouse_id"] == route["depot_id"])
            prev = (depot["lat"], depot["lon"])
            for stop in route["stops"]:
                segments.append((prev, (stop["lat"], stop["lon"])))
                prev = (stop["lat"], stop["lon"])
            geometries = await get_route_geometries(segments)
            for stop, geom in zip(route["stops"], geometries):
                stop["road_geometry"] = geom

    plan = {
        "routes": routes,
        "sub_orders": solve_result["sub_orders"],
        "partial": partial,
        "routing_fallback": routing_fallback,
        "kpis": {
            "orders_fulfilled": len(orders),
            "cross_depot_splits": sum(
                1 for so in solve_result["sub_orders"]
                if len([x for x in solve_result["sub_orders"] if x["parent_order_id"] == so["parent_order_id"]]) > 1
            ),
            "avg_utilization_pct": round(
                sum(r.get("utilization_pct", 0) for r in routes) / max(len(routes), 1), 1
            )
        }
    }

    plan_id = store.save_plan(plan)
    plan["plan_id"] = plan_id
    return plan
```

---

## 5. ai-service (Port 8004)

### 5.1 Dependencies

```
anthropic>=0.40.0
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
```

### 5.2 Tool definitions (`tools.py`)

```python
TOOLS = [
    {
        "name": "create_order",
        "description": (
            "Create a new delivery order. Use this when the dispatcher asks to add, "
            "create, or schedule a delivery."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "delivery_address": {
                    "type": "string",
                    "description": "Delivery address or place name in San Jose, CA. "
                                   "Pass exactly what the dispatcher said — do not invent coordinates."
                },
                "units": {
                    "type": "integer",
                    "description": "Number of units to deliver."
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Order priority. Default to 'normal' if not specified."
                }
            },
            "required": ["delivery_address", "units"]
        }
    },
    {
        "name": "update_order_priority",
        "description": "Change the priority of an existing order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "priority": {"type": "string", "enum": ["normal", "high"]}
            },
            "required": ["order_id", "priority"]
        }
    },
    {
        "name": "search_orders",
        "description": "Search or list orders by status, priority, or area.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status":   {"type": "string", "enum": ["pending", "assigned", "delivered"]},
                "priority": {"type": "string", "enum": ["normal", "high"]},
                "area":     {"type": "string", "description": "Neighborhood or area name to filter by"}
            }
        }
    },
    {
        "name": "trigger_replan",
        "description": "Run route optimization with the current set of orders, vehicles, and inventory.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_plan_summary",
        "description": "Retrieve and summarize the most recent completed plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "Optional. Omit to get the latest plan."}
            }
        }
    }
]

SYSTEM_PROMPT = """
You are a logistics dispatch assistant for a San Jose delivery operation.
You help dispatchers manage orders, check status, and trigger route planning.

You have exactly five tools. Use them to take actions when the dispatcher makes a
request. If a dispatcher's request cannot be handled by one of your tools, say so
clearly rather than guessing.

When creating orders, pass the address exactly as the dispatcher described it.
Do not generate or guess lat/lon coordinates — the system resolves addresses itself.

Be concise. Dispatchers are busy. One or two sentences is usually enough.
"""
```

### 5.3 Session history (`session.py`)

```python
from collections import defaultdict

_sessions: dict[str, list[dict]] = defaultdict(list)

def get_history(session_id: str) -> list[dict]:
    return _sessions[session_id]

def append(session_id: str, role: str, content):
    _sessions[session_id].append({"role": role, "content": content})

def clear(session_id: str):
    _sessions[session_id] = []
```

### 5.4 Tool dispatch and multi-turn flow (`tools.py`)

```python
import httpx
import anthropic

PLANNER_URL = "http://planner-service:8001"
client = anthropic.Anthropic()

async def execute_tool(name: str, inputs: dict) -> str:
    """
    Maps tool name to planner-service endpoint call.
    Returns a string result to send back to Claude as tool_result.
    """
    async with httpx.AsyncClient() as http:
        try:
            if name == "create_order":
                r = await http.post(f"{PLANNER_URL}/data/orders",
                                    json={"orders": [inputs]}, timeout=15.0)
                r.raise_for_status()
                return f"Order created: {r.json()}"

            elif name == "update_order_priority":
                r = await http.patch(
                    f"{PLANNER_URL}/data/orders/{inputs['order_id']}",
                    json={"priority": inputs["priority"]}, timeout=5.0
                )
                r.raise_for_status()
                return "Priority updated."

            elif name == "search_orders":
                r = await http.get(f"{PLANNER_URL}/data/orders",
                                   params={k: v for k, v in inputs.items() if v},
                                   timeout=5.0)
                r.raise_for_status()
                orders = r.json()
                return f"{len(orders)} orders found: {orders[:5]}"  # cap for context size

            elif name == "trigger_replan":
                r = await http.post(f"{PLANNER_URL}/plan", timeout=20.0)
                r.raise_for_status()
                data = r.json()
                return (f"Plan complete. {data['kpis']['orders_fulfilled']} orders, "
                        f"{data['kpis']['cross_depot_splits']} splits, "
                        f"{data['kpis']['avg_utilization_pct']}% avg utilization."
                        + (" (partial — time limit reached)" if data.get("partial") else ""))

            elif name == "get_plan_summary":
                plan_id = inputs.get("plan_id", "latest")
                r = await http.get(f"{PLANNER_URL}/plan/{plan_id}", timeout=5.0)
                r.raise_for_status()
                return str(r.json())

        except httpx.HTTPStatusError as e:
            return f"Error: {e.response.status_code} — {e.response.text}"
        except Exception as e:
            return f"Tool execution failed: {str(e)}"

async def handle_message(session_id: str, message: str) -> str:
    from session import get_history, append
    # TOOLS, SYSTEM_PROMPT, execute_tool are defined earlier in this file — no import needed

    history = get_history(session_id)
    append(session_id, "user", message)

    # Turn 1: get tool call or direct response
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=history + [{"role": "user", "content": message}]
    )

    if response.stop_reason == "tool_use":
        tool_block = next(b for b in response.content if b.type == "tool_use")

        # Execute the tool
        tool_result = await execute_tool(tool_block.name, tool_block.input)

        # Turn 2: send result back to Claude for final response
        followup_messages = (
            history
            + [{"role": "user", "content": message}]
            + [{"role": "assistant", "content": response.content}]
            + [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": tool_result
                }]
            }]
        )
        final = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=followup_messages
        )
        reply = final.content[0].text
    else:
        reply = response.content[0].text

    append(session_id, "assistant", reply)
    return reply
```

### 5.5 Chat endpoint (`main.py`)

```python
from pydantic import BaseModel
import uuid

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None

@app.post("/chat")
async def chat(req: ChatRequest):
    from tools import handle_message
    session_id = req.session_id or str(uuid.uuid4())
    reply = await handle_message(session_id, req.message)
    return {"response": reply, "session_id": session_id}
```

---

## 6. Dashboard (Port 8501)

### 6.1 Dependencies

```
streamlit>=1.35.0
folium>=0.17.0
httpx>=0.27.0
pandas>=2.2.0
```

### 6.2 API client (`api_client.py`)

```python
import httpx

PLANNER = "http://planner-service:8001"
AI      = "http://ai-service:8004"

def get_orders(**filters) -> list[dict]:
    r = httpx.get(f"{PLANNER}/data/orders", params=filters, timeout=5.0)
    r.raise_for_status()
    return r.json()

def upload_orders(orders: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/orders", json={"orders": orders}, timeout=10.0)
    r.raise_for_status()
    return r.json()

def upload_vehicles(vehicles: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/vehicles", json={"vehicles": vehicles}, timeout=10.0)
    r.raise_for_status()
    return r.json()

def upload_inventory(inventory: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/inventory", json={"inventory": inventory}, timeout=10.0)
    r.raise_for_status()
    return r.json()

def get_depots() -> list[dict]:
    r = httpx.get(f"{PLANNER}/data/inventory", timeout=5.0)
    r.raise_for_status()
    return r.json()

def run_plan() -> dict:
    r = httpx.post(f"{PLANNER}/plan", timeout=20.0)
    r.raise_for_status()
    return r.json()

def chat(message: str, session_id: str | None) -> dict:
    r = httpx.post(f"{AI}/chat",
                   json={"message": message, "session_id": session_id},
                   timeout=20.0)
    r.raise_for_status()
    return r.json()

def service_health() -> dict[str, bool]:
    services = {
        "planner":  f"{PLANNER}/health",
        "routing":  "http://routing-service:8002/health",
        "solver":   "http://solver-service:8003/health",
        "ai":       f"{AI}/health",
    }
    status = {}
    for name, url in services.items():
        try:
            status[name] = httpx.get(url, timeout=1.0).status_code == 200
        except Exception:
            status[name] = False
    return status
```

### 6.3 Map rendering (`map_render.py`)

```python
import folium

VEHICLE_COLORS = ["red", "blue", "green", "purple", "orange",
                  "darkred", "cadetblue", "darkgreen"]

DEPOT_ICON = {"icon": "home", "prefix": "fa", "color": "black"}

def build_map(plan: dict, depots: list[dict]) -> folium.Map:
    m = folium.Map(location=[37.3382, -121.8863], zoom_start=12,
                   tiles="OpenStreetMap")

    # Depot markers
    for depot in depots:
        folium.Marker(
            location=[depot["lat"], depot["lon"]],
            tooltip=depot["warehouse_id"],
            icon=folium.Icon(**DEPOT_ICON)
        ).add_to(m)

    # Per-vehicle routes
    for i, route in enumerate(plan.get("routes", [])):
        color = VEHICLE_COLORS[i % len(VEHICLE_COLORS)]
        for stop in route["stops"]:
            # Road geometry polyline
            if stop.get("road_geometry"):
                folium.PolyLine(
                    stop["road_geometry"],
                    color=color, weight=3, opacity=0.8
                ).add_to(m)
            # Delivery pin
            folium.CircleMarker(
                location=[stop["lat"], stop["lon"]],
                radius=7, color=color, fill=True, fill_opacity=0.9,
                tooltip=f"Truck {i+1} | {stop['sub_order_id'][:8]}"
            ).add_to(m)

    return m

def render_map(plan: dict, depots: list[dict]) -> str:
    """Returns HTML string for st.components.v1.html()"""
    return build_map(plan, depots)._repr_html_()
```

### 6.4 Main app (`app.py`)

```python
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
from api_client import (get_orders, upload_orders, upload_vehicles, upload_inventory,
                        get_depots, run_plan, chat, service_health)
from map_render import render_map

st.set_page_config(page_title="ALRO", layout="wide")

# ── Service health bar ────────────────────────────────────────────────────────
health = service_health()
cols = st.columns(len(health))
for col, (name, ok) in zip(cols, health.items()):
    col.metric(name, "✓" if ok else "✗")

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_data, tab_plan = st.tabs(["Data", "Route Plan"])

with tab_data:
    st.subheader("Upload Data")
    col1, col2, col3 = st.columns(3)

    with col1:
        f = st.file_uploader("Orders CSV", type="csv", key="orders_csv")
        if f:
            df = pd.read_csv(f)
            upload_orders(df.to_dict("records"))
            st.success(f"{len(df)} orders loaded")

    with col2:
        f = st.file_uploader("Vehicles CSV", type="csv", key="vehicles_csv")
        if f:
            df = pd.read_csv(f)
            upload_vehicles(df.to_dict("records"))
            st.success(f"{len(df)} vehicles loaded")

    with col3:
        f = st.file_uploader("Inventory CSV", type="csv", key="inventory_csv")
        if f:
            df = pd.read_csv(f)
            upload_inventory(df.to_dict("records"))
            st.success(f"{len(df)} depots loaded")

    st.subheader("Current Orders")
    # Always fetch from API — do not cache in session_state
    orders = get_orders()
    if orders:
        st.dataframe(pd.DataFrame(orders), use_container_width=True)

with tab_plan:
    if st.button("Run Optimization", type="primary"):
        with st.spinner("Optimizing routes..."):
            try:
                plan = run_plan()
                st.session_state["current_plan"] = plan
            except Exception as e:
                st.error(str(e))

    if plan := st.session_state.get("current_plan"):
        # KPI row
        kpis = plan.get("kpis", {})
        k1, k2, k3 = st.columns(3)
        k1.metric("Orders Fulfilled", kpis.get("orders_fulfilled", 0))
        k2.metric("Cross-Depot Splits", kpis.get("cross_depot_splits", 0))
        k3.metric("Avg Utilization", f"{kpis.get('avg_utilization_pct', 0)}%")

        if plan.get("partial"):
            st.warning("Plan is partial — solver reached time limit before finding the optimal solution.")
        if plan.get("routing_fallback"):
            st.info("Road geometry unavailable — showing approximate straight-line routes.")

        # Map — fetch depot locations for markers
        depots = get_depots()
        components.html(render_map(plan, depots), height=520)

        # Per-vehicle manifests
        for route in plan.get("routes", []):
            with st.expander(f"Truck {route['vehicle_id']} — {route['utilization_pct']}% utilization"):
                st.dataframe(pd.DataFrame(route["stops"]))

# ── AI Chat sidebar ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("AI Assistant")
    if not health.get("ai"):
        st.warning("AI assistant unavailable — use the form interface.")
    else:
        if "chat_history" not in st.session_state:
            st.session_state.chat_history = []
        if "session_id" not in st.session_state:
            st.session_state.session_id = None

        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        if prompt := st.chat_input("Ask or instruct..."):
            st.session_state.chat_history.append({"role": "user", "content": prompt})
            with st.spinner():
                try:
                    result = chat(prompt, st.session_state.session_id)
                    st.session_state.session_id = result["session_id"]
                    st.session_state.chat_history.append(
                        {"role": "assistant", "content": result["response"]}
                    )
                except Exception as e:
                    st.error(str(e))
            st.rerun()
```

---

## 7. Infrastructure

### 7.1 OTel Collector (`infra/otel-collector-config.yaml`)

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s

exporters:
  jaeger:
    endpoint: jaeger:14250
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus]
```

### 7.2 Prometheus (`infra/prometheus.yml`)

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: planner
    static_configs:
      - targets: ["planner-service:8001"]
  - job_name: routing
    static_configs:
      - targets: ["routing-service:8002"]
  - job_name: solver
    static_configs:
      - targets: ["solver-service:8003"]
  - job_name: ai
    static_configs:
      - targets: ["ai-service:8004"]
  - job_name: otel-collector
    static_configs:
      - targets: ["otel-collector:8889"]
```

### 7.3 Docker Compose (`docker-compose.yml`)

```yaml
version: "3.9"

networks:
  alro:
    driver: bridge

volumes:
  graph-data:    # persists sj_graph.graphml across routing-service restarts

services:

  routing-service:
    build: ./routing-service
    ports: ["8002:8002"]
    volumes:
      - graph-data:/data
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    networks: [alro]
    depends_on: [otel-collector]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/ready"]
      interval: 10s
      timeout: 5s
      retries: 12        # graph download can take up to 60s on first run
      start_period: 90s

  solver-service:
    build: ./solver-service
    ports: ["8003:8003"]
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    networks: [alro]
    depends_on: [otel-collector]

  planner-service:
    build: ./planner-service
    ports: ["8001:8001"]
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    networks: [alro]
    depends_on:
      routing-service:
        condition: service_healthy
      solver-service:
        condition: service_started

  ai-service:
    build: ./ai-service
    ports: ["8004:8004"]
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    networks: [alro]
    depends_on: [planner-service]

  dashboard:
    build: ./dashboard
    ports: ["8501:8501"]
    networks: [alro]
    depends_on: [planner-service, ai-service]

  otel-collector:
    image: otel/opentelemetry-collector:0.102.1
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./infra/otel-collector-config.yaml:/etc/otel-collector-config.yaml
    ports: ["4317:4317", "4318:4318"]
    networks: [alro]
    depends_on: [jaeger]

  jaeger:
    image: jaegertracing/all-in-one:1.57
    ports: ["16686:16686", "14250:14250"]
    networks: [alro]

  prometheus:
    image: prom/prometheus:v2.52.0
    volumes:
      - ./infra/prometheus.yml:/etc/prometheus/prometheus.yml
    ports: ["9090:9090"]
    networks: [alro]
```

---

## 8. Implementation Order

Each step is a deployable checkpoint. At the end of every step, `docker compose up`
should start cleanly with no errors, and the implemented endpoints should respond.

| Step | What gets built | Done when |
|---|---|---|
| **1** | All four service scaffolds: Dockerfile, `requirements.txt`, `main.py` with `/health`, `/ready`, `/metrics`, OTel wired, structlog configured. `docker-compose.yml` with all containers. | `docker compose up` → all eight containers start, all `/health` endpoints return 200 |
| **2** | `routing-service`: `graph.py` (load/download, single-source Dijkstra matrix, cache), `/distances` endpoint, `/ready` returns 503 until graph loads | `POST /distances` returns a matrix for a list of SJ coordinates |
| **3** | `routing-service`: `geocoding.py` and `geometry.py`, `/geocode` and `/geometry` endpoints | `POST /geocode` with "SAP Center, San Jose" returns lat/lon; `/geometry` returns road points |
| **4** | `solver-service`: `models.py`, `allocation.py` (scipy LP), `/solve` partially (Phase 1 only, returns sub-orders) | `POST /solve` with test orders and depots returns sub-order list |
| **5** | `solver-service`: `vrp.py` (OR-Tools MDVRP), `/solve` complete (both phases) | `POST /solve` returns routes with stop sequences |
| **6** | `planner-service`: `store.py`, all `/data/*` endpoints, `haversine.py` | Orders, vehicles, inventory can be uploaded and retrieved |
| **7** | `planner-service`: `orchestrator.py`, `POST /plan` with fallback logic, manual OTel spans | Full plan cycle works end-to-end; routing fallback activates when `routing-service` is stopped |
| **8** | `ai-service`: `tools.py` (schemas + dispatch), `session.py`, `/chat` endpoint | Chat returns a response; `create_order` via chat creates an order visible in planner |
| **9** | `dashboard`: `api_client.py`, CSV upload, orders table, "Run Optimization" button, map render | Full standard UI flow works: upload → optimize → see map |
| **10** | `dashboard`: AI chat sidebar, service health indicators, degradation banners | Chat sidebar works; stopping `ai-service` shows warning, standard UI unaffected |
| **11** | Prometheus scrape targets, Jaeger trace verification, OTel trace propagation confirmed | A single `/plan` request produces one connected trace tree in Jaeger |

---

## 9. Critical Implementation Notes

**OR-Tools node indexing.** The location list passed to `solver-service` must be
constructed identically in `planner-service` and interpreted identically in `vrp.py`.
Depot nodes go first. Delivery nodes follow in the same order as the orders list.
Any mismatch silently produces wrong routes. Write a unit test that verifies a
two-depot, three-order scenario produces routes that respect depot assignment before
integrating with the full pipeline.

**OSMnx takes `(lon, lat)`, not `(lat, lon)`.** `ox.nearest_nodes(G, X, Y)` takes
longitude as `X` and latitude as `Y`. This is the opposite of most geographic
conventions. Every call to `nearest_nodes` and `ox.geocode` must pass coordinates
in this order. A transposed call will silently snap to the wrong road node.

**`opentelemetry-instrumentation-httpx` must be activated.** Without it, outbound
HTTP calls from `planner-service` to `routing-service` and `solver-service` do not
carry trace context headers, and the Jaeger trace appears as three disconnected spans
instead of one tree. Activate with `HTTPXClientInstrumentor().instrument()` before
the first HTTP call is made (in `setup_telemetry`).

**OR-Tools requires a fresh `RoutingModel` per solve.** Do not reuse the model
object across requests. Create it inside the function call, not at module level.
Concurrent solves sharing a model produce undefined behavior.

**`asyncio.timeout` requires Python 3.11+.** Use `async with asyncio.timeout(n):`.
For Python 3.10 and below, use `asyncio.wait_for(coro, timeout=n)` instead.

**Streamlit re-runs on every interaction.** Any state that must persist between
interactions lives in `st.session_state` (chat history, session_id, current plan).
Data that should always be fresh (orders table, service health) is fetched from the
API on every run — not cached in `session_state`.

---

## 10. Testing

Tests cover the pure-Python logic that produces **silently wrong output** — the failure
mode hardest to diagnose at demo time. No OSMnx graph download, no network calls, no
Streamlit. Run with `pytest tests/` from the repo root in under 10 seconds.

### 10.1 Dependencies

```
pytest>=8.2.0
pytest-asyncio>=0.23.0
```

Add to a root-level `requirements-dev.txt`. Services don't need these at runtime.

### 10.2 Fixtures (`tests/conftest.py`)

```python
import sys, os, pytest
# Add service source directories to path so tests can import without packaging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../solver-service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../planner-service"))

# Reusable fixture coordinates (no real graph; just San Jose-scale floats)
DEPOT_A  = (37.360, -121.920)
DEPOT_B  = (37.310, -121.890)
ORDER_1  = (37.340, -121.910)
ORDER_2  = (37.320, -121.895)
ORDER_3  = (37.300, -121.870)

def minimal_matrix(n: int, off_diag: float = 10.0) -> list[list[float]]:
    """n×n matrix: 0 on diagonal, off_diag everywhere else."""
    return [[0.0 if i == j else off_diag for j in range(n)] for i in range(n)]
```

### 10.3 Allocation tests (`tests/test_allocation.py`)

These protect the LP solver — the most common failure mode is the infeasibility case
reaching `linprog` with an obscure error instead of a clean 422.

```python
import numpy as np
import pytest
from allocation import allocate
from models import Order, Depot

def order(oid, lat, lon, units):
    return Order(order_id=oid, delivery_lat=lat, delivery_lon=lon, units=units)

def depot(wid, lat, lon, stock):
    return Depot(warehouse_id=wid, lat=lat, lon=lon, units_available=stock)

def test_single_depot_single_order():
    result = allocate(
        [order("O1", 37.34, -121.91, 50)],
        [depot("D1", 37.36, -121.92, 100)],
        np.array([[5.0]])
    )
    assert len(result) == 1
    assert result[0].depot_id == "D1"
    assert result[0].units == 50

def test_order_split_when_one_depot_insufficient():
    """80-unit order; each depot has only 50. LP must split."""
    result = allocate(
        [order("O1", 37.34, -121.91, 80)],
        [depot("D1", 37.36, -121.92, 50), depot("D2", 37.31, -121.89, 50)],
        np.array([[3.0], [7.0]])   # D1 closer → D1 gets larger share
    )
    assert len(result) == 2
    assert sum(so.units for so in result) == 80
    d1_units = next(so.units for so in result if so.depot_id == "D1")
    d2_units = next(so.units for so in result if so.depot_id == "D2")
    assert d1_units > d2_units   # cost-optimal: nearer depot serves more

def test_no_split_when_single_depot_covers_all():
    """Two depots available but one has enough — no splitting needed."""
    result = allocate(
        [order("O1", 37.34, -121.91, 20), order("O2", 37.32, -121.89, 15)],
        [depot("D1", 37.36, -121.92, 100), depot("D2", 37.31, -121.89, 100)],
        np.array([[3.0, 5.0], [8.0, 2.0]])
    )
    # Each order should come entirely from one depot (no fractional split needed)
    from collections import Counter
    by_order = Counter(so.parent_order_id for so in result)
    assert all(v == 1 for v in by_order.values())

def test_infeasible_raises_value_error():
    with pytest.raises(ValueError, match="infeasible"):
        allocate(
            [order("O1", 37.34, -121.91, 200)],
            [depot("D1", 37.36, -121.92, 100)],
            np.array([[5.0]])
        )
```

### 10.4 VRP tests (`tests/test_vrp.py`)

These protect the MDVRP solver — the bugs most likely to survive to demo day are silent
wrong depot assignment and an incorrect distance total.

```python
import pytest
from vrp import solve
from models import SubOrder, Depot, Vehicle

def sub_order(sid, parent, depot_id, lat, lon, units):
    return SubOrder(sub_order_id=sid, parent_order_id=parent,
                    depot_id=depot_id, delivery_lat=lat, delivery_lon=lon, units=units)

def depot(wid, lat, lon):
    return Depot(warehouse_id=wid, lat=lat, lon=lon, units_available=999)

def vehicle(vid, depot_id, cap):
    return Vehicle(vehicle_id=vid, depot_id=depot_id, capacity_units=cap)

def _mat(n, off=10.0):
    return [[0.0 if i == j else off for j in range(n)] for i in range(n)]

def test_empty_returns_empty():
    routes, partial = solve([], [], [], [], [])
    assert routes == [] and partial is False

def test_single_route_has_correct_stop():
    depots = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 50)]
    locs = [(37.36, -121.92), (37.34, -121.91)]   # depot, then sub_order

    routes, _ = solve(sub_orders, depots, vehicles, _mat(2), locs)

    assert len(routes) == 1
    assert routes[0].stops[0].sub_order_id == "SO1"

def test_depot_assignment_enforced():
    """V1 is from D1; V2 is from D2. Each must serve only its own sub_orders."""
    depots = [depot("D1", 37.36, -121.92), depot("D2", 37.31, -121.89)]
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

    routes, _ = solve(sub_orders, depots, vehicles, _mat(4), locs)

    for route in routes:
        for stop in route.stops:
            # Find the sub_order for this stop
            so = next(s for s in sub_orders if s.sub_order_id == stop.sub_order_id)
            assert so.depot_id == route.depot_id, (
                f"Vehicle {route.vehicle_id} (depot {route.depot_id}) "
                f"was assigned sub_order from depot {so.depot_id}"
            )

def test_distance_is_per_route_not_plan_total():
    """With two vehicles but only one active route, distance must not be plan_total/2."""
    depots = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100), vehicle("V2", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 10)]
    locs = [(37.36, -121.92), (37.34, -121.91)]
    mat = [[0.0, 8.0], [8.0, 0.0]]   # 8km each way

    routes, _ = solve(sub_orders, depots, vehicles, mat, locs)

    assert len(routes) == 1
    # Route is: depot → SO1 → depot = 8 + 8 = 16 km
    assert routes[0].total_distance_km == pytest.approx(16.0, abs=0.5)

def test_utilization_pct():
    depots = [depot("D1", 37.36, -121.92)]
    vehicles = [vehicle("V1", "D1", 100)]
    sub_orders = [sub_order("SO1", "O1", "D1", 37.34, -121.91, 75)]
    locs = [(37.36, -121.92), (37.34, -121.91)]

    routes, _ = solve(sub_orders, depots, vehicles, _mat(2), locs)

    assert routes[0].utilization_pct == pytest.approx(75.0)
```

### 10.5 Store tests (`tests/test_store.py`)

```python
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
    store.add_orders([{"delivery_address": "SAP Center, San Jose", "units": 5, "priority": "high"}])
    o = store.get_orders()[0]
    assert o["delivery_lat"] is None
    assert o["delivery_address"] == "SAP Center, San Jose"

def test_status_filter(store):
    ids = store.add_orders([{"units": 10, "priority": "normal"}, {"units": 5, "priority": "normal"}])
    store.patch_order(ids[0], {"status": "assigned"})

    assert len(store.get_orders(status="pending")) == 1
    assert len(store.get_orders(status="assigned")) == 1

def test_patch_missing_order_raises(store):
    with pytest.raises(KeyError):
        store.patch_order("does-not-exist", {"status": "assigned"})

def test_get_latest_plan_empty(store):
    assert store.get_latest_plan() is None

def test_get_latest_plan_returns_last(store):
    store.save_plan({"version": 1})
    store.save_plan({"version": 2})
    assert store.get_latest_plan()["version"] == 2
```

### 10.6 Haversine tests (`tests/test_haversine.py`)

```python
import pytest
from orchestrator import _haversine

def test_zero_same_point():
    assert _haversine(37.33, -121.89, 37.33, -121.89) == pytest.approx(0.0)

def test_symmetric():
    a = _haversine(37.36, -121.92, 37.31, -121.89)
    b = _haversine(37.31, -121.89, 37.36, -121.92)
    assert a == pytest.approx(b)

def test_sjc_to_sap_center():
    # SJC airport → SAP Center San Jose: ~9 km straight-line
    dist = _haversine(37.3639, -121.9289, 37.3328, -121.9008)
    assert 8.0 < dist < 11.0
```

### 10.7 Orchestrator fallback tests (`tests/test_orchestrator_fallback.py`)

These verify that the partial-failure pattern works: planner completes even when
routing-service is unreachable, and the `routing_fallback` flag is returned correctly.

```python
import pytest
import httpx
from unittest.mock import patch, AsyncMock

pytestmark = pytest.mark.asyncio

async def test_returns_road_matrix_when_routing_available():
    mock_matrix = [[0.0, 5.2], [5.2, 0.0]]

    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        resp = AsyncMock()
        resp.json.return_value = {"matrix": mock_matrix}
        resp.raise_for_status = AsyncMock()
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)

        from orchestrator import get_distance_matrix
        matrix, is_fallback = await get_distance_matrix([(37.36, -121.92), (37.31, -121.89)])

    assert matrix == mock_matrix
    assert is_fallback is False

async def test_falls_back_to_haversine_on_connect_error():
    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("routing-service down")
        )

        from orchestrator import get_distance_matrix
        locations = [(37.36, -121.92), (37.31, -121.89)]
        matrix, is_fallback = await get_distance_matrix(locations)

    assert is_fallback is True
    assert matrix[0][0] == pytest.approx(0.0)      # diagonal = 0
    assert matrix[0][1] > 0.0                       # off-diagonal > 0
    assert matrix[0][1] == pytest.approx(matrix[1][0], rel=0.01)  # symmetric

async def test_fallback_matrix_dimensions_match_input():
    with patch("orchestrator.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("")
        )

        from orchestrator import get_distance_matrix
        locations = [(37.36, -121.92), (37.34, -121.91), (37.31, -121.89)]
        matrix, _ = await get_distance_matrix(locations)

    assert len(matrix) == 3
    assert all(len(row) == 3 for row in matrix)
```

### 10.8 What is not tested

| Area | Reason |
|---|---|
| OSMnx graph loading and geocoding | Requires network + Nominatim; slow and flaky in CI |
| `GET /geometry` road path correctness | Depends on real graph topology |
| Streamlit UI | No headless test driver in scope |
| AI response content | Non-deterministic; Claude API not mocked |
| Full `/plan` pipeline end-to-end | Covered by Step 7 acceptance criteria in the implementation table |

The implementation table in Section 8 serves as the integration test: each step has a
manual acceptance check. The unit tests above protect the logic that produces silent
wrong output — wrong routes, wrong distances, wrong depot assignment — which would
fail a demo without a clear error message.

---

*End of Technical Specification — ALRO v2*
