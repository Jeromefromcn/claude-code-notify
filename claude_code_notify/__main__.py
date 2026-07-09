import sys

from . import __version__


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    print(f"claude-code-notify {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
