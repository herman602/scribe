## What changed

Describe one coherent change, its user-visible behavior, privacy effect, and
known limitations.

## Evidence

- [ ] I ran `uv run ruff check .`.
- [ ] I ran `uv run ruff format --check .`.
- [ ] I ran `uv run mypy .`.
- [ ] I ran `uv run pytest` and read the final result.
- [ ] I ran `git diff --check`.
- [ ] If the schema changed, I added and tested an upgrade/downgrade migration
      round trip against synthetic data with integrity and foreign-key checks.

Paste concise command outcomes, not private logs:

```text
ruff:
format:
mypy:
pytest:
migration (if applicable):
```

## Privacy and licensing

- [ ] I have the right to contribute this work under `AGPL-3.0-or-later`.
- [ ] Every fixture, screenshot, address, identity, merchant, amount, date,
      account/card suffix, and password in this change is synthetic.
- [ ] This change contains no credentials, tokens, database/backup, logs, real
      email/PDF content, account/card numbers, or financial screenshot.
- [ ] I reviewed generated artifacts and the complete diff for private data.

## Parser changes (if applicable)

- [ ] I followed `docs/adding-bank-parser.md`.
- [ ] The sender is local configuration, not a hard-coded personal address.
- [ ] Positive, refund, multi-row, missing, malformed, and unrelated cases are
      tested where applicable.

