import unittest
from unittest.mock import patch

with patch('threading.Thread'):
    import app


MIB = 1024 * 1024


class SabSpeedProbeTests(unittest.TestCase):
    def setUp(self):
        app.reset_sab_probe()

    def run_samples(self, speeds, qbt_speed=10 * MIB, now_start=0):
        result = None
        for index, speed in enumerate(speeds):
            result = app.update_sab_speed_probe(
                qbt_speed,
                speed,
                sab_active=True,
                qbt_active=True,
                priority_source='sab',
                now=now_start + index * 20,
            )
        return result

    def test_confirmed_gain_keeps_probe_cap_until_sab_finishes(self):
        self.assertEqual(self.run_samples([10 * MIB] * 4), MIB)
        self.assertEqual(self.run_samples([14 * MIB] * 4, now_start=80), None)
        self.assertEqual(self.run_samples([10 * MIB] * 4, now_start=160), MIB)
        self.assertEqual(app.sab_probe_state['phase'], 'confirmed')

        self.assertIsNone(app.update_sab_speed_probe(0, 0, False, True, 'qbit', now=240))
        self.assertEqual(app.sab_probe_state['phase'], 'idle')

    def test_no_confirmed_gain_restores_normal_allocation_and_cools_down(self):
        self.assertEqual(self.run_samples([10 * MIB] * 4), MIB)
        self.assertIsNone(self.run_samples([12 * MIB] * 4, now_start=80))
        self.assertIsNone(self.run_samples([11 * MIB] * 4, now_start=160))
        self.assertEqual(app.sab_probe_state['phase'], 'cooldown')
        self.assertEqual(app.sab_probe_state['cooldown_until'], 220 + app.SAB_PROBE_COOLDOWN_SECONDS)

        self.assertIsNone(app.update_sab_speed_probe(
            10 * MIB, 10 * MIB, True, True, 'sab', now=200
        ))
        self.assertEqual(app.sab_probe_state['phase'], 'cooldown')

    def test_manual_override_ends_a_confirmed_probe_cap(self):
        self.run_samples([10 * MIB] * 4)
        self.run_samples([14 * MIB] * 4, now_start=80)
        self.run_samples([10 * MIB] * 4, now_start=160)
        self.assertEqual(app.sab_probe_state['phase'], 'confirmed')

        override = app.update_sab_speed_probe(
            10 * MIB, 10 * MIB, True, False, 'qbit', now=240
        )

        self.assertIsNone(override)
        self.assertEqual(app.sab_probe_state['phase'], 'cooldown')

    def test_priority_change_aborts_probe_and_restores_normal_allocation(self):
        self.run_samples([10 * MIB] * 4)
        self.assertEqual(app.sab_probe_state['phase'], 'probe')

        override = app.update_sab_speed_probe(
            10 * MIB, 10 * MIB, True, True, 'qbit', now=100
        )

        self.assertIsNone(override)
        self.assertEqual(app.sab_probe_state['phase'], 'cooldown')

    def test_duplicate_rebalance_does_not_count_as_an_independent_sample(self):
        app.update_sab_speed_probe(10 * MIB, 10 * MIB, True, True, 'sab', now=0)
        app.update_sab_speed_probe(10 * MIB, 10 * MIB, True, True, 'sab', now=1)

        self.assertEqual(len(app.sab_probe_state['samples']), 1)

    def test_rate_limit_override_caps_qbittorrent_without_lowering_sab_target(self):
        app.throttle_level = 1
        app.last_applied_qbt_limit = None
        app.last_applied_qbt_mode = None
        app.last_applied_sab_limit = None
        session = unittest.mock.Mock()
        session.post.return_value.raise_for_status.return_value = None

        with (
            patch.object(app, 'SAB_API_KEY', 'test-key'),
            patch.object(app, 'qbt_login_session', return_value=session),
            patch.object(app.requests, 'get') as get,
        ):
            get.return_value.status_code = 200
            app.apply_rate_limits(
                45 * MIB,
                20 * MIB,
                10 * MIB,
                [{'source': 'sab'}],
                qbt_limit_override=MIB,
            )

        self.assertEqual(get.call_args.kwargs['params']['value'], '45M')
        preference_call = next(call for call in session.post.call_args_list if 'setPreferences' in call.args[0])
        self.assertEqual(preference_call.kwargs['data']['json'], '{"dl_limit": 1048576, "alt_dl_limit": 1048576}')


if __name__ == '__main__':
    unittest.main()
