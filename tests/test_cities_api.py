"""Cities CRUD and the onboarding rule: a city and its feeds arrive together,
and a source is never created on its own. See CLAUDE.md's invariants."""

from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models.source import Source


def _source(**overrides) -> dict:
    payload = {
        "name": "Test Times",
        "feed_url": "https://example.test/rss",
        "language": "en",
        "publisher_group": "test-times",
    }
    payload.update(overrides)
    return payload


async def _create(client: AsyncClient, name: str = "Nagpur", count: int = 1, **overrides) -> dict:
    payload = {
        "name": name,
        "sources": [_source(name=f"Feed {n}", publisher_group=f"g{n}") for n in range(count)],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/cities", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_city_is_created_with_its_sources(client: AsyncClient) -> None:
    body = await _create(client, count=3, state="Maharashtra")
    assert body["name"] == "Nagpur"
    assert body["slug"] == "nagpur"
    assert body["state"] == "Maharashtra"
    assert body["source_count"] == 3
    assert [s["city_name"] for s in body["sources"]] == ["Nagpur"] * 3


async def test_a_city_needs_at_least_one_source(client: AsyncClient) -> None:
    """A city with no feeds produces nothing and reports nothing — which looks
    exactly like a quiet news day."""
    assert (
        await client.post("/api/v1/cities", json={"name": "Nagpur", "sources": []})
    ).status_code == 422


async def test_a_bad_source_rolls_the_whole_city_back(client: AsyncClient, db_session) -> None:
    """Onboarding is one transaction. A city that half-committed would sit there
    with fewer feeds than were entered and no error to explain it."""
    response = await client.post(
        "/api/v1/cities",
        json={
            "name": "Nagpur",
            "sources": [_source(), _source(fetch_interval_minutes=0)],
        },
    )
    assert response.status_code == 422
    assert (await client.get("/api/v1/cities")).json() == []
    assert (await db_session.scalar(select(func.count()).select_from(Source))) == 0


async def test_the_slug_is_normalised_so_one_city_is_one_row(client: AsyncClient) -> None:
    """The slug is what `/city/<slug>/` URL segments resolve against, so two
    spellings of one city would make that resolution ambiguous."""
    assert (await _create(client, name="  Nagpur  "))["slug"] == "nagpur"
    duplicate = await client.post("/api/v1/cities", json={"name": "NAGPUR", "sources": [_source()]})
    assert duplicate.status_code == 409


class TestTheSourceCap:
    async def test_five_is_allowed(self, client: AsyncClient) -> None:
        assert (await _create(client, count=5))["source_count"] == 5

    async def test_six_at_onboarding_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/cities",
            json={
                "name": "Nagpur",
                "sources": [_source(name=f"F{n}", publisher_group=f"g{n}") for n in range(6)],
            },
        )
        assert response.status_code == 422
        assert "at most 5" in str(response.json()["detail"])

    async def test_a_sixth_added_later_is_rejected(self, client: AsyncClient) -> None:
        city = await _create(client, count=5)
        response = await client.post(
            f"/api/v1/cities/{city['id']}/sources", json=_source(name="Sixth")
        )
        assert response.status_code == 422

    async def test_archiving_a_source_frees_its_slot(self, client: AsyncClient) -> None:
        """The cap is about how much an editor reads, not how much history we
        keep — so a dead feed should not hold a slot forever."""
        city = await _create(client, count=5)
        await client.post(f"/api/v1/sources/{city['sources'][0]['id']}/archive")

        assert (await client.get(f"/api/v1/cities/{city['id']}")).json()["source_count"] == 4
        added = await client.post(
            f"/api/v1/cities/{city['id']}/sources", json=_source(name="Sixth")
        )
        assert added.status_code == 201


