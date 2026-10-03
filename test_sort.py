import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


# Importing the service normally starts its poller, so keep unit tests isolated.
with patch.object(threading, "Thread"):
    import app


def make_item(
    name,
    *,
    source="qbit",
    season=999,
    episode=999,
    is_tv=False,
    is_prefetch=False,
    completed=0,
    total=1000,
    added_on=1,
    **overrides,
):
    item = {
        "source": source,
        "id": name,
        "name": name,
        "added_on": added_on,
        "priority": (season, episode, 1 if is_tv else 2, added_on),
        "is_tv": is_tv,
        "is_prefetch": is_prefetch,
        "is_manual_override": False,
        "is_manual_pause": False,
        "state": "downloading",
        "total_size": total,
        "completed_bytes": completed,
        "remaining_bytes": total - completed,
        "current_speed": 0,
        "is_paused": False,
        "qbt_pos": -1,
    }
    item.update(overrides)
    return item


def run_rebalance_for_test(items, prefetch_shows, diagnostics=False, max_rows=12):
    synchronized_orders = []
    output = StringIO()

    with redirect_stdout(output):
        with (
            patch.object(app, "update_prefetch_shows"),
            patch.object(app, "active_prefetch_shows", prefetch_shows),
            patch.object(app, "qbt_get_downloads", return_value=[item for item in items if item["source"] == "qbit"]),
            patch.object(app, "sab_get_downloads", return_value=[item for item in items if item["source"] == "sab"]),
            patch.object(app, "qbt_sync_queue_order", side_effect=lambda ranked: synchronized_orders.append([item["id"] for item in ranked])),
            patch.object(app, "sab_sync_queue_order"),
            patch.object(app, "apply_rate_limits"),
            patch.object(app, "qbt_toggle_torrents"),
            patch.object(app, "throttle_level", 1),
            patch.object(app, "FULL_SPEED", "1M"),
            patch.object(app, "QUEUE_DIAGNOSTICS", diagnostics),
            patch.object(app, "QUEUE_DIAGNOSTIC_MAX_ITEMS", max_rows),
        ):
            app.rebalance_downloads()

    return output.getvalue(), synchronized_orders


