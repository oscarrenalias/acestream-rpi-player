import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from catalog.app import (
    Catalog,
    RefreshSettings,
    content_id,
    discover_events,
    handler,
    ignored_by,
    load_event_tls_verify,
    load_ignore_patterns,
    load_playlist_url_template,
    load_refresh_settings,
    parse_feed,
    playlist,
    playlist_title,
    process_events,
    resolve_active_events,
)


FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <item>
    <title>European Championship</title>
    <link>https://example.test/event/123</link>
    <description>Athletics. European Championship</description>
    <pubDate>Fri, 14 Aug 2026 12:30:00 +0300</pubDate>
  </item>
</channel></rss>
"""
STREAM_ID = "0123456789abcdef0123456789abcdef01234567"


class CatalogTests(unittest.TestCase):
    def test_parse_feed(self):
        self.assertEqual(
            parse_feed(FEED),
            [
                {
                    "title": "European Championship",
                    "link": "https://example.test/event/123",
                    "category": "Athletics",
                    "description": "Athletics. European Championship",
                    "starts_at": "2026-08-14T12:30:00+03:00",
                }
            ],
        )

    def test_content_id_formats(self):
        self.assertEqual(content_id(STREAM_ID.upper()), STREAM_ID)
        self.assertEqual(content_id(f"acestream://{STREAM_ID}"), STREAM_ID)
        self.assertEqual(content_id(f"http://engine/ace/getstream?id={STREAM_ID}"), STREAM_ID)
        with self.assertRaises(ValueError):
            content_id("not-a-stream")

    def test_database_filters_and_playlist(self):
        event = parse_feed(FEED)[0]
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            catalog.save(event, [("English", STREAM_ID), ("Finnish", STREAM_ID)])
            self.assertEqual(len(catalog.events("athletics", "2026-08-14")), 1)
            self.assertEqual(catalog.events("Tennis"), [])
            self.assertEqual(
                catalog.events()[0]["streams"],
                [
                    {"content_id": STREAM_ID, "metadata": "English"},
                    {"content_id": STREAM_ID, "metadata": "Finnish"},
                ],
            )
            rendered = playlist(
                catalog.events(),
                "{base_url}/ace/getstream?id={content_id}",
                "http://player:8080",
            )
        self.assertIn('group-title="Athletics",[Athletics] European Championship', rendered)
        self.assertIn(f"http://player:8080/ace/getstream?id={STREAM_ID}", rendered)
        self.assertIn("[English]", rendered)
        self.assertIn("[Finnish]", rendered)

    def test_playlist_assigns_distinct_fresh_playback_pids(self):
        event = parse_feed(FEED)[0]
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            catalog.save(event, [("English", STREAM_ID), ("Finnish", STREAM_ID)])
            first = playlist(
                catalog.events(),
                "{base_url}/ace/getstream?id={content_id}",
                "http://player:8080",
            )
            second = playlist(
                catalog.events(),
                "{base_url}/ace/getstream?id={content_id}",
                "http://player:8080",
            )

        def pids(rendered):
            urls = [line for line in rendered.splitlines() if line.startswith("http://")]
            return [parse_qs(urlparse(url).query)["pid"][0] for url in urls]

        first_pids = pids(first)
        second_pids = pids(second)
        self.assertEqual(len(first_pids), 2)
        self.assertEqual(len(set(first_pids)), 2)
        self.assertTrue(set(first_pids).isdisjoint(second_pids))

    def test_playlist_response_disables_caching(self):
        event = parse_feed(FEED)[0]
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            catalog.save(event, [("", STREAM_ID)])
            request_handler = handler(
                catalog,
                RefreshSettings(),
                "{base_url}/ace/getstream?id={content_id}",
            )
            response = {}
            request = type("Request", (), {})()
            request.path = "/playlist.m3u"
            request.headers = {"Host": "player:8080"}
            request._public_base_url = lambda: "http://player:8080"
            request._send = lambda status, body, content_type, headers=None: response.update(
                status=status,
                body=body.decode(),
                content_type=content_type,
                headers=headers or {},
            )
            request_handler.do_GET(request)
        self.assertEqual(response["headers"], {"Cache-Control": "no-store"})
        self.assertIn(f"id={STREAM_ID}", response["body"])
        self.assertIn("pid=", response["body"])

    def test_playlist_title_adds_category_and_description_detail(self):
        self.assertEqual(
            playlist_title(
                {
                    "title": "ATP/WTA, Cincinnati",
                    "category": "Tennis",
                    "description": "Tennis. ATP/WTA Tour",
                }
            ),
            "[Tennis] ATP/WTA, Cincinnati — ATP/WTA Tour",
        )

    def test_existing_database_is_migrated_with_description(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL,
                        event_url TEXT NOT NULL UNIQUE,
                        category TEXT NOT NULL,
                        starts_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_checked_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE streams (
                        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                        content_id TEXT NOT NULL,
                        PRIMARY KEY (event_id, content_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO events(id, title, event_url, category, starts_at, updated_at, last_checked_at)
                    VALUES (1, 'Legacy event', 'https://example.test/event/legacy', 'Other',
                            '2026-08-14T12:30:00+03:00', '2026-08-14T12:00:00+03:00', NULL)
                    """
                )
                connection.execute(
                    "INSERT INTO streams(event_id, content_id) VALUES (1, ?)", (STREAM_ID,)
                )
            catalog = Catalog(database)
            with closing(sqlite3.connect(database)) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(events)")]
                stream_columns = [row[1] for row in connection.execute("PRAGMA table_info(streams)")]
            stored = catalog.events()
        self.assertIn("description", columns)
        self.assertIn("metadata", stream_columns)
        self.assertEqual(
            stored[0]["streams"], [{"content_id": STREAM_ID, "metadata": ""}]
        )

    def test_processing_calls_resolver_and_persists_empty_results(self):
        event = parse_feed(FEED)[0]
        seen = []

        def resolver(candidate):
            seen.append(candidate["link"])
            return []

        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            self.assertEqual(process_events([event], catalog, resolver), 1)
            stored = catalog.events()
        self.assertEqual(seen, [event["link"]])
        self.assertEqual(stored[0]["content_ids"], [])
        self.assertEqual(playlist(stored), "#EXTM3U\n")

    def test_max_events_limits_processing(self):
        first_event = parse_feed(FEED)[0]
        second_event = {
            **first_event,
            "title": "Second event",
            "link": "https://example.test/event/456",
        }
        resolved = []

        def resolver(event):
            resolved.append(event["title"])
            return [STREAM_ID]

        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            self.assertEqual(
                process_events([first_event, second_event], catalog, resolver, max_events=1),
                1,
            )
            stored = catalog.events()
        self.assertEqual(resolved, ["European Championship"])
        self.assertEqual([event["title"] for event in stored], ["European Championship"])

    def test_ignore_patterns_skip_events_before_resolution(self):
        baseball_event = {
            **parse_feed(FEED)[0],
            "title": "Kia Tigers - Doosan Bears",
            "category": "Baseball",
            "description": "Baseball. South Korea. KBO League",
        }
        resolved = []

        def resolver(event):
            resolved.append(event["title"])
            return [STREAM_ID]

        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            self.assertEqual(
                process_events([baseball_event], catalog, resolver, ignore_patterns=["baseball"]),
                0,
            )
            self.assertEqual(catalog.events(), [])
        self.assertEqual(resolved, [])
        self.assertEqual(ignored_by(baseball_event, ["BASEBALL"]), "BASEBALL")

    def test_load_ignore_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("ignore:\n  - baseball\n  - '  tennis  '\n", encoding="utf-8")
            self.assertEqual(load_ignore_patterns(config), ["baseball", "tennis"])

    def test_event_tls_verification_defaults_to_enabled_and_is_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("ignore: []\n", encoding="utf-8")
            self.assertTrue(load_event_tls_verify(config))
            config.write_text("event_requests:\n  verify_tls: false\n", encoding="utf-8")
            self.assertFalse(load_event_tls_verify(config))

    def test_refresh_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "refresh:\n  discovery_interval_seconds: 120\n  stream_window_hours: 6\n",
                encoding="utf-8",
            )
            settings = load_refresh_settings(config)
        self.assertEqual(settings.discovery_interval_seconds, 120)
        self.assertEqual(settings.stream_window_hours, 6)
        self.assertEqual(settings.stream_interval_seconds, 300)

    def test_playlist_url_template(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "playlist:\n  stream_url_template: 'http://engine:6878/ace/getstream?id={content_id}'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_playlist_url_template(config),
                "http://engine:6878/ace/getstream?id={content_id}",
            )

    def test_discovery_filters_before_stream_resolution(self):
        event = parse_feed(FEED)[0]
        feed = FEED.replace(
            b"</channel>",
            (
                b"<item><title>Baseball fixture</title>"
                b"<link>https://example.test/event/baseball</link>"
                b"<description>Baseball. Example League</description>"
                b"<pubDate>Fri, 14 Aug 2026 12:30:00 +0300</pubDate></item></channel>"
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            with patch("catalog.app.fetch_feed", return_value=feed):
                self.assertEqual(discover_events("https://example.test/feed", catalog, ["baseball"]), 1)
            self.assertEqual([stored["title"] for stored in catalog.events()], [event["title"]])

    def test_stream_refresh_only_resolves_events_in_window(self):
        event = parse_feed(FEED)[0]
        current = event["starts_at"]
        from datetime import datetime

        now = datetime.fromisoformat(current)
        future = {**event, "title": "Later event", "link": "https://example.test/event/later", "starts_at": "2026-08-16T12:30:00+03:00"}
        resolved = []

        def resolver(candidate):
            resolved.append(candidate["title"])
            return [STREAM_ID]

        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "catalog.db")
            catalog.upsert_event(event)
            catalog.upsert_event(future)
            checked = resolve_active_events(
                catalog,
                resolver,
                RefreshSettings(stream_lookback_hours=1, stream_window_hours=1),
                now=now,
            )
            stored = catalog.events()
        self.assertEqual(checked, 1)
        self.assertEqual(resolved, [event["title"]])
        self.assertEqual(stored[0]["content_ids"], [STREAM_ID])
        self.assertEqual(stored[1]["content_ids"], [])


if __name__ == "__main__":
    unittest.main()
