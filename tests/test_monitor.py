import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
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
        for name in ("B200", "B300", "NVIDIA B200", "HGX B300", "b200",
                     "NVIDIA Blackwell B200", "B300 SXM"):
            with self.subTest(name=name):
                self.assertEqual(len(m.find_blackwell([instance(name)])), 1)

    def test_only_single_gpu_configurations_are_matched(self):
        for count in (2, 4, 8):
            with self.subTest(count=count):
                row = instance("B300", type=f"excesssupply_B300x{count}",
                               supportedGpus=[{"name": "B300", "count": count}])
                self.assertEqual(m.find_blackwell([row]), [])
        mixed = [instance("B300", supportedGpus=[{"name": "B300", "count": 8}]),
                 instance("B300")]
        match, = m.find_blackwell(mixed)
        self.assertEqual(match.count, 1)

    def test_other_blackwell_models_are_not_matched(self):
        for name in ("Blackwell", "NVIDIA Blackwell", "NVIDIA B100", "GB10",
                     "GB200", "GB200 NVL72", "gb200", "GB300", "GB300 NVL72"):
            with self.subTest(name=name):
                self.assertEqual(m.find_blackwell([instance(name)]), [])

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
                     "RTX4090", "B2000", "B3000", "RTXPro60000"):
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
                     m.STATE_START + '{"version": 4}' + m.STATE_END):
            with self.subTest(body=body), self.assertRaises(m.MonitorError):
                m.decode_state(body)

    def test_version_1_state_is_migrated(self):
        old = {"version": 1, "matches": {}, "error": "", "expiry_notice": "",
               "last_success": ""}
        state = m.decode_state(m.STATE_START + json.dumps(old) + m.STATE_END)
        self.assertEqual(state, m.initial_state())

    def test_version_2_state_keeps_used_label(self):
        old = {"version": 2, "matches": {}, "error": "", "expiry_notice": "",
               "last_success": "", "autostart": "1", "autostart_error": ""}
        state = m.decode_state(m.STATE_START + json.dumps(old) + m.STATE_END)
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["autostart"], "1")
        self.assertEqual(state["autostart_failures"], {})
        for bad in ({"1": 0}, {"1": "2"}, ["1"]):
            broken = dict(state, autostart_failures=bad)
            with self.subTest(bad=bad), self.assertRaises(m.MonitorError):
                m.decode_state(m.STATE_START + json.dumps(broken) + m.STATE_END)

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
        # CI supplies a real summary file; simulated checks must not publish to it.
        summary_env = patch.dict(m.os.environ, {"GITHUB_STEP_SUMMARY": ""})
        summary_env.start()
        self.addCleanup(summary_env.stop)
        self.github = Mock()
        self.github.find_issue.return_value = None
        self.github.create_issue.return_value = {"number": 1}
        self.kwargs = {
            "api_key": "bak-test", "expires_on": "2026-12-31",
            "notify_user": "example", "run_url": "https://github.com/example/run",
            "now": NOW,
        }

    def test_mock_checks_disable_actions_summary(self):
        self.assertEqual(m.os.environ.get("GITHUB_STEP_SUMMARY"), "")

    @patch.object(m, "fetch_catalog", return_value=[instance("H100")])
    @patch("builtins.print")
    def test_live_summary_identifies_source_and_contains_only_current_result(self, _print, _fetch):
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            with patch.dict(m.os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                m.run_monitor(self.github, **self.kwargs)
            summary = summary_path.read_text(encoding="utf-8")
        self.assertIn("## Live Brev organization check", summary)
        self.assertIn("API key's organization", summary)
        self.assertNotIn("RTX", summary)
        self.assertIn(NOW.isoformat(timespec="seconds"), summary)
        self.assertEqual(summary.count("**Result: NO MATCHES**"), 1)
        self.assertIn("Live query succeeded. Catalog entries scanned: 1.", summary)
        self.assertIn("No monitored GPU configurations were found", summary)
        self.assertNotIn("Missing BREV_API_KEY", summary)
        self.assertNotIn("region-a", summary)
        self.assertNotIn("USD 4.00/h", summary)

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    @patch("builtins.print")
    def test_initial_alert_and_repeated_check(self, _print, _fetch):
        m.run_monitor(self.github, **self.kwargs)
        body = self.github.update_issue.call_args.args[1]
        comment = self.github.comment.call_args.args[1]
        self.assertIn("**Result: MATCHES FOUND**", body)
        self.assertIn("Matching GPU configurations: 1.", body)
        self.assertIn("USD 4.00/h", body)
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
        body = self.github.update_issue.call_args.args[1]
        self.assertIn("**Result: CHECK FAILED**", body)
        self.assertNotIn("**Result: NO MATCHES**", body)
        state = m.decode_state(body)
        self.assertEqual(state["error"], "Missing BREV_API_KEY")

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    def test_failed_notification_does_not_acknowledge_state(self, _fetch):
        self.github.comment.side_effect = m.MonitorError("GitHub: HTTP 403.")
        with self.assertRaises(m.MonitorError):
            m.run_monitor(self.github, **self.kwargs)
        self.github.update_issue.assert_not_called()
        state = m.decode_state(self.github.create_issue.call_args.args[0])
        self.assertEqual(state["matches"], {})

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    @patch("builtins.print")
    def test_autostart_disabled_without_label(self, _print, _fetch):
        with patch.object(m, "request_json") as request:
            m.run_monitor(self.github, **self.kwargs)
        request.assert_not_called()
        self.assertIn("Auto-start: disabled.", self.github.update_issue.call_args.args[1])


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


class AutostartTests(unittest.TestCase):
    def setUp(self):
        summary_env = patch.dict(m.os.environ, {"GITHUB_STEP_SUMMARY": ""})
        summary_env.start()
        self.addCleanup(summary_env.stop)
        self.github = Mock()
        self.github.find_issue.return_value = None
        self.github.create_issue.return_value = {"number": 1}
        self.kwargs = {
            "api_key": "bak-test", "expires_on": "2026-12-31",
            "notify_user": "example", "run_url": "https://github.com/example/run",
            "now": NOW, "autostart_value": "1",
        }
        print_patch = patch("builtins.print")
        print_patch.start()
        self.addCleanup(print_patch.stop)

    def brev(self, workspaces=None, created=None, create_error=None):
        """Fake Brev REST calls; return the mock to inspect requests."""
        def fake(method, url, **kwargs):
            if url == m.BREV_ORGS_URL:
                return [{"id": "org1"}]
            if method == "GET":
                return workspaces
            if create_error:
                raise create_error
            return created or {"id": "ws1", "name": kwargs["body"]["name"]}
        return patch.object(m, "request_json", side_effect=fake)

    def creates(self, request):
        return [c for c in request.call_args_list if c.args[0] == "POST"]

    def test_label_parsing(self):
        for value in ("", " ", "0", "false", "OFF", "no"):
            self.assertEqual(m.autostart_label(value), "")
        self.assertEqual(m.autostart_label(" 1 "), "1")
        self.assertEqual(m.autostart_label("Run-2"), "run-2")
        for value in ("-1", "a b", "x" * 21, "1;rm"):
            with self.subTest(value=value), self.assertRaises(m.MonitorError):
                m.autostart_label(value)

    def test_candidates_are_available_single_gpu_cheapest_first(self):
        items = [
            instance("B300", type="b300-1x", cloudCredId="c3",
                     basePrice={"currency": "USD", "amount": "9.00"}),
            instance("B200", type="b200-1x", cloudCredId="c2"),
            instance("B200", type="b200-off", available=False, cloudCredId="c4"),
            instance("B300", type="b300-8x", cloudCredId="c5",
                     supportedGpus=[{"name": "B300", "count": 8}]),
            instance("GB200", type="gb200", cloudCredId="c6"),
            {"type": "cpu"},
        ]
        candidates = m.autostart_candidates(items)
        self.assertEqual([c["instance_type"] for c in candidates], ["b200-1x", "b300-1x"])
        self.assertEqual(candidates[0]["cloud_cred_id"], "c2")

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_starts_one_instance_then_never_again_for_same_label(self, _fetch):
        with self.brev(workspaces=[]) as request:
            m.run_monitor(self.github, **self.kwargs)
        create, = self.creates(request)
        body = create.kwargs["body"]
        self.assertEqual(create.args[1], m.BREV_ORGS_URL + "/org1/workspaces")
        self.assertFalse(create.kwargs.get("retry"))
        self.assertEqual(body["name"], "blackwell-auto-1")
        self.assertEqual(body["instanceType"], "b200-1x")
        self.assertEqual(body["cloudCredId"], "cred1")
        comment = self.github.comment.call_args.args[1]
        self.assertIn("Instance started", comment)
        self.assertIn("USD 4.00/h", comment)
        self.assertIn("Billing continues", comment)
        issue = self.github.update_issue.call_args.args[1]
        self.assertEqual(m.decode_state(issue)["autostart"], "1")
        self.assertFalse(m.decode_state(issue)["autostart_ran"])
        for status in ("DEPLOYING", "RUNNING", "STOPPED", "FAILURE", None):
            with self.subTest(status=status):
                self.github.find_issue.return_value = {
                    "number": 1, "body": self.github.update_issue.call_args.args[1],
                }
                # None: the owner deleted the instance after it had run.
                workspaces = [{"name": "blackwell-auto-1", "status": status}] if status else []
                with self.brev(workspaces=workspaces) as request:
                    m.run_monitor(self.github, **self.kwargs)
                self.assertEqual(self.creates(request), [])
        state = m.decode_state(self.github.update_issue.call_args.args[1])
        self.assertTrue(state["autostart_ran"])
        self.assertEqual(state["autostart_failures"], {})

    def run_after_start(self, workspaces):
        """Run once with label `1` already used by `blackwell-auto-1`."""
        state = m.initial_state()
        state["autostart"] = "1"
        self.github.find_issue.return_value = {
            "number": 1, "body": m.issue_body(state, "summary", "https://x/run"),
        }
        with self.brev(workspaces=workspaces) as request:
            m.run_monitor(self.github, **self.kwargs)
        return request, m.decode_state(self.github.update_issue.call_args.args[1])

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_failed_instance_rearms_and_starts_a_replacement(self, _fetch):
        failed = [{"name": "blackwell-auto-1", "status": "FAILURE"}]
        request, state = self.run_after_start(failed)
        create, = self.creates(request)
        self.assertEqual(create.kwargs["body"]["name"], "blackwell-auto-1-2")
        self.assertEqual(state["autostart"], "1")
        self.assertEqual(state["autostart_failures"], {"1": 1})
        comment = self.github.comment.call_args.args[1]
        self.assertIn("Started instance failed", comment)
        self.assertIn("Instance started", comment)
        # The replacement is now the one that is checked; the old failure is ignored.
        self.github.find_issue.return_value = {
            "number": 1, "body": self.github.update_issue.call_args.args[1],
        }
        with self.brev(workspaces=failed + [{"name": "blackwell-auto-1-2",
                                             "status": "RUNNING"}]) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(self.creates(request), [])

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_instance_deleted_before_running_rearms(self, _fetch):
        for workspaces in ([], [{"name": "blackwell-auto-1", "status": "DELETING"}]):
            with self.subTest(workspaces=workspaces):
                request, state = self.run_after_start(workspaces)
                create, = self.creates(request)
                self.assertEqual(create.kwargs["body"]["name"], "blackwell-auto-1-2")
                self.assertFalse(state["autostart_ran"])

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_deploying_instance_is_waited_for(self, _fetch):
        request, state = self.run_after_start([{"name": "blackwell-auto-1",
                                                "status": "DEPLOYING"}])
        self.assertEqual(self.creates(request), [])
        self.assertEqual(state["autostart"], "1")
        self.assertNotIn("Auto-start", self.github.comment.call_args.args[1])
        self.assertNotIn("Started instance", self.github.comment.call_args.args[1])

    @patch.object(m, "fetch_catalog", return_value=[instance(available=False, cloudCredId="c")])
    def test_failed_instance_rearms_without_available_listing(self, _fetch):
        request, state = self.run_after_start([{"name": "blackwell-auto-1", "status": "FAILURE"}])
        self.assertEqual(self.creates(request), [])
        self.assertEqual(state["autostart"], "")
        self.assertEqual(state["autostart_failures"], {"1": 1})
        self.assertIn("re-armed", self.github.comment.call_args.args[1])

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_instance_list_failure_after_start_does_not_create(self, _fetch):
        def fake(method, url, **kwargs):
            if url == m.BREV_ORGS_URL:
                return [{"id": "org1"}]
            raise m.MonitorError("Brev instance list: HTTP 500.")
        state = m.initial_state()
        state["autostart"] = "1"
        self.github.find_issue.return_value = {
            "number": 1, "body": m.issue_body(state, "summary", "https://x/run"),
        }
        with patch.object(m, "request_json", side_effect=fake) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(self.creates(request), [])
        self.assertEqual(m.decode_state(self.github.update_issue.call_args.args[1])["autostart"], "1")

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_existing_instance_with_same_name_prevents_duplicate(self, _fetch):
        with self.brev(workspaces=[{"name": "blackwell-auto-1", "status": "RUNNING"}]) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(self.creates(request), [])
        self.assertIn("Auto-start skipped", self.github.comment.call_args.args[1])
        state = m.decode_state(self.github.update_issue.call_args.args[1])
        self.assertEqual(state["autostart"], "1")

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="cred1")])
    def test_failed_creation_alerts_once_and_stays_armed(self, _fetch):
        error = m.MonitorError("Brev instance creation: connection failed or timed out.")
        with self.brev(workspaces=None, create_error=error):
            m.run_monitor(self.github, **self.kwargs)
        self.assertIn("Auto-start failed", self.github.comment.call_args.args[1])
        issue = self.github.update_issue.call_args.args[1]
        state = m.decode_state(issue)
        self.assertEqual(state["autostart"], "")
        self.github.find_issue.return_value = {"number": 1, "body": issue}
        self.github.comment.reset_mock()
        with self.brev(workspaces=None, create_error=error) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(len(self.creates(request)), 1)
        self.github.comment.assert_not_called()

    @patch.object(m, "fetch_catalog", return_value=[instance(available=False, cloudCredId="c")])
    def test_unavailable_listing_is_not_started(self, _fetch):
        with self.brev(workspaces=[]) as request:
            m.run_monitor(self.github, **self.kwargs)
        request.assert_not_called()

    @patch.object(m, "fetch_catalog", side_effect=m.MonitorError("Brev GPU catalog: HTTP 500."))
    def test_catalog_failure_never_starts(self, _fetch):
        with self.brev(workspaces=[]) as request, self.assertRaises(m.MonitorError):
            m.run_monitor(self.github, **self.kwargs)
        request.assert_not_called()

    @patch.object(m, "fetch_catalog", return_value=[instance()])
    def test_missing_cloud_cred_is_an_alert_not_a_create(self, _fetch):
        with self.brev(workspaces=[]) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(self.creates(request), [])
        self.assertIn("cloudCredId", self.github.comment.call_args.args[1])

    @patch.object(m, "fetch_catalog", return_value=[instance(cloudCredId="c")])
    def test_failed_notification_after_start_is_safe_to_rerun(self, _fetch):
        self.github.comment.side_effect = m.MonitorError("GitHub: HTTP 403.")
        with self.brev(workspaces=[]), self.assertRaises(m.MonitorError):
            m.run_monitor(self.github, **self.kwargs)
        self.github.update_issue.assert_not_called()
        # The rerun finds the instance by name instead of creating a second one.
        self.github.comment.side_effect = None
        with self.brev(workspaces=[{"name": "blackwell-auto-1"}]) as request:
            m.run_monitor(self.github, **self.kwargs)
        self.assertEqual(self.creates(request), [])


class HttpTests(unittest.TestCase):
    @patch.object(m.time, "sleep")
    @patch.object(m, "build_opener")
    def test_read_retries_transient_error(self, build, sleep):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"items":[]}'
        build.return_value.open.side_effect = [
            URLError("private detail"), TimeoutError(), response,
        ]
        result = m.request_json("GET", "https://example.com", label="Brev", retry=True)
        self.assertEqual(result, {"items": []})
        self.assertEqual(build.return_value.open.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])

    @patch.object(m.time, "sleep")
    @patch.object(m, "build_opener")
    def test_read_gives_up_after_three_attempts(self, build, sleep):
        build.return_value.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(m.MonitorError, "connection failed or timed out"):
            m.request_json("POST", "https://example.com", label="Brev GPU catalog",
                           body={}, retry=True)
        self.assertEqual(build.return_value.open.call_count, 3)

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
