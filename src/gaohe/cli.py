import argparse
from collections.abc import Sequence
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from . import __version__
from .config import Settings, load_settings
from .domain import Source
from .monitor import watch_once
from .sources import UrllibTransport
from .storage import Store, redact_url
from .web import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaohe")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    status = commands.add_parser("serve")
    status.add_argument("--host", default="127.0.0.1")
    status.add_argument("--port", type=int, default=8000)
    status.add_argument("--env-file", type=Path, default=Path(".env"))
    watch = commands.add_parser("watch")
    watch.add_argument("--once", action="store_true", required=True)
    watch.add_argument("--env-file", type=Path, default=Path(".env"))
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
    return parser


def _store(settings: Settings) -> Store:
    store = Store(settings.database_path)
    store.initialize()
    return store


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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
    if args.command == "serve":
        serve(host=args.host, port=args.port, env_file=args.env_file)
        return 0
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
    parser.print_help()
    return 0
