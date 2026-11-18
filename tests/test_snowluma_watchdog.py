import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_message_bridge.snowluma_watchdog import SnowLumaRecovery


class NativeManager:
    def __init__(self):
        self.deploy_mode = "native"
        self.process = SimpleNamespace(returncode=None)
        self.starts = 0
        self.start_ok = True

    async def start(self):
        self.starts += 1
        if not self.start_ok:
            await self.stop()  # Native manager also cleans up failed starts.
            return False
        self.process = SimpleNamespace(returncode=None)
        return True

    async def stop(self):
        if self.process:
            self.process.returncode = 0
        self.process = None


class SnowLumaRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = NativeManager()
        self.events = Mock()
        self.recovery = SnowLumaRecovery(self.manager, self.events, interval=.01)

    async def asyncTearDown(self):
        await self.recovery.close()

    async def test_only_an_observed_native_managed_child_can_be_watched(self):
        self.assertTrue(SnowLumaRecovery.can_attach(self.manager))
        for manager in (SimpleNamespace(), SimpleNamespace(deploy_mode="external", process=self.manager.process),
                        SimpleNamespace(deploy_mode="native", process=None)):
            self.assertFalse(SnowLumaRecovery.can_attach(manager))

    async def test_process_exit_recovers_through_same_manager_without_login_checks(self):
        self.manager.process.returncode = 99
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 1)
        self.assertIsNone(self.manager.process.returncode)
        # A running process with QQ logged out or waiting for consent is left alone.
        self.manager.auth_ok = False
        self.manager.consent_required = True
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 1)
        self.events.write.assert_any_call("recovery", "snowluma", True, state="started")

    async def test_manual_stop_stays_stopped_and_explicit_start_reenables_recovery(self):
        await self.manager.stop()
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 0)
        self.assertFalse(self.recovery.desired)
        await self.manager.start()
        self.manager.process.returncode = 5
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 2)

    async def test_failed_restart_uses_backoff_and_internal_cleanup_keeps_desired_state(self):
        self.manager.process = None
        self.manager.start_ok = False
        await self.recovery.tick()
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 1)
        self.assertTrue(self.recovery.desired)
        self.assertGreater(self.recovery.next_try, time.monotonic())
        self.recovery.next_try = 0
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 2)
        self.assertGreater(self.recovery.next_try - time.monotonic(), 9)

    async def test_external_mode_is_not_restarted(self):
        self.manager.deploy_mode = "external"
        self.manager.process = None
        await self.recovery.tick()
        self.assertEqual(self.manager.starts, 0)

    async def test_stop_during_recovery_wins_over_child_spawned_later(self):
        await self.recovery.close()
        entered, release = asyncio.Event(), asyncio.Event()
        original_start = self.manager.start

        async def delayed_start():
            entered.set()
            await release.wait()
            return await original_start()

        self.manager.start = delayed_start
        self.recovery = SnowLumaRecovery(self.manager, self.events)
        self.manager.process = None
        restart = asyncio.create_task(self.recovery.tick())
        await entered.wait()
        await self.manager.stop()
        release.set()
        await restart
        self.assertFalse(self.recovery.desired)
        self.assertIsNone(self.manager.process)

    async def test_close_cancels_monitor_restores_methods_and_keeps_child_running(self):
        self.recovery.start()
        task = self.recovery._task
        await self.recovery.close()
        self.assertTrue(task.cancelled())
        self.assertNotIn("start", vars(self.manager))
        self.assertNotIn("stop", vars(self.manager))
        self.assertIsNone(self.manager.process.returncode)

    async def test_background_loop_recovers_crash_and_honors_subsequent_stop(self):
        self.recovery.start()
        self.manager.process.returncode = 99
        for _ in range(30):
            if self.manager.starts:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.manager.starts, 1)
        await self.manager.stop()
        await asyncio.sleep(.04)
        self.assertEqual(self.manager.starts, 1)
