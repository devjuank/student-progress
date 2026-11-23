import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app
from app.models import ProgressRecord

client = TestClient(app)


@pytest.fixture(autouse=True)
def fixed_progress(monkeypatch):
    monkeypatch.setattr(store, "PROGRESS", [
        ProgressRecord("s-001", "l-01", True, 90),
        ProgressRecord("s-001", "l-02", True, 80),
        ProgressRecord("s-002", "l-01", True, 70),
        ProgressRecord("s-003", "l-01", True, 95),
    ])


def test_leaderboard_sorted_by_average_desc():
    rows = client.get("/leaderboard").json()
    assert [row["average_score"] for row in rows] == [95.0, 85.0, 70.0]


def test_leaderboard_skips_students_without_progress():
    rows = client.get("/leaderboard").json()
    assert len(rows) == 3


def test_leaderboard_limit():
    rows = client.get("/leaderboard", params={"limit": 2}).json()
    assert [row["average_score"] for row in rows] == [95.0, 85.0]


@pytest.mark.parametrize("limit", [0, 51])
def test_leaderboard_invalid_limit(limit):
    assert client.get("/leaderboard", params={"limit": limit}).status_code == 422
