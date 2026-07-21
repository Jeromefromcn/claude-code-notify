import dataclasses

from . import notifier


def destinations(config):
    """Distinct (bot_token, chat_id) across the global default and every route
    that has a chat_id. Order-preserving; mute is not consulted."""
    out = []
    seen = set()

    def add(bot_token, chat_id):
        if not chat_id:
            return
        pair = (bot_token, chat_id)
        if pair in seen:
            return
        seen.add(pair)
        out.append(pair)

    add(config.bot_token, config.chat_id)
    for route in config.routes:
        add(route.bot_token or config.bot_token, route.chat_id)
    return out


def send_all(config, text, send=None):
    """Send text to every distinct destination. Each send is guarded so one
    dead destination never aborts the rest. Returns the count sent."""
    sender = notifier.send if send is None else send
    sent = 0
    for bot_token, chat_id in destinations(config):
        dest = dataclasses.replace(config, bot_token=bot_token, chat_id=chat_id)
        try:
            sender(dest, text)
            sent += 1
        except Exception:
            pass
    return sent
