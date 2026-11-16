# Security policy

## Supported versions

Security fixes are provided for the latest `0.1.x` release while Scribe remains
in the initial public-release series. Older checkouts may receive a fix only
when the maintainer explicitly says so.

## Reporting a vulnerability

Until a private GitHub security-reporting address is published, do not open a
public issue containing exploit details. Contact the repository owner through a
private channel and provide the smallest synthetic reproduction possible. The
maintainer will add the final private reporting channel before `v0.1.0` is made
public.

Do not upload credentials, OAuth tokens, a database or backup, logs, bank or
card statements, real email content or attachments, PDF passwords, account or
card numbers, Cloudflare assertions, or screenshots containing financial data.
If a report cannot be reproduced without private material, describe its shape
and wait for a safe transfer method instead of attaching it.

## Local security boundary

Scribe is a local-first application. By default the web server listens only on
`127.0.0.1`, and each operator supplies their own Google Desktop OAuth client.
LAN or Cloudflare exposure is optional and changes the threat boundary; it must
be configured and protected by the operator.

The application limits OAuth scope, protects local files with restrictive
permissions where supported, validates browser origins, and avoids logging
email bodies and financial evidence. These controls do not protect a machine
whose user account, browser session, filesystem, or keychain is already
compromised.

SQLite databases and retained email content are not encrypted by Scribe. Use
full-disk encryption such as FileVault, a strong login password, private backup
storage, and normal operating-system updates. Anyone who can read the database
file can read its stored financial and email-derived data.

## Public issue hygiene

Public bug reports must use invented identities, institutions, merchants,
amounts, dates, last-four values, and fixture-only passwords. Redact command
output before posting it. Never force-add a file ignored by `.gitignore` merely
to make a reproduction convenient.

Repository automation and secret-history scanning are prevention layers, not a
safe transfer channel. A green workflow does not make a credential, database,
log, real message, statement, or financial screenshot acceptable in an issue or
pull request.
