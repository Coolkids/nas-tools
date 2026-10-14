"""Offline HTTP contracts for third-party webhook and chat ingress routes."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from app.utils.types import SearchType


def _client():
    from web.main import App

    return App.test_client()


def test_media_server_webhooks_validate_ip_parse_payload_and_schedule(monkeypatch):
    import web.main as main

    calls = []

    class Webhook:
        def plex_action(self, payload):
            pass

        def jellyfin_action(self, payload):
            pass

        def emby_action(self, payload):
            pass

    class Limiter:
        def plex_action(self, payload):
            pass

        def jellyfin_action(self, payload):
            pass

        def emby_action(self, payload):
            pass

    monkeypatch.setattr(main, "WebhookEvent", Webhook)
    monkeypatch.setattr(main, "SpeedLimiter", Limiter)
    monkeypatch.setattr(main.SecurityHelper, "check_mediaserver_ip", lambda self, ip: True)
    monkeypatch.setattr(main.ThreadHelper(), "start_thread",
                        lambda func, args: calls.append((func.__name__, args)))

    payloads = {
        "/plex": {"event": "media.play", "Metadata": {"title": "Fixture"}},
        "/jellyfin": {"NotificationType": "PlaybackStart", "Name": "Fixture"},
        "/emby": {"Event": "playback.start", "Item": {"Name": "Fixture"}},
    }
    for path, payload in payloads.items():
        calls.clear()
        if path == "/plex":
            response = _client().post(path, data={"payload": json.dumps(payload)})
        elif path == "/emby":
            response = _client().post(path, data={"data": json.dumps(payload)})
        else:
            response = _client().post(path, json=payload)
        assert response.status_code == 200
        assert response.get_data(as_text=True) == "Ok"
        assert len(calls) == 2
        assert all(args == (payload,) for _, args in calls)


def test_media_server_webhook_rejects_disallowed_ip_without_scheduling(monkeypatch):
    import web.main as main

    scheduled = []
    monkeypatch.setattr(main.SecurityHelper, "check_mediaserver_ip", lambda self, ip: False)
    monkeypatch.setattr(main.ThreadHelper(), "start_thread",
                        lambda func, args: scheduled.append((func, args)))

    response = _client().post("/jellyfin", json={"NotificationType": "PlaybackStart"})

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "不允许的IP地址请求"
    assert scheduled == []


def test_chat_callbacks_reject_untrusted_sender_or_token(monkeypatch):
    import web.main as main

    dispatched = []
    client_config = SimpleNamespace(
        get_admin=lambda: ["42"],
        get_users=lambda: ["42"],
        check_token=lambda value: value == "fixture-token",
    )
    monkeypatch.setattr(main.Message(), "get_interactive_client",
                        lambda channel: {"client": client_config})
    monkeypatch.setattr(main.Message(), "send_channel_msg", lambda **kwargs: None)
    monkeypatch.setattr(main.WebAction, "handle_message_job",
                        staticmethod(lambda **kwargs: dispatched.append(kwargs)))
    monkeypatch.setattr(main.SecurityHelper, "check_telegram_ip", lambda self, ip: False)
    monkeypatch.setattr(main.SecurityHelper, "check_synology_ip", lambda self, ip: True)
    monkeypatch.setattr(main.SecurityHelper, "check_slack_ip", lambda self, ip: False)

    telegram = _client().post("/telegram", json={"message": {
        "text": "/ssa", "from": {"id": 42, "username": "fixture"}}})
    synology = _client().post("/synology", data={
        "token": "wrong-token", "text": "/ssa", "user_id": "42"})
    slack = _client().post("/slack", json={"type": "message", "client": "C-fixture",
                                            "text": "/ssa"})

    assert telegram.get_data(as_text=True) == "不允许的IP地址请求"
    assert synology.get_data(as_text=True) == "token校验不通过"
    assert slack.get_data(as_text=True) == "不允许的IP地址请求"
    assert dispatched == []


def test_telegram_and_synology_callbacks_dispatch_commands_after_validation(monkeypatch):
    import web.main as main

    dispatched = []
    client_config = SimpleNamespace(
        get_admin=lambda: ["42"],
        get_users=lambda: ["42"],
        check_token=lambda value: value == "fixture-token",
    )
    monkeypatch.setattr(main.Message(), "get_interactive_client",
                        lambda channel: {"client": client_config})
    monkeypatch.setattr(main.SecurityHelper, "check_telegram_ip", lambda self, ip: True)
    monkeypatch.setattr(main.SecurityHelper, "check_synology_ip", lambda self, ip: True)
    monkeypatch.setattr(main.WebAction, "handle_message_job",
                        staticmethod(lambda **kwargs: dispatched.append(kwargs)))

    telegram = _client().post("/telegram", json={"message": {
        "text": "/ssa", "from": {"id": 42, "username": "fixture"}}})
    synology = _client().post("/synology", data={
        "token": "fixture-token", "text": "/ssa", "user_id": "42", "username": "fixture"})

    assert telegram.status_code == 200
    assert synology.status_code == 200
    assert len(dispatched) == 2
    assert [item["msg"] for item in dispatched] == ["/ssa", "/ssa"]
    assert [item["in_from"] for item in dispatched] == [SearchType.TG, SearchType.SYNOLOGY]
    assert [item["user_id"] for item in dispatched] == [42, 42]


def test_slack_callback_extracts_event_command(monkeypatch):
    import web.main as main

    dispatched = []
    monkeypatch.setattr(main.Message(), "get_interactive_client",
                        lambda channel: {"client": object()})
    monkeypatch.setattr(main.SecurityHelper, "check_slack_ip", lambda self, ip: True)
    monkeypatch.setattr(main.WebAction, "handle_message_job",
                        staticmethod(lambda **kwargs: dispatched.append(kwargs)))

    response = _client().post("/slack", json={"type": "event_callback", "event": {
        "client": "C-fixture", "text": "<@U123ABC> /ssa"}})

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "Ok"
    assert dispatched == [{"msg": "/ssa", "in_from": SearchType.SLACK,
                           "user_id": "C-fixture", "user_name": ""}]


def test_wechat_callback_decrypts_and_dispatches_text(monkeypatch):
    import web.main as main

    dispatched = []
    plain_xml = (b"<xml><MsgType><![CDATA[text]]></MsgType>"
                 b"<FromUserName><![CDATA[fixture-user]]></FromUserName>"
                 b"<Content><![CDATA[/ssa]]></Content></xml>")

    class FakeWechatCipher:
        def __init__(self, token, aes_key, corp_id):
            assert (token, aes_key, corp_id) == ("fixture-token", "fixture-aes", "fixture-corp")

        def DecryptMsg(self, data, signature, timestamp, nonce):
            assert data == b"encrypted fixture"
            assert (signature, timestamp, nonce) == ("signature", "10", "nonce")
            return 0, plain_xml

    monkeypatch.setattr(main.Message(), "get_interactive_client", lambda channel: {
        "config": {"token": "fixture-token", "encodingAESKey": "fixture-aes",
                   "corpid": "fixture-corp"}})
    monkeypatch.setattr(main, "WXBizMsgCrypt", FakeWechatCipher)
    monkeypatch.setattr(main.WebAction, "handle_message_job",
                        staticmethod(lambda **kwargs: dispatched.append(kwargs)))

    response = _client().post("/wechat?msg_signature=signature&timestamp=10&nonce=nonce",
                              data=b"encrypted fixture")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "/ssa"
    assert dispatched == [{"msg": "/ssa", "in_from": SearchType.WX,
                           "user_id": "fixture-user", "user_name": "fixture-user"}]