class QueuePriorityTests(unittest.TestCase):
    def test_prefetch_log_records_season_and_episode(self):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        log_line = f'{timestamp} level=info title="My Show" episode: 7 season: 2\n'

        with TemporaryDirectory() as directory:
            Path(directory, "prefetcharr.log").write_text(log_line, encoding="utf-8")
            with (
                patch.object(app, "PREFETCHARR_LOGS_DIR", directory),
                patch.object(app, "last_prefetch_check", 0),
                patch.object(app, "active_prefetch_shows", {}),
            ):
                app.update_prefetch_shows()

                self.assertEqual(app.active_prefetch_shows, {"my show": (2, 7)})

    def test_prefetch_trigger_matches_season_and_episode_boundaries(self):
        pre_trigger = make_item("My Show S02E03", season=2, episode=3, is_tv=True)
        trigger_episode = make_item("My Show S02E04", season=2, episode=4, is_tv=True)
        later_season = make_item("My Show S03E01", season=3, episode=1, is_tv=True)
        season_pack = make_item("My Show S02", season=2, episode=999, is_tv=True)
        other_show = make_item("Other Show S02E04", season=2, episode=4, is_tv=True)

        trigger = {"my show": (2, 4)}
        self.assertFalse(app.is_prefetch_target(pre_trigger, trigger))
        self.assertTrue(app.is_prefetch_target(trigger_episode, trigger))
        self.assertTrue(app.is_prefetch_target(later_season, trigger))
        self.assertTrue(app.is_prefetch_target(season_pack, trigger))
        self.assertFalse(app.is_prefetch_target(other_show, trigger))
        self.assertTrue(app.is_prefetch_target(pre_trigger, {"my show": (None, 4)}))
        self.assertTrue(app.is_prefetch_target(pre_trigger, {"my show": (2, None)}))

    def test_policy_tiers_and_early_finish_promotions(self):
        items = [
            make_item("waiting movie", total=50),
            make_item("normal in-progress movie", completed=40, total=100),
            make_item("waiting episode", season=2, episode=4, is_tv=True, total=99),
            make_item("normal in-progress episode", season=2, episode=3, is_tv=True, completed=50, total=150),
            make_item("equal-size episode", season=1, episode=1, is_tv=True, total=100),
            make_item("prefetch pack", season=1, episode=999, is_tv=True, is_prefetch=True, completed=10),
            make_item("prefetch episode", season=1, episode=2, is_tv=True, is_prefetch=True),
            make_item("manual resume", is_manual_override=True),
        ]

        ordered = app.queue_priority_order(items, now=100)

        self.assertEqual(
            [item["name"] for item in ordered],
            [
                "manual resume",
                "prefetch episode",
                "prefetch pack",
                "waiting movie",
                "waiting episode",
                "normal in-progress movie",
                "normal in-progress episode",
                "equal-size episode",
            ],
        )

    def test_normal_tv_order_packs_movies_and_stable_ties(self):
        items = [
            make_item("S01 pack", season=1, episode=999, is_tv=True),
            make_item("S02E02", season=2, episode=2, is_tv=True),
            make_item("S02E01", season=2, episode=1, is_tv=True),
            make_item("S03E01", season=3, episode=1, is_tv=True),
            make_item("same age sab", source="sab", added_on=5, total=20),
            make_item("same age qbit z", source="qbit", added_on=5, total=20),
            make_item("same age qbit a", source="qbit", added_on=5, total=20),
            make_item("older movie", added_on=4, total=30),
        ]
        ordered = app.queue_priority_order(items, now=100)

        self.assertEqual(
            [item["name"] for item in ordered],
            [
                "S03E01",
                "S02E01",
                "S02E02",
                "S01 pack",
                "same age qbit a",
                "same age qbit z",
                "same age sab",
                "older movie",
            ],
        )
    def test_completed_stale_and_manually_paused_items_are_excluded(self):
        items = [
            make_item("eligible", added_on=19_999),
            make_item("manual pause", is_manual_pause=True),
            make_item("stale", added_on=1),
            make_item("completed state", state="completed"),
            make_item("fully downloaded", completed=1000),
        ]

        ordered = app.queue_priority_order(items, now=20_000)

        self.assertEqual([item["name"] for item in ordered], ["eligible"])


class ClientQueueMappingTests(unittest.TestCase):
    def test_qbittorrent_queue_position_uses_queue_position_field(self):
        now = int(app.time.time())
        torrents = [
            {
                "hash": "first",
                "name": "Show S01E01",
                "state": "downloading",
                "added_on": now - 1,
                "size": 1000,
                "completed": 0,
                "priority": 7,
                "queue_position": 2,
            },
            {
                "hash": "second",
                "name": "Show S01E02",
                "state": "downloading",
                "added_on": now - 1,
                "size": 1000,
                "completed": 0,
                "priority": 1,
                "queue_position": 0,
            },
        ]
        session = Mock()
        session.get.return_value.json.return_value = torrents

        with (
            patch.object(app, "qbt_login_session", return_value=session),
            patch.object(app.time, "time", return_value=now),
            patch.object(app, "qbt_intended_states", {}),
            patch.object(app, "qbt_manual_overrides", set()),
            patch.object(app, "qbt_manual_pauses", set()),
        ):
            downloads = app.qbt_get_downloads()

        self.assertEqual({item["id"]: item["qbt_pos"] for item in downloads}, {"first": 2, "second": 0})

    def test_paused_sab_slot_is_excluded_from_queue_ranking(self):
        now = int(app.time.time())
        response = Mock()
        response.json.return_value = {
            "queue": {
                "kbpersec": "0",
                "slots": [
                    {
                        "status": "Paused",
                        "added": now - 10,
                        "mb": "1",
                        "mbleft": "1",
                        "filename": "Paused Movie",
                        "nzo_id": "paused-sab",
                    }
                ],
            }
        }

        with (
            patch.object(app, "SAB_API_KEY", "sab-key"),
            patch.object(app.time, "time", return_value=now),
            patch.object(app.requests, "get", return_value=response),
        ):
            downloads = app.sab_get_downloads()

        self.assertTrue(downloads[0]["is_paused"])
        self.assertTrue(downloads[0]["is_manual_pause"])
        self.assertEqual(app.queue_priority_order(downloads, now=now), [])


