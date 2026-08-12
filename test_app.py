import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


MOVIES = [
    {
        "Id": "0123456789abcdef0123456789abcdef",
        "Name": "The Test Feature",
        "ProductionYear": 1987,
        "Genres": ["Comedy"],
        "Overview": "A test movie.",
        "RunTimeTicks": 6_000_000_000,
        "CommunityRating": 7.25,
        "ImageTags": {"Primary": "abc"},
        "People": [
            {"Name": "A. Director", "Type": "Director"},
            {"Name": "Once Only", "Type": "Actor", "Role": "The Tester (voice)"},
        ],
        "Taglines": ["Testing never sleeps."],
        "ProviderIds": {"Imdb": "tt1234567"},
    },
    {
        "Id": "fedcba9876543210fedcba9876543210",
        "Name": "A Newer Film",
        "PremiereDate": "2021-05-02T00:00:00.0000000Z",
        "Genres": ["Drama"],
    },
]


class JellyGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.fact_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.fact_directory.cleanup)
        self.fact_path_patch = patch.object(app, "FACT_DB_PATH", Path(self.fact_directory.name) / "facts.db")
        self.fact_path_patch.start()
        self.addCleanup(self.fact_path_patch.stop)
        app._draws.clear()
        app._recent_clues.clear()
        app._trivia_cache.clear()
        self.trivia_patch = patch.object(app, "fetch_wikidata_facts", return_value={})
        self.mock_trivia = self.trivia_patch.start()
        self.addCleanup(self.trivia_patch.stop)

    def test_movie_year_prefers_production_year(self):
        self.assertEqual(app.movie_year(MOVIES[0]), 1987)

    def test_movie_year_falls_back_to_premiere_date(self):
        self.assertEqual(app.movie_year(MOVIES[1]), 2021)

    def test_library_meta(self):
        self.assertEqual(app.library_meta(MOVIES), {"minYear": 1987, "maxYear": 2021, "movieCount": 2})

    def test_public_movie_does_not_expose_jellyfin_key(self):
        wildcard = {"id": "blind-draw", "title": "Mystery clue", "icon": "🔮", "text": "A clue."}
        result = app.public_movie(MOVIES[0], wildcard)
        self.assertEqual(result["title"], "The Test Feature")
        self.assertEqual(result["runtimeMinutes"], 10)
        self.assertEqual(result["posterUrl"], "/api/poster/0123456789abcdef0123456789abcdef")
        self.assertEqual(result["imdbUrl"], "https://www.imdb.com/title/tt1234567/")
        self.assertNotIn("apiKey", result)
        self.assertNotIn("token", str(result).lower())

    def test_fact_pool_finds_voice_and_one_film_actor_clues(self):
        facts = app.movie_fact_pool(MOVIES[0], "Comedy", [MOVIES[0]], [MOVIES[0]], MOVIES)
        categories = {fact["category"] for fact in facts}
        self.assertIn("voice", categories)
        self.assertIn("actor_once", categories)

    def test_wikidata_rows_create_sourced_real_world_fact(self):
        rows = [
            {
                "imdb": {"value": "tt1234567"},
                "film": {"value": "http://www.wikidata.org/entity/Q100"},
                "kind": {"value": "occupation"},
                "subject": {"value": "http://www.wikidata.org/entity/Q200"},
                "subjectLabel": {"value": "Example Performer"},
                "value": {"value": "http://www.wikidata.org/entity/Q11631"},
                "valueLabel": {"value": "astronaut"},
            }
        ]
        facts = app.wikidata_rows_to_facts(rows)["tt1234567"]
        self.assertIn("astronaut", facts[0]["text"])
        self.assertEqual(facts[0]["sourceUrl"], "https://www.wikidata.org/wiki/Q200")

    def test_researched_fact_is_stored_and_prioritised(self):
        payload = {
            "clueTitle": "A very unlikely rehearsal",
            "clueText": "One performer prepared for the role by shadowing a real night-shift crew.",
            "icon": "🌙",
            "sourceUrl": "https://example.com/interview",
            "sourceLabel": "Read the interview",
        }
        self.assertTrue(app.store_researched_fact(MOVIES[0], payload))
        facts = app.fetch_researched_facts([MOVIES[0]])["tt1234567"]
        self.assertEqual(facts[0]["text"], payload["clueText"])

        draw_id, wildcards = app.create_hidden_draw("Comedy", [MOVIES[0]], [MOVIES[0]], MOVIES)
        self.assertEqual(wildcards[0]["description"], payload["clueText"])
        _, revealed, _, _ = app.reveal_hidden_wildcard(draw_id, wildcards[0]["id"])
        self.assertEqual(revealed["sourceUrl"], payload["sourceUrl"])
        self.assertNotIn("_researchId", revealed)

    def test_pending_research_movies_tracks_coverage(self):
        before = app.pending_research_movies(MOVIES, 10)
        self.assertEqual(before["pendingMovieCount"], 2)
        self.assertEqual(before["movies"][0]["factCount"], 0)

        app.store_researched_fact(
            MOVIES[0],
            {
                "clueTitle": "An unlikely rehearsal",
                "clueText": "A performer prepared by shadowing a real night-shift crew for several days.",
                "icon": "🌙",
                "sourceUrl": "https://example.com/interview",
                "sourceLabel": "Read the interview",
            },
        )
        after = app.pending_research_movies(MOVIES, 10)
        movie = next(item for item in after["movies"] if item["movieKey"] == "tt1234567")
        self.assertEqual(movie["factCount"], 1)

        self.assertTrue(app.mark_movie_researched(MOVIES[0]))
        completed = app.pending_research_movies(MOVIES, 10)
        self.assertEqual(completed["researchedMovieCount"], 1)
        self.assertEqual(completed["unresearchedMovieCount"], 1)
        self.assertNotIn("tt1234567", {item["movieKey"] for item in completed["movies"]})

    def test_researched_fact_cannot_reveal_movie_title(self):
        with self.assertRaisesRegex(ValueError, "must not reveal"):
            app.store_researched_fact(
                MOVIES[0],
                {
                    "clueTitle": "A revealing clue",
                    "clueText": "The Test Feature was made after an unusual rehearsal process.",
                    "icon": "🔎",
                    "sourceUrl": "https://example.com/interview",
                    "sourceLabel": "Read the interview",
                },
            )

    def test_researched_facts_are_capped_at_three_per_movie(self):
        for number in range(3):
            self.assertTrue(
                app.store_researched_fact(
                    MOVIES[0],
                    {
                        "clueTitle": f"Unusual production story {number}",
                        "clueText": f"A directly sourced and suitably unusual production detail number {number}.",
                        "icon": "🔎",
                        "sourceUrl": f"https://example.com/interview-{number}",
                        "sourceLabel": "Read the interview",
                    },
                )
            )
        with self.assertRaisesRegex(ValueError, "at most 3"):
            app.store_researched_fact(
                MOVIES[0],
                {
                    "clueTitle": "A fourth production story",
                    "clueText": "This otherwise valid fourth fact should be rejected by the per-film ceiling.",
                    "icon": "🔎",
                    "sourceUrl": "https://example.com/interview-four",
                    "sourceLabel": "Read the interview",
                },
            )

    def test_only_completed_movies_without_facts_are_reopened(self):
        app.mark_movie_researched(MOVIES[0])
        app.mark_movie_researched(MOVIES[1])
        app.store_researched_fact(
            MOVIES[0],
            {
                "clueTitle": "A useful production connection",
                "clueText": "Only two established characters returned from the related television production.",
                "icon": "📺",
                "sourceUrl": "https://www.imdb.com/title/tt1234567/trivia/",
                "sourceLabel": "Read the IMDb trivia",
            },
        )
        self.assertEqual(app.reopen_empty_researched_movies(MOVIES), 1)
        pending = app.pending_research_movies(MOVIES, 10)
        self.assertEqual(pending["pendingMovieCount"], 1)
        self.assertEqual(pending["movies"][0]["movieKey"], app.movie_research_key(MOVIES[1]))

    def test_distressing_conviction_is_not_used_as_a_fun_clue(self):
        rows = [
            {
                "imdb": {"value": "tt1234567"},
                "film": {"value": "http://www.wikidata.org/entity/Q100"},
                "kind": {"value": "conviction"},
                "subject": {"value": "http://www.wikidata.org/entity/Q200"},
                "subjectLabel": {"value": "Example Performer"},
                "value": {"value": "http://www.wikidata.org/entity/Q300"},
                "valueLabel": {"value": "sexual assault"},
            }
        ]
        self.assertEqual(app.wikidata_rows_to_facts(rows), {})

    def test_source_is_kept_private_until_reveal(self):
        sourced = app.fact_card(
            "occupation",
            "Cast member Example Performer is also listed as an astronaut—not only a performer.",
            source_url="https://www.wikidata.org/wiki/Q200",
            source_label="Check the Wikidata biography",
        )
        self.mock_trivia.return_value = {"tt1234567": [sourced]}
        draw_id, wildcards = app.create_hidden_draw("Comedy", [MOVIES[0]], [MOVIES[0]], MOVIES)
        self.assertNotIn("wikidata.org", str(wildcards).casefold())
        _, fact, _, _ = app.reveal_hidden_wildcard(draw_id, wildcards[0]["id"])
        self.assertEqual(fact["sourceUrl"], "https://www.wikidata.org/wiki/Q200")

    def test_one_draw_does_not_repeat_the_same_clue(self):
        sourced = app.fact_card(
            "occupation",
            "Cast member Example Performer is also listed as an astronaut—not only a performer.",
            source_url="https://www.wikidata.org/wiki/Q200",
        )
        self.mock_trivia.return_value = {"tt1234567": [sourced]}
        second = {
            **MOVIES[0],
            "Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Name": "Another Secret Film",
        }
        _, wildcards = app.create_hidden_draw("Comedy", [MOVIES[0], second], [MOVIES[0], second], MOVIES)
        descriptions = [wildcard["description"] for wildcard in wildcards]
        self.assertEqual(len(descriptions), len(set(descriptions)))

    def test_hidden_wildcards_do_not_expose_movie_identity(self):
        second = {
            **MOVIES[0],
            "Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Name": "Another Secret Film",
            "People": [{"Name": "Another Actor", "Type": "Actor"}],
        }
        genre_movies = [MOVIES[0], second]
        draw_id, wildcards = app.create_hidden_draw("Comedy", genre_movies, genre_movies, genre_movies)
        public_text = str(wildcards).casefold()
        self.assertNotIn("the test feature", public_text)
        self.assertNotIn("another secret film", public_text)
        self.assertNotIn(MOVIES[0]["Id"], public_text)
        self.assertEqual(len(wildcards), 2)

        movie, fact, genre, count = app.reveal_hidden_wildcard(draw_id, wildcards[0]["id"])
        self.assertIn(movie["Id"], {MOVIES[0]["Id"], second["Id"]})
        self.assertEqual(fact["text"], wildcards[0]["description"])
        self.assertEqual((genre, count), ("Comedy", 2))

    def test_each_draw_uses_new_opaque_ids(self):
        second = {**MOVIES[0], "Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Name": "Another Secret Film"}
        movies = [MOVIES[0], second]
        draw_one, choices_one = app.create_hidden_draw("Comedy", movies, movies, movies)
        draw_two, choices_two = app.create_hidden_draw("Comedy", movies, movies, movies)
        self.assertNotEqual(draw_one, draw_two)
        self.assertTrue({choice["id"] for choice in choices_one}.isdisjoint({choice["id"] for choice in choices_two}))


if __name__ == "__main__":
    unittest.main()
