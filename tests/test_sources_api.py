"""Sources CRUD lifecycle, plus the invariants CLAUDE.md calls out: no hard
delete, and /check goes through the SSRF-safe fetcher for real."""

from fastapi.testclient import TestClient


def _create(client: TestClient, **overrides) -> dict:
    payload = {
        "name": "Test Times",
        "feed_url": "https://example.test/rss",
        "language": "en",
        "publisher_group": "test-times",
    }
    payload.update(overrides)
    response = client.post("/api/v1/sources", json=payload)
    assert response.status_code == 201
    return response.json()


def test_create_source(client: TestClient) -> None:
    body = _create(client)
    assert body["name"] == "Test Times"
    assert body["enabled"] is True
    assert body["archived_at"] is None
    assert body["category_map"] == {}


def test_create_source_rejects_unknown_category(client: TestClient) -> None:
    response = client.post(
        "/api/v1/sources",
        json={
            "name": "Test Times",
            "feed_url": "https://example.test/rss",
            "language": "en",
            "publisher_group": "test-times",
            "category_map": {"local": "not-a-real-category"},
        },
    )
    assert response.status_code == 422


def test_get_source(client: TestClient) -> None:
    created = _create(client)
    response = client.get(f"/api/v1/sources/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_source_404(client: TestClient) -> None:
    assert client.get("/api/v1/sources/999").status_code == 404


def test_list_sources(client: TestClient) -> None:
    _create(client, name="A", publisher_group="a")
    _create(client, name="B", publisher_group="b")
    response = client.get("/api/v1/sources")
    assert response.status_code == 200
    assert [s["name"] for s in response.json()] == ["A", "B"]


def test_patch_source_partial_update(client: TestClient) -> None:
    created = _create(client)
    response = client.patch(f"/api/v1/sources/{created['id']}", json={"enabled": False})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["name"] == "Test Times"  # untouched fields survive PATCH


def test_patch_source_404(client: TestClient) -> None:
    assert client.patch("/api/v1/sources/999", json={"enabled": False}).status_code == 404


def test_archive_is_soft_delete_and_hides_from_default_list(client: TestClient) -> None:
    created = _create(client)

    archived = client.post(f"/api/v1/sources/{created['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    assert client.get("/api/v1/sources").json() == []
    assert len(client.get("/api/v1/sources?include_archived=true").json()) == 1

    # still readable directly by id — archiving isn't deletion
    assert client.get(f"/api/v1/sources/{created['id']}").status_code == 200


def test_archive_is_idempotent(client: TestClient) -> None:
    created = _create(client)
    first = client.post(f"/api/v1/sources/{created['id']}/archive").json()["archived_at"]
    second = client.post(f"/api/v1/sources/{created['id']}/archive").json()["archived_at"]
    assert first is not None
    assert second is not None


def test_no_hard_delete_endpoint(client: TestClient) -> None:
    created = _create(client)
    response = client.delete(f"/api/v1/sources/{created['id']}")
    assert response.status_code == 405


def test_check_rejects_loopback_target(client: TestClient) -> None:
    """The SSRF guard must be live on the real API path, not just unit-tested
    in isolation — this hits it end to end with no fetch mocked out."""
    created = _create(client, feed_url="http://127.0.0.1:9/rss")
    response = client.post(f"/api/v1/sources/{created['id']}/check")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "non-public address" in body["error"]

    # the failed check is recorded on the source row
    source = client.get(f"/api/v1/sources/{created['id']}").json()
    assert source["last_error"] is not None
    assert source["last_error_at"] is not None


def test_check_rejects_file_scheme(client: TestClient) -> None:
    created = _create(client, feed_url="file:///etc/passwd")
    response = client.post(f"/api/v1/sources/{created['id']}/check")
    body = response.json()
    assert body["ok"] is False
    assert "scheme" in body["error"]


def test_check_404(client: TestClient) -> None:
    assert client.post("/api/v1/sources/999/check").status_code == 404
