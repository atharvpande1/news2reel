"""Six papers running the same wire copy must count as one effective
masthead, not six — see CLAUDE.md's domain rules."""

from app.core.config import Settings
from app.services.syndication import collapse_syndication

WIRE_TEXT = (
    "A major fire broke out at a chemical factory in the Butibori MIDC area on "
    "Tuesday night. Fire brigade officials said the blaze was brought under "
    "control after several hours. No casualties have been reported so far."
)


def test_identical_wire_copy_collapses_to_one_masthead(
    db_session, make_source, make_article
) -> None:
    sources = [make_source(publisher_group=f"paper-{i}") for i in range(4)]
    articles = [
        make_article(s, title="Fire at Butibori factory", body_text=WIRE_TEXT) for s in sources
    ]

    result = collapse_syndication(articles, Settings())

    assert result.raw_masthead_count == 4
    assert result.effective_masthead_count == 1
    assert len(result.duplicate_groups) == 1


def test_independent_reporting_does_not_collapse(db_session, make_source, make_article) -> None:
    sources = [make_source(publisher_group=f"paper-{i}") for i in range(3)]
    bodies = [
        "The city council approved a new drainage project for the eastern wards on Monday.",
        "Local cricket team wins the state championship after a tense final over.",
        "A new flyover was inaugurated near the railway station, easing traffic congestion.",
    ]
    articles = [
        make_article(s, title=f"Story {i}", body_text=b)
        for i, (s, b) in enumerate(zip(sources, bodies, strict=True))
    ]

    result = collapse_syndication(articles, Settings())

    assert result.raw_masthead_count == 3
    assert result.effective_masthead_count == 3
    assert len(result.duplicate_groups) == 3


def test_one_publisher_two_feeds_collapses_regardless_of_source_id(
    db_session, make_source, make_article
) -> None:
    """City edition + main edition of the same masthead share publisher_group
    but are different Source rows — both carrying the same wire copy must
    still collapse to one."""
    city_edition = make_source(name="Paper City", publisher_group="paper")
    main_edition = make_source(name="Paper Main", publisher_group="paper")
    articles = [
        make_article(city_edition, title="Fire at Butibori factory", body_text=WIRE_TEXT),
        make_article(main_edition, title="Fire at Butibori factory", body_text=WIRE_TEXT),
    ]

    result = collapse_syndication(articles, Settings())

    assert result.raw_masthead_count == 1
    assert result.effective_masthead_count == 1


def test_mixed_wire_copy_and_one_independent_article(db_session, make_source, make_article) -> None:
    wire_sources = [make_source(publisher_group=f"paper-{i}") for i in range(2)]
    independent_source = make_source(publisher_group="paper-independent")
    articles = [
        make_article(wire_sources[0], title="Fire at Butibori factory", body_text=WIRE_TEXT),
        make_article(wire_sources[1], title="Fire at Butibori factory", body_text=WIRE_TEXT),
        make_article(
            independent_source,
            title="Butibori fire: residents allege factory ignored safety warnings",
            body_text=(
                "Residents near the Butibori MIDC chemical factory say they had "
                "repeatedly flagged safety violations to the pollution control "
                "board in the months before Tuesday's fire, according to "
                "documents reviewed by this reporter."
            ),
        ),
    ]

    result = collapse_syndication(articles, Settings())

    assert result.raw_masthead_count == 3
    assert result.effective_masthead_count == 2  # one wire group + one independent
    assert len(result.duplicate_groups) == 2


def test_single_article_cluster(db_session, make_source, make_article) -> None:
    source = make_source()
    article = make_article(source, title="Solo story", body_text="Some body text.")
    result = collapse_syndication([article], Settings())
    assert result.raw_masthead_count == 1
    assert result.effective_masthead_count == 1
    assert result.duplicate_groups == [[0]]
