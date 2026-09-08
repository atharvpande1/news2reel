from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWS2REEL_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./news2reel.db"

    # Set to require this value in an `X-API-Key` header on mutating endpoints.
    # Leave unset only when the server is bound to 127.0.0.1 (see CLAUDE.md's
    # runtime constraints — POST /runs and outbound fetches cost real money).
    api_shared_secret: str | None = None

    # core/net.py fetch limits — every outbound fetch (feed polling, source
    # checks) goes through these caps. See "External input is hostile".
    fetch_connect_timeout_seconds: float = 5.0
    fetch_read_timeout_seconds: float = 10.0
    fetch_max_response_bytes: int = 5_000_000
    fetch_max_redirects: int = 3

    # Local multilingual model — no per-run API cost or latency. Bump the
    # version suffix in embedding_model_version, not just the name, if you
    # change model weights: it invalidates the embedding cache and the tuned
    # similarity_threshold. See CLAUDE.md's domain rules.
    embedding_model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_model_version: str = "v1"

    # HashingVectorizer needs no fitting, so it's inherently stationary across
    # runs — see CLAUDE.md: "TF-IDF IDF must be stationary across runs".
    tfidf_n_features: int = 2**18
    tfidf_ngram_min: int = 3
    tfidf_ngram_max: int = 5

    # Clustering — all need real-data tuning; see docs/plan-phase-1.md's
    # verification section for how.
    cluster_window_hours: int = 36
    similarity_threshold: float = 0.55
    cluster_density_floor: float = 0.45
    max_cluster_size: int = 8
    max_split_depth: int = 3
    split_threshold_step: float = 0.1

    # Syndication collapse — near-identical body text within a cluster.
    syndication_similarity_threshold: float = 0.9

    # Prescore funnel width — the LLM funnel is load-bearing (CLAUDE.md):
    # never score every article, only the strongest ~30 clusters.
    scoring_funnel_size: int = 30

    # Selection.
    lineup_size: int = 10
    alternate_size: int = 10
    category_cap_default: int = 3
    videoability_floor: float = 0.4

    # Cross-day seen-store — wider than the clustering window since follow-ups
    # span days, not hours. The threshold is also lower than
    # similarity_threshold: a follow-up headline is often reworded entirely
    # while clustering same-day duplicates expects near-identical wording.
    # Empirically (see tests/test_seen.py): a genuine follow-up measured
    # ~0.45-0.47 combined similarity against the original; an unrelated
    # same-locality story measured ~0.02 — wide margin either side of 0.4,
    # but this needs real-data validation like every threshold here.
    seen_window_days: int = 14
    seen_similarity_threshold: float = 0.4

    # services/llm.py — retry with backoff, never a bare API call. No key
    # means score_cluster raises before ever constructing OpenAIClient; the
    # test suite always injects a fake LlmClient instead.
    llm_api_key: str | None = None
    # Lightest tier — cluster scoring is a high-volume, well-specified
    # extraction task (five axis scores + category + locality + a bool
    # against a strict schema), not open-ended reasoning; funnel width
    # (~30 clusters/run) makes per-call cost the thing to keep down.
    llm_model: str = "gpt-5-nano"
    llm_max_attempts: int = 3
    llm_retry_backoff_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
