# Contributing

Thanks for helping improve claude-code-notify.

## Development

- Runtime requires only `python3` (stdlib). Tests require `pytest`.
- Install dev deps: `pip install -e ".[dev]"`
- Run the suite: `python -m pytest -v`
- Lint shell: `shellcheck install.sh hooks/*.sh`

## Ground rules

- The core must stay testable without a live Claude Code session and
  without hitting real Telegram. Add fixtures under `tests/fixtures/`.
- Parse transcripts at the JSON envelope level — never substring-match
  free text to classify an entry.
- `hooks.py` entry points must never raise or exit non-zero.
- Never commit secrets. Config lives only in `config.env` (chmod 600).
- Credit any external project consulted in README's "Related work".

## Pull requests

- One logical change per PR. Update `CHANGELOG.md`.
- Every accuracy fix should state the concrete case it corrects.
