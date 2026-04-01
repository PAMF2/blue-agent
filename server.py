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

from flask import Flask, request, jsonify

from agent import Agent
from blooio import BlooioClient


def create_app(agent: Agent, signing_secret: str = None):
    """Create Flask app with webhook endpoint."""
    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        status = agent.get_status()
        return jsonify({"status": "ok", **status})

    @app.route("/webhook", methods=["POST"])
    def webhook():
        # Verify HMAC signature
        if signing_secret:
            sig = request.headers.get("X-Blooio-Signature", "")
            body = request.get_data()
            expected = hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return jsonify({"error": "Invalid signature"}), 401

        data = request.json
        if not data:
            return jsonify({"error": "No data"}), 400

        # Extract message fields (flexible — handles different Blooio payload shapes)
        phone = data.get("from", data.get("phone_number", data.get("sender", "")))
        text = data.get("text", data.get("message", data.get("body", "")))
        is_from_me = data.get("is_from_me", False)
        direction = data.get("direction", data.get("type", "inbound"))

        # Skip our own outbound messages
        if is_from_me or direction in ("outbound", "sent"):
            return jsonify({"status": "skipped", "reason": "outbound"})

        if not phone or not text:
            return jsonify({"status": "skipped", "reason": "missing phone or text"})

        phone = BlooioClient.format_phone(phone)

        # Enqueue
        conn = agent._get_conn()
        conn.execute(
            "INSERT INTO message_queue (phone, text, direction, created_at) VALUES (?, ?, ?, ?)",
            (phone, text, "inbound", agent._now()))
        conn.commit()
        conn.close()

        # Process in background thread (don't block webhook response)
        threading.Thread(target=_process, args=(agent, phone, text), daemon=True).start()

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
        print(f"ERROR processing {phone}: {e}")


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
