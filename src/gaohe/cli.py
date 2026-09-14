import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import load_settings
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"gaohe {__version__}")
        return 0
    if args.command == "doctor":
        settings = load_settings(args.env_file)
        print(f"llm_provider={settings.llm_provider}")
        print(f"gemini_model={settings.gemini_model}")
        print(f"search_provider={settings.search_provider}")
        print(f"data_dir={settings.data_dir}")
        print(f"google_api_key={'present' if settings.has_api_key else 'missing'}")
        return 0
    if args.command == "serve":
        serve(host=args.host, port=args.port, env_file=args.env_file)
        return 0
    parser.print_help()
    return 0
