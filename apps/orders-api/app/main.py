import logging
import time
import uuid
from typing import Annotated

import redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings
from app.logging_config import configure_logging, request_id_var
from app.metrics import ERRORS, LATENCY, REQUESTS
from app.store import Order, OrderIn, OrderStore, build_store

logger = logging.getLogger("orders-api")


def get_store(request: Request) -> OrderStore:
    return request.app.state.store


StoreDep = Annotated[OrderStore, Depends(get_store)]


def create_app(
    store: OrderStore | None = None, settings: Settings | None = None
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="orders-api")
    app.state.store = store or build_store(
        settings.redis_url, settings.redis_timeout_seconds
    )

    @app.middleware("http")
    async def observe(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        except Exception:
            logger.exception("unhandled error")
            raise
        finally:
            duration = time.perf_counter() - started
            route = request.scope.get("route")
            path = route.path if route else "unmatched"
            REQUESTS.labels(request.method, path, str(status)).inc()
            LATENCY.labels(request.method, path).observe(duration)
            if status >= 500:
                ERRORS.labels(request.method, path).inc()
            logger.info(
                "request",
                extra={
                    "fields": {
                        "method": request.method,
                        "path": path,
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    }
                },
            )
            request_id_var.reset(token)

    @app.exception_handler(redis.RedisError)
    async def redis_error_handler(
        request: Request, exc: redis.RedisError
    ) -> JSONResponse:
        logger.error("redis error", extra={"fields": {"error": type(exc).__name__}})
        return JSONResponse(status_code=503, content={"detail": "storage unavailable"})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness: the process is up. Must not depend on Redis."""
        return {"status": "ok"}

    @app.get("/version")
    def version() -> dict[str, str]:
        """The Git commit this image was built from, for tracing a pod to its code."""
        return {"version": settings.version}

    @app.get("/ready")
    def ready(store: StoreDep) -> dict[str, str]:
        """Readiness: the service can do useful work, so Redis must answer."""
        try:
            store.ping()
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="redis unavailable") from exc
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/orders", response_model=Order, status_code=201)
    def create_order(order_in: OrderIn, store: StoreDep) -> Order:
        order = store.create(order_in)
        logger.info("order created", extra={"fields": {"order_id": order.id}})
        return order

    @app.get("/orders", response_model=list[Order])
    def list_orders(
        store: StoreDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[Order]:
        return store.list(limit)

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(order_id: int, store: StoreDep) -> Order:
        order = store.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    if settings.enable_chaos_endpoints:
        _add_chaos_endpoints(app)

    return app


def _add_chaos_endpoints(app: FastAPI) -> None:
    """Load generators for chaos scenarios. Demo only, never enable in production."""
    held: list[bytearray] = []

    @app.get("/chaos/cpu")
    def burn_cpu(ms: int = Query(200, ge=1, le=5000)) -> dict[str, int]:
        end = time.perf_counter() + ms / 1000
        while time.perf_counter() < end:
            sum(i * i for i in range(1000))
        return {"burned_ms": ms}

    @app.get("/chaos/memory")
    def hold_memory(mb: int = Query(10, ge=1, le=512)) -> dict[str, int]:
        # Non-zero bytes force the OS to really back the pages, so RSS grows.
        held.append(bytearray(b"x") * (mb * 1024 * 1024))
        return {"held_mb": sum(len(b) for b in held) // (1024 * 1024)}

    @app.post("/chaos/memory/release")
    def release_memory() -> dict[str, int]:
        held.clear()
        return {"held_mb": 0}


app = create_app()
