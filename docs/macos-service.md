# macOS background services

Scribe generates two current-user LaunchAgents from the checkout you invoke:

- `scribe-local.web` runs `python -m app serve`, starts at login, and restarts
  after an unexpected exit.
- `scribe-local.sync` runs `python -m app sync` at 00:00, 03:00, 06:00, 09:00,
  12:00, 15:00, 18:00, and 21:00 local time.

They run as the logged-in user, never root. Generated plist files contain no
OAuth token, Gmail password, PDF password, or Cloudflare credential.

## Install or update

From the checkout and Python environment that should run Scribe:

```bash
uv run python -m app service install
uv run python -m app service status
```

The installer resolves the current checkout and Python executable, renders XML
with Python's plist library, validates both files with `plutil`, writes them
atomically with mode `0600`, and loads them in the current `gui/<uid>` domain.
Reinstalling stops the existing generated labels and replaces only Scribe's two
files. If the pair cannot be installed, the previous pair and loaded state are
restored.

To operate a different checkout or interpreter explicitly:

```bash
uv run python -m app service install \
  --instance-dir /absolute/path/to/scribe \
  --python-executable /absolute/path/to/python
```

Reinstall after moving the checkout, replacing `.venv`, or changing the Python
executable. Running `uv sync` alone does not rewrite an already-installed plist.

## Inspect

```bash
uv run python -m app service status
```

`installed` means the generated file exists. `loaded` means launchd currently
has that label. The two states are reported separately and status never writes
or loads anything.

Application logs are under the configured `log_dir`. Generated service stdout
and stderr use separate files in the same directory so startup failures are
inspectable without mixing them into financial application records.

## Stop and remove

```bash
uv run python -m app service uninstall
```

Uninstall unloads both labels and removes only `scribe-local.web.plist` and
`scribe-local.sync.plist`. It is idempotent and does not touch unrelated
LaunchAgents. Re-run `service install` to start them again.

Use uninstall before an exclusive database restore. A normal online backup does
not require stopping services.

## Scheduling limitations

A LaunchAgent works only after the desktop user logs in. It cannot serve or
sync while the Mac is shut down or asleep. launchd may run a missed calendar
job after wake, and Scribe's SQLite lease prevents overlap with a manual or
dashboard sync. The exact wall-clock run is therefore not a bank-grade
scheduler.
