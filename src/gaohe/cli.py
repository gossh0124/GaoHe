from collections.abc import Sequence

from . import __version__


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if list(argv) == ["--version"]:
        print(f"gaohe {__version__}")
        return 0
    print("usage: gaohe --version")
    return 0
