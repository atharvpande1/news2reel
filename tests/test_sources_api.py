"""Sources read/edit lifecycle, plus the invariants CLAUDE.md calls out: no
hard delete, no standalone create, and /check goes through the SSRF-safe
fetcher for real."""

from httpx import AsyncClient

_CITIES = {"n": 0}


async def _create(client: AsyncClient, city: str = "Nagpur", **overrides) -> dict:
    """A source can only be born inside a city. The first call for a given city
    onboards it with this source; later ones add to it."""
    payload = {
        "name": "Test Times",
        "feed_url": "https://example.test/rss",
        "language": "en",
        "publisher_group": "test-times",
    }
    payload.update(overrides)

    existing = next(
        (c for c in (await client.get("/api/v1/cities")).json() if c["name"] == city), None
    )
    if existing is None:
        response = await client.post("/api/v1/cities", json={"name": city, "sources": [payload]})
        assert response.status_code == 201, response.text
        return response.json()["sources"][0]

    response = await client.post(f"/api/v1/cities/{existing['id']}/sources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_source(client: AsyncClient) -> None:
    body = await _create(client)
    assert body["name"] == "Test Times"
    assert body["city_name"] == "Nagpur"
    assert body["enabled"] is True
    assert body["archived_at"] is None


async def test_get_source(client: AsyncClient) -> None:
    created = await _create(client)
    response = await client.get(f"/api/v1/sources/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_source_404(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/sources/999")).status_code == 404


async def test_list_sources(client: AsyncClient) -> None:
    await _create(client, name="A", publisher_group="a")
    await _create(client, name="B", publisher_group="b")

    response = await client.get("/api/v1/sources")
    assert response.status_code == 200
    assert [s["name"] for s in response.json()] == ["A", "B"]


async def test_patch_source_partial_update(client: AsyncClient) -> None:
    created = await _create(client)
    response = await client.patch(f"/api/v1/sources/{created['id']}", json={"enabled": False})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["name"] == "Test Times"  # untouched fields survive PATCH


async def test_patch_source_404(client: AsyncClient) -> None:
    assert (await client.patch("/api/v1/sources/999", json={"enabled": False})).status_code == 404


async def test_archive_is_soft_delete_and_hides_from_default_list(client: AsyncClient) -> None:
    created = await _create(client)

    archived = await client.post(f"/api/v1/sources/{created['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    assert (await client.get("/api/v1/sources")).json() == []
    assert len((await client.get("/api/v1/sources?include_archived=true")).json()) == 1

    # still readable directly by id — archiving isn't deletion
    assert (await client.get(f"/api/v1/sources/{created['id']}")).status_code == 200


async def test_archive_is_idempotent(client: AsyncClient) -> None:
    created = await _create(client)
    first = (await client.post(f"/api/v1/sources/{created['id']}/archive")).json()["archived_at"]
    second = (await client.post(f"/api/v1/sources/{created['id']}/archive")).json()["archived_at"]
    assert first is not None
    assert second is not None


async def test_no_hard_delete_endpoint(client: AsyncClient) -> None:
    created = await _create(client)
    response = await client.delete(f"/api/v1/sources/{created['id']}")
    assert response.status_code == 405


async def test_no_standalone_create_endpoint(client: AsyncClient) -> None:
    """A source is only ever born inside a city — CLAUDE.md's invariant. A feed
    with no city can never produce a local story, and locality is the rank."""
    response = await client.post(
        "/api/v1/sources",
        json={
            "name": "Orphan",
            "feed_url": "https://example.test/rss",
            "language": "en",
            "publisher_group": "orphan",
        },
    )
    assert response.status_code == 405


async def test_check_rejects_loopback_target(client: AsyncClient) -> None:
    """The SSRF guard must be live on the real API path, not just unit-tested
    in isolation — this hits it end to end with no fetch mocked out."""
    created = await _create(client, feed_url="http://127.0.0.1:9/rss")
    response = await client.post(f"/api/v1/sources/{created['id']}/check")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "non-public address" in body["error"]

    # the failed check is recorded on the source row
    source = (await client.get(f"/api/v1/sources/{created['id']}")).json()
    assert source["last_error"] is not None
    assert source["last_error_at"] is not None


async def test_check_rejects_file_scheme(client: AsyncClient) -> None:
    created = await _create(client, feed_url="file:///etc/passwd")
    response = await client.post(f"/api/v1/sources/{created['id']}/check")
    body = response.json()
    assert body["ok"] is False
    assert "scheme" in body["error"]


async def test_check_404(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/sources/999/check")).status_code == 404


async def test_patch_cannot_move_a_source_between_cities(client: AsyncClient) -> None:
    """Not supported on purpose: a move would land a source in a city that may
    already be at its cap, behind the check the service does on the way in."""
    created = await _create(client, city="Pune")
    response = await client.patch(
        f"/api/v1/sources/{created['id']}", json={"city_id": 999, "name": "Renamed"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["city_id"] == created["city_id"]  # the unknown field was ignored


async def test_is_local_outlet_defaults_to_false(client: AsyncClient) -> None:
    assert (await _create(client))["is_local_outlet"] is False


async def test_is_local_outlet_round_trips(client: AsyncClient) -> None:
    created = await _create(client, is_local_outlet=True)
    assert created["is_local_outlet"] is True

    patched = await client.patch(
        f"/api/v1/sources/{created['id']}", json={"is_local_outlet": False}
    )
    assert patched.status_code == 200
    assert patched.json()["is_local_outlet"] is False
