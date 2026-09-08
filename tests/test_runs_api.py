"""API contract for /runs, /runs/{id}/stories and /runs/{id}/report.

POST /runs is tested against its immediate contract (202, idempotency,
force=true) with the background pipeline stubbed out — BackgroundTasks would
otherwise reach for the real production SessionLocal/AnthropicClient (see
pipeline.execute_run_in_background), which is the wrong thing to exercise in
an API-layer test. Story/report reads are tested against data seeded by a
direct run_pipeline() call against the test's own db_session — the same
pipeline test_pipeline.py already verifies end to end.
"""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.models.run import Run
from app.services.pipeline import run_pipeline
from tests.test_pipeline import LOGICAL_DATE, ConstantLlmClient, _Handler


def test_create_run_returns_202_and_queued(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.v1.endpoints.runs.execute_run_in_background", lambda run_id: None)
    response = client.post("/api/v1/runs", params={"logical_date": "2026-02-01"})
    assert response.status_code == 202
    body = response.json()
    assert body["logical_date"] == "2026-02-01"
    assert body["attempt"] == 1
    assert body["is_current"] is True


def test_create_run_is_idempotent_for_same_date(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.v1.endpoints.runs.execute_run_in_background", lambda run_id: None)
    first = client.post("/api/v1/runs", params={"logical_date": "2026-02-02"}).json()
    second = client.post("/api/v1/runs", params={"logical_date": "2026-02-02"}).json()
    assert first["id"] == second["id"]


def test_create_run_force_creates_new_attempt(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.v1.endpoints.runs.execute_run_in_background", lambda run_id: None)
    first = client.post("/api/v1/runs", params={"logical_date": "2026-02-03"}).json()
    second = client.post(
        "/api/v1/runs", params={"logical_date": "2026-02-03", "force": "true"}
    ).json()

    assert second["id"] != first["id"]
    assert second["attempt"] == 2
    assert second["is_current"] is True

    # the previous attempt is retained but no longer current
    stale = client.get(f"/api/v1/runs/{first['id']}").json()
    assert stale["is_current"] is False


def test_get_run_404(client: TestClient) -> None:
    assert client.get("/api/v1/runs/999").status_code == 404


def test_list_runs(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.v1.endpoints.runs.execute_run_in_background", lambda run_id: None)
    client.post("/api/v1/runs", params={"logical_date": "2026-02-04"})
    client.post("/api/v1/runs", params={"logical_date": "2026-02-05"})
    response = client.get("/api/v1/runs")
    assert response.status_code == 200
    assert len(response.json()) >= 2


def _seed_ready_run(db_session, local_http_server) -> int:
    with local_http_server(_Handler) as base_url:
        _make_sources(db_session, base_url)
        run = Run(logical_date=LOGICAL_DATE)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        run_pipeline(db_session, run, Settings(), ConstantLlmClient())
    return run.id


def _make_sources(db_session, base_url: str) -> None:
    from app.db.models.source import Source

    db_session.add_all(
        [
            Source(
                name="A",
                feed_url=f"{base_url}/a.xml",
                language="en",
                publisher_group="paper-a",
            ),
            Source(
                name="B",
                feed_url=f"{base_url}/b.xml",
                language="en",
                publisher_group="paper-b",
            ),
        ]
    )
    db_session.commit()


def test_get_stories_default_returns_selected_only(
    client: TestClient, db_session, local_http_server, allow_loopback
) -> None:
    run_id = _seed_ready_run(db_session, local_http_server)
    response = client.get(f"/api/v1/runs/{run_id}/stories")
    assert response.status_code == 200
    stories = response.json()
    assert len(stories) == 2
    assert all(s["selected"] is True for s in stories)
    assert [s["order"] for s in stories] == sorted(s["order"] for s in stories)


def test_get_stories_with_alternates(
    client: TestClient, db_session, local_http_server, allow_loopback
) -> None:
    run_id = _seed_ready_run(db_session, local_http_server)
    response = client.get(f"/api/v1/runs/{run_id}/stories", params={"include": "alternates"})
    assert response.status_code == 200
    # both seeded stories pass the floor/cap trivially and are selected, so
    # there are no alternates here — the point is the request succeeds and
    # doesn't duplicate the selected ones.
    stories = response.json()
    assert len({s["id"] for s in stories}) == len(stories)


def test_get_stories_404_for_unknown_run(client: TestClient) -> None:
    assert client.get("/api/v1/runs/999/stories").status_code == 404


def test_get_report_renders_html(
    client: TestClient, db_session, local_http_server, allow_loopback
) -> None:
    run_id = _seed_ready_run(db_session, local_http_server)
    response = client.get(f"/api/v1/runs/{run_id}/report")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Butibori" in response.text
    assert f"Run {run_id}" in response.text


def test_get_report_escapes_untrusted_content(
    client: TestClient, db_session, local_http_server, allow_loopback
) -> None:
    """A headline containing markup must not execute in the rendered page —
    autoescape covers text nodes; safe_url covers the one place a raw URL is
    rendered into an href. See CLAUDE.md: "External input is hostile"."""
    run_id = _seed_ready_run(db_session, local_http_server)

    from app.db.models.story import Story

    story = db_session.query(Story).filter_by(run_id=run_id).first()
    story.headline = "<script>alert(1)</script>"
    story.sources = [
        {
            "source_name": "Evil",
            "publisher_group": "x",
            "url": "javascript:alert(1)",
            "feed_position": 1,
        }
    ]
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run_id}/report")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert 'href="javascript:alert(1)"' not in response.text
    assert 'href="#"' in response.text


def test_get_report_404(client: TestClient) -> None:
    assert client.get("/api/v1/runs/999/report").status_code == 404
