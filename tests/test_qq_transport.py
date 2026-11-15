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


if __name__ == "__main__":
    unittest.main()
