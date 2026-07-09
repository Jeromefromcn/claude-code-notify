import json
import os
import sys

MANAGED_MARKER = "claude-code-notify"

# Sidecar state file recording the exact hook commands this installer last
# wrote, kept next to settings.json rather than under base_dir. base_dir
# (CLAUDE_NOTIFY_HOME) is allowed to change between runs; settings.json's
# location is not, for real installs. See docs/adr/0001-hook-installation-tracking.md.
STATE_FILENAME = ".claude-code-notify-hooks.json"

_EVENTS = {
    "Stop": "stop.sh",
    "StopFailure": "stop_failure.sh",
    "PermissionRequest": "permission_request.sh",
}


def hook_entry(base_dir, event_script):
    command = os.path.join(base_dir, "hooks", event_script)
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _entry_commands(entry):
    return [hook.get("command", "") for hook in entry.get("hooks", [])]


def _matches_recorded(entry, recorded_command):
    return recorded_command is not None and recorded_command in _entry_commands(entry)


def _matches_legacy_marker(entry):
    # Fallback for entries written by pre-ADR-0001 installer versions,
    # applied only when there's no recorded state yet for that event.
    return any(MANAGED_MARKER in command for command in _entry_commands(entry))


def _is_ours(entry, recorded_command):
    if _matches_recorded(entry, recorded_command):
        return True
    return recorded_command is None and _matches_legacy_marker(entry)


def state_path_for(settings_path):
    directory = os.path.dirname(os.path.abspath(settings_path))
    return os.path.join(directory, STATE_FILENAME)


def load_state(path):
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("commands"), dict):
                return data
        except Exception:
            pass
    return {"commands": {}}


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def merge_hooks(settings, base_dir, state):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    commands = dict(state.get("commands", {}))
    for event, script in _EVENTS.items():
        recorded_command = commands.get(event)
        existing = [e for e in hooks.get(event, []) if not _is_ours(e, recorded_command)]
        new_entry = hook_entry(base_dir, script)
        existing.append(new_entry)
        hooks[event] = existing
        commands[event] = new_entry["hooks"][0]["command"]
    settings["hooks"] = hooks
    return settings, {"commands": commands}


def remove_hooks(settings, state):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    commands = state.get("commands", {})
    for event in list(hooks.keys()):
        recorded_command = commands.get(event)
        kept = [e for e in hooks[event] if not _is_ours(e, recorded_command)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


class InstallerError(Exception):
    pass


def _load(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            text = fh.read().strip()
    except OSError as exc:
        raise InstallerError(f"cannot read {path}: {exc}") from None
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallerError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InstallerError(
            f"{path} must contain a JSON object, found {type(data).__name__}"
        )
    return data


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv):
    try:
        if len(argv) >= 3 and argv[0] == "merge":
            settings_path, base_dir = argv[1], argv[2]
            st_path = state_path_for(settings_path)
            settings, state = merge_hooks(_load(settings_path), base_dir, load_state(st_path))
            _save(settings_path, settings)
            save_state(st_path, state)
            return 0
        if len(argv) >= 2 and argv[0] == "remove":
            settings_path = argv[1]
            st_path = state_path_for(settings_path)
            settings = remove_hooks(_load(settings_path), load_state(st_path))
            _save(settings_path, settings)
            if os.path.exists(st_path):
                os.remove(st_path)
            return 0
        sys.stderr.write("usage: installer.py merge <settings> <base_dir> | remove <settings>\n")
        return 2
    except InstallerError as exc:
        sys.stderr.write(f"claude-code-notify: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
