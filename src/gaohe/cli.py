import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .config import Settings, load_settings
from .domain import REVIEW_STATUSES, Source
from .monitor import watch_once
from .pipeline import local_day_start, run_pending_analysis, utc_iso
from .providers import DirectPageFetcher, build_analysis_provider, build_evidence_assessor, build_search_provider
from .sources import UrllibTransport
from .safety import is_http_url as _is_http_url, redact_text, redact_url
from .storage import SCHEMA_VERSION, Store
from .web import serve
from .setup_flow import run_setup_wizard
from .scheduler import SchedulerRunner, install_task, remove_task, task_status


__all__ = ["build_parser", "main", "run_pending_analysis"]

# The local page is for this computer only (spec 9): never bind a LAN or public interface.
LOCAL_SERVE_HOSTS = ("127.0.0.1", "localhost", "::1")
_ANALYSIS_ORDER = ("pending", "running", "completed", "failed", "skipped", "unanalyzed")
_RUN_KEYS = ("started_at", "finished_at", "sources_checked", "candidates_seen", "revisions_created", "failures")
_MAX_LINE_TEXT_CHARS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaohe")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    status = commands.add_parser("status")
    status.add_argument("--env-file", type=Path, default=Path(".env"))
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    setup = commands.add_parser("setup")
    setup.add_argument("--env-file", type=Path, default=Path(".env"))
    setup.add_argument("--no-browser", action="store_true")
    schedule = commands.add_parser("schedule")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_install = schedule_commands.add_parser("install")
    schedule_install.add_argument("--task-name", default="GaoHe Watch")
    schedule_install.add_argument("--project-dir", type=Path)
    schedule_install.add_argument("--python-path", type=Path)
    schedule_install.add_argument("--env-file", type=Path, default=Path(".env"))
    for action in ("status", "remove"):
        schedule_action = schedule_commands.add_parser(action)
        schedule_action.add_argument("--task-name", default="GaoHe Watch")
    watch = commands.add_parser("watch")
    watch.add_argument("--once", action="store_true", required=True)
    watch.add_argument("--env-file", type=Path, default=Path(".env"))
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--pending", action="store_true", required=True)
    analyze.add_argument("--limit", default="20")
    analyze.add_argument("--env-file", type=Path, default=Path(".env"))
    source = commands.add_parser("source")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    add = source_commands.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--feed-url", required=True)
    add.add_argument("--article-url")
    add.add_argument("--env-file", type=Path, default=Path(".env"))
    listing = source_commands.add_parser("list")
    listing.add_argument("--env-file", type=Path, default=Path(".env"))
    for action in ("disable", "enable"):
        toggle = source_commands.add_parser(action)
        toggle.add_argument("--id", type=int, required=True)
        toggle.add_argument("--env-file", type=Path, default=Path(".env"))
    finding = commands.add_parser("finding")
    finding_commands = finding.add_subparsers(dest="finding_command", required=True)
    finding_list = finding_commands.add_parser("list")
    finding_list.add_argument("--all", action="store_true", help="include findings that are not visible")
    finding_list.add_argument("--limit", default="50")
    finding_list.add_argument("--env-file", type=Path, default=Path(".env"))
    finding_review = finding_commands.add_parser("review")
    finding_review.add_argument("--id", type=int, required=True)
    finding_review.add_argument("--status", choices=sorted(REVIEW_STATUSES - {"unreviewed"}), required=True)
    finding_review.add_argument("--note")
    finding_review.add_argument("--env-file", type=Path, default=Path(".env"))
    topic = commands.add_parser("topic")
    topic_commands = topic.add_subparsers(dest="topic_command", required=True)
    topic_list = topic_commands.add_parser("list")
    topic_list.add_argument("--limit", default="50")
    topic_list.add_argument("--env-file", type=Path, default=Path(".env"))
    topic_review = topic_commands.add_parser("review")
    topic_review.add_argument("--id", type=int, required=True)
    topic_review.add_argument("--status", choices=("active", "dismissed"), required=True)
    topic_review.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def _store(settings: Settings) -> Store:
    store = Store(settings.database_path)
    store.initialize()
    return store


def _positive_int(value: str) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 1 else None


def _line_text(value: object) -> str:
    """One-line, redacted, bounded free text for terminal output; '-' when empty."""
    text = " ".join(redact_text(value, _MAX_LINE_TEXT_CHARS).split())
    return text or "-"


