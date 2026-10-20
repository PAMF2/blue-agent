"""
server.py — Webhook server for receiving Blooio inbound messages.

Receives webhooks from Blooio, validates HMAC signatures, and routes
messages to the agent for processing.

Usage:
    python server.py                          # Start on port 8787
    python server.py --port 9000              # Custom port
    python server.py --register-webhook URL   # Register webhook with Blooio
"""
import hmac
import hashlib
import threading
import json
import argparse
import re
import time
from datetime import datetime, timezone

from flask import Flask, request, jsonify

from agent import Agent
from blooio import BlooioClient


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_PHONE_LIKE = re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")


def _safe_id(value):
    value = str(value or "")
    if not _SAFE_ID.fullmatch(value) or _PHONE_LIKE.search(value):
        return ""
    return value


def _log_provider_event(*, event_type, outcome, event_id="", message_id="",
                        delivery_id="", api_version="", status="",
                        error_code="", http_status=None,
                        retry_action=""):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": "blooio",
        "event_type": _safe_id(event_type),
        "outcome": _safe_id(outcome),
        "event_id": _safe_id(event_id),
        "message_id": _safe_id(message_id),
        "delivery_id": _safe_id(delivery_id),
        "api_version": _safe_id(api_version),
        "status": _safe_id(status),
        "error_code": _safe_id(error_code),
        "http_status": http_status,
        "retry_action": _safe_id(retry_action),
    }
    print("provider_event=" + json.dumps(record, sort_keys=True), flush=True)


