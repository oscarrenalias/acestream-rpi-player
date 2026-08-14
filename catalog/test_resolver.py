import unittest
from pathlib import Path

from catalog.resolver import parse_event_page


FIXTURES = Path(__file__).with_name("fixtures")
STREAM_IDS = [
    "5f966c123759de46dff29c379266b7a403452033",
    "1b0bc4d4dcd609d3c092712e721392b199f50a62",
]


class ResolverTests(unittest.TestCase):
    def fixture(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_page_without_streams_returns_empty_list(self):
        streams = parse_event_page(
            self.fixture("event_without_acestream.html"),
            "https://example.test/event/without-stream",
        )
        self.assertEqual(streams, [])

    def test_extracts_stream_from_local_fixture(self):
        streams = parse_event_page(
            self.fixture("event_with_acestream.html"),
            "https://example.test/event/with-stream",
        )
        self.assertEqual(
            streams,
            [f"acestream://{stream_id}" for stream_id in STREAM_IDS],
        )


if __name__ == "__main__":
    unittest.main()