class RebalancePriorityTests(unittest.TestCase):
    def test_rebalance_uses_trigger_episode_and_syncs_policy_order(self):
        added_on = int(app.time.time())
        early = make_item("Show S01E01", season=1, episode=1, is_tv=True, added_on=added_on)
        target = make_item("Show S01E02", season=1, episode=2, is_tv=True, added_on=added_on)
        output, synchronized_orders = run_rebalance_for_test([early, target], {"show": (1, 2)})

        self.assertEqual(synchronized_orders, [["Show S01E02", "Show S01E01"]])
        self.assertIn("[REBALANCE]", output)
        self.assertNotIn("Show S01E02", output)

    def test_opt_in_queue_details_are_reasoned_and_bounded(self):
        added_on = int(app.time.time())
        early = make_item("Show S01E01", season=1, episode=1, is_tv=True, added_on=added_on)
        target = make_item("Show S01E02", season=1, episode=2, is_tv=True, added_on=added_on)

        output, _ = run_rebalance_for_test(
            [early, target], {"show": (1, 2)}, diagnostics=True, max_rows=1
        )

        self.assertIn("[QUEUE] rank=1", output)
        self.assertIn("tier=prefetch", output)
        self.assertIn('title="Show S01E02"', output)
        self.assertIn("omitted=1", output)
        self.assertNotIn("Show S01E01", output)

    def test_opt_in_queue_details_identify_excluded_manual_pauses(self):
        paused = make_item(
            "Manually Paused Movie",
            is_manual_pause=True,
            added_on=int(app.time.time()),
        )

        output, _ = run_rebalance_for_test([paused], {}, diagnostics=True, max_rows=1)

        self.assertIn("tier=manual-pause action=excluded", output)
        self.assertIn('title="Manually Paused Movie"', output)