async def test_adding_a_source_to_a_missing_city_is_404(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/cities/999/sources", json=_source())).status_code == 404


class TestArchiving:
    async def test_archiving_a_city_archives_its_sources(
        self, client: AsyncClient, db_session
    ) -> None:
        """The load-bearing half. The scheduler filters on the source and knows
        nothing about cities, so feeds left active would keep being polled — and
        keep paying the classifier — for a city nobody can select."""
        city = await _create(client, count=2)
        body = (await client.post(f"/api/v1/cities/{city['id']}/archive")).json()
        assert body["archived_at"] is not None

        live = await db_session.scalar(
            select(func.count()).select_from(Source).where(Source.archived_at.is_(None))
        )
        assert live == 0

    async def test_archiving_hides_the_city_but_keeps_it(self, client: AsyncClient) -> None:
        city = await _create(client)
        await client.post(f"/api/v1/cities/{city['id']}/archive")

        assert (await client.get("/api/v1/cities")).json() == []
        assert len((await client.get("/api/v1/cities?include_archived=true")).json()) == 1
        # still readable by id — archiving isn't deletion
        assert (await client.get(f"/api/v1/cities/{city['id']}")).status_code == 200

    async def test_archiving_is_idempotent(self, client: AsyncClient) -> None:
        city = await _create(client)
        first = (await client.post(f"/api/v1/cities/{city['id']}/archive")).json()["archived_at"]
        second = (await client.post(f"/api/v1/cities/{city['id']}/archive")).json()["archived_at"]
        assert first is not None and second is not None

    async def test_no_hard_delete_endpoint(self, client: AsyncClient) -> None:
        """Sources hold city_id forever — a delete would cascade away history."""
        city = await _create(client)
        assert (await client.delete(f"/api/v1/cities/{city['id']}")).status_code == 405


class TestUnarchiving:
    async def test_it_brings_back_the_city_and_its_feeds(
        self, client: AsyncClient, db_session
    ) -> None:
        """The cascade has to reverse. A city back in the picker whose every feed
        is still archived produces nothing and reports nothing — the same failure
        the "a city arrives with its feeds" rule exists to prevent."""
        city = await _create(client, count=2)
        await client.post(f"/api/v1/cities/{city['id']}/archive")

        body = (await client.post(f"/api/v1/cities/{city['id']}/unarchive")).json()
        assert body["archived_at"] is None
        assert body["source_count"] == 2

        archived = await db_session.scalar(
            select(func.count()).select_from(Source).where(Source.archived_at.isnot(None))
        )
        assert archived == 0

    async def test_it_revives_a_feed_archived_before_the_city_was(
        self, client: AsyncClient
    ) -> None:
        """Known and accepted: the row records *that* a feed was archived, never
        why, so unarchive cannot tell a hand-retired feed from one the cascade
        took down and brings back both. Pinned so the behaviour is a decision
        rather than a surprise."""
        city = await _create(client, count=2)
        retired = city["sources"][0]["id"]
        await client.post(f"/api/v1/sources/{retired}/archive")
        await client.post(f"/api/v1/cities/{city['id']}/archive")

        body = (await client.post(f"/api/v1/cities/{city['id']}/unarchive")).json()
        assert body["source_count"] == 2

    async def test_it_is_idempotent(self, client: AsyncClient) -> None:
        city = await _create(client)
        await client.post(f"/api/v1/cities/{city['id']}/archive")
        assert (await client.post(f"/api/v1/cities/{city['id']}/unarchive")).json()[
            "archived_at"
        ] is None
        assert (await client.post(f"/api/v1/cities/{city['id']}/unarchive")).json()[
            "archived_at"
        ] is None

    async def test_unknown_city_is_404(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/cities/999/unarchive")).status_code == 404


class TestTheSourceList:
    """`sources` carries archived feeds; `source_count` counts only live ones.
    The admin page needs both — the table draws the live ones, the header counts
    every feed the city has ever had, and the cap is measured on the live ones."""

    async def test_archived_feeds_stay_in_the_list_but_leave_the_count(
        self, client: AsyncClient
    ) -> None:
        city = await _create(client, count=3)
        await client.post(f"/api/v1/sources/{city['sources'][0]['id']}/archive")

        body = (await client.get(f"/api/v1/cities/{city['id']}")).json()
        assert body["source_count"] == 2
        assert len(body["sources"]) == 3
        assert sum(1 for s in body["sources"] if s["archived_at"]) == 1

    async def test_an_archived_city_still_reports_the_feeds_it_had(
        self, client: AsyncClient
    ) -> None:
        """Cascade archiving stamps every feed, so a live-only list said nothing
        at all about an archived city — it looked like one that never had any."""
        city = await _create(client, count=2)
        await client.post(f"/api/v1/cities/{city['id']}/archive")

        body = (await client.get(f"/api/v1/cities/{city['id']}")).json()
        assert body["source_count"] == 0
        assert len(body["sources"]) == 2


async def test_patch_renames_but_cannot_change_the_slug(client: AsyncClient) -> None:
    """The slug is the key articles were already resolved against; changing it
    would orphan them."""
    city = await _create(client)
    body = (await client.patch(f"/api/v1/cities/{city['id']}", json={"name": "Nagpur City"})).json()
    assert body["name"] == "Nagpur City"
    assert body["slug"] == "nagpur"


async def test_get_and_patch_404(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/cities/999")).status_code == 404
    assert (await client.patch("/api/v1/cities/999", json={"name": "x"})).status_code == 404
    assert (await client.post("/api/v1/cities/999/archive")).status_code == 404


class TestRefresh:
    """POST /cities/{id}/refresh — the button that polls a city's feeds now."""

    async def test_it_reports_per_source_outcomes(self, client: AsyncClient) -> None:
        """The feed URLs here are unreachable, which is the point: a refresh
        that cannot reach anything still answers 200 with what happened, rather
        than failing the request. Per-source failures are never fatal."""
        city = await _create(client, count=2)
        response = await client.post(f"/api/v1/cities/{city['id']}/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["city_id"] == city["id"]
        assert body["sources_polled"] == 2
        assert body["failed"] == 2
        assert body["new_articles"] == 0
        assert sorted(s["name"] for s in body["sources"]) == ["Feed 0", "Feed 1"]
        assert all(s["error"] for s in body["sources"])

    async def test_it_records_the_failure_on_the_source(
        self, client: AsyncClient, db_session
    ) -> None:
        """Same health surface a scheduled tick writes, so a manual refresh and
        a poll leave consistent history.

        expunge_all first: the refresh writes from its own session, and this
        test's session is still holding the rows it read before that happened.
        """
        city = await _create(client)
        await client.post(f"/api/v1/cities/{city['id']}/refresh")
        db_session.expunge_all()

        source = (await client.get(f"/api/v1/sources/{city['sources'][0]['id']}")).json()
        assert source["last_error"] is not None
        assert source["last_fetched_at"] is not None

    async def test_it_skips_archived_sources(self, client: AsyncClient) -> None:
        city = await _create(client, count=2)
        await client.post(f"/api/v1/sources/{city['sources'][0]['id']}/archive")

        body = (await client.post(f"/api/v1/cities/{city['id']}/refresh")).json()
        assert body["sources_polled"] == 1

    async def test_a_city_with_nothing_to_poll_is_still_200(self, client: AsyncClient) -> None:
        city = await _create(client)
        await client.post(f"/api/v1/sources/{city['sources'][0]['id']}/archive")

        body = (await client.post(f"/api/v1/cities/{city['id']}/refresh")).json()
        assert body["sources_polled"] == 0
        assert body["sources"] == []

    async def test_unknown_city_is_404(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/cities/999/refresh")).status_code == 404
