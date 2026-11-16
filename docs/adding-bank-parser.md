# Adding a bank transaction parser

This guide is for contributors and for a coding LLM working in a checked-out
copy of Scribe. It describes the current extension contract; it does not grant
access to a live mailbox, credentials, or financial records.

Adding a bank has two independent parts:

1. Python code teaches Scribe one notification format.
2. A local `sender-rules` record tells one installation which sender is trusted
   and which parser should receive that sender's messages.

A parser alone does not authorize a sender. A sender rule alone does not teach
the parser a new layout.

## Privacy rules before doing any investigation

- Do not access a live Gmail account, Google Cloud project, password store,
  database, backup, log, or production dashboard while implementing a parser.
- Do not commit, paste into an issue, or give an LLM a real email, PDF,
  screenshot, sender address, recipient, message ID, thread ID, merchant,
  amount, card/account suffix, authorization code, or password.
- Do not "anonymize" a real file in place. Hidden PDF objects and metadata can
  remain identifying. Recreate the minimum format from scratch as a
  **synthetic fixture** with invented values and an `.invalid` sender domain.
- Never send Gmail or attachment content to an external service. Parser code
  must stay deterministic and local.
- A fixture password must be obviously test-only and may only unlock a PDF
  generated from synthetic source inside `tests/fixtures`.

If the format was discovered privately, record only its abstract grammar: field
labels, ordering, separators, optional fields, repeated-row behavior, encoding,
and date/currency rules. Delete any scratch export when that abstraction is
complete.

## The extension boundary

The framework-free contracts live in [`app/domain/parsing.py`](../app/domain/parsing.py):

- `TransactionParser` declares `name`, `version`, `priority`, `supports()`, and
  `parse()`.
- `ParserEmail` is the immutable input for **one Gmail message**. Gmail may put
  many messages in one thread, but synchronization expands the thread and
  persists/parses each message separately.
- `ParseResult` is the auditable outcome for that message.
- `TransactionCandidate` represents one transaction row. One message can
  produce zero, one, or several candidates.

The parser must not call Gmail, SQLAlchemy, repositories, the network, the OS
credential store, or a PDF library. MIME decoding, HTML-to-text conversion,
sender matching, bounded PDF extraction, and password lookup happen before the
parser is called.

`ParserEmail.searchable_text` combines the subject, normalized text body, and
locally extracted attachment text. A parser for a structured PDF should prefer
the extracted attachment text in `ParserEmail.attachments` so it does not
mistake envelope text for a transaction row.

## Choose the closest existing implementation

Bank parsers live under
[`app/infrastructure/parsers/banks`](../app/infrastructure/parsers/banks):

- A field-oriented or plain-text notice can reuse helpers in `common.py` or
  `extractors.py`.
- A bank whose details are in an encrypted text PDF should follow `sinopac.py`
  or `hncb.py` and the safe result helpers in `pdf_notice.py`.
- `generic.py` is the conservative fallback, not a template for claiming
  bank-specific certainty.

Do not create a new abstraction until at least two concrete parsers need it.
Keep a bank-specific regex or row grammar beside that bank parser when it has no
proven reuse.

## Implement the parser

Create, for example,
`app/infrastructure/parsers/banks/example_bank.py`:

```python
from decimal import Decimal

from app.domain.enums import ClassificationStatus
from app.domain.parsing import ParserEmail, ParseResult


class ExampleBankTransactionParser:
    name = "banks.example_bank"
    version = "1.0.0"
    priority = 170

    def supports(self, email: ParserEmail) -> bool:
        return email.parser_hint == self.name or email.institution_key == "example_bank"

    def parse(self, email: ParserEmail) -> ParseResult:
        # Parse a documented synthetic layout and return an explicit result.
        ...
```

Follow these rules:

1. `name` is stable and unique. Use `banks.<institution_key>`.
2. Increase `version` whenever parsing behavior changes. Existing parser
   provenance then explains which logic produced a row.
3. Give the parser an explicit priority. Lower values run first;
   `DefaultParserRegistry` also sorts by stable parser name.
4. `supports()` must depend on `parser_hint` or `institution_key`, not a
   hard-coded real sender address. Sender addresses belong to local rules.
