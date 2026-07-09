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


def build_message(kind, cwd, when, title=None):
    parts = [_HEADS[kind]]
    if title:
        parts.append(title)
    parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)


def send(config, text):
    url = f"{config.api_base}/bot{config.bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": config.chat_id, "text": text}).encode()
    request = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        # `from None` prevents the token-bearing URL leaking via __context__.
        raise NotifierError(scrub(str(exc), config.bot_token)) from None
