# ADR-014: Local encrypted text-PDF extraction

- Status: Accepted
- Date: 2026-07-27

## Decision

Use `pypdf` with `cryptography` for bounded local decryption and `pdfplumber` for
page-aware extraction. Image-only OCR is outside the first-release scope.

## Consequences

Password-encrypted text PDFs are parseable without external services. Malformed,
oversized and image-only files produce isolated, reviewable attachment states.