5. `parse()` must always return a `ParseResult`. A supported but incomplete
   format should normally be `UNCERTAIN`, not an exception and not a fabricated
   transaction.
6. Use `Decimal` for confidence and integer minor units for money. Never use a
   float for amounts. Negative display amounts become a positive magnitude with
   `TransactionType.REFUND`.
7. Produce one `TransactionCandidate` per independent transaction row. Each
   must have a deterministic `source_key`; repeated parsing of unchanged input
   must produce the same key, while two repeated equal-looking rows in one
   message must still be distinguishable.
8. Preserve missing data as `None` plus a stable `ParseWarning`. Do not invent a
   date, merchant, currency, or card suffix.
9. Evidence excerpts must be bounded and must not copy the full email. Prefer a
   structural description such as `"TWD amount; structured row 1"`.
10. Recognize marketing or unrelated messages explicitly so they do not become
    false transactions.

Export the class from
[`app/infrastructure/parsers/banks/__init__.py`](../app/infrastructure/parsers/banks/__init__.py)
and, if useful to callers, from
[`app/infrastructure/parsers/__init__.py`](../app/infrastructure/parsers/__init__.py).
Then add one instance to `DefaultParserRegistry` inside
`_build_parser_pipeline` in [`app/bootstrap.py`](../app/bootstrap.py). The
generic parser stays last.

## Plain text, HTML, and encrypted PDFs

### Plain-text or HTML notification

The MIME layer has already normalized the content into `ParserEmail.text_body`.
`has_html_body` is provenance; the parser should not parse raw HTML. Test the
normalized layout and any meaningful whitespace/encoding variations.

### Encrypted text PDF

An encrypted text PDF follows this path:

1. Gmail MIME decoding identifies a bounded PDF attachment.
2. The configured sender rule supplies opaque password references.
3. Password values are read from the **OS credential store** and used only at
   the local extraction boundary.
4. The parser receives a `ParserAttachment` with extraction status and, when
   successful, bounded `extracted_text`.

The parser never receives or looks up a password. Use
`extracted_pdf_text()` and `unavailable_pdf_result()` from `pdf_notice.py`.
Password-required, wrong-password, image-only, malformed, oversized, and
timed-out states must remain reviewable results rather than aborting the whole
mailbox sync. R1 does not perform OCR on image-only PDFs.

Generate a reproducible encrypted fixture from invented source. The existing
[`tests/fixtures/generate_pdf_fixtures.py`](../tests/fixtures/generate_pdf_fixtures.py)
shows the accepted pattern.

## Configure the local sender rule

After the code is installed and after the user has authorized Gmail, add a rule
with an exact sender address whenever possible:

```bash
uv run python -m app sender-rules add \
  --institution-key example_bank \
  --institution-name "Example Bank" \
  --address notice@example.invalid \
  --parser-name banks.example_bank
```

`domain_suffix` is available but trusts a wider sender set. Use it only when the
institution genuinely rotates among controlled subdomains. Inspect the result:

```bash
uv run python -m app sender-rules list
```

For an encrypted PDF, add the password interactively; do not place it in shell
history, `config.toml`, source code, or a fixture:

```bash
uv run python -m app sender-rules add-pdf-password RULE_ID
```

The `sender-rules` command stores only an opaque reference in SQLite and keeps
the secret in the OS credential store.

## Test matrix

Start with a failing test in
[`tests/test_bank_parsers.py`](../tests/test_bank_parsers.py). Put handwritten
text/HTML fixtures under `tests/fixtures/emails`; generated PDFs belong under
`tests/fixtures/pdf` with their generator checked in.

Cover every applicable case:

- `supports()` accepts the configured hint/key and rejects another bank;
- one valid purchase and one refund, including sign-to-direction behavior;
- multiple rows in one message produce distinct stable `source_key` values;
- missing amount, currency, date, merchant, or suffix does not get invented;
- malformed rows and marketing mail do not become confirmed transactions;
- month/year boundary behavior uses the message time as its documented
  reference and the correct local time zone;
- zero-decimal and decimal currencies convert to exact minor units;
- PDF extracted, password required, wrong password, malformed, and image-only
  states are isolated;
- registry ordering and parser provenance are deterministic;
- reparsing unchanged input is idempotent, and changed parser output does not
  silently overwrite user-maintained financial fields.

