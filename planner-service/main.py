import os
from contextlib import asynccontextmanager
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException
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
    from seed import seed
    seed()
    is_ready = True
    log.info("planner_service_ready")
    yield


app = FastAPI(lifespan=lifespan)
setup_telemetry("planner-service")
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


# ── Request models ─────────────────────────────────────────────────────────────

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


# ── Data endpoints ─────────────────────────────────────────────────────────────

@app.post("/data/orders")
async def create_orders(req: OrdersUpload):
    """
    Accepts orders with explicit lat/lon or a delivery_address string.
    Geocodes via routing-service when only an address is provided.
    """
    from orchestrator import geocode
    from store import store

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
def list_orders(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    address: Optional[str] = None,
    units_min: Optional[int] = None,
    units_max: Optional[int] = None,
    depot_id: Optional[str] = None,
    created_after: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    limit: Optional[int] = None,
):
    from store import store
    try:
        return store.get_orders(
            status=status,
            priority=priority,
            address=address,
            units_min=units_min,
            units_max=units_max,
            depot_id=depot_id,
            created_after=created_after,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
        )
    except LookupError as e:
        if str(e) == "no_plan":
            raise HTTPException(
                status_code=422,
                detail="depot_id filter requires an optimization plan to have been run. "
                       "Run optimization first, then filter by depot.",
            )
        raise


