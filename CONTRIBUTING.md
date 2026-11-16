# Contributing to Scribe

Scribe welcomes fixes, privacy improvements, documentation, and parser support
that preserve its local-first boundary.

## License

Unless a file says otherwise, contributions are submitted under
`AGPL-3.0-or-later`, the same license as Scribe. Do not contribute code, email
content, fixtures, or assets that you do not have the right to share.

## Development environment

Use Python 3.12 or newer and install the locked development environment:

```bash
uv sync --extra dev
```

Before submitting a change, run the quality gate documented in
[`DEVELOPMENT.md`](DEVELOPMENT.md):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Do not claim a command passed unless you ran it and read its successful output.

## Architecture and migrations

Keep domain rules independent of FastAPI, Typer, SQLAlchemy, and Gmail. Routes
and CLI commands translate input and invoke application services; they do not
issue SQL or Gmail API calls. See [`ARCHITECTURE.md`](ARCHITECTURE.md) and the
ADRs under [`docs/adr`](docs/adr/README.md).

Database changes require an Alembic revision, upgrade and downgrade coverage,
foreign-key and integrity checks, and a rehearsal against synthetic data. Follow
the transactional migration protocol in `DEVELOPMENT.md`. Never run a bare
Alembic command that can guess the formal database target.

## Test and fixture privacy

Every test fixture must be synthetic. It must use invented names, email
addresses, merchants, amounts, dates, account and card suffixes, PDF text, and
fixture-only passwords. Real financial data, real email content, statements,
screenshots, exports, credentials, tokens, databases, or logs must not enter a
test, commit, issue, or pull request.

Do not merely redact a real fixture: structure, metadata, hidden PDF objects,
and message identifiers can remain identifying. Recreate the smallest relevant
shape from scratch.

## Bank parser contributions

A parser recognizes a notification format; it is not a bank API integration or
a promise that every message from that institution is supported. A parser
change should include synthetic fixtures for positive, negative, refund, and
malformed cases relevant to the format. Preserve message-level Gmail identity
even when Gmail groups multiple messages in one thread.

Follow the complete [`docs/adding-bank-parser.md`](docs/adding-bank-parser.md)
contract for domain interfaces, registry wiring, sender rules, encrypted PDFs,
the test matrix, and the LLM-safe implementation prompt.

Encrypted PDF fixtures must be reproducible from synthetic source and use only
fixture passwords. Never share a real bank PDF or its password.

## Pull requests

Keep one coherent change per pull request. Explain the user-visible behavior,
privacy effect, tests actually run, migrations if any, and known limitations.
Review generated artifacts and `git diff --check` before requesting review.
Use the repository pull-request template; do not replace its privacy and
licensing confirmations with free-form claims.
