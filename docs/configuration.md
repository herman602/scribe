# Configuration reference

Settings resolve in this order: explicit invocation override, environment
variable, `.env`, `config.toml`, then the safe code default. Environment names
use the `SCRIBE_` prefix and uppercase field name. Paths may be relative to the
Scribe instance directory. The examples in `config.example.toml` and
`.env.example` keep external access disabled and the listener on loopback.

## Paths and local storage

- `data_dir`: application data root; default `data`.
- `database_path`: SQLite file; default `data/app.sqlite3`.
- `gmail_credentials_path`: user-owned Desktop client JSON; default
  `credentials.json`.
- `gmail_token_path`: private OAuth token file; default `data/token.json`.
- `log_dir`: rotating log directory; default `data/logs`.
- `log_file_name`: rotating application log name; default `scribe.log`.
- `log_level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; default
  `INFO`. Logs are redacted but still sensitive local operational data.
- `body_retention_policy`: retain full text, a bounded summary, or purge body
  text after parsing. The generated example documents accepted values.
- `body_summary_chars`: retained summary bound; default `1000`.

## Gmail and parsing

- `gmail_oauth_testing_mode`: enable the day-six Testing-project warning;
  default `true`.
- `gmail_labeling_enabled`: opt in to processed-message labels and
  `gmail.modify`; default `false`.
- `gmail_processed_label`: label name; default `Scribe/已處理`.
- `default_gmail_query`: optional full-sync query; default unset.
- `gmail_request_timeout_seconds`: request timeout; default `30`.
- `gmail_retry_max_attempts`: bounded request attempts; default `5`.
- `gmail_backoff_base_seconds`: retry base; default `1.0`.
- `gmail_backoff_max_seconds`: retry ceiling; default `64.0`.
- `gmail_max_body_bytes`: decoded message-body bound; default `5242880`.
- `gmail_max_attachment_bytes`: attachment download bound; default `26214400`.
- `pdf_max_bytes`: local PDF extraction bound; default `10485760`.
- `pdf_max_pages`: PDF page bound; default `30`.
- `pdf_max_text_chars`: extracted text bound; default `200000`.
- `pdf_max_password_attempts`: password attempts per attachment; default `5`.
- `pdf_max_seconds`: local extraction time bound; default `10.0`.
- `parser_confidence_threshold`: confirmed classification threshold; default
  `0.75`.

PDF passwords are referenced from sender rules and stored through the OS
credential store. SQLite stores only opaque references. Plain or decrypted PDF
bytes are never persisted; an encrypted original and bounded extracted text may
be retained for local reparse.

## Web access

- `host`: bind address; safe default `127.0.0.1`.
- `port`: TCP port; default `8000`.
- `lan_access_enabled`: opt in to trusted home-LAN access; default `false`.
- `lan_trusted_hosts`: exact private IP host allowlist; default empty. Subnets
  and wildcards are rejected.
- `external_access_enabled`: opt in to one Cloudflare hostname; default
  `false`.
- `external_hostname`: exact external DNS hostname; default unset.
- `external_read_only`: keep the external surface read-only; default `true`.
- `external_confirmation_enabled`: allow the one guarded batch-confirm route;
  default `false`.
- `cloudflare_access_team_domain`: Access issuer team domain; default unset.
- `cloudflare_access_audience`: Access application audience; default unset.
- `cloudflare_access_allowed_email`: exact allowed identity; default unset.

LAN mode is unauthenticated plain HTTP and is intended only for a controlled
home network. Never combine it with router port forwarding. External mode still
requires Cloudflare Access and does not expose money administration pages.

## Synchronization and maintenance

- `sync_batch_size`: Gmail page/batch bound; default `100`.
- `sync_worker_lease_seconds`: cross-process sync lease; default `300`.
- `maintenance_enabled`: run local scheduled-transfer maintenance; default
  `true`.
- `maintenance_poll_seconds`: maintenance polling interval; default `60`.
- `maintenance_batch_size`: due items per pass; default `20`.
- `maintenance_lease_seconds`: execution lease; default `120`.
- `maintenance_retry_base_seconds`: retry base; default `30`.
- `maintenance_retry_max_seconds`: retry ceiling; default `1800`.

The generated sync LaunchAgent invokes the same incremental command every three
hours. SQLite leases prevent overlap with dashboard or foreground syncs.

## Money and local time

- `local_timezone`: display, day, and month boundary zone; default
  `Asia/Taipei`.
- `default_currency`: new local money configuration default; default `TWD`.
- `scheduled_transfer_default_time`: default assumed transfer time; default
  `06:00`.
- `scheduled_transfer_timezone`: zone for scheduled-transfer policy; default
  `Asia/Taipei`.

Money is stored as integer minor units. Currency is always explicit and Scribe
never calculates a cross-currency total.