def _verify_blooio_signature(raw_body, signature_header, signing_secret, now=None):
    """Validate Blooio's timestamped HMAC-SHA256 signature."""
    parts = {}
    for part in (signature_header or "").split(","):
        key, separator, value = part.strip().partition("=")
        if separator:
            parts[key.strip()] = value.strip()
    timestamp = parts.get("t", "")
    signature = parts.get("v1", "")
    if not timestamp.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        return False, "signature_malformed"
    signed_at = int(timestamp)
    current_time = time.time() if now is None else now
    if abs(current_time - signed_at) >= 300:
        return False, "signature_stale"
    signed_payload = timestamp.encode("ascii") + b"." + raw_body
    expected = hmac.new(signing_secret.encode(), signed_payload,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        return False, "signature_invalid"
    return True, ""


def create_app(agent: Agent, signing_secret: str = None):
    """Create Flask app with webhook endpoint."""
    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        status = agent.get_status()
        return jsonify({"status": "ok", **status})

    @app.route("/webhook", methods=["POST"])
    def webhook():
        raw_body = request.get_data(cache=True)
        delivery_id = request.headers.get("X-Blooio-Delivery", "")
        api_version = request.headers.get("X-Blooio-Version", "")
        if not signing_secret:
            _log_provider_event(
                event_type="webhook.authentication", outcome="secret_missing",
                delivery_id=delivery_id, api_version=api_version, http_status=503)
            return jsonify({"error": "Webhook signing secret is not configured"}), 503
        is_valid, signature_error = _verify_blooio_signature(
            raw_body, request.headers.get("X-Blooio-Signature", ""), signing_secret)
        if not is_valid:
            _log_provider_event(
                event_type="webhook.authentication", outcome="rejected",
                delivery_id=delivery_id, api_version=api_version,
                error_code=signature_error, http_status=401)
            return jsonify({"error": "Invalid signature"}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            _log_provider_event(
                event_type="webhook.invalid", outcome="invalid_json",
                delivery_id=delivery_id, api_version=api_version,
                http_status=400)
            return jsonify({"error": "No data"}), 400

        resource = payload.get("data")
        if not isinstance(resource, dict):
            resource = payload
        event_type = (payload.get("type") or payload.get("event")
                      or request.headers.get("X-Blooio-Event", ""))
        event_id = payload.get("id") or payload.get("event_id") or ""
        message_id = (resource.get("message_id") or payload.get("message_id")
                      or request.headers.get("X-Blooio-Message-Id", ""))
        event_api_version = payload.get("api_version") or api_version
        error = resource.get("error") or payload.get("error") or {}
        if not isinstance(error, dict):
            error = {}
        status = resource.get("status") or ""

        if event_type == "message.failed" or status == "failed":
            _log_provider_event(
                event_type=event_type or "message.failed", outcome="delivery_failed",
                event_id=event_id, message_id=message_id, delivery_id=delivery_id,
                api_version=event_api_version, status=status or "failed",
                error_code=(error.get("code") or error.get("error_code")
                            or resource.get("error_code") or payload.get("error_code", "")),
                http_status=200,
                retry_action="acknowledged_no_retry")
            return jsonify({"status": "recorded"}), 200

        # Do not send Blooio lifecycle events to the conversational agent.
        if event_type.startswith("message.") and event_type != "message.received":
            _log_provider_event(
                event_type=event_type, outcome="lifecycle_event_ignored",
                event_id=event_id, message_id=message_id, delivery_id=delivery_id,
                api_version=event_api_version, status=status, http_status=200)
            return jsonify({"status": "ignored"}), 200

        # Extract inbound message fields from v2 flat or v4 envelope payloads.
        contact = resource.get("contact")
        contact_identifier = contact.get("identifier", "") if isinstance(contact, dict) else ""
        phone = (resource.get("from") or resource.get("phone_number")
                 or resource.get("sender") or contact_identifier)
        text = resource.get("text") or resource.get("message") or resource.get("body") or ""
        is_from_me = resource.get("is_from_me", payload.get("is_from_me", False))
        direction = resource.get("direction") or payload.get("direction") or "inbound"

        # Skip our own outbound messages
        if is_from_me or direction in ("outbound", "sent"):
            _log_provider_event(
                event_type=event_type or "message", outcome="outbound_ignored",
                event_id=event_id, message_id=message_id, delivery_id=delivery_id,
                api_version=event_api_version, status=status, http_status=200)
            return jsonify({"status": "skipped", "reason": "outbound"})

        if not phone or not text:
            reason = "missing_sender" if not phone else "missing_text"
            _log_provider_event(
                event_type=event_type or "message", outcome=reason,
                event_id=event_id, message_id=message_id, delivery_id=delivery_id,
                api_version=event_api_version, status=status, http_status=200)
            return jsonify({"status": "skipped", "reason": "missing phone or text"})

        phone = BlooioClient.format_phone(phone)

        # Enqueue
        conn = None
        try:
            conn = agent._get_conn()
            conn.execute(
                "INSERT INTO message_queue (phone, text, direction, created_at) VALUES (?, ?, ?, ?)",
                (phone, text, "inbound", agent._now()))
            conn.commit()
        except Exception as exc:
            _log_provider_event(
                event_type=event_type or "message.received", outcome="queue_failed",
                event_id=event_id, message_id=message_id, delivery_id=delivery_id,
                api_version=event_api_version, status=status,
                error_code="webhook_queue_failed",
                http_status=503,
                retry_action="provider_retry_requested")
            return jsonify({"error": "Unable to queue inbound message"}), 503
        finally:
            if conn is not None:
                conn.close()

        # Process in background thread (don't block webhook response)
        threading.Thread(target=_process, args=(agent, phone, text), daemon=True).start()

        _log_provider_event(
            event_type=event_type or "message.received", outcome="inbound_queued",
            event_id=event_id, message_id=message_id, delivery_id=delivery_id,
            api_version=event_api_version, status=status, http_status=200)

        return jsonify({"status": "queued"})

    @app.route("/send", methods=["POST"])
    def send():
        """Manual send endpoint for testing."""
        data = request.json
        phone = data.get("phone", "")
        text = data.get("text", "")
        if not phone or not text:
            return jsonify({"error": "phone and text required"}), 400
        result = agent.send_reply(phone, text)
        return jsonify({"results": result})

    @app.route("/conversation/<phone>", methods=["GET"])
    def conversation(phone):
        """View conversation history."""
        history = agent.get_history(phone, limit=50)
        profile = agent.get_profile(phone)
        return jsonify({"phone": phone, "profile": profile, "messages": history})

    return app


def _process(agent: Agent, phone: str, text: str):
    """Process an inbound message in a background thread."""
    try:
        response = agent.run(phone, text)
        if response:
            agent.send_reply(phone, response)

        conn = agent._get_conn()
        conn.execute("UPDATE message_queue SET processed = 1 WHERE phone = ? AND processed = 0", (phone,))
        conn.commit()
        conn.close()
    except Exception as e:
        _log_provider_event(
            event_type="inbound.process", outcome="failed",
            error_code="processing_exception",
            error_message=type(e).__name__, http_status=500)


def main():
    parser = argparse.ArgumentParser(description="Blue Agent webhook server")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--register-webhook", metavar="URL",
                        help="Register Blooio webhook at this URL")
    parser.add_argument("--api-key", default=None,
                        help="Blooio API key (or set BLOOIO_API_KEY env var)")
    args = parser.parse_args()

    import os
    api_key = args.api_key or os.environ.get("BLOOIO_API_KEY", "")

    if args.register_webhook:
        if not api_key:
            print("ERROR: --api-key or BLOOIO_API_KEY required")
            return
        client = BlooioClient(api_key=api_key)
        result = client.register_webhook(args.register_webhook)
        print(f"Webhook registered: {json.dumps(result, indent=2)}")
        return

    # For the generic server, create a minimal agent
    # Users should import and customize — this is just for testing
    agent = Agent(
        system_prompt="you are a helpful assistant over text. keep messages short and casual, like texting a friend. use --- to split into multiple messages.",
        blooio_api_key=api_key or None,
    )

    signing_secret = None
    conn = agent._get_conn()
    row = conn.execute("SELECT value FROM config WHERE key = 'signing_secret'").fetchone()
    conn.close()
    if row:
        signing_secret = row["value"]

    app = create_app(agent, signing_secret=signing_secret)
    print(f"Blue Agent server starting on {args.host}:{args.port}")
    print(f"Webhook endpoint: http://{args.host}:{args.port}/webhook")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
