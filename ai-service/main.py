import os
import uuid
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
    is_ready = True
    log.info("ai_service_ready")
    yield


app = FastAPI(lifespan=lifespan)
setup_telemetry("ai-service")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "ai_configured": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.get("/ready")
def ready():
    if not is_ready:
        return Response(status_code=503, content="not ready")
    return {"status": "ready"}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest):
    from tools import handle_message
    session_id = req.session_id or str(uuid.uuid4())
    reply = await handle_message(session_id, req.message)
    return {"response": reply, "session_id": session_id}