def _line_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    try:
        return redact_url(value)
    except ValueError:
        return "-"


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _print_status(settings: Settings, store: Store) -> None:
    snapshot = store.dashboard_snapshot(limit=1)
    counts = snapshot["analysis"]
    calls_today = store.count_llm_calls_since(local_day_start(datetime.now().astimezone()))
    print(f"schema_version={SCHEMA_VERSION}")
    print("analysis " + " ".join(f"{key}={counts.get(key, 0)}" for key in _ANALYSIS_ORDER))
    print(f"llm_calls_today={calls_today} daily_llm_call_limit={settings.daily_llm_call_limit}")
    last_run = snapshot["last_run"]
    if isinstance(last_run, Mapping):
        values = " ".join(f"{key}={'-' if last_run.get(key) is None else last_run.get(key)}" for key in _RUN_KEYS)
        print(f"last_run {values}")
    else:
        print("last_run=none")
    feeds = {source.id: source.feed_url for source in store.list_sources()}
    for item in snapshot["sources"]:
        checked = item["checked_at"] or "-"
        seen = item["candidates_seen"] if item["candidates_seen"] is not None else "-"
        status = _line_text(item["status"]).replace(" ", "_")
        print(
            f"source id={item['id']} enabled={_yes_no(item['enabled'])} status={status} "
            f"checked_at={checked} candidates_seen={seen} feed_url={_line_url(feeds.get(item['id']))} "
            f"error={_line_text(item['error'])} name={_line_text(item['name'])}"
        )


def _print_findings(store: Store, limit: int, include_hidden: bool) -> None:
    findings = store.list_findings(limit, visible_only=not include_hidden)
    if not findings:
        print("no findings" if include_hidden else "no visible findings (use --all to include pending ones)")
    for item in findings:
        print(
            f"id={item['id']} type={item['finding_type']} evidence_status={item['evidence_status']} "
            f"review_status={item['review_status']} visible={_yes_no(item['visible'])} "
            f"title={_line_text(item['article_title'])} url={_line_url(item['article_url'])} "
            f"summary={_line_text(item['summary'])}"
        )


def _print_topics(store: Store, limit: int) -> None:
    topics = store.dashboard_snapshot(limit=limit)["comparisons"]
    if not topics:
        print("no topics")
    for item in topics:
        print(
            f"id={item['id']} status={item['status']} confidence={item['confidence']} "
            f"articles={len(item['articles'])} label={_line_text(item['label'])}"
        )
        for article in item["articles"]:
            print(
                f"  revision_id={article['revision_id']} source={_line_text(article['source'])} "
                f"title={_line_text(article['title'])} url={_line_url(article['url'])}"
            )


def _review(args: argparse.Namespace) -> int:
    """Handle `finding review` and `topic review`; exit 2 when the id does not exist."""
    kind = args.command
    try:
        store = _store(load_settings(args.env_file))
        if kind == "finding":
            found = store.review_finding(args.id, args.status, utc_iso(datetime.now(timezone.utc)), args.note)
        else:
            found = store.set_topic_status(args.id, args.status)
    except (OSError, ValueError, sqlite3.Error):
        print(f"error: {kind} review unavailable", file=sys.stderr)
        return 2
    if not found:
        print(f"error: {kind} id={args.id} was not found", file=sys.stderr)
        return 2
    print(f"reviewed {kind} id={args.id} status={args.status}")
    return 0


