import pytest

from app.repositories.orders import OrderRepository


@pytest.fixture
def repo(tmp_path):
    return OrderRepository(tmp_path / "test.db")
