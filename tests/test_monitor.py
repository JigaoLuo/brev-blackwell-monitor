import copy
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import monitor as m


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def instance(name="NVIDIA B200", available=True, **changes):
    row = {
        "type": "b200-1x", "provider": "example",
        "supportedGpus": [{"name": name, "count": 1}],
        "basePrice": {"currency": "USD", "amount": "4.00"},
        "isAvailable": available, "availableLocations": ["region-a"],
    }
    row.update(changes)
    return row


class CatalogTests(unittest.TestCase):
    def test_blackwell_aliases(self):
        for name in ("Blackwell", "NVIDIA B100", "B200", "B300", "GB200 NVL72",
                     "GB300", "GB10", "NVIDIA Blackwell B200", "gb200"):
            with self.subTest(name=name):
                self.assertEqual(len(m.find_blackwell([instance(name)])), 1)

    def test_all_rtx_models_are_excluded_even_with_blackwell_in_name(self):
        names = [
            "RTXPro6000", "RTX PRO Server 6000", "RTX5090", "RTX 5070 Ti",
            "RTX 5090D", "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "NVIDIA Blackwell rTx 5090", "Blackwell RTXPro6000",
        ]
        names.extend(f"RTX PRO {model}" for model in (2000, 4000, 4500, 5000, 6000))
        names.extend(f"RTX {model} Blackwell" for model in (5050, 5060, 5070, 5080, 5090))
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(m.find_blackwell([instance(name)]), [])

    def test_non_blackwell_is_not_matched(self):
        for name in ("H100", "H200", "A6000", "RTX6000Ada", "RTX 6000",
                     "RTX4090", "B2000", "RTXPro60000"):
            with self.subTest(name=name):
                self.assertEqual(m.find_blackwell([instance(name)]), [])

    def test_cpu_rows_and_empty_catalog(self):
        self.assertEqual(m.find_blackwell([{"type": "cpu"}, instance("H100")]), [])
        self.assertEqual(m.find_blackwell([]), [])

    def test_missing_gpu_metadata_is_not_a_successful_empty_check(self):
        with self.assertRaises(m.MonitorError):
            m.find_blackwell([{"type": "b200", "renamedGpuField": [{"name": "B200"}]}])

    def test_regions_are_merged_and_order_does_not_change_availability(self):
        rows = [
            instance(available=False),
            instance(available=True, availableLocations=["region-b"]),
        ]
        for order in (rows, list(reversed(rows))):
            match, = m.find_blackwell(order)
            self.assertEqual(match.availability, "available")
            self.assertEqual(match.locations, {"region-a", "region-b"})
            self.assertEqual(len(match.prices), 1)

    def test_omitted_availability_is_unknown(self):
        row = instance()
        del row["isAvailable"]
        self.assertEqual(m.find_blackwell([row])[0].availability, "unknown")

    def test_invalid_catalog_entries_fail_loudly(self):
        invalid = [
            None, {}, instance(supportedGpus="B200"),
            instance(supportedGpus=[{"name": "B200", "count": True}]),
            instance(supportedGpus=[{"name": "B200", "count": 0}]),
            instance(isAvailable="false"), instance(availableLocations="region-a"),
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(m.MonitorError):
                m.find_blackwell([row])

    @patch.object(m, "request_json")
    def test_authenticated_query_uses_org_and_read_only_rpc(self, request):
        request.side_effect = [[{"id": "org-1"}], {"items": [instance()]}]
        self.assertEqual(len(m.fetch_catalog("bak-test")), 1)
        call = request.call_args
        self.assertTrue(call.args[1].endswith("/ListOrganizationAvailableInstanceTypes"))
        self.assertEqual(call.kwargs["body"]["organizationId"], "org-1")
        self.assertTrue(call.kwargs["body"]["options"]["includeUnavailable"])
        self.assertFalse(call.kwargs["body"]["options"]["skipAccessFilter"])
        self.assertEqual(call.kwargs["token"], "bak-test")

    @patch.object(m, "request_json", return_value={})
    def test_empty_protobuf_response_is_valid(self, request):
        self.assertEqual(m.fetch_catalog("", public=True), [])
        self.assertEqual(request.call_args.kwargs["token"], "")

    @patch.object(m, "request_json")
    def test_unexpected_schema_or_pagination_is_not_an_empty_catalog(self, request):
        for response in ([], {"error": "bad"}, {"items": {}},
                         {"items": [], "nextPageToken": "next"}):
            request.return_value = response
            with self.subTest(response=response), self.assertRaises(m.MonitorError):
                m.fetch_catalog("", public=True)

    @patch.object(m, "request_json")
    def test_bad_auth_never_falls_back_to_public_data(self, request):
        request.side_effect = m.MonitorError("Brev: HTTP 401.")
        with self.assertRaises(m.MonitorError):
            m.fetch_catalog("bak-test")
        self.assertEqual(request.call_count, 1)

    @patch.object(m, "request_json")
    def test_missing_or_wrong_key_sends_no_request(self, request):
        for key in ("", "not-a-brev-key"):
            with self.assertRaises(m.MonitorError):
                m.fetch_catalog(key)
        request.assert_not_called()


class StateTests(unittest.TestCase):
    def evaluate(self, state, matches=None, **kwargs):
        return m.evaluate(state, now=NOW, expires_on="2026-12-31",
                          matches=matches, **kwargs)

    def test_first_match_repeat_disappearance_and_reappearance(self):
        matches = m.find_blackwell([instance()])
        state, alerts = self.evaluate(m.initial_state(), matches)
        self.assertEqual(len(alerts), 1)
        state, alerts = self.evaluate(state, matches)
        self.assertEqual(alerts, [])
        state, alerts = self.evaluate(state, [])
        self.assertEqual(alerts, [])
        state, alerts = self.evaluate(state, matches)
        self.assertEqual(len(alerts), 1)
        self.assertIn("detected", alerts[0])

    def test_becoming_available_notifies(self):
        state, _ = self.evaluate(m.initial_state(), m.find_blackwell([instance(available=False)]))
        _, alerts = self.evaluate(state, m.find_blackwell([instance(available=True)]))
        self.assertEqual(len(alerts), 1)
        self.assertIn("availability changed", alerts[0])

    def test_failures_preserve_matches_and_are_deduplicated(self):
        matches = m.find_blackwell([instance()])
        previous, _ = self.evaluate(m.initial_state(), matches)
        original = copy.deepcopy(previous)
        state, alerts = self.evaluate(previous, error="Brev: HTTP 401.")
        self.assertEqual(previous, original)
        self.assertEqual(state["matches"], previous["matches"])
        self.assertEqual(state["last_success"], previous["last_success"])
        self.assertEqual(len(alerts), 1)
        state, alerts = self.evaluate(state, error="Brev: HTTP 401.")
        self.assertEqual(alerts, [])
        state, alerts = self.evaluate(state, matches)
        self.assertEqual(len(alerts), 1)
        self.assertIn("recovered", alerts[0])

    def test_state_round_trip_and_corruption(self):
        state, _ = self.evaluate(m.initial_state(), m.find_blackwell([instance()]))
        body = m.issue_body(state, "summary", "https://github.com/example/run")
        self.assertEqual(m.decode_state(body), state)
        for body in ("", m.STATE_START + "bad" + m.STATE_END,
                     m.STATE_START + '{"version": 2}' + m.STATE_END):
            with self.subTest(body=body), self.assertRaises(m.MonitorError):
                m.decode_state(body)

    def test_expiry_threshold_missing_invalid_and_expired(self):
        for value, expected in (
            ("2026-10-06", ""), ("2026-10-05", "2026-10-05:due"),
            ("2026-09-28", "2026-09-28:due"),
            ("2026-09-27", "2026-09-27:expired"), ("", "missing"),
            ("2026-99-99", "invalid"), ("20260928", "invalid"),
        ):
            with self.subTest(value=value):
                self.assertEqual(m.expiry_notice(value, NOW.date())[0], expected)

    def test_expiry_notice_is_once_per_date_and_stage(self):
        state = m.initial_state()
        for day, count in ((28, 1), (29, 0), (30, 0)):
            state, alerts = m.evaluate(
                state, matches=[], expires_on="2026-09-30",
                now=NOW.replace(day=day),
            )
            self.assertEqual(len(alerts), count)
        _, alerts = m.evaluate(state, matches=[], expires_on="2026-09-30",
                               now=NOW.replace(month=10, day=1))
        self.assertEqual(len(alerts), 1)

    def test_all_matches_and_prices_are_in_notifications(self):
        matches = m.find_blackwell([
            instance(type=f"b200-{i}", basePrice={"currency": "USD", "amount": str(i)})
            for i in range(51)
        ])
        _, alerts = self.evaluate(m.initial_state(), matches)
        comments = m.notification_comments(alerts, "example", "https://github.com/example/run")
        content = "\n\n".join(comments)
        for i in range(51):
            self.assertIn(f"| b200-{i} |", content)
            self.assertIn(f"USD {i}/h", content)
        self.assertTrue(all(len(comment) <= 60000 for comment in comments))

    def test_large_notifications_are_split_without_dropping_alerts(self):
        alerts = ["first " + "x" * 31000, "second " + "y" * 31000]
        comments = m.notification_comments(alerts, "example", "https://github.com/example/run")
        self.assertEqual(len(comments), 2)
        self.assertIn(alerts[0], comments[0])
        self.assertIn(alerts[1], comments[1])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.github = Mock()
        self.github.find_issue.return_value = None
        self.github.create_issue.return_value = {"number": 1}
        self.kwargs = {
            "api_key": "bak-test", "expires_on": "2026-12-31",
            "notify_user": "example", "run_url": "https://github.com/example/run",
            "now": NOW,
        }

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    @patch("builtins.print")
    def test_initial_alert_and_repeated_check(self, _print, _fetch):
        m.run_monitor(self.github, **self.kwargs)
        body = self.github.update_issue.call_args.args[1]
        comment = self.github.comment.call_args.args[1]
        self.assertIn("@example", comment)
        self.assertIn("Blackwell listings", comment)
        self.assertNotIn("bak-test", body + comment)
        self.github.find_issue.return_value = {"number": 1, "body": body}
        self.github.comment.reset_mock()
        m.run_monitor(self.github, **self.kwargs)
        self.github.comment.assert_not_called()

    @patch.object(m, "fetch_catalog", side_effect=m.MonitorError("Missing BREV_API_KEY"))
    @patch("builtins.print")
    def test_failure_alert_is_persisted_but_run_fails(self, _print, _fetch):
        with self.assertRaises(m.MonitorError):
            m.run_monitor(self.github, **self.kwargs)
        self.assertIn("Monitoring failed", self.github.comment.call_args.args[1])
        state = m.decode_state(self.github.update_issue.call_args.args[1])
        self.assertEqual(state["error"], "Missing BREV_API_KEY")

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    def test_failed_notification_does_not_acknowledge_state(self, _fetch):
        self.github.comment.side_effect = m.MonitorError("GitHub: HTTP 403.")
        with self.assertRaises(m.MonitorError):
            m.run_monitor(self.github, **self.kwargs)
        self.github.update_issue.assert_not_called()
        state = m.decode_state(self.github.create_issue.call_args.args[0])
        self.assertEqual(state["matches"], {})

    def test_issue_pagination_ignores_pull_requests(self):
        github = m.GitHub("owner/repo", "test")
        github.call = Mock(side_effect=[
            [{"title": "unrelated"}] * 99 + [
                {"title": m.ISSUE_TITLE, "pull_request": {}}
            ],
            [{"title": m.ISSUE_TITLE, "number": 2}],
        ])
        self.assertEqual(github.find_issue()["number"], 2)
        self.assertIn("page=2", github.call.call_args.args[1])

    def test_table_does_not_create_untrusted_mentions(self):
        matches = m.find_blackwell([instance("B200 @someone | <tag>\ntext")])
        table = m.match_table(matches)
        self.assertNotIn("@someone", table)
        self.assertIn("\\|", table)
        self.assertIn("&lt;tag&gt;", table)


class HttpTests(unittest.TestCase):
    @patch.object(m.time, "sleep")
    @patch.object(m, "build_opener")
    def test_read_retries_transient_error(self, build, sleep):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"items":[]}'
        build.return_value.open.side_effect = [URLError("private detail"), response]
        result = m.request_json("GET", "https://example.com", label="Brev", retry=True)
        self.assertEqual(result, {"items": []})
        self.assertEqual(build.return_value.open.call_count, 2)

    @patch.object(m, "build_opener")
    def test_auth_error_does_not_leak_response_body_or_key(self, build):
        build.return_value.open.side_effect = HTTPError(
            "https://example.com", 401, "secret", {}, io.BytesIO(b"bak-secret"),
        )
        with self.assertRaisesRegex(m.MonitorError, "HTTP 401") as caught:
            m.request_json("GET", "https://example.com", label="Brev",
                           token="bak-secret", retry=True)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(build.return_value.open.call_count, 1)

    @patch.object(m, "build_opener")
    def test_notification_post_is_not_retried(self, build):
        build.return_value.open.side_effect = URLError("timeout")
        with self.assertRaises(m.MonitorError):
            m.request_json("POST", "https://example.com", label="GitHub", body={})
        self.assertEqual(build.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
