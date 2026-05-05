import asyncio
import math
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()


def setup_telemetry(service_name: str):
    provider = TracerProvider()
    exporter = OTLPSpanExporter(
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317"),
        insecure=True,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()


is_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global is_ready
    is_ready = True
    log.info("solver_service_ready")
    yield


app = FastAPI(lifespan=lifespan)
setup_telemetry("solver-service")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    if not is_ready:
        return Response(status_code=503, content="not ready")
    return {"status": "ready"}


# ── Haversine (used for LP cost matrix — road distances not available at Phase 1) ──

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Request model ──────────────────────────────────────────────────────────────

class SolveRequest(BaseModel):
    orders: list[dict]
    depots: list[dict]
    vehicles: list[dict]
    distance_matrix: list[list[float]]    # depots+orders indexed, from routing-service
    locations: list[tuple[float, float]]  # depots first, then orders (same order as matrix)
    time_limit_seconds: int = 5


# ── Endpoint ───────────────────────────────────────────────────────────────────

@app.post("/solve")
async def solve_endpoint(req: SolveRequest):
    import numpy as np
    from allocation import allocate
    from models import Depot, Order, Vehicle
    from vrp import solve

    _order_fields  = {"order_id", "delivery_lat", "delivery_lon", "units", "priority"}
    _depot_fields  = {"warehouse_id", "lat", "lon", "units_available"}
    _vehicle_fields = {"vehicle_id", "depot_id", "capacity_units"}
    orders   = [Order(**{k: v for k, v in o.items() if k in _order_fields}) for o in req.orders]
    depots   = [Depot(**{k: v for k, v in d.items() if k in _depot_fields}) for d in req.depots]
    vehicles = [Vehicle(**{k: v for k, v in v.items() if k in _vehicle_fields}) for v in req.vehicles]

    # Phase 1: Haversine cost matrix for LP.
    # Road distances aren't available at allocation time; Haversine is accurate
    # enough to guide the LP toward depot-proximity-based assignment.
    n_d, n_o = len(depots), len(orders)
    cost_matrix = np.zeros((n_d, n_o))
    for i, depot in enumerate(depots):
        for j, order in enumerate(orders):
            cost_matrix[i, j] = _haversine(
                depot.lat, depot.lon, order.delivery_lat, order.delivery_lon
            )

    loop = asyncio.get_running_loop()
    try:
        sub_orders = await loop.run_in_executor(None, allocate, orders, depots, cost_matrix)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})

    # Build OR-Tools location list: depots first, then one node per sub_order.
    # Split orders produce multiple sub_orders at the same lat/lon — they must be
    # separate OR-Tools nodes so different vehicles can each make a partial delivery.
    n_depots = len(depots)
    order_id_to_orig_idx = {o.order_id: n_depots + j for j, o in enumerate(orders)}

    vrp_locations = (
        [(d.lat, d.lon) for d in depots] +
        [(so.delivery_lat, so.delivery_lon) for so in sub_orders]
    )

    # Remap original (depots×orders) distance matrix to (depots×sub_orders).
    # Sub_orders for the same parent order share the original order's distances.
    n_vrp = len(vrp_locations)
    vrp_matrix = [[0.0] * n_vrp for _ in range(n_vrp)]
    for i in range(n_vrp):
        orig_i = i if i < n_depots else order_id_to_orig_idx[sub_orders[i - n_depots].parent_order_id]
        for j in range(n_vrp):
            orig_j = j if j < n_depots else order_id_to_orig_idx[sub_orders[j - n_depots].parent_order_id]
            vrp_matrix[i][j] = req.distance_matrix[orig_i][orig_j]

    try:
        routes, partial = await loop.run_in_executor(
            None, solve, sub_orders, depots, vehicles,
            vrp_matrix, vrp_locations, req.time_limit_seconds,
        )
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})

    def route_to_dict(r):
        d = vars(r)
        d["stops"] = [vars(s) for s in d["stops"]]
        return d

    return {
        "sub_orders": [vars(so) for so in sub_orders],
        "routes": [route_to_dict(r) for r in routes],
        "partial": partial,
    }
