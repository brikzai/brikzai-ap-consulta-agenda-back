from django.test import Client


def test_health_returns_ok():
    response = Client().get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
