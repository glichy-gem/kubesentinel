import fakeredis
import pytest
from app.config import Settings
from app.main import create_app
from app.store import OrderStore
from fastapi.testclient import TestClient


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def client(redis_server: fakeredis.FakeServer) -> TestClient:
    fake = fakeredis.FakeStrictRedis(server=redis_server, decode_responses=True)
    app = create_app(
        store=OrderStore(fake), settings=Settings(enable_chaos_endpoints=True)
    )
    return TestClient(app)