class LoggingTests(unittest.TestCase):
    def test_error_sanitizer_redacts_known_secrets_and_credentials(self):
        message = (
            "request failed https://sab/api?apikey=sab-secret "
            "Authorization: Bearer trace-secret https://user:pass@host/api"
        )

        with (
            patch.object(app, "SAB_API_KEY", "sab-secret"),
            patch.object(app, "TRACEARR_TOKEN", "trace-secret"),
            patch.object(app, "QBT_PASS", "pass"),
        ):
            sanitized = app.sanitize_log_text(message)

        for secret in ("sab-secret", "trace-secret", "user:pass"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("[REDACTED]", sanitized)




class ManualPauseTests(unittest.TestCase):
    def test_recent_pause_transition_is_not_auto_resumed_during_grace(self):
        torrent = {
            "hash": "recent-manual-pause",
            "name": "Recent Movie",
            "state": "pausedDL",
            "added_on": 19_990,
            "size": 1000,
            "completed": 0,
        }
        session = Mock()
        session.get.return_value.json.return_value = [torrent]
        intended_states = {"recent-manual-pause": {"paused": False, "time": 19_980}}

        with (
            patch.object(app, "qbt_login_session", return_value=session),
            patch.object(app.time, "time", return_value=20_000),
            patch.object(app, "qbt_intended_states", intended_states),
            patch.object(app, "qbt_manual_overrides", set()),
            patch.object(app, "qbt_manual_pauses", set()),
        ):
            downloads = app.qbt_get_downloads()
            app.qbt_toggle_torrents({"recent-manual-pause"}, downloads)

        self.assertTrue(downloads[0]["is_manual_pause"])
        session.post.assert_not_called()
        self.assertFalse(intended_states["recent-manual-pause"]["paused"])

    def test_stale_manually_paused_torrent_is_not_resumed_on_later_polls(self):
        torrent = {
            "hash": "manual-pause",
            "name": "Old Movie",
            "state": "pausedDL",
            "added_on": 1,
            "size": 1000,
            "completed": 0,
        }
        session = Mock()
        session.get.return_value.json.return_value = [torrent]
        intended_states = {"manual-pause": {"paused": False, "time": 0}}
        manual_pauses = set()

        with (
            patch.object(app, "qbt_login_session", return_value=session),
            patch.object(app.time, "time", return_value=20_000),
            patch.object(app, "qbt_intended_states", intended_states),
            patch.object(app, "qbt_manual_overrides", set()),
            patch.object(app, "qbt_manual_pauses", manual_pauses),
        ):
            app.qbt_get_downloads()
            app.qbt_get_downloads()

        session.post.assert_not_called()
        self.assertIn("manual-pause", manual_pauses)

    def test_stale_torrent_paused_by_throttlarr_is_still_rescued(self):
        torrent = {
            "hash": "service-pause",
            "name": "Old Movie",
            "state": "pausedDL",
            "added_on": 1,
            "size": 1000,
            "completed": 0,
        }
        session = Mock()
        session.get.return_value.json.return_value = [torrent]

        with (
            patch.object(app, "qbt_login_session", return_value=session),
            patch.object(app.time, "time", return_value=20_000),
            patch.object(app, "qbt_intended_states", {"service-pause": {"paused": True, "time": 0}}),
            patch.object(app, "qbt_manual_overrides", set()),
            patch.object(app, "qbt_manual_pauses", set()),
        ):
            app.qbt_get_downloads()

        session.post.assert_called_once_with(
            f"{app.QBT_HOST}/api/v2/torrents/start",
            data={"hashes": "service-pause"},
            timeout=5,
        )


class ThroughputCoordinationTests(unittest.TestCase):
    def test_remaining_capacity_is_given_to_the_other_client(self):
        sab_response = Mock(status_code=200)
        qbt_session = Mock()

        with (
            patch.object(app, "SAB_API_KEY", "sab-key"),
            patch.object(app, "last_applied_sab_limit", None),
            patch.object(app, "last_applied_qbt_limit", None),
            patch.object(app, "last_applied_qbt_mode", None),
            patch.object(app, "throttle_level", 1),
            patch.object(app.requests, "get", return_value=sab_response) as sab_get,
            patch.object(app, "qbt_login_session", return_value=qbt_session),
        ):
            app.apply_rate_limits(
                total_speed_limit=100_000,
                current_qbt_speed=60_000,
                current_sab_speed=20_000,
                active_items=[{"source": "qbit"}],
            )

        self.assertEqual(sab_get.call_args.kwargs["params"]["value"], "39.06K")
        self.assertNotIn("apikey", sab_get.call_args.args[0])
        preferences = qbt_session.post.call_args_list[0].kwargs["data"]["json"]
        self.assertEqual(app.json.loads(preferences)["dl_limit"], 100_000)

    def test_sab_request_failure_does_not_log_api_key(self):
        output = StringIO()
        qbt_session = Mock()
        failure = app.requests.ConnectionError(
            "request failed at https://sab/api?apikey=sab-secret&mode=config"
        )

        with redirect_stdout(output):
            with (
                patch.object(app, "SAB_API_KEY", "sab-secret"),
                patch.object(app, "last_applied_sab_limit", None),
                patch.object(app, "last_applied_qbt_limit", None),
                patch.object(app, "last_applied_qbt_mode", None),
                patch.object(app, "throttle_level", 1),
                patch.object(app.requests, "get", side_effect=failure),
                patch.object(app, "qbt_login_session", return_value=qbt_session),
            ):
                app.apply_rate_limits(
                    total_speed_limit=100_000,
                    current_qbt_speed=0,
                    current_sab_speed=0,
                    active_items=[{"source": "sab"}],
                )

        self.assertNotIn("sab-secret", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())


class ThrottleTransitionTests(unittest.TestCase):
    def setUp(self):
        self.previous_level = app.throttle_level
        app.throttle_level = 2
        self.rebalance = Mock()
        self.rebalance_patch = patch.object(app, "rebalance_downloads", self.rebalance)
        self.rebalance_patch.start()

    def tearDown(self):
        self.rebalance_patch.stop()
        app.throttle_level = self.previous_level

    def test_webhook_cannot_downgrade_hard_throttle(self):
        app.set_throttles(1, "webhook")

        self.assertEqual(app.throttle_level, 2)
        self.rebalance.assert_not_called()

    def test_tracearr_can_downgrade_hard_throttle_to_soft(self):
        app.set_throttles(1, "Tracearr lower load", allow_downgrade=True)

        self.assertEqual(app.throttle_level, 1)
        self.rebalance.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
