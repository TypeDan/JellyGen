# JellyGen fact enrichment run

Enrich JellyGen's private, sourced wildcard database without changing the application code.

1. Run `python3 jellygen_enricher.py pending --limit 250` in this project.
2. If no movies are returned, report that every current library film has been researched and stop.
3. For each returned movie, research genuinely surprising, film-specific facts using the web. Save up to three distinct facts that are genuinely strong and directly sourced; do not add filler merely to reach three. One excellent fact is enough. Two or three are welcome when the film genuinely has several unusual stories.
4. Prefer authoritative interviews, studios, film institutes, established publications, or other sources that directly support the fact. IMDb title and trivia pages are acceptable direct sources for straightforward production, cast, continuity, filming, and credit facts. Use IMDb as a discovery lead for more contentious claims and corroborate those elsewhere when practical. Do not use a search-result snippet as evidence.
5. Good clues include unusual production methods, casting stories, practical effects, props, locations, source material, career crossovers, records, connections to another production, an actor playing multiple roles, unusually few returning cast members, or improbable behind-the-scenes events. A modest but genuinely curious production fact is useful; it does not need to be historically extraordinary.
6. Avoid plot summaries presented as facts, plot spoilers, distressing crime, abuse, death, sexual material, medical details, private gossip, and claims that the source does not establish.
7. Write a short clue that creates curiosity without using the film title. It must be understandable before the reveal and between 20 and 280 characters.
8. Add each worthwhile fact with `python3 jellygen_enricher.py add`, supplying the exact `movieKey`, a short `clue-title`, the clue text, one suitable emoji, the direct source URL, and a useful source label.
9. When that film's research pass is finished, run `python3 jellygen_enricher.py complete --movie-key MOVIE_KEY`. Complete the film even if careful research found no suitable fact, but never complete a film you did not actually research.
10. Continue through every returned film. If the run cannot finish the whole batch, leave untouched films incomplete so the next run resumes them.
11. Run the pending command again and report the number of films researched, facts accepted, films completed without a fact, and films remaining. If a fact is rejected, correct it once; do not bypass the validation.

Do not edit source files, credentials, or server configuration. Use only `jellygen_enricher.py` for JellyGen writes.
