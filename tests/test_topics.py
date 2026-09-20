from gaohe.domain import ArticleRevision, article_content_hash


def revision(
    revision_id: int,
    url: str,
    title: str,
    text: str,
    fetched_at: str = "2026-09-20T12:00:00Z",
) -> ArticleRevision:
    return ArticleRevision(revision_id, revision_id, url, title, text, article_content_hash(title, text), fetched_at)


def test_group_revision_marks_same_event_with_independent_anchors_high():
    from gaohe.topics import group_revision

    existing = revision(
        1,
        "https://alpha.test/forum",
        "Taipei defense forum opens",
        "Taipei Defense Ministry forum opens with 1000 delegates.",
    )
    incoming = revision(
        2,
        "https://bravo.test/security-forum",
        "Taipei security forum begins",
        "The Taipei Defense Ministry forum begins with 1000 delegates.",
        "2026-09-21T10:00:00Z",
    )

    topic = group_revision(incoming, (existing,))

    assert topic is not None
    assert (topic.confidence, topic.status) == ("high", "active")


def test_group_revision_does_not_group_articles_that_only_share_a_person():
    from gaohe.topics import group_revision

    existing = revision(1, "https://alpha.test/budget", "Chen Wei discusses the budget", "Chen Wei spoke about the city budget.")
    incoming = revision(2, "https://bravo.test/health", "Chen Wei visits hospital", "Chen Wei visited a hospital after the storm.")

    assert group_revision(incoming, (existing,)) is None


def test_group_revision_never_treats_an_updated_same_url_as_cross_media_peer():
    from gaohe.topics import group_revision

    original = revision(1, "https://alpha.test/forum", "Taipei defense forum", "Taipei Defense Ministry forum has 1000 delegates.")
    update = revision(2, "https://alpha.test/forum", "Taipei defense forum update", "Taipei Defense Ministry forum now has 1100 delegates.")

    assert group_revision(update, (original,)) is None


def test_group_revision_returns_possible_for_a_near_match_without_independent_anchors():
    from gaohe.topics import group_revision

    existing = revision(1, "https://alpha.test/forum", "Taipei forum", "Taipei forum discussed local issues.")
    incoming = revision(2, "https://bravo.test/forum", "Taipei forum", "Taipei forum covered public questions.")

    topic = group_revision(incoming, (existing,))

    assert topic is not None
    assert (topic.confidence, topic.status) == ("possible", "possible")


def test_group_revision_respects_time_window_and_source_hosts():
    from gaohe.topics import group_revision

    existing = revision(1, "https://alpha.test/forum", "Taipei defense forum", "Taipei Defense Ministry forum has 1000 delegates.")
    late = revision(2, "https://bravo.test/forum", "Taipei defense forum", "Taipei Defense Ministry forum has 1000 delegates.", "2026-09-25T12:00:01Z")
    same_host = revision(3, "https://alpha.test/other", "Taipei defense forum", "Taipei Defense Ministry forum has 1000 delegates.")

    assert group_revision(late, (existing,)) is None
    possible = group_revision(same_host, (existing,))
    assert possible is not None
    assert possible.confidence == "possible"


def test_compare_topic_returns_a_bounded_material_numeric_difference_with_source_context():
    from gaohe.providers import MAX_QUERY_CHARS
    from gaohe.topics import compare_topic

    primary = revision(1, "https://alpha.test/forum?token=secret", "Taipei defense forum", "Taipei Defense Ministry reports 100 troops deployed.")
    peer = revision(2, "https://bravo.test/forum", "Taipei security forum", "Taipei Defense Ministry reports 1000 troops deployed.", "2026-09-20T13:00:00Z")

    candidates = compare_topic((primary, peer))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.finding_type == "material_cross_media_difference"
    assert candidate.materiality == "material"
    assert candidate.claim_id is None
    assert candidate.revision_id == primary.id
    assert primary.text[candidate.start:candidate.end] == "100 troops"
    assert "https://alpha.test/forum" in candidate.summary
    assert "https://bravo.test/forum" in candidate.summary
    assert "secret" not in candidate.summary + (candidate.query or "")
    assert candidate.query is not None and len(candidate.query) <= MAX_QUERY_CHARS


def test_compare_topic_skips_possible_groups_and_harmless_wording_differences():
    from gaohe.topics import compare_topic

    possible_left = revision(1, "https://alpha.test/forum", "Taipei forum", "Taipei forum discussed local issues.")
    possible_right = revision(2, "https://bravo.test/forum", "Taipei forum", "Taipei forum covered public questions.")
    harmless_left = revision(3, "https://charlie.test/forum", "Taipei defense forum", "Taipei Defense Ministry forum opens today.")
    harmless_right = revision(4, "https://delta.test/forum", "Taipei security forum", "The Taipei Defense Ministry forum begins today.")

    assert compare_topic((possible_left, possible_right)) == []
    assert compare_topic((harmless_left, harmless_right)) == []


def test_compare_topic_keeps_approximate_450_500_520_as_background():
    from gaohe.topics import compare_topic

    left = revision(1, "https://alpha.test/forum", "Taipei defense forum", "Taipei Defense Ministry says about 450 delegates attended the forum.")
    right = revision(2, "https://bravo.test/forum", "Taipei security forum", "Taipei Defense Ministry says nearly 520 delegates attended the forum.")

    assert compare_topic((left, right)) == []


def test_compare_topic_requires_exact_bounded_spans_and_never_compares_same_url_revisions():
    from gaohe.providers import MAX_QUERY_CHARS
    from gaohe.topics import compare_topic

    original = revision(1, "https://alpha.test/forum", "Taipei defense forum", "Taipei Defense Ministry reports 100 troops deployed.")
    update = revision(2, "https://alpha.test/forum", "Taipei defense forum update", "Taipei Defense Ministry reports 1000 troops deployed.")
    assert compare_topic((original, update)) == []

    primary = revision(3, "https://charlie.test/forum", "Taipei defense forum", "Taipei Defense Ministry reports 100 troops deployed.")
    peer = revision(4, "https://delta.test/forum", "Taipei security forum", "Taipei Defense Ministry reports 1000 troops deployed.")
    candidate = compare_topic((primary, peer))[0]

    assert 0 <= candidate.start < candidate.end <= len(primary.text)
    assert primary.text[candidate.start:candidate.end] == "100 troops"
    assert candidate.query is not None and len(candidate.query) <= MAX_QUERY_CHARS
