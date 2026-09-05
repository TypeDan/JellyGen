#!/usr/bin/env python3
"""A tiny, dependency-free Jellyfin movie picker."""

from __future__ import annotations

import json
import hmac
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
INDEX_HTML = (APP_DIR / "index.html").read_bytes()
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
PORT = int(os.environ.get("PORT", "8787"))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "300"))
FACT_DB_PATH = Path(os.environ.get("FACT_DB_PATH", "/var/lib/jellygenerator/facts.db"))
ENRICHMENT_TOKEN = os.environ.get("ENRICHMENT_TOKEN", "")
MAX_BODY_BYTES = 16_384
MAX_POSTER_BYTES = 12 * 1024 * 1024
ITEM_ID_RE = re.compile(r"^[A-Fa-f0-9-]{16,64}$")
IMDB_ID_RE = re.compile(r"^tt\d{5,12}$")
RNG = random.SystemRandom()

_cache_lock = threading.Lock()
_movie_cache: dict[str, Any] = {"expires": 0.0, "items": []}
_draw_lock = threading.Lock()
_draws: dict[str, dict[str, Any]] = {}
_recent_clues: dict[str, set[str]] = {}
_fact_db_lock = threading.Lock()
DRAW_TTL_SECONDS = 30 * 60
MAX_ACTIVE_DRAWS = 200
DRAW_SIZE = 6
MAX_ENRICHMENT_BATCH = 250
MAX_RESEARCHED_FACTS_PER_MOVIE = 3

FACT_STYLE = {
    "voice": ("Rare voice", "🎙️"),
    "actor_once": ("One-film face", "👤"),
    "actor_twice": ("Small-world cast", "🎭"),
    "director_once": ("One-off director", "🎬"),
    "writer_once": ("One-off writer", "✍️"),
    "tagline": ("Tagline tease", "💬"),
    "genre_mix": ("Genre collision", "🦄"),
    "rating": ("Rating outlier", "⭐"),
    "runtime": ("Clock watcher", "⏱️"),
    "year": ("Year loner", "📅"),
    "premiere": ("Calendar oddity", "🗓️"),
    "original_title": ("Lost in translation", "🌍"),
    "location": ("Made over there", "📍"),
    "studio": ("Studio rarity", "🎞️"),
    "cast": ("Cast curiosity", "🎭"),
    "age": ("Time capsule", "🕰️"),
    "researched": ("Stranger than fiction", "🔎"),
}

PREFERRED_LOCAL_FACT_CATEGORIES = {
    "voice",
    "tagline",
    "genre_mix",
    "rating",
    "runtime",
    "premiere",
    "original_title",
}

DISTRESSING_CONVICTION_TERMS = (
    "abuse",
    "assault",
    "battery",
    "child",
    "homicide",
    "kidnap",
    "manslaughter",
    "murder",
    "pornograph",
    "rape",
    "sexual",
)


class JellyfinError(RuntimeError):
    pass


