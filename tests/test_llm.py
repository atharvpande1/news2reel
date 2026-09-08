"""score_cluster: retry with backoff, schema validation, content_hash
caching, and per-call usage logging — never a bare LLM call. The suite always
injects a FakeLlmClient; it must never make a real network call."""

import json

import pytest

from app.core.config import Settings
from app.db.models.llm import LlmCall, LlmScoreCache
from app.services.llm import ScoringError, score_cluster

VALID_RESPONSE = {
    "local_relevance": {"score": 8, "reason": "Directly affects a named ward."},
    "consequence": {"score": 6, "reason": "One factory, contained fire."},
    "human_interest": {"score": 4, "reason": "No named victims."},
    "novelty": {"score": 5, "reason": "First fire at this factory this year."},
    "visual_potential": {"score": 7, "reason": "Flames and smoke, strong visual."},
    "category": "accident",
    "locality": {"name": "Butibori", "admin_level": "ward"},
    "is_discrete_event": True,
    "sensitivity_flags": [],
}


class FakeLlmClient:
    def __init__(self, responses: list[str | Exception]):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, *, model: str, system: str, prompt: str):
        from app.services.llm import LlmResponse

        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResponse(text=item, prompt_tokens=100, completion_tokens=50)


def _settings(**overrides) -> Settings:
    defaults = {"llm_max_attempts": 3, "llm_retry_backoff_seconds": 0.0}
    defaults.update(overrides)
    return Settings(**defaults)


def test_score_cluster_success_on_first_attempt(db_session, make_run) -> None:
    run = make_run()
    client = FakeLlmClient([json.dumps(VALID_RESPONSE)])

    result = score_cluster(
        db_session, run.id, "hash-1", "Fire at factory", ["body"], client, _settings()
    )

    assert result.category == "accident"
    assert result.locality.name == "Butibori"
    assert client.calls == 1

    calls = db_session.query(LlmCall).filter_by(run_id=run.id).all()
    assert len(calls) == 1
    assert calls[0].outcome == "ok"
    assert calls[0].prompt_tokens == 100


def test_score_cluster_retries_on_invalid_json_then_succeeds(db_session, make_run) -> None:
    run = make_run()
    client = FakeLlmClient(["not json", json.dumps(VALID_RESPONSE)])

    result = score_cluster(
        db_session, run.id, "hash-2", "Fire at factory", ["body"], client, _settings()
    )

    assert result.category == "accident"
    assert client.calls == 2

    calls = db_session.query(LlmCall).filter_by(run_id=run.id).order_by(LlmCall.attempt).all()
    assert [c.outcome for c in calls] == ["retried", "ok"]


def test_score_cluster_raises_after_exhausting_retries(db_session, make_run) -> None:
    run = make_run()
    client = FakeLlmClient(["not json", "still not json", "nope"])

    with pytest.raises(ScoringError):
        score_cluster(
            db_session, run.id, "hash-3", "Fire at factory", ["body"], client, _settings()
        )

    assert client.calls == 3
    calls = db_session.query(LlmCall).filter_by(run_id=run.id).order_by(LlmCall.attempt).all()
    assert [c.outcome for c in calls] == ["retried", "retried", "failed"]


def test_score_cluster_rejects_response_missing_required_field(db_session, make_run) -> None:
    run = make_run()
    incomplete = dict(VALID_RESPONSE)
    del incomplete["locality"]
    client = FakeLlmClient([json.dumps(incomplete), json.dumps(VALID_RESPONSE)])

    result = score_cluster(
        db_session, run.id, "hash-4", "Fire at factory", ["body"], client, _settings()
    )
    assert result.locality.name == "Butibori"
    assert client.calls == 2


def test_score_cluster_uses_cache_and_never_calls_client_again(db_session, make_run) -> None:
    run = make_run()
    client = FakeLlmClient([json.dumps(VALID_RESPONSE)])
    score_cluster(db_session, run.id, "hash-5", "Fire at factory", ["body"], client, _settings())
    assert client.calls == 1

    # Second call, same content_hash, a client that would raise if touched.
    poisoned_client = FakeLlmClient([RuntimeError("must not be called")])
    result = score_cluster(
        db_session, run.id, "hash-5", "Fire at factory", ["body"], poisoned_client, _settings()
    )
    assert result.category == "accident"
    assert poisoned_client.calls == 0

    assert db_session.query(LlmScoreCache).filter_by(content_hash="hash-5").count() == 1
