import os


def should_send(marker_path, threshold, now):
    try:
        with open(marker_path) as fh:
            last = float(fh.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return True
    return (now - last) >= threshold


def record_sent(marker_path, now):
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    tmp = f"{marker_path}.tmp"
    with open(tmp, "w") as fh:
        fh.write(str(now))
    os.replace(tmp, marker_path)
