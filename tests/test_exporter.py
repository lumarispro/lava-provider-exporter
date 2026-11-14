import io
import unittest
import urllib.error
from unittest.mock import patch

import lava_provider_exporter as exporter


class PollRetryTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://example.invalid/lava"

    def test_poll_recovers_after_one_timeout(self):
        collector = exporter.Collector(self.url, "provider", 15)
        responses = [
            urllib.error.URLError(TimeoutError("timed out")),
            io.BytesIO(b'{"stakeEntries": []}'),
            io.BytesIO(b'{"delegations": []}'),
        ]
        with (
            patch.object(exporter.urllib.request, "urlopen", side_effect=responses) as fetch,
            patch.object(exporter.time, "sleep") as sleep,
        ):
            collector.poll()

        self.assertIn("lava_provider_up 1\n", collector.render())
        self.assertEqual(fetch.call_count, 3)
        urls = [call.args[0].full_url for call in fetch.call_args_list]
        self.assertEqual(urls[0], urls[1])
        self.assertNotEqual(urls[1], urls[2])
        sleep.assert_called_once()

    def test_persistent_timeout_is_bounded_and_preserves_last_success(self):
        collector = exporter.Collector(self.url, "provider", 15)
        collector.up = 1
        collector.last_success = 42.0
        with (
            patch.object(exporter.urllib.request, "urlopen", side_effect=TimeoutError) as fetch,
            patch.object(exporter.time, "sleep") as sleep,
            self.assertLogs(exporter.log, level="WARNING"),
        ):
            collector.poll()

        self.assertIn("lava_provider_up 0\n", collector.render())
        self.assertEqual(collector.last_success, 42.0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_http_service_unavailable_retries(self):
        error = urllib.error.HTTPError(
            self.url, 503, "Service unavailable", None, io.BytesIO(b"")
        )
        with (
            patch.object(
                exporter.urllib.request, "urlopen", side_effect=[error, io.BytesIO(b"{}")]
            ) as fetch,
            patch.object(exporter.time, "sleep") as sleep,
        ):
            self.assertEqual(exporter.fetch_json(self.url, 15), {})

        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once()

    def test_permanent_http_error_is_not_retried(self):
        error = urllib.error.HTTPError(self.url, 404, "Not found", None, io.BytesIO(b""))
        with (
            patch.object(exporter.urllib.request, "urlopen", side_effect=error) as fetch,
            patch.object(exporter.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            exporter.fetch_json(self.url, 15)

        fetch.assert_called_once()
        sleep.assert_not_called()
