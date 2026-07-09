import json
import os
import sys

MANAGED_MARKER = "claude-code-notify"

_EVENTS = {
    "Stop": "stop.sh",
    "StopFailure": "stop_failure.sh",
    "PermissionRequest": "permission_request.sh",
}


def hook_entry(base_dir, event_script):
    command = os.path.join(base_dir, "hooks", event_script)
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _is_ours(entry):
    for hook in entry.get("hooks", []):
        if MANAGED_MARKER in hook.get("command", ""):
            return True
    return False


def merge_hooks(settings, base_dir):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    for event, script in _EVENTS.items():
        existing = [e for e in hooks.get(event, []) if not _is_ours(e)]
        existing.append(hook_entry(base_dir, script))
        hooks[event] = existing
    settings["hooks"] = hooks
    return settings


def remove_hooks(settings):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    for event in list(hooks.keys()):
        kept = [e for e in hooks[event] if not _is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def _load(path):
    if os.path.exists(path):
        with open(path) as fh:
            text = fh.read().strip()
        if text:
            return json.loads(text)
    return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv):
    if len(argv) >= 3 and argv[0] == "merge":
        _save(argv[1], merge_hooks(_load(argv[1]), argv[2]))
        return 0
    if len(argv) >= 2 and argv[0] == "remove":
        _save(argv[1], remove_hooks(_load(argv[1])))
        return 0
    sys.stderr.write("usage: installer.py merge <settings> <base_dir> | remove <settings>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
