from conftest import FakeTransport
from gaohe.domain import AnalysisResult, FetchedArticle, Source, article_content_hash, ArticleCandidate
from gaohe.errors import ProviderError
from gaohe.pipeline import SUMMARY_KEYS, run_pending_analysis
from gaohe.providers import DirectPageFetcher, NullSearchProvider

AT = "2026-10-01T00:00:00Z"


def add_articles(store, count):
    source = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    ids = []
    for index in range(count):
        text = f"第 {index} 篇內文"
        candidate = ArticleCandidate(source, f"https://a.example/{index}", f"標題{index}", None, AT, {})
        ids.append(store.save_fetched_article(FetchedArticle(candidate, text, AT, article_content_hash(candidate.title, text)))[0])
    return ids


class Analysis:
    model = "fake-model"

    def __init__(self, failures=None):
        self.failures = failures or {}
        self.seen = []

    def analyze(self, revision):
        self.seen.append(revision.id)
        if revision.id in self.failures:
            raise self.failures[revision.id]
        return AnalysisResult(revision.id, (), ())


def run(store, analysis, **kwargs):
    return run_pending_analysis(store, analysis, NullSearchProvider(), DirectPageFetcher(FakeTransport({})), None, now=AT, **kwargs)


def test_each_revision_is_isolated_and_failures_retry(store):
    first, second, third = add_articles(store, 3)
    analysis = Analysis({second: RuntimeError("boom"), third: ProviderError("blocked")})
    summary = run(store, analysis)
    assert set(SUMMARY_KEYS) <= set(summary)
    assert (summary["analyzed"], summary["failed"], summary["stopped"]) == (1, 2, 0)
    assert store.analysis_status(first)["model"] == "fake-model"
    assert store.analysis_status(second)["last_error"].startswith("RuntimeError")
    assert store.analysis_status(third)["last_error"] == "blocked"
    assert [r.id for r in store.list_pending_revisions()] == [second, third]


def test_a_setup_or_connection_problem_stops_the_run_without_using_attempts(store):
    first, second = add_articles(store, 2)
    analysis = Analysis({first: ProviderError("auth")})
    summary = run(store, analysis)
    assert (summary["stopped"], summary["stop_code"], summary["failed"]) == (1, "auth", 0)
    assert analysis.seen == [first]  # the second revision is not attempted with a rejected key
    assert store.analysis_status(first) is None
    assert [r.id for r in store.list_pending_revisions()] == [first, second]


def test_revision_ids_and_limit_bound_the_run(store):
    first, second, third = add_articles(store, 3)
    analysis = Analysis()
    assert run(store, analysis, revision_ids=[third])["analyzed"] == 1 and analysis.seen == [third]
    assert run(store, analysis, limit=1)["analyzed"] == 1 and analysis.seen == [third, first]
