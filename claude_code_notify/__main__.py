import os
import sys

from . import __version__
from . import config as cfg
from . import routing


def _check_route(argv):
    target = None
    if "--check-route" in argv:
        i = argv.index("--check-route")
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            target = argv[i + 1]
    if not target:
        target = os.getcwd()
    try:
        config = cfg.load()
    except cfg.ConfigError as exc:
        print(f"config error: {exc}")
        return 1
    res = routing.resolve(target, config.routes, config.bot_token, config.chat_id)
    print(f"cwd: {os.path.realpath(target)}")
    if res.matched_dir:
        print(f"matched route: {res.matched_dir}")
    else:
        print("matched route: (none — using global default)")
    if res.muted:
        print("result: MUTED — no notification will be sent")
        return 0
    bot_scope = "per-route override" if res.bot_token != config.bot_token else "global default bot"
    print(f"chat_id: {res.chat_id}")
    print(f"bot: {bot_scope}")
    return 0


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    if "--check-route" in argv:
        return _check_route(argv)
    print(f"claude-code-notify {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