def jellyfin_request(path: str, query: dict[str, Any] | None = None) -> tuple[bytes, str]:
    if not JELLYFIN_API_KEY:
        raise JellyfinError("The Jellyfin API key is not configured.")
    url = f"{JELLYFIN_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"
    request = Request(
        url,
        headers={
            "X-Emby-Token": JELLYFIN_API_KEY,
            "Accept": "application/json, image/*;q=0.9",
            "User-Agent": "JellyGenerator/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.read(MAX_POSTER_BYTES + 1), response.headers.get_content_type()
    except HTTPError as exc:
        raise JellyfinError(f"Jellyfin returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise JellyfinError("Jellyfin could not be reached.") from exc


def fetch_movies(force: bool = False) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _cache_lock:
        if not force and _movie_cache["items"] and now < _movie_cache["expires"]:
            return _movie_cache["items"]

        items: list[dict[str, Any]] = []
        start_index = 0
        page_size = 500
        while True:
            payload, _ = jellyfin_request(
                "/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie",
                    "Fields": "Genres,Overview,ImageTags,CommunityRating,RunTimeTicks,Taglines,People,PremiereDate,ProviderIds,DateCreated,Studios,ProductionLocations,OriginalTitle,OfficialRating",
                    "EnableImages": "true",
                    "ImageTypeLimit": 1,
                    "StartIndex": start_index,
                    "Limit": page_size,
                    "SortBy": "SortName",
                },
            )
            try:
                page = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise JellyfinError("Jellyfin returned an invalid library response.") from exc
            page_items = page.get("Items", [])
            if not isinstance(page_items, list):
                raise JellyfinError("Jellyfin returned an unexpected library response.")
            items.extend(page_items)
            start_index += len(page_items)
            total = int(page.get("TotalRecordCount", len(items)))
            if not page_items or start_index >= total:
                break

        _movie_cache["items"] = items
        _movie_cache["expires"] = time.monotonic() + CACHE_SECONDS
        return items


def movie_year(movie: dict[str, Any]) -> int | None:
    year = movie.get("ProductionYear")
    if isinstance(year, int):
        return year
    premiere = movie.get("PremiereDate")
    if isinstance(premiere, str) and len(premiere) >= 4 and premiere[:4].isdigit():
        return int(premiere[:4])
    return None


def movie_rating(movie: dict[str, Any]) -> float | None:
    rating = movie.get("CommunityRating")
    return float(rating) if isinstance(rating, (int, float)) else None


def runtime_minutes(movie: dict[str, Any]) -> int | None:
    runtime_ticks = movie.get("RunTimeTicks")
    return round(runtime_ticks / 600_000_000) if isinstance(runtime_ticks, int) and runtime_ticks > 0 else None


def movies_in_range(movies: list[dict[str, Any]], start_year: int, end_year: int) -> list[dict[str, Any]]:
    return [movie for movie in movies if (year := movie_year(movie)) is not None and start_year <= year <= end_year]


def person_names(movie: dict[str, Any], person_type: str) -> list[str]:
    return [
        str(person["Name"])
        for person in (movie.get("People") or [])
        if person.get("Type") == person_type and person.get("Name")
    ]


def person_counts(movies: list[dict[str, Any]], person_type: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for movie in movies:
        counts.update(set(person_names(movie, person_type)))
    return counts


def fact_card(
    category: str,
    text: str,
    *,
    source_url: str | None = None,
    source_label: str | None = None,
) -> dict[str, str]:
    title, icon = FACT_STYLE[category]
    card = {"category": category, "title": title, "icon": icon, "text": text}
    if source_url:
        card["sourceUrl"] = source_url
        card["sourceLabel"] = source_label or "View the source"
    return card


def movie_imdb_id(movie: dict[str, Any]) -> str | None:
    imdb_id = (movie.get("ProviderIds") or {}).get("Imdb")
    return imdb_id if isinstance(imdb_id, str) and IMDB_ID_RE.fullmatch(imdb_id) else None


def movie_research_key(movie: dict[str, Any]) -> str:
    imdb_id = movie_imdb_id(movie)
    if imdb_id:
        return imdb_id
    return f"jellyfin:{movie.get('Id', '')}"


def init_fact_db() -> None:
    FACT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS researched_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_key TEXT NOT NULL,
                imdb_id TEXT,
                movie_title TEXT NOT NULL,
                movie_year INTEGER,
                clue_title TEXT NOT NULL,
                clue_text TEXT NOT NULL,
                icon TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_label TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(movie_key, clue_text)
            );
            CREATE INDEX IF NOT EXISTS researched_facts_movie
                ON researched_facts(movie_key, use_count, last_used_at);
            CREATE TABLE IF NOT EXISTS researched_movies (
                movie_key TEXT PRIMARY KEY,
                imdb_id TEXT,
                movie_title TEXT NOT NULL,
                movie_year INTEGER,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        migration = connection.execute(
            "INSERT OR IGNORE INTO app_metadata (key, value) VALUES (?, CURRENT_TIMESTAMP)",
            ("researched_movies_backfill_v1",),
        )
        if migration.rowcount == 1:
            connection.execute(
                """
                INSERT OR IGNORE INTO researched_movies (
                    movie_key, imdb_id, movie_title, movie_year, completed_at
                )
                SELECT movie_key, MAX(imdb_id), MAX(movie_title), MAX(movie_year), MIN(added_at)
                FROM researched_facts
                GROUP BY movie_key
                """
            )
    FACT_DB_PATH.chmod(0o640)


def researched_fact_counts() -> dict[str, int]:
    init_fact_db()
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        rows = connection.execute(
            "SELECT movie_key, COUNT(*) FROM researched_facts GROUP BY movie_key"
        ).fetchall()
    return {str(movie_key): int(count) for movie_key, count in rows}


def researched_movie_keys() -> set[str]:
    init_fact_db()
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        rows = connection.execute("SELECT movie_key FROM researched_movies").fetchall()
    return {str(row[0]) for row in rows}


def fetch_researched_facts(movies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    keys = list(dict.fromkeys(movie_research_key(movie) for movie in movies))
    if not keys:
        return {}
    init_fact_db()
    placeholders = ",".join("?" for _ in keys)
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT id, movie_key, clue_title, clue_text, icon, source_url, source_label
            FROM researched_facts
            WHERE movie_key IN ({placeholders})
            ORDER BY use_count ASC, COALESCE(last_used_at, '') ASC, id ASC
            """,
            keys,
        ).fetchall()
    facts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        facts.setdefault(str(row["movie_key"]), []).append(
            {
                "category": "researched",
                "title": str(row["clue_title"]),
                "icon": str(row["icon"]),
                "text": str(row["clue_text"]),
                "sourceUrl": str(row["source_url"]),
                "sourceLabel": str(row["source_label"]),
                "_researchId": int(row["id"]),
            }
        )
    return facts


def mark_researched_fact_used(fact: dict[str, Any]) -> None:
    fact_id = fact.get("_researchId")
    if not isinstance(fact_id, int):
        return
    init_fact_db()
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        connection.execute(
            """
            UPDATE researched_facts
            SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (fact_id,),
        )


def pending_research_movies(movies: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    counts = researched_fact_counts()
    completed = researched_movie_keys()
    library_keys = {movie_research_key(movie) for movie in movies}
    pending = [movie for movie in movies if movie_research_key(movie) not in completed]
    pending.sort(
        key=lambda movie: (
            movie_year(movie) or 9999,
            str(movie.get("Name") or "").casefold(),
        )
    )
    selected = pending[:max(1, min(limit, MAX_ENRICHMENT_BATCH))]
    return {
        "libraryMovieCount": len(movies),
        "researchedMovieCount": len(completed & library_keys),
        "unresearchedMovieCount": len(pending),
        "pendingMovieCount": len(pending),
        "movies": [
            {
                "movieKey": movie_research_key(movie),
                "title": movie.get("Name") or "Untitled",
                "year": movie_year(movie),
                "imdbId": movie_imdb_id(movie),
                "genres": movie.get("Genres") or [],
                "directors": person_names(movie, "Director")[:3],
                "cast": person_names(movie, "Actor")[:8],
                "factCount": counts.get(movie_research_key(movie), 0),
            }
            for movie in selected
        ],
    }


def mark_movie_researched(movie: dict[str, Any]) -> bool:
    init_fact_db()
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO researched_movies (
                movie_key, imdb_id, movie_title, movie_year
            ) VALUES (?, ?, ?, ?)
            """,
            (
                movie_research_key(movie),
                movie_imdb_id(movie),
                str(movie.get("Name") or "Untitled"),
                movie_year(movie),
            ),
        )
        return cursor.rowcount == 1


def reopen_empty_researched_movies(movies: list[dict[str, Any]]) -> int:
    """Requeue completed library films for which research stored no usable fact."""
    keys = [movie_research_key(movie) for movie in movies]
    if not keys:
        return 0
    init_fact_db()
    placeholders = ",".join("?" for _ in keys)
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        cursor = connection.execute(
            f"""
            DELETE FROM researched_movies
            WHERE movie_key IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1
                  FROM researched_facts
                  WHERE researched_facts.movie_key = researched_movies.movie_key
              )
            """,
            keys,
        )
        return cursor.rowcount


def validate_researched_fact(movie: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    fields = {
        "clueTitle": (4, 60),
        "clueText": (20, 280),
        "icon": (1, 12),
        "sourceUrl": (10, 1000),
        "sourceLabel": (4, 90),
    }
    cleaned: dict[str, str] = {}
    for field, (minimum, maximum) in fields.items():
        value = payload.get(field)
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum} characters.")
        cleaned[field] = value.strip()

    parsed_source = urlparse(cleaned["sourceUrl"])
    if parsed_source.scheme not in ("http", "https") or not parsed_source.netloc:
        raise ValueError("sourceUrl must be a valid HTTP or HTTPS URL.")
    clue = {"text": f"{cleaned['clueTitle']} {cleaned['clueText']}"}
    if fact_mentions_movie_title(movie, clue):
        raise ValueError("The clue must not reveal the movie title.")
    if any(term in cleaned["clueText"].casefold() for term in DISTRESSING_CONVICTION_TERMS):
        raise ValueError("Choose a less distressing fact for movie night.")
    return cleaned


def store_researched_fact(movie: dict[str, Any], payload: dict[str, Any]) -> bool:
    fact = validate_researched_fact(movie, payload)
    init_fact_db()
    with _fact_db_lock, sqlite3.connect(FACT_DB_PATH, timeout=5) as connection:
        existing_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM researched_facts WHERE movie_key = ?",
                (movie_research_key(movie),),
            ).fetchone()[0]
        )
        if existing_count >= MAX_RESEARCHED_FACTS_PER_MOVIE:
            raise ValueError(
                f"A film can have at most {MAX_RESEARCHED_FACTS_PER_MOVIE} researched facts."
            )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO researched_facts (
                movie_key, imdb_id, movie_title, movie_year,
                clue_title, clue_text, icon, source_url, source_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                movie_research_key(movie),
                movie_imdb_id(movie),
                str(movie.get("Name") or "Untitled"),
                movie_year(movie),
                fact["clueTitle"],
                fact["clueText"],
                fact["icon"],
                fact["sourceUrl"],
                fact["sourceLabel"],
            ),
        )
        return cursor.rowcount == 1


def movie_fact_pool(
    movie: dict[str, Any],
    genre: str,
    genre_movies: list[dict[str, Any]],
    range_movies: list[dict[str, Any]],
    all_movies: list[dict[str, Any]],
) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    title = str(movie.get("Name") or "").casefold()
    actors = [person for person in (movie.get("People") or []) if person.get("Type") == "Actor" and person.get("Name")]
    actor_library_counts = person_counts(all_movies, "Actor")
    director_library_counts = person_counts(all_movies, "Director")
    writer_library_counts = person_counts(all_movies, "Writer")

    voice_roles = [
        person for person in actors
        if isinstance(person.get("Role"), str) and re.search(r"\bvoice\b|vocal", person["Role"], re.IGNORECASE)
    ]
    if voice_roles:
        person = RNG.choice(voice_roles)
        role = re.sub(r"\s*\(?voice\)?\s*", "", person["Role"], flags=re.IGNORECASE).strip(" ,-/")
        detail = f" as {role}" if role else ""
        facts.append(fact_card("voice", f"{person['Name']} has a credited voice role{detail}."))

    one_film_actors = [person["Name"] for person in actors if actor_library_counts[person["Name"]] == 1]
    if one_film_actors:
        name = RNG.choice(one_film_actors)
        facts.append(fact_card("actor_once", f"{name} appears in only this one movie across your entire Jellyfin library."))

    two_film_actors = [person["Name"] for person in actors if actor_library_counts[person["Name"]] == 2]
    if two_film_actors:
        name = RNG.choice(two_film_actors)
        facts.append(fact_card("actor_twice", f"{name} appears in exactly two films in your whole library. This is one of them."))

    unique_directors = [name for name in person_names(movie, "Director") if director_library_counts[name] == 1]
    if unique_directors:
        name = RNG.choice(unique_directors)
        facts.append(fact_card("director_once", f"This is the only film in your library directed by {name}."))

    unique_writers = [name for name in person_names(movie, "Writer") if writer_library_counts[name] == 1]
    if unique_writers:
        name = RNG.choice(unique_writers)
        facts.append(fact_card("writer_once", f"This is the only film in your library written by {name}."))

    safe_taglines = [
        str(tagline) for tagline in (movie.get("Taglines") or [])
        if tagline and (not title or title not in str(tagline).casefold())
    ]
    if safe_taglines:
        facts.append(fact_card("tagline", f"Its tagline is: “{RNG.choice(safe_taglines)}”"))

    combo = tuple(sorted(movie.get("Genres") or ["Unclassified"]))
    combo_count = Counter(tuple(sorted(item.get("Genres") or ["Unclassified"])) for item in range_movies)[combo]
    if combo_count <= 2 or len(combo) >= 4:
        facts.append(fact_card("genre_mix", f"Only {combo_count} film{'s' if combo_count != 1 else ''} in your selected years use this exact mix: {' + '.join(combo)}."))

    ratings = [rating for item in genre_movies if (rating := movie_rating(item)) is not None]
    rating = movie_rating(movie)
    if rating is not None and ratings:
        if rating == max(ratings):
            facts.append(fact_card("rating", f"This is the highest-rated {genre} film in your chosen years at {rating:.1f}/10."))
        elif rating == min(ratings):
            facts.append(fact_card("rating", f"This is the lowest-rated {genre} film in your chosen years at {rating:.1f}/10."))

    runtimes = [runtime for item in genre_movies if (runtime := runtime_minutes(item)) is not None]
    runtime = runtime_minutes(movie)
    if runtime is not None and runtimes:
        if runtime == min(runtimes):
            facts.append(fact_card("runtime", f"At {runtime} minutes, this is the shortest {genre} film in the chosen years."))
        elif runtime == max(runtimes):
            facts.append(fact_card("runtime", f"At {runtime} minutes, this is the longest {genre} film in the chosen years."))

    year = movie_year(movie)
    year_counts = Counter(movie_year(item) for item in genre_movies)
    if year is not None and year_counts[year] == 1:
        facts.append(fact_card("year", f"This is the only {genre} film from {year} in your chosen range."))

    premiere = movie.get("PremiereDate")
    if isinstance(premiere, str):
        try:
            premiere_date = datetime.fromisoformat(premiere.replace("Z", "+00:00"))
            if premiere_date.month == 2 and premiere_date.day == 29:
                facts.append(fact_card("premiere", "It premiered on leap day—29 February."))
            elif premiere_date.day == 13 and premiere_date.strftime("%A") == "Friday":
                facts.append(fact_card("premiere", "It premiered on a Friday the 13th."))
            elif (premiere_date.month, premiere_date.day) in ((10, 31), (12, 25)):
                facts.append(fact_card("premiere", f"It premiered on {premiere_date.strftime('%d %B')}."))
        except ValueError:
            pass

    original_title = movie.get("OriginalTitle")
    if isinstance(original_title, str) and original_title and original_title.casefold() != title:
        facts.append(fact_card("original_title", f"Its original title was “{original_title}”."))

    locations = [str(location) for location in (movie.get("ProductionLocations") or []) if location]
    if locations:
        facts.append(fact_card("location", f"Jellyfin lists {RNG.choice(locations)} as one of its production locations."))

    studios = [studio.get("Name") for studio in (movie.get("Studios") or []) if isinstance(studio, dict) and studio.get("Name")]
    if studios:
        studio_counts = Counter(
            studio.get("Name")
            for item in all_movies
            for studio in (item.get("Studios") or [])
            if isinstance(studio, dict) and studio.get("Name")
        )
        rare_studios = [studio for studio in studios if studio_counts[studio] <= 2]
        if rare_studios:
            studio = RNG.choice(rare_studios)
            count = studio_counts[studio]
            facts.append(fact_card("studio", f"Only {count} film{'s' if count != 1 else ''} in your library carry a {studio} studio credit."))

    if len(actors) >= 20:
        facts.append(fact_card("cast", f"Jellyfin credits {len(actors)} different actors to this film."))

    if year is not None:
        age = max(0, time.gmtime().tm_year - year)
        facts.append(fact_card("age", f"This film was released {age} year{'s' if age != 1 else ''} ago."))

    return facts or [fact_card("cast", f"Jellyfin stores {len(actors)} credited actor{'s' if len(actors) != 1 else ''} for this film.")]


def fact_mentions_movie_title(movie: dict[str, Any], fact: dict[str, str]) -> bool:
    title = str(movie.get("Name") or "").strip().casefold()
    text = fact.get("text", "").casefold()
    return bool(title and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text))


def create_hidden_draw(
    genre: str,
    genre_movies: list[dict[str, Any]],
    range_movies: list[dict[str, Any]],
    all_movies: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    research_candidates = RNG.sample(genre_movies, min(10, len(genre_movies)))
    researched_facts = fetch_researched_facts(research_candidates)
    researched_movies = [
        movie
        for movie in research_candidates
        if any(
            not fact_mentions_movie_title(movie, fact)
            for fact in researched_facts.get(movie_research_key(movie), [])
        )
    ]
    chosen_movies = RNG.sample(researched_movies, min(DRAW_SIZE, len(researched_movies)))
    minimum_choices = min(DRAW_SIZE, len(genre_movies))
    if len(chosen_movies) < minimum_choices:
        chosen_ids = {id(movie) for movie in chosen_movies}
        remaining = [movie for movie in genre_movies if id(movie) not in chosen_ids]
        chosen_movies.extend(RNG.sample(remaining, min(minimum_choices - len(chosen_movies), len(remaining))))

    choices: list[dict[str, str]] = []
    private_choices: dict[str, dict[str, Any]] = {}
    used_categories: set[str] = set()
    used_texts: set[str] = set()
    with _draw_lock:
        previous_clues = set(_recent_clues.get(genre, set()))

    for movie in chosen_movies:
        pool = movie_fact_pool(movie, genre, genre_movies, range_movies, all_movies)
        preferred_local_pool = [fact for fact in pool if fact["category"] in PREFERRED_LOCAL_FACT_CATEGORIES]
        local_pool = preferred_local_pool or pool
        researched_pool = [
            fact
            for fact in researched_facts.get(movie_research_key(movie), [])
            if not fact_mentions_movie_title(movie, fact)
        ]
        fresh_researched = [
            fact for fact in researched_pool
            if fact["text"] not in previous_clues and fact["text"] not in used_texts
        ]
        unique_researched = [fact for fact in researched_pool if fact["text"] not in used_texts]
        fresh_local = [
            fact for fact in local_pool
            if fact["text"] not in previous_clues and fact["text"] not in used_texts
        ]
        unique_local = [fact for fact in local_pool if fact["text"] not in used_texts]
        unused_local = [fact for fact in fresh_local if fact["category"] not in used_categories]
        fact = RNG.choice(
            fresh_researched
            or unique_researched
            or unused_local
            or fresh_local
            or unique_local
            or local_pool
        )
        used_categories.add(fact["category"])
        used_texts.add(fact["text"])
        mark_researched_fact_used(fact)
        wildcard_id = secrets.token_urlsafe(9)
        public_fact = {
            key: value for key, value in fact.items()
            if key != "category" and not key.startswith("_")
        }
        choices.append({
            "id": wildcard_id,
            "title": public_fact["title"],
            "icon": public_fact["icon"],
            "description": public_fact["text"],
        })
        private_choices[wildcard_id] = {"movie": movie, "fact": public_fact}

    draw_id = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _draw_lock:
        expired = [key for key, draw in _draws.items() if draw["expires"] <= now]
        for key in expired:
            del _draws[key]
        if len(_draws) >= MAX_ACTIVE_DRAWS:
            oldest = min(_draws, key=lambda key: _draws[key]["created"])
            del _draws[oldest]
        _draws[draw_id] = {
            "created": now,
            "expires": now + DRAW_TTL_SECONDS,
            "genre": genre,
            "matchingMovies": len(genre_movies),
            "choices": private_choices,
        }
        _recent_clues[genre] = {choice["description"] for choice in choices}
    RNG.shuffle(choices)
    return draw_id, choices


def reveal_hidden_wildcard(draw_id: str, wildcard_id: str) -> tuple[dict[str, Any], dict[str, str], str, int]:
    now = time.monotonic()
    with _draw_lock:
        draw = _draws.get(draw_id)
        if draw is None or draw["expires"] <= now:
            if draw is not None:
                del _draws[draw_id]
            raise ValueError("That draw expired. Draw a fresh genre and try again.")
        choice = draw["choices"].get(wildcard_id)
        if choice is None:
            raise ValueError("Choose one of the wildcard clues from this draw.")
        return choice["movie"], choice["fact"], draw["genre"], draw["matchingMovies"]


def public_movie(movie: dict[str, Any], wildcard_result: dict[str, str]) -> dict[str, Any]:
    people = movie.get("People") or []
    directors = [person.get("Name") for person in people if person.get("Type") == "Director" and person.get("Name")]
    taglines = movie.get("Taglines") or []
    primary_image = bool((movie.get("ImageTags") or {}).get("Primary"))
    movie_id = str(movie.get("Id", ""))
    imdb_id = movie_imdb_id(movie)
    return {
        "id": movie_id,
        "title": movie.get("Name") or "Untitled",
        "year": movie_year(movie),
        "genres": movie.get("Genres") or ["Unclassified"],
        "overview": movie.get("Overview") or "No synopsis is stored in Jellyfin for this film.",
        "tagline": taglines[0] if taglines else None,
        "runtimeMinutes": runtime_minutes(movie),
        "rating": movie_rating(movie),
        "directors": directors[:2],
        "posterUrl": f"/api/poster/{movie_id}" if primary_image and ITEM_ID_RE.fullmatch(movie_id) else None,
        "imdbUrl": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        "wildcard": wildcard_result,
    }


def library_meta(movies: list[dict[str, Any]]) -> dict[str, Any]:
    years = [year for movie in movies if (year := movie_year(movie)) is not None]
    if not years:
        raise JellyfinError("No dated movies were found in the Jellyfin library.")
    return {"minYear": min(years), "maxYear": max(years), "movieCount": len(movies)}


class Handler(BaseHTTPRequestHandler):
    server_version = "JellyGenerator/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def send_bytes(self, status: int, body: bytes, content_type: str, *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        self.send_bytes(status, json.dumps(data, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def enrichment_authorized(self) -> bool:
        if not ENRICHMENT_TOKEN:
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Enrichment is not configured.")
            return False
        provided = self.headers.get("Authorization", "")
        expected = f"Bearer {ENRICHMENT_TOKEN}"
        if not hmac.compare_digest(provided, expected):
            self.send_error_json(HTTPStatus.FORBIDDEN, "Invalid enrichment credentials.")
            return False
        return True

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            body = INDEX_HTML if path == "/" else b'{"status":"ok"}'
            content_type = "text/html; charset=utf-8" if path == "/" else "application/json; charset=utf-8"
            self.send_bytes(HTTPStatus.OK, body, content_type)
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8", cache="no-cache")
            return
        if parsed.path == "/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/api/meta":
            try:
                self.send_json(HTTPStatus.OK, library_meta(fetch_movies()))
            except JellyfinError as exc:
                self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
            return
        if parsed.path == "/api/enrichment/pending":
            if not self.enrichment_authorized():
                return
            try:
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["3"])[0])
                self.send_json(HTTPStatus.OK, pending_research_movies(fetch_movies(), limit))
            except (TypeError, ValueError):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Choose a valid pending-film limit.")
            except JellyfinError as exc:
                self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
            return
        if parsed.path.startswith("/api/poster/"):
            item_id = parsed.path.rsplit("/", 1)[-1]
            if not ITEM_ID_RE.fullmatch(item_id):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid movie identifier.")
                return
            try:
                body, content_type = jellyfin_request(f"/Items/{item_id}/Images/Primary", {"maxWidth": 700, "quality": 88})
                if len(body) > MAX_POSTER_BYTES or not content_type.startswith("image/"):
                    raise JellyfinError("The poster response was invalid.")
                self.send_bytes(HTTPStatus.OK, body, content_type, cache="private, max-age=86400")
            except JellyfinError as exc:
                self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path not in (
            "/api/genre",
            "/api/pick",
            "/api/enrichment/facts",
            "/api/enrichment/complete",
            "/api/enrichment/retry-empty",
        ):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if request_path.startswith("/api/enrichment/") and not self.enrichment_authorized():
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid request length.")
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid request body.")
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, TypeError, ValueError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON request.")
            return

        if request_path == "/api/enrichment/retry-empty":
            try:
                reopened = reopen_empty_researched_movies(fetch_movies())
                self.send_json(HTTPStatus.OK, {"reopenedMovieCount": reopened})
            except JellyfinError as exc:
                self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
            return

        if request_path in ("/api/enrichment/facts", "/api/enrichment/complete"):
            if not isinstance(payload, dict) or not isinstance(payload.get("movieKey"), str):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Choose a valid library movie.")
                return
            try:
                movies = fetch_movies()
                movie = next(
                    (item for item in movies if movie_research_key(item) == payload["movieKey"]),
                    None,
                )
                if movie is None:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "That movie is not in the Jellyfin library.")
                    return
                if request_path == "/api/enrichment/complete":
                    created = mark_movie_researched(movie)
                    self.send_json(
                        HTTPStatus.CREATED if created else HTTPStatus.OK,
                        {
                            "completed": created,
                            "movieKey": movie_research_key(movie),
                            "factCount": researched_fact_counts().get(movie_research_key(movie), 0),
                        },
                    )
                    return
                created = store_researched_fact(movie, payload)
                self.send_json(
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {
                        "stored": created,
                        "movieKey": movie_research_key(movie),
                        "factCount": researched_fact_counts().get(movie_research_key(movie), 0),
                    },
                )
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except JellyfinError as exc:
                self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
            return

        if request_path == "/api/pick":
            draw_id = payload.get("drawId") if isinstance(payload, dict) else None
            wildcard_id = payload.get("wildcard") if isinstance(payload, dict) else None
            if (
                not isinstance(draw_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{12,40}", draw_id)
                or not isinstance(wildcard_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,24}", wildcard_id)
            ):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Choose a valid wildcard clue.")
                return
            try:
                movie, wildcard_result, genre, matching_movies = reveal_hidden_wildcard(draw_id, wildcard_id)
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "movie": public_movie(movie, wildcard_result),
                        "matchingMovies": matching_movies,
                        "genre": genre,
                    },
                )
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            start_year = payload["startYear"]
            end_year = payload["endYear"]
            if type(start_year) is not int or type(end_year) is not int:
                raise ValueError("Years must be whole numbers.")
        except (KeyError, TypeError, ValueError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Choose a valid start and end year.")
            return
        current_year = time.gmtime().tm_year
        if start_year < 1888 or end_year > current_year + 2 or start_year > end_year:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Choose a valid year range.")
            return
        try:
            movies = fetch_movies()
            range_candidates = movies_in_range(movies, start_year, end_year)
            if not range_candidates:
                self.send_error_json(HTTPStatus.NOT_FOUND, f"No movies were found from {start_year} to {end_year}.")
                return

            genre_counts = Counter(genre for movie in range_candidates for genre in (movie.get("Genres") or []) if genre)
            if not genre_counts:
                self.send_error_json(HTTPStatus.NOT_FOUND, "No genre information was found for those movies.")
                return
            eligible_genres = [genre for genre, count in genre_counts.items() if count >= 2] or list(genre_counts)
            genre = RNG.choice(sorted(eligible_genres))
            genre_candidates = [movie for movie in range_candidates if genre in (movie.get("Genres") or [])]
            draw_id, wildcards = create_hidden_draw(genre, genre_candidates, range_candidates, movies)
            self.send_json(
                HTTPStatus.OK,
                {
                    "genre": genre,
                    "genreMovieCount": len(genre_candidates),
                    "yearRangeMovies": len(range_candidates),
                    "drawId": draw_id,
                    "wildcards": wildcards,
                },
            )
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except JellyfinError as exc:
            self.send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))


def main() -> None:
    init_fact_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"JellyGenerator listening on port {PORT}; Jellyfin={JELLYFIN_URL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
