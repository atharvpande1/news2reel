from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClusterEdge(Base):
    """Persisted so a bad cluster is debuggable after the fact and the
    threshold is empirically tunable — without this, debugging a wrong
    cluster is guesswork. See CLAUDE.md's observability section."""

    __tablename__ = "cluster_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    article_a_id: Mapped[str] = mapped_column(String, ForeignKey("articles.id"), nullable=False)
    article_b_id: Mapped[str] = mapped_column(String, ForeignKey("articles.id"), nullable=False)

    embed_sim: Mapped[float] = mapped_column(Float, nullable=False)
    tfidf_sim: Mapped[float] = mapped_column(Float, nullable=False)
    combined: Mapped[float] = mapped_column(Float, nullable=False)
