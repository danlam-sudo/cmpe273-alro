import asyncio
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import Response
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
    log.info("routing_service_starting", msg="downloading/loading San Jose road graph")
    from graph import load_graph
    await load_graph()
    is_ready = True
    log.info("routing_service_ready")
    yield


app = FastAPI(lifespan=lifespan)
setup_telemetry("routing-service")
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

class DistanceRequest(BaseModel):
    locations: list[tuple[float, float]]   # [(lat, lon), ...]

class GeometryRequest(BaseModel):
    segments: list[tuple[tuple[float, float], tuple[float, float]]]

class GeocodeRequest(BaseModel):
    address: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/distances")
async def distances(req: DistanceRequest):
    from graph import compute_distance_matrix
    loop = asyncio.get_running_loop()
    matrix = await loop.run_in_executor(None, compute_distance_matrix, req.locations)
    return {"matrix": matrix}


@app.post("/geometry")
async def geometry(req: GeometryRequest):
    from geometry import get_route_geometry
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
