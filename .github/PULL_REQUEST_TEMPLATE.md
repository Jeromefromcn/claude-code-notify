## What & why

Describe the change and the problem it solves. One logical change per PR.

## Checklist

- [ ] `CHANGELOG.md` updated
- [ ] Tests added/updated (`python -m pytest -v`)
- [ ] `shellcheck install.sh hooks/*.sh` passes (if shell files changed)
- [ ] Transcript parsing still uses JSON-envelope matching, not substring
      matching on free text
- [ ] No secrets committed; `config.env`/tokens untouched
