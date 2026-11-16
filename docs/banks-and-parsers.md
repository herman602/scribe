# Banks, notification shapes, and parsers

Scribe parses notification formats observed in synthetic regression fixtures.
Support is not a bank API guarantee: institutions can change wording, sender,
HTML, PDF encryption, column order, or date formats without notice. Every
result remains reviewable and retains parser name, version, confidence, reason,
and bounded evidence.

## Current format coverage

| Institution | Parser | Tested notification shapes |
|---|---|---|
| Taipei Fubon | `banks.fubon` | plain-text email and HTML email fallback for authorization notices; multiple transactions per message |
| Sunny Bank | `banks.sunny` | plain-text email and HTML email fallback with amount, date/time, and optional card suffix |
| Cathay United Bank | `banks.cathay` | plain-text email and HTML email fallback for card spending notices |
| First Bank | `banks.first_bank` | plain-text email and HTML email fallback for card and account notification layouts |
| Standard Chartered Taiwan | `banks.standard_chartered` | bilingual plain-text email rows with one candidate per transaction row |
| SinoPac | `banks.sinopac` | plain-text envelope plus encrypted text PDF transaction table |
| Hua Nan Commercial Bank | `banks.hncb` | plain-text envelope plus encrypted text PDF transaction table |
| Other senders | `generic` | conservative plain-text email / HTML-derived text scoring, never a bank-specific promise |

Image-only PDFs do not have OCR in R1. Malformed, oversized, timed-out, or
password-rejected attachments receive a sanitized status instead of aborting
the mailbox run.

## Sender rules are part of support

A parser does not make every sender trusted. Configure exact sender addresses
when possible; domain suffix rules are broader and should be reviewed. Gmail
discovery uses enabled rules to reduce downloads, while incremental sync first
fetches metadata and downloads a body only after local sender gating.

Use `sender-rules --help` to list, add, disable, and associate encrypted-PDF
password references. Keep passwords in the OS credential store.

## Adding or correcting a parser

Use the complete [bank parser contribution guide](adding-bank-parser.md). It
documents the actual domain contracts, registry wiring, sender-rule and
encrypted-PDF flows, synthetic fixture policy, test matrix, and a prompt that
can be handed to a coding LLM without exposing private mailbox data.

1. Do not commit or attach a real message, PDF, screenshot, address, merchant,
   card suffix, message ID, amount, or password.
2. Hand-write or programmatically generate the smallest synthetic fixture that
   preserves only labels, field order, optionality, and type rules.
3. Add classification and parser tests for successful, missing, malformed, and
   marketing/non-transaction cases.
4. Preserve one Gmail message as one parser input even when Gmail groups many
   messages into a thread.
5. Prove reparsing updates parsed values without overwriting manual fields.
6. Run the full quality gate in `CONTRIBUTING.md`.

If a real mailbox is used during private investigation, inspect a narrow query,
expand every shortlisted thread, and copy only the abstract shape into a new
synthetic fixture. Delete scratch exports afterward; never add them to Git.
