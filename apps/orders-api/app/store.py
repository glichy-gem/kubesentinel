from datetime import UTC, datetime

import redis
from pydantic import BaseModel, Field

ORDER_IDS_KEY = "orders:ids"
NEXT_ID_KEY = "orders:next_id"
MAX_STORED_ORDERS = 1000


class OrderIn(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=1000)


class Order(OrderIn):
    id: int
    created_at: datetime


class OrderStore:
    """Orders persisted in Redis. Any Redis failure surfaces as redis.RedisError."""

    def __init__(self, client: redis.Redis) -> None:
        self._r = client

    def ping(self) -> bool:
        return bool(self._r.ping())

    def create(self, order_in: OrderIn) -> Order:
        order_id = int(self._r.incr(NEXT_ID_KEY))
        order = Order(
            id=order_id, created_at=datetime.now(UTC), **order_in.model_dump()
        )
        pipe = self._r.pipeline()
        pipe.set(f"orders:{order_id}", order.model_dump_json())
        pipe.lpush(ORDER_IDS_KEY, order_id)
        pipe.ltrim(ORDER_IDS_KEY, 0, MAX_STORED_ORDERS - 1)
        pipe.execute()
        return order

    def get(self, order_id: int) -> Order | None:
        raw = self._r.get(f"orders:{order_id}")
        return Order.model_validate_json(raw) if raw else None

    def list(self, limit: int = 50) -> list[Order]:
        ids = self._r.lrange(ORDER_IDS_KEY, 0, limit - 1)
        if not ids:
            return []
        raws = self._r.mget([f"orders:{i}" for i in ids])
        return [Order.model_validate_json(raw) for raw in raws if raw]


def build_store(url: str, timeout: float) -> OrderStore:
    client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
    )
    return OrderStore(client)
