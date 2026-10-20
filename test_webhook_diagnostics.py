import hashlib
import hmac
import json
import time

import pytest

from server import _process, create_app


SECRET = "test-signing-secret"


class FakeAgent:
    def __init__(self):
        self.calls = []

    def get_status(self):
        return {}

    def run(self, phone, text):
        self.calls.append((phone, text))
        return None


def signed_header(body, timestamp="1800000000"):
    signed = timestamp.encode("ascii") + b"." + body
    signature = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def post_signed(client, payload, timestamp=None):
    timestamp = timestamp or str(int(time.time()))
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Blooio-Signature": signed_header(body, timestamp),
            "X-Blooio-Delivery": "delivery_abc123",
            "X-Blooio-Event": payload.get("type") or payload.get("event", ""),
            "X-Blooio-Message-Id": payload.get("message_id", ""),
            "X-Blooio-Version": "2026-10-01",
        },
    )


@pytest.fixture
def agent():
    return FakeAgent()


def test_v4_message_failed_is_logged_and_not_sent_to_agent(agent, capsys):
    client = create_app(agent, SECRET).test_client()
    response = post_signed(client, {
        "id": "evt_abc123",
        "type": "message.failed",
        "api_version": "2026-10-01",
        "data": {
            "message_id": "msg_abc123",
            "error": {"code": "send_refused", "message": "Private detail for Jane Doe"},
        },
    })

    assert response.status_code == 200
    assert response.json == {"status": "recorded"}
    assert agent.calls == []
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "delivery_failed"
    assert record["error_code"] == "send_refused"
    assert record["message_id"] == "msg_abc123"
    assert record["delivery_id"] == "delivery_abc123"
    assert "Private detail" not in json.dumps(record)
    assert "Jane Doe" not in json.dumps(record)


def test_missing_or_invalid_signature_is_rejected_and_logged(agent, capsys):
    client = create_app(agent, SECRET).test_client()
    body = b'{"type":"message.failed","data":{"message_id":"msg_abc123"}}'
    response = client.post("/webhook", data=body, content_type="application/json")
    assert response.status_code == 401
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "rejected"
    assert record["error_code"] == "signature_malformed"
    assert record["retry_action"] == "provider_retry_requested"


def test_missing_signing_secret_fails_closed_and_is_logged(agent, capsys):
    client = create_app(agent).test_client()
    response = client.post("/webhook", data=b"{}", content_type="application/json")
    assert response.status_code == 503
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "secret_missing"
    assert record["retry_action"] == "provider_retry_requested"


def test_v2_failed_event_is_logged_without_routing_to_agent(agent, capsys):
    client = create_app(agent, SECRET).test_client()
    response = post_signed(client, {
        "event": "message.failed",
        "status": "failed",
        "message_id": "msg_v2abc",
        "error_code": "device_send_error",
        "error_message": "Private text from Jane Doe",
        "text": "Private message content",
    })

    assert response.status_code == 200
    assert agent.calls == []
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["error_code"] == "device_send_error"
    assert record["message_id"] == "msg_v2abc"
    assert "error_message" not in record
    assert "Private message content" not in json.dumps(record)
    assert "Jane Doe" not in json.dumps(record)


def test_lifecycle_event_is_acknowledged_without_agent_or_message_data(agent, capsys):
    client = create_app(agent, SECRET).test_client()
    response = post_signed(client, {
        "id": "evt_delivered",
        "type": "message.delivered",
        "data": {"message_id": "msg_delivered", "status": "delivered"},
    })

    assert response.status_code == 200
    assert response.json == {"status": "ignored"}
    assert agent.calls == []
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "lifecycle_event_ignored"


def test_database_queue_failure_is_logged_and_returns_retryable_status(capsys):
    class BrokenQueueAgent(FakeAgent):
        def _get_conn(self):
            raise RuntimeError("database error with +15551234567")

    agent = BrokenQueueAgent()
    client = create_app(agent, SECRET).test_client()
    response = post_signed(client, {
        "type": "message.received",
        "data": {"message_id": "msg_inbound", "sender": "+15551234567", "text": "hello"},
    })

    assert response.status_code == 503
    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "queue_failed"
    assert record["error_code"] == "webhook_queue_failed"
    assert record["retry_action"] == "provider_retry_requested"
    assert "+15551234567" not in json.dumps(record)
    assert "database error" not in json.dumps(record)


def test_background_processing_failure_is_logged_after_webhook_ack(capsys):
    class BrokenAgent(FakeAgent):
        def run(self, phone, text):
            raise RuntimeError("private processing details")

    _process(BrokenAgent(), "+15551234567", "private message text")

    record = json.loads(capsys.readouterr().out.removeprefix("provider_event="))
    assert record["outcome"] == "failed"
    assert record["error_code"] == "processing_exception"
    assert record["retry_action"] == "provider_already_acknowledged"
    assert "private processing details" not in json.dumps(record)
    assert "+15551234567" not in json.dumps(record)
    assert "private message text" not in json.dumps(record)
