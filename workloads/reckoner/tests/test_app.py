from fastapi.testclient import TestClient
from reckoner.app import app


def test_liveness_is_available_without_data_or_credentials():
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
