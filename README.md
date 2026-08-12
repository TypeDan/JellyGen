# JellyGen

JellyGen is a dependency-free Python web app for settling movie night from a Jellyfin library. The user chooses a year range, the generator draws a genre, and it creates a fresh set of film-specific wildcard clues without revealing the films. It prioritises sourced real-world oddities from Wikidata—unexpected cast careers, sporting or military history, unusual source material, production journeys, records, and carefully filtered off-screen incidents—then falls back to Jellyfin metadata when needed. The source link stays hidden until the chosen film is revealed. Opaque, short-lived server-side draw IDs keep movie identities out of the pre-reveal response, and the poster proxy never exposes the Jellyfin API key.

## Configuration

Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` for your own Jellyfin server before running `python3 app.py`.

## Researched wildcard database

JellyGen keeps sourced wildcard clues and a one-time research marker for each film in a small SQLite database at `/var/lib/jellygenerator/facts.db`. A film can have up to three worthwhile clues: one is enough, while two or three can be stored when the research genuinely turns up several strong oddities. Each clue includes a direct source URL, stays hidden until the film reveal, and is rotated by prior use. The protected enrichment interface accepts only films already present in the Jellyfin library and rejects clues that reveal the film title or contain unsuitable distressing material.

The local `jellygen_enricher.py` client is the only interface needed by the scheduled Codex task:

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

Adding facts and completing the research pass are deliberately separate operations. This makes interrupted runs resumable and keeps the interface independent of the researcher: Codex today, or a local LLM and web-retrieval process later. The client reads its private token from `.jellygen-enrichment-token`, which is excluded from Git and stored with mode `0600`. JellyGen itself does not use an OpenAI API key; the scheduled research run uses the signed-in Codex desktop app.

IMDb title and trivia pages are accepted for straightforward production and cast facts, alongside interviews, studio material, film institutes, production documents, and reputable publications. Plot summaries are not used as wildcard facts.

## Source

- `app.py`: HTTP server, Jellyfin and trivia caches, picker, poster proxy, and wildcards
- `index.html`: responsive single-page interface
- `jellygenerator.initd`: OpenRC service definition
- `jellygen_enricher.py`: protected client for scheduled fact research
- `ENRICHMENT_TASK.md`: durable scope and quality rules for the scheduled run
- `test_app.py`: focused unit tests

Run the tests with:

```sh
python3 -m unittest -v
```

## Deployment workflow

Test changes in a clone, then deploy the application files to your own server and restart the service. Keep credentials out of Git.
