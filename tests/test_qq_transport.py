import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_message_bridge.qq_transport import QQRoutingError, QQSendError, send_qq


def context_for(bot):
    adapter = SimpleNamespace(meta=lambda: SimpleNamespace(id="qq_bridge", name="aiocqhttp"), get_client=lambda: bot)
    return SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [adapter]))


def account(identity, groups):
    return {"platform_id": "qq_bridge", "user_id": identity, "connected": True, "identity_verified": True,
            "groups_status": "verified", "groups": [{"group_id": group} for group in groups]}


class QQTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(_wsr_api_clients={"111": object(), "222": object()},
                                   call_action=AsyncMock(return_value={"message_id": 987}))
        self.context = context_for(self.bot)
        self.segments = [{"type": "text", "data": {"text": "fixture"}}]
        self.parser = patch("astrbot_plugin_message_bridge.qq_transport._onebot_segments", new=AsyncMock(return_value=self.segments))
        self.parser.start()
        self.addCleanup(self.parser.stop)

    async def test_multiple_accounts_route_to_the_unique_verified_group_owner_and_return_receipt(self):
        state = {"qq": {"accounts": [account("111", ["10001"]), account("222", ["20002"])]}}
        receipt = await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), state)
        self.assertEqual(receipt, "987")
        self.bot.call_action.assert_awaited_once_with("send_group_msg", self_id="222", group_id=20002, message=self.segments)

    async def test_runtime_status_is_refreshed_for_multiple_accounts(self):
        monitor = SimpleNamespace(refresh=AsyncMock(return_value={"qq": {"accounts": [account("222", ["20002"])]}}))
        await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), monitor)
        monitor.refresh.assert_awaited_once()

    async def test_single_account_is_explicit_even_without_status(self):
        self.bot._wsr_api_clients = {"111": object()}
        await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None)
        self.bot.call_action.assert_awaited_once_with("send_private_msg", self_id="111", user_id=333, message=self.segments)

    async def test_explicit_account_resolves_multi_account_private_and_shared_group(self):
        state = {"qq": {"accounts": [account("111", ["10001"]), account("222", ["10001"])]}}
        await send_qq(self.context, "qq_bridge:GroupMessage:10001", object(), state, account_id="222")
        self.assertEqual(self.bot.call_action.await_args.kwargs["self_id"], "222")
        await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), state, account_id="111")
        self.assertEqual(self.bot.call_action.await_args.kwargs["self_id"], "111")

    async def test_explicit_account_must_be_online_and_respect_verified_group_membership(self):
        state = {"qq": {"accounts": [account("111", ["10001"]), account("222", ["20002"])]}}
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), state, account_id="333")
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), state, account_id="111")
        self.bot.call_action.assert_not_awaited()

    async def test_ambiguous_group_and_private_never_send_to_an_arbitrary_account(self):
        state = {"qq": {"accounts": [account("111", ["10001"]), account("222", ["10001"])]}}
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:10001", object(), state)
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), state)
        self.bot.call_action.assert_not_awaited()

    async def test_unverified_or_stale_membership_never_counts_as_a_route(self):
        entry = account("222", ["20002"])
        entry["identity_verified"] = False
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), {"qq": {"accounts": [entry]}})
        entry["identity_verified"] = True
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), {"stale": True, "qq": {"accounts": [entry]}})
        self.bot.call_action.assert_not_awaited()

    async def test_signed_server_receipt_is_valid_but_zero_or_missing_is_a_failure(self):
        self.bot._wsr_api_clients = {"111": object()}
        for response, expected in (({"message_id": -987}, "-987"), ({"message_id": "42"}, "42")):
            self.bot.call_action.return_value = response
            self.assertEqual(await send_qq(self.context, "qq_bridge:GroupMessage:10001", object(), None), expected)
        for response in (None, {}, {"message_id": 0}, {"message_id": "0"}, {"message_id": "not-an-id"}, {"message_id": "--1"}, {"message_id": True}, {"message_id": 1.5}):
            self.bot.call_action.return_value = response
            with self.assertRaises(QQSendError):
                await send_qq(self.context, "qq_bridge:GroupMessage:10001", object(), None)

    async def test_disconnected_adapter_and_invalid_destination_never_send(self):
        self.bot._wsr_api_clients.clear()
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:10001", object(), None)
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:GroupMessage:not-a-number", object(), None)
        self.bot.call_action.assert_not_awaited()

    def attach_snowluma_manager(self, response=(200, {"status": "ok", "retcode": 0, "data": {"message_id": -456}})):
        api = SimpleNamespace(raw_request=AsyncMock(return_value=response))
        plugin = SimpleNamespace(manager=SimpleNamespace(api=api))
        self.context.get_registered_star = lambda name: SimpleNamespace(star_cls=plugin, activated=True) if name == "pulids_snowluma_go_to_astrbot" else None
        return api

    async def test_snowluma_manager_is_primary_and_uses_selected_account_for_group_media(self):
        api = self.attach_snowluma_manager()
        self.segments[:] = [
            {"type": "record", "data": {"file": "base64://fixture-audio"}},
            {"type": "video", "data": {"file": "file:///C:/fixture.mp4"}},
        ]
        state = {"qq": {"accounts": [account("111", ["10001"]), account("222", ["20002"])]}}
        result = await send_qq(self.context, "qq_bridge:GroupMessage:20002", object(), state, return_account=True)
        self.assertEqual(result, ("222", "-456"))
        api.raw_request.assert_awaited_once_with(
            "POST", "/api/debug/invoke",
            json_body={"uin": "222", "action": "send_group_msg", "params": {"group_id": 20002, "message": self.segments}},
            timeout=120,
        )
        self.bot.call_action.assert_not_awaited()

    async def test_snowluma_manager_private_send_respects_explicit_selected_account(self):
        api = self.attach_snowluma_manager((200, {"status": "ok", "retcode": 0, "data": {"message_id": "37"}}))
        result = await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.assertEqual(result, "37")
        self.assertEqual(api.raw_request.await_args.kwargs["json_body"], {
            "uin": "111", "action": "send_private_msg", "params": {"user_id": 333, "message": self.segments},
        })
        self.bot.call_action.assert_not_awaited()

    async def test_manager_response_failures_never_retry_through_generic_onebot(self):
        responses = [
            (200.0, {"status": "ok", "retcode": 0, "data": {"message_id": 1}}),
            (401, {"status": "ok", "retcode": 0, "data": {"message_id": 1}}),
            (500, {"status": "failed", "retcode": 100, "message": "fixture-private-detail"}),
            (200, {"status": "failed", "retcode": 0, "data": {"message_id": 1}}),
            (200, {"status": "ok", "retcode": 100, "data": {"message_id": 1}}),
            (200, {"status": "ok", "retcode": False, "data": {"message_id": 1}}),
            (200, {"status": "ok", "retcode": "0", "data": {"message_id": 1}}),
            (200, {"status": "ok", "data": {"message_id": 1}}),
            (200, {"status": "ok", "retcode": 0, "data": None}),
            (200, "fixture-private-response"),
            None,
        ]
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                api = self.attach_snowluma_manager(response)
                with self.assertRaises(QQSendError) as caught:
                    await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
                self.assertNotIn("fixture-private", str(caught.exception))
                api.raw_request.assert_awaited_once()
        self.bot.call_action.assert_not_awaited()

    async def test_manager_network_failure_and_missing_session_do_not_switch_transport(self):
        api = self.attach_snowluma_manager()
        api.raw_request.side_effect = TimeoutError("fixture-private-session")
        with self.assertRaises(QQSendError) as caught:
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.assertNotIn("fixture-private-session", str(caught.exception))
        self.context.get_registered_star = lambda name: SimpleNamespace(star_cls=SimpleNamespace(manager=SimpleNamespace(api=None)))
        with self.assertRaises(QQSendError):
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.bot.call_action.assert_not_awaited()

    async def test_inactive_manager_and_lookup_error_do_not_use_generic_onebot(self):
        api = self.attach_snowluma_manager()
        self.context.get_registered_star = lambda name: SimpleNamespace(star_cls=SimpleNamespace(manager=SimpleNamespace(api=api)), activated=False)
        with self.assertRaises(QQSendError):
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        def unavailable(name):
            raise RuntimeError("fixture-private-registry-detail")
        self.context.get_registered_star = unavailable
        with self.assertRaises(QQSendError) as caught:
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.assertNotIn("fixture-private-registry-detail", str(caught.exception))
        api.raw_request.assert_not_awaited()
        self.bot.call_action.assert_not_awaited()

    async def test_absent_registered_manager_preserves_generic_onebot_path(self):
        self.context.get_registered_star = lambda name: None
        await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.bot.call_action.assert_awaited_once_with("send_private_msg", self_id="111", user_id=333, message=self.segments)

    async def test_snowluma_receipt_validation_rejects_zero_missing_and_invalid_ids(self):
        for raw_id in (0, "0", None, "invalid", True, 1.5):
            with self.subTest(raw_id=raw_id):
                self.attach_snowluma_manager((200, {"status": "ok", "retcode": 0, "data": {"message_id": raw_id}}))
                with self.assertRaises(QQSendError):
                    await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="111")
        self.bot.call_action.assert_not_awaited()

    async def test_account_validation_happens_before_manager_send(self):
        api = self.attach_snowluma_manager()
        with self.assertRaises(QQRoutingError):
            await send_qq(self.context, "qq_bridge:FriendMessage:333", object(), None, account_id="offline")
        api.raw_request.assert_not_awaited()
        self.bot.call_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
