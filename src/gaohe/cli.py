import argparse
from collections.abc import Sequence
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .config import Settings, load_settings
from .domain import Source
from .safety import is_http_url, redact_url
from .storage import Store


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaohe")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    def command(name: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("--env-file", type=Path, default=Path(".env"))
        return sub

    command("doctor")
    serve = command("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    command("setup").add_argument("--no-browser", action="store_true")
    command("watch").add_argument("--once", action="store_true", required=True)
    analyze = command("analyze")
    analyze.add_argument("--pending", action="store_true", required=True)
    analyze.add_argument("--limit", type=int, default=20)
    command("check").add_argument("url")

    schedule = commands.add_parser("schedule").add_subparsers(dest="schedule_command", required=True)
    install = schedule.add_parser("install")
    install.add_argument("--env-file", type=Path, default=Path(".env"))
    install.add_argument("--project-dir", type=Path)
    install.add_argument("--python-path", type=Path)
    for name in ("install", "status", "remove"):
        target = install if name == "install" else schedule.add_parser(name)
        target.add_argument("--task-name", default="GaoHe Watch")

    source = commands.add_parser("source").add_subparsers(dest="source_command", required=True)
    add = source.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--feed-url", required=True)
    add.add_argument("--article-url")
    for name in ("add", "list", "enable", "disable"):
        target = add if name == "add" else source.add_parser(name)
        target.add_argument("--env-file", type=Path, default=Path(".env"))
        if name in ("enable", "disable"):
            target.add_argument("--id", type=int, required=True)
    return parser


def _store(settings: Settings) -> Store:
    store = Store(settings.database_path)
    store.initialize()
    return store


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _use_utf8_output() -> None:
    # A Windows console or redirect may default to cp950/cp1252; zh-TW output must never crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _providers(settings: Settings):
    from .providers import DirectPageFetcher, build_analysis_provider, build_evidence_assessor, build_search_provider

    return (
        build_analysis_provider(settings), build_search_provider(settings),
        DirectPageFetcher(), build_evidence_assessor(settings),
    )


def _schedule(args) -> int:
    from .scheduler import SchedulerRunner, install_task, remove_task, task_status

    runner = SchedulerRunner()
    if args.schedule_command == "install":
        settings = load_settings(args.env_file)
        install_task(
            args.task_name, args.project_dir or Path.cwd(), args.python_path or Path(sys.executable),
            settings.poll_interval_minutes, runner, args.env_file.resolve(),
        )
        print("schedule installed")
    elif args.schedule_command == "remove":
        remove_task(args.task_name, runner)
        print("schedule removed")
    else:
        print(task_status(args.task_name, runner))
    return 0


def _source(args) -> int:
    if args.source_command == "add":
        if not args.name.strip():
            return _error("--name must not be empty")
        for option, value in (("--feed-url", args.feed_url), ("--article-url", args.article_url)):
            if value is not None and not is_http_url(value):
                return _error(f"{option} must be an HTTP(S) URL")
    store = _store(load_settings(args.env_file))
    if args.source_command == "add":
        print(f"added source id={store.add_source(Source(None, args.name.strip(), args.feed_url, args.article_url))}")
    elif args.source_command == "list":
        for source in store.list_sources():
            article_url = redact_url(source.article_url) if source.article_url else "-"
            print(
                f"id={source.id} enabled={'yes' if source.enabled else 'no'} name={source.name} "
                f"feed_url={redact_url(source.feed_url)} article_url={article_url}"
            )
    else:
        enabled = args.source_command == "enable"
        if not store.set_source_enabled(args.id, enabled):
            return _error(f"source id={args.id} was not found")
        print(f"{'enabled' if enabled else 'disabled'} source id={args.id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"gaohe {__version__}")
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "schedule":
            return _schedule(args)
        if args.command == "source":
            return _source(args)
        settings = load_settings(args.env_file)
        if args.command == "doctor":
            missing = settings.validate()
            print(f"llm_provider={settings.llm_provider or 'unset'}")
            print(f"llm_model={'configured' if settings.llm_model else 'missing'}")
            print(f"llm_api_key={'present' if settings.has_llm_key else 'missing'}")
            print(f"web_search_provider={settings.web_search_provider}")
            print("missing_setup=" + (",".join(item.split(" ", 1)[0] for item in missing) or "none"))
            return 0
        if args.command == "serve":
            if args.host not in LOOPBACK_HOSTS:
                return _error("--host must be 127.0.0.1, localhost or ::1; GaoHe serves this computer only")
            if not 1 <= args.port <= 65535:
                return _error("--port must be between 1 and 65535")
            from .web import serve

            print(f"稿核本機頁面：http://{'[::1]' if args.host == '::1' else args.host}:{args.port}/", flush=True)
            serve(host=args.host, port=args.port, env_file=args.env_file)
            return 0
        if args.command == "setup":
            from .setup_flow import run_setup_wizard

            run_setup_wizard(settings, _store(settings), open_browser=not args.no_browser, env_path=args.env_file)
            return 0
        if args.command == "watch":
            from .monitor import watch_once
            from .sources import UrllibTransport

            summary = watch_once(settings, _store(settings), UrllibTransport())
            print(
                f"checked={summary.sources_checked} candidates={summary.candidates_seen} "
                f"revisions={summary.revisions_created} failures={summary.failures}"
            )
            return 0
        if args.command in ("analyze", "check"):
            if settings.validate():
                return _error("AI 設定不完整，請先執行設定精靈（setup.cmd）")
            store = _store(settings)
            if args.command == "check":
                from .checks import check_article_url

                analysis, search, fetcher, assessor = _providers(settings)
                outcome = check_article_url(settings, store, args.url, analysis=analysis, search=search, fetcher=fetcher, assessor=assessor)
                print(outcome.message)
                return 0 if outcome.status == "completed" else 1
            if args.limit < 1:
                return _error("--limit must be a positive integer")
            from .pipeline import run_pending_analysis

            summary = run_pending_analysis(store, *_providers(settings), args.limit)
            print(" ".join(f"{key}={value}" for key, value in summary.items()))
            if summary["stopped"]:
                from .errors import PROVIDER_ERROR_MESSAGES

                print(PROVIDER_ERROR_MESSAGES.get(str(summary["stop_code"]), ""), file=sys.stderr)
                return 3
            return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        from .storage import IncompatibleDatabase

        if isinstance(error, IncompatibleDatabase):
            return _error(str(error))
        return _error(f"{args.command} unavailable")
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