@app.patch("/data/orders/{order_id}")
def update_order(order_id: str, updates: OrderUpdate):
    from store import store
    try:
        store.patch_order(order_id, {k: v for k, v in updates.dict().items() if v is not None})
        return {"status": "updated"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")


@app.post("/data/vehicles")
def upload_vehicles(req: VehiclesUpload):
    from store import store
    store.set_vehicles(req.vehicles)
    return {"count": len(req.vehicles)}


@app.get("/data/vehicles")
def list_vehicles():
    from store import store
    return store.get_vehicles()


@app.post("/data/inventory")
def upload_inventory(req: InventoryUpload):
    from store import store
    store.set_inventory(req.inventory)
    return {"count": len(req.inventory)}


@app.get("/data/inventory")
def list_inventory():
    from store import store
    return store.get_depots()


@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    from store import store
    plan = store.get_latest_plan() if plan_id == "latest" else store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


# ── Plan endpoint ──────────────────────────────────────────────────────────────

@app.post("/plan")
async def plan():
    from orchestrator import get_distance_matrix, get_route_geometries, run_solve
    from store import store

    tracer = trace.get_tracer(__name__)

    all_orders = store.get_orders()
    depots     = store.get_depots()
    vehicles   = store.get_vehicles()

    if not all_orders or not depots or not vehicles:
        return JSONResponse(
            status_code=422,
            content={"detail": "Orders, depots, and vehicles must all be loaded before planning."},
        )

    # Exclude orders that were never geocoded; report them as skipped
    def _skip(o, reason):
        return {
            "order_id": o.get("order_id"),
            "address": o.get("delivery_address"),
            "units": o.get("units"),
            "priority": o.get("priority"),
            "reason": reason,
        }

    skipped_orders = [_skip(o, "no coordinates") for o in all_orders if not (o.get("delivery_lat") and o.get("delivery_lon"))]
    orders = [o for o in all_orders if o.get("delivery_lat") and o.get("delivery_lon")]
    if not orders:
        return JSONResponse(
            status_code=422,
            content={"detail": "No orders with resolved coordinates. Provide lat/lon or a delivery_address."},
        )

    # Drop orders that can never fit in any single vehicle (no amount of splitting helps
    # because each sub_order must fit in one vehicle trip).
    max_vehicle_capacity = max((v["capacity_units"] for v in vehicles), default=0)
    oversized = [o for o in orders if o["units"] > max_vehicle_capacity]
    for o in oversized:
        skipped_orders.append(_skip(o, f"exceeds max vehicle capacity ({max_vehicle_capacity} units)"))
    orders = [o for o in orders if o["units"] <= max_vehicle_capacity]
    if not orders:
        return JSONResponse(
            status_code=422,
            content={"detail": "All orders exceed the largest vehicle capacity and cannot be served."},
        )

    # If total demand exceeds supply, prioritize high-priority orders first,
    # then greedily fill remaining capacity with normal-priority orders.
    # Deferred orders are recorded in skipped_orders for the next planning run.
    total_supply = sum(d["units_available"] for d in depots)
    total_demand = sum(o["units"] for o in orders)
    if total_demand > total_supply:
        sorted_candidates = sorted(orders, key=lambda o: 0 if o.get("priority") == "high" else 1)
        selected, running = [], 0
        for o in sorted_candidates:
            if running + o["units"] <= total_supply:
                selected.append(o)
                running += o["units"]
            else:
                skipped_orders.append(_skip(o, "deferred — insufficient supply"))
        orders = selected
        if not orders:
            return JSONResponse(
                status_code=422,
                content={"detail": "No orders could be fulfilled — supply is exhausted or all orders exceed available inventory."},
            )
        log.info(
            "plan_supply_constrained",
            fulfilled=len(orders),
            deferred=len([s for s in skipped_orders if s["reason"] == "deferred — insufficient supply"]),
            supply=total_supply,
            demand=total_demand,
        )

    # Location list for distance matrix: depots first, then delivery locations.
    # This ordering is contract with solver-service — must not change.
    locations = (
        [(d["lat"], d["lon"]) for d in depots] +
        [(o["delivery_lat"], o["delivery_lon"]) for o in orders]
    )

    with tracer.start_as_current_span("get_distance_matrix"):
        matrix, routing_fallback = await get_distance_matrix(locations)

    with tracer.start_as_current_span("run_solve"):
        try:
            solve_result, partial = await run_solve(orders, depots, vehicles, matrix, locations)
        except Exception as e:
            import httpx as _httpx
            if isinstance(e, _httpx.HTTPStatusError) and e.response.status_code == 422:
                detail = e.response.json().get("detail", str(e))
                return JSONResponse(status_code=422, content={"detail": detail})
            raise

    # Enrich routes with road geometry (straight lines if routing-service unavailable)
    routes = solve_result["routes"]
    with tracer.start_as_current_span("get_route_geometries"):
        for route in routes:
            depot = next(d for d in depots if d["warehouse_id"] == route["depot_id"])
            prev = (depot["lat"], depot["lon"])
            segments = []
            for stop in route["stops"]:
                segments.append((prev, (stop["lat"], stop["lon"])))
                prev = (stop["lat"], stop["lon"])
            geometries = await get_route_geometries(segments)
            for stop, geom in zip(route["stops"], geometries):
                stop["road_geometry"] = geom

    # Count unique orders that have >1 sub_order (cross-depot splits)
    sub_orders = solve_result["sub_orders"]
    from collections import Counter
    split_counts = Counter(so["parent_order_id"] for so in sub_orders)
    n_splits = sum(1 for count in split_counts.values() if count > 1)

    result = {
        "routes": routes,
        "sub_orders": sub_orders,
        "partial": partial,
        "routing_fallback": routing_fallback,
        "skipped_orders": [
            {"order_id": o.get("order_id"), "reason": "no coordinates", "address": o.get("delivery_address")}
            for o in skipped_orders
        ],
        "kpis": {
            "orders_fulfilled": len(orders),
            "orders_skipped": len(skipped_orders),
            "cross_depot_splits": n_splits,
            "avg_utilization_pct": round(
                sum(r.get("utilization_pct", 0) for r in routes) / max(len(routes), 1), 1
            ),
        },
    }

    plan_id = store.save_plan(result)
    result["plan_id"] = plan_id
    log.info("plan_complete", plan_id=plan_id, routes=len(routes), splits=n_splits, fallback=routing_fallback)
    return result
