import urllib.error
import urllib.parse
import urllib.request


class NotifierError(Exception):
    pass


_HEADS = {
    "finished": "Claude Code finished",
    "error": "Claude Code stopped with error",
    "needs-input": "Claude Code needs your input",
}


def scrub(text, token):
    if token:
        text = text.replace(token, "***")
    return text


def build_message(kind, cwd, when, title=None, duration=None):
    parts = [_HEADS[kind]]
    if duration:
        parts.append(duration)
    if title:
        parts.append(title)
    parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)


def send(config, text):
    url = f"{config.api_base}/bot{config.bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": config.chat_id, "text": text}).encode()
    request = urllib.request.Request(url, data=data)
    error_message = None
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        # Don't raise from inside this except block: Python implicitly sets
        # __context__ to `exc` for any exception raised while it's active,
        # regardless of `raise ... from None` (which only affects default
        # traceback display, not the __context__ attribute). `exc` may be an
        # HTTPError whose `.url` holds the full unredacted bot token, so we
        # only compute the scrubbed message here and raise once this except
        # block has exited, which avoids the implicit chaining entirely.
        error_message = scrub(str(exc), config.bot_token)
    if error_message is not None:
        raise NotifierError(error_message)
