# JellyGen

JellyGen is a dependency-free Python web app for settling movie night from a Jellyfin library. The user chooses a year range, the generator draws a genre, and it creates a fresh set of film-specific wildcard clues without revealing the films. It prioritises sourced facts researched and saved ahead of time from IMDb trivia, interviews, studio material, film institutes, production documents, and established publications, then falls back to metadata already held by Jellyfin. Creating a draw makes no live request to an external trivia service. Opaque, short-lived server-side draw IDs keep movie identities out of the pre-reveal response, and the poster proxy never exposes the Jellyfin API key.

## Quick start

Runs on Windows, macOS, and Linux with Python 3.10 or later and access to a Jellyfin server. No third-party Python packages are needed. Other Unix systems with Python and SQLite should also work, but are not included in automated testing. Use a current browser. The OpenRC service at the end of this guide is optional and Linux-specific.

1. Clone or download this repository and open a terminal in its directory.
2. Copy `.env.example` to `.env` and set `JELLYFIN_URL` and `JELLYFIN_API_KEY` for your own Jellyfin server. Create the API key in your Jellyfin dashboard.
3. Load the configuration and start the app (macOS/Linux):

```sh
chmod 600 .env
set -a
. ./.env
set +a
python3 app.py
```

On Windows, open PowerShell in the repository directory and set the environment variables directly (no `.env` file is needed):

```powershell
$env:JELLYFIN_URL = 'http://localhost:8096'
$env:JELLYFIN_API_KEY = 'replace-with-your-jellyfin-api-key'
python app.py
```

Use your own Jellyfin URL and API key. If Python is installed through the Windows launcher, `py -3 app.py` also works. Optional settings use the same syntax, for example `$env:FACT_DB_PATH = 'C:\JellyGen\facts.db'`; choose a directory your account can write to.

Open `http://localhost:8787`. Stop the app with Ctrl+C. The application reads environment variables; it does not automatically load `.env` files. macOS/Linux shell commands in this guide use `python3`; on Windows use `python` or `py -3` and enter multi-line enrichment commands on one line, omitting the shell continuation backslashes. For token files on Windows, restrict access using Windows file permissions instead of `chmod`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `JELLYFIN_URL` | `http://localhost:8096` | Your Jellyfin server's base URL |
| `JELLYFIN_API_KEY` | Empty | Required to access your Jellyfin library |
| `PORT` | `8787` | HTTP listening port |
| `CACHE_SECONDS` | `300` | Jellyfin metadata cache lifetime |
| `FACT_DB_PATH` | `facts.db` beside `app.py` | Writable SQLite database location |
| `ENRICHMENT_TOKEN` | Empty | Optional shared secret; enrichment is disabled when empty |
| `JELLYGEN_URL` | `http://localhost:8787` | App URL used by the enrichment client |
| `JELLYGEN_ENRICHMENT_TOKEN` | Token file fallback | Client secret matching the server's `ENRICHMENT_TOKEN` |
| `JELLYGEN_ENRICHMENT_TOKEN_FILE` | `.jellygen-enrichment-token` beside the client | Alternative client token file |

Keep actual configuration, tokens, databases, and logs out of Git. `.env.example` contains only generic examples. The app listens on all network interfaces and has no sign-in for movie picking; use it on a trusted network or behind an authenticated reverse proxy. Use HTTPS when sending enrichment tokens over a network.

## Researched wildcard database

JellyGen keeps sourced wildcard clues and a one-time research marker for each film in a small SQLite database at the configured `FACT_DB_PATH`. A film can have up to three worthwhile clues: one is enough, while two or three can be stored when the research genuinely turns up several strong oddities. Each clue includes a direct source URL, stays hidden until the film reveal, and is rotated by prior use. The protected enrichment interface accepts only films already present in the Jellyfin library and rejects clues that reveal the film title or contain unsuitable distressing material.

Enrichment is optional; movie draws also work with Jellyfin metadata alone. Generate a secret with `python3 -c "import secrets; print(secrets.token_hex(32))"`, then set it as `ENRICHMENT_TOKEN` on the server and `JELLYGEN_ENRICHMENT_TOKEN` for the client. Restart the server after configuring it. For a remote app, also set `JELLYGEN_URL`.

Use the `jellygen_enricher.py` client for manual or scheduled research:

```sh
python3 jellygen_enricher.py pending --limit 250
python3 jellygen_enricher.py add --movie-key tt1234567 \
  --clue-title "An unlikely rehearsal" \
  --clue-text "A performer prepared by shadowing a real night-shift crew." \
  --icon "🌙" \
  --source-url "https://example.com/interview" \
  --source-label "Read the interview"
python3 jellygen_enricher.py complete --movie-key tt1234567
python3 jellygen_enricher.py retry-empty
```

Adding facts and completing the research pass are deliberately separate operations. This makes interrupted runs resumable and keeps the interface independent of the researcher: Codex today, or a local LLM and web-retrieval process later. The client can alternatively read its token from `.jellygen-enrichment-token`, which is excluded from Git; create that file with mode `0600`. JellyGen itself does not use an OpenAI API key. `ENRICHMENT_TASK.md` provides optional research instructions for a researcher or automation you configure yourself.

IMDb title and trivia pages are accepted for straightforward production and cast facts, alongside interviews, studio material, film institutes, production documents, and reputable publications. Plot summaries are not used as wildcard facts.

## Source

- `app.py`: HTTP server, Jellyfin cache, researched-fact database, picker, poster proxy, and wildcards
- `index.html`: responsive single-page interface
- `jellygenerator.initd`: OpenRC service definition
- `jellygen_enricher.py`: protected client for scheduled fact research
- `ENRICHMENT_TASK.md`: durable scope and quality rules for the scheduled run
- `test_app.py`: focused unit tests

Run the tests with:

```sh
python3 -m unittest -v
```

The tests exercise database cleanup, HTTP endpoints, and draw logic without requiring a real Jellyfin server. On Windows, run `python -m unittest -v`.

## Optional OpenRC deployment

`jellygenerator.initd` is a service template for Linux systems using OpenRC. Install Python 3, create a `jellygenerator` user and group, and copy the application to `/opt/jellygenerator`. Create `/var/lib/jellygenerator` owned by that service user for the database.

Install the template as `/etc/init.d/jellygenerator` with executable permissions. Copy `.env.example` to `/etc/conf.d/jellygenerator`, replace the example values, and set `FACT_DB_PATH=/var/lib/jellygenerator/facts.db`. Protect this configuration with mode `0600`. OpenRC reads this file and the service exports its settings to Python. The optional `/etc/jellygenerator/enrichment.env` file may supply the enrichment secret.

```sh
rc-update add jellygenerator default
rc-service jellygenerator start
rc-service jellygenerator status
```

Logs are written to `/var/log/jellygenerator.log` and `/var/log/jellygenerator.err`. To update, test the new source, copy it to the installation directory, and restart the service. Existing installations should explicitly set `JELLYFIN_URL`, `FACT_DB_PATH`, and the enrichment client's `JELLYGEN_URL` to preserve their deployment configuration.