Run the focused tests first:

```bash
uv run pytest -q --no-cov tests/test_bank_parsers.py
uv run pytest -q --no-cov tests/test_transaction_parsing.py
```

Then run the complete contribution gate:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
git diff --check
```

If a test needs a real sender or secret to pass, the test and the design are
wrong.

## Pull-request checklist

- The institution key, parser name, version, and priority are intentional.
- The parser is registered exactly once and the generic fallback remains last.
- The sender is configuration, not a constant in parser code.
- All fixtures and example values are synthetic and safe to publish.
- No password, OAuth file, database, real email, screenshot, or PDF is tracked.
- Positive, negative, multi-row, refund, malformed, and unrelated cases exist.
- Amounts are integer minor units and currency rules are explicit.
- Missing values produce warnings/review rather than guesses.
- Focused and full gates were actually run and their output was read.

## Prompt for an LLM

Copy the prompt below into a fresh coding session and replace only the bracketed
fields. Give the LLM the repository checkout and synthetic format description,
not live data.

```text
You are implementing one new bank transaction-notification parser in the Scribe
repository. Work only in the checked-out repository and use only synthetic
fixtures. Do not access a live Gmail account, Google Cloud, Keychain/credential
store, database, backups, logs, network services, or the user's real files. Do
not ask for or reproduce a real email, PDF, screenshot, sender, recipient,
message/thread ID, merchant, amount, card/account suffix, authorization code,
or password.

Bank/display name: [BANK DISPLAY NAME]
Stable institution key: [lowercase_key]
Parser name: banks.[lowercase_key]
Notification shapes: [plain normalized email / HTML-derived text / encrypted
text PDF]
Abstract synthetic grammar: [FIELD LABELS, ORDER, SEPARATORS, REPEATED ROWS,
OPTIONAL FIELDS, ENCODING, DATE AND CURRENCY RULES]
Expected sender configuration example: [invented address at .invalid]

Before editing:
1. Read AGENTS.md, CONTRIBUTING.md, ARCHITECTURE.md,
   docs/adding-bank-parser.md, app/domain/parsing.py,
   app/infrastructure/parsers/registry.py, app/bootstrap.py, and the closest
   existing bank parser and tests.
2. Confirm that one ParserEmail means one Gmail message even when Gmail groups
   multiple messages into a thread.
3. State the closest existing parser and the smallest required file set. Do not
   introduce a generic abstraction without two concrete users.

Implement with test-driven development:
1. First add failing tests to tests/test_bank_parsers.py and fully synthetic
   fixtures. Include configured/unrelated supports(), purchase, refund,
   multi-row stable identity, missing required data, malformed/marketing, and
   relevant date/currency boundaries. For PDFs include extraction-unavailable
   states; generate any encrypted fixture from synthetic source with a
   fixture-only password.
2. Add a TransactionParser implementation under
   app/infrastructure/parsers/banks. It must have a unique stable name,
   semantic version, explicit priority, configuration-driven supports(), and a
   deterministic ParseResult. Use Decimal confidence and integer minor units;
   never float money. Do not access Gmail, a database, repositories, network,
   secrets, or raw PDF passwords from the parser.
3. Export it and register exactly one instance in DefaultParserRegistry inside
   _build_parser_pipeline. Keep GenericTransactionParser last.
4. Add/update public documentation only for behavior proven by tests. Show a
   sender-rules command with an invented .invalid address. Never hard-code a
   real sender in parser code.
5. Run focused tests, then ruff, format check, mypy, the complete pytest suite,
   and git diff --check. Report exact outputs, changed files, privacy review,
   and known limitations. Do not claim success from partial tests.

Acceptance criteria:
- unchanged input reparses to the same candidate source keys;
- distinct rows remain distinct;
- refunds are positive magnitudes with refund direction;
- missing fields are not guessed;
- encrypted PDF passwords stay in the OS credential store and PDF failures do
  not abort mailbox synchronization;
- every committed fixture is demonstrably synthetic;
- no real/private material appears in the diff;
- all repository quality gates pass.

Stop after this parser is implemented and verified. Do not migrate the database,
change accounting behavior, contact Gmail, install a service, deploy, push, or
publish anything.
```

