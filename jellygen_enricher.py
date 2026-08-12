#!/usr/bin/env python3
"""Small authenticated client used by the Codex fact-enrichment task."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "http://localhost:8787"
DEFAULT_TOKEN_FILE = APP_DIR / ".jellygen-enrichment-token"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def enrichment_token() -> str:
    from_environment = os.environ.get("JELLYGEN_ENRICHMENT_TOKEN", "").strip()
    if from_environment:
        return from_environment
    token_path = Path(os.environ.get("JELLYGEN_ENRICHMENT_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read the enrichment token from {token_path}.") from exc
    if not token:
        raise RuntimeError(f"The enrichment token file is empty: {token_path}")
    return token


def api_request(path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = os.environ.get("JELLYGEN_URL", DEFAULT_URL).rstrip("/")
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {enrichment_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "JellyGen-Codex-Enricher/1.0",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        try:
            message = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"JellyGen returned HTTP {exc.code}: {message}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("JellyGen could not be reached.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("JellyGen returned an unexpectedly large response.")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("JellyGen returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("JellyGen returned an unexpected response.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and enrich JellyGen's researched fact database.")
    commands = parser.add_subparsers(dest="command", required=True)

    pending = commands.add_parser("pending", help="List library films that have not been researched yet.")
    pending.add_argument("--limit", type=int, default=25, choices=range(1, 251))

    add = commands.add_parser("add", help="Add one sourced, non-spoiling wildcard clue.")
    add.add_argument("--movie-key", required=True)
    add.add_argument("--clue-title", required=True)
    add.add_argument("--clue-text", required=True)
    add.add_argument("--icon", default="🔎")
    add.add_argument("--source-url", required=True)
    add.add_argument("--source-label", default="Read the source")

    complete = commands.add_parser("complete", help="Mark a film's research pass as complete.")
    complete.add_argument("--movie-key", required=True)

    commands.add_parser(
        "retry-empty",
        help="Reopen completed library films that have no researched fact.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "pending":
            result = api_request(f"/api/enrichment/pending?{urlencode({'limit': args.limit})}")
        elif args.command == "add":
            result = api_request(
                "/api/enrichment/facts",
                payload={
                    "movieKey": args.movie_key,
                    "clueTitle": args.clue_title,
                    "clueText": args.clue_text,
                    "icon": args.icon,
                    "sourceUrl": args.source_url,
                    "sourceLabel": args.source_label,
                },
            )
        elif args.command == "complete":
            result = api_request(
                "/api/enrichment/complete",
                payload={"movieKey": args.movie_key},
            )
        else:
            result = api_request("/api/enrichment/retry-empty", payload={})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