def _listing(args: argparse.Namespace) -> int:
    """Handle `finding list` and `topic list`."""
    kind = args.command
    limit = _positive_int(args.limit)
    if limit is None:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    try:
        store = _store(load_settings(args.env_file))
        if kind == "finding":
            _print_findings(store, limit, args.all)
        else:
            _print_topics(store, limit)
    except (OSError, ValueError, sqlite3.Error):
        print(f"error: {kind} list unavailable", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"gaohe {__version__}")
        return 0
    if args.command == "doctor":
        settings = load_settings(args.env_file)
        missing_setup = settings.validate()
        print(f"llm_provider={settings.llm_provider or 'unset'}")
        print(f"web_search_provider={settings.web_search_provider}")
        print(f"llm_model={'configured' if settings.llm_model else 'missing'}")
        print(f"llm_api_key={'present' if settings.has_llm_key else 'missing'}")
        print(f"firecrawl_api_key={'present' if settings.has_firecrawl_key else 'missing'}")
        print(
            "missing_setup="
            + (",".join(item.split(" is required", 1)[0] for item in missing_setup) or "none")
        )
        return 0
    if args.command == "status":
        try:
            settings = load_settings(args.env_file)
            _print_status(settings, _store(settings))
        except (OSError, ValueError, sqlite3.Error):
            print("error: status unavailable", file=sys.stderr)
            return 2
        return 0
    if args.command == "serve":
        if args.host not in LOCAL_SERVE_HOSTS:
            print("error: --host must be 127.0.0.1, localhost or ::1; GaoHe serves this computer only", file=sys.stderr)
            return 2
        try:
            serve(host=args.host, port=args.port, env_file=args.env_file)
        except (OSError, ValueError):
            print("error: serve unavailable", file=sys.stderr)
            return 2
        return 0
    if args.command == "setup":
        try:
            settings = load_settings(args.env_file)
            run_setup_wizard(settings, _store(settings), open_browser=not args.no_browser, env_path=args.env_file)
        except (OSError, ValueError, sqlite3.Error):
            print("error: local setup unavailable", file=sys.stderr)
            return 2
        return 0
    if args.command == "schedule":
        runner = SchedulerRunner()
        try:
            if args.schedule_command == "install":
                settings = load_settings(args.env_file)
                install_task(
                    args.task_name,
                    args.project_dir or Path.cwd(),
                    args.python_path or Path(sys.executable),
                    settings.poll_interval_minutes,
                    runner,
                    args.env_file.resolve(),
                )
                print("schedule installed")
                return 0
            if args.schedule_command == "remove":
                remove_task(args.task_name, runner)
                print("schedule removed")
                return 0
            print(task_status(args.task_name, runner))
            return 0
        except (OSError, ValueError, RuntimeError):
            print("error: schedule unavailable", file=sys.stderr)
            return 2
    if args.command == "watch":
        try:
            settings = load_settings(args.env_file)
            store = _store(settings)
        except (OSError, ValueError, sqlite3.Error):
            return 2
        summary = watch_once(settings, store, UrllibTransport())
        print(
            f"checked={summary.sources_checked} candidates={summary.candidates_seen} "
            f"revisions={summary.revisions_created} failures={summary.failures}"
        )
        return 0
    if args.command == "analyze":
        try:
            limit = _positive_int(args.limit)
            if limit is None:
                raise ValueError
            settings = load_settings(args.env_file)
            if settings.validate():
                raise ValueError
            if settings.web_search_provider != "none":
                raise ValueError
            summary = run_pending_analysis(
                _store(settings),
                build_analysis_provider(settings),
                build_search_provider(settings),
                DirectPageFetcher(UrllibTransport()),
                limit,
                assessor=build_evidence_assessor(settings),
                daily_llm_call_limit=settings.daily_llm_call_limit,
            )
        except (OSError, ValueError, sqlite3.Error):
            print("error: analyze unavailable", file=sys.stderr)
            return 2
        print(" ".join(f"{key}={value}" for key, value in summary.items()))
        return 0
    if args.command == "source":
        if args.source_command == "add":
            if not args.name.strip():
                print("error: --name must not be empty", file=sys.stderr)
                return 2
            for option, value in (("--feed-url", args.feed_url), ("--article-url", args.article_url)):
                if value is not None and not _is_http_url(value):
                    print(f"error: {option} must be an HTTP(S) URL", file=sys.stderr)
                    return 2
        try:
            store = _store(load_settings(args.env_file))
        except (OSError, ValueError, sqlite3.Error):
            return 2
        if args.source_command == "add":
            source_id = store.add_source(Source(None, args.name.strip(), args.feed_url, args.article_url))
            print(f"added source id={source_id}")
            return 0
        if args.source_command == "list":
            for source in store.list_sources():
                print(
                    f"id={source.id} enabled={'yes' if source.enabled else 'no'} name={source.name} "
                    f"feed_url={redact_url(source.feed_url)} "
                    f"article_url={redact_url(source.article_url) if source.article_url else '-'}"
                )
            return 0
        enabled = args.source_command == "enable"
        if not store.set_source_enabled(args.id, enabled):
            print(f"error: source id={args.id} was not found", file=sys.stderr)
            return 2
        print(f"{'enabled' if enabled else 'disabled'} source id={args.id}")
        return 0
    if args.command in {"finding", "topic"}:
        subcommand = args.finding_command if args.command == "finding" else args.topic_command
        return _listing(args) if subcommand == "list" else _review(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
