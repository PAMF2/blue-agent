"""
blooio.py — Blooio iMessage/SMS API client.

Handles sending messages, reading conversations, webhook registration,
and account status via the Blooio V2 API.

Usage:
    from blooio import BlooioClient

    client = BlooioClient(api_key="your_key")
    client.send("+15551234567", "Hello from Blue Agent!")

    # Register webhook for instant inbound delivery
    client.register_webhook("https://your-domain.com/webhook")
"""
import re
import time
import requests


class BlooioClient:
    """Blooio iMessage/SMS API client."""

    def __init__(self, api_key, base_url="https://backend.blooio.com/v2/api"):
        self.api_key = api_key
        self.base_url = base_url

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def format_phone(phone):
        """Convert phone to E.164 format (+1XXXXXXXXXX)."""
        clean = re.sub(r'[^\d+]', '', phone)
        if not clean.startswith('+'):
            if len(clean) == 10:
                clean = f"+1{clean}"
            elif len(clean) == 11 and clean.startswith('1'):
                clean = f"+{clean}"
        return clean

    def send(self, phone, message):
        """Send an iMessage/SMS. Returns dict with success status."""
        formatted = self.format_phone(phone)
        try:
            resp = requests.post(
                f"{self.base_url}/chats/{formatted}/messages",
                headers=self._headers(),
                json={"text": message},
                timeout=15,
            )
            if resp.status_code in (200, 201, 202):
                data = resp.json() if resp.text else {}
                return {"success": True, "message_id": data.get("message_id"),
                        "status": data.get("status", "sent"), "phone": formatted}
            return {"success": False, "error": resp.text[:500],
                    "status_code": resp.status_code, "phone": formatted}
        except Exception as e:
            return {"success": False, "error": str(e), "phone": formatted}

    def send_bulk(self, messages, delay=3.0):
        """Send multiple messages with rate limiting.
        Args: messages = list of (phone, message) tuples
        """
        results = []
        for i, (phone, msg) in enumerate(messages):
            results.append(self.send(phone, msg))
            if i < len(messages) - 1:
                time.sleep(delay)
        return results

    def get_conversations(self, limit=20):
        """List recent conversations."""
        try:
            resp = requests.get(f"{self.base_url}/chats",
                                headers=self._headers(),
                                params={"limit": limit}, timeout=10)
            return resp.json() if resp.status_code == 200 else {"error": resp.text}
        except Exception as e:
            return {"error": str(e)}

    def get_messages(self, phone, limit=20):
        """Get message history with a contact."""
        formatted = self.format_phone(phone)
        try:
            resp = requests.get(f"{self.base_url}/chats/{formatted}/messages",
                                headers=self._headers(),
                                params={"limit": limit}, timeout=10)
            return resp.json() if resp.status_code == 200 else {"error": resp.text}
        except Exception as e:
            return {"error": str(e)}

    def get_account_status(self):
        """Check Blooio account status."""
        try:
            resp = requests.get(f"{self.base_url}/me",
                                headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "active": True,
                    "phone": data.get("devices", [{}])[0].get("phone_number"),
                    "org": data.get("organization", {}).get("name"),
                    "device_active": data.get("devices", [{}])[0].get("is_active"),
                }
            return {"active": False, "error": resp.text}
        except Exception as e:
            return {"active": False, "error": str(e)}

    def register_webhook(self, url, webhook_type="all"):
        """Register a webhook for inbound message delivery.
        Returns dict with webhook_id and signing_secret for HMAC verification.
        """
        resp = requests.post(
            f"{self.base_url}/webhooks",
            headers=self._headers(),
            json={"webhook_url": url, "webhook_type": webhook_type},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise Exception(f"Webhook registration failed: {resp.status_code} {resp.text}")

    def delete_webhook(self, webhook_id):
        """Delete a webhook."""
        resp = requests.delete(
            f"{self.base_url}/webhooks/{webhook_id}",
            headers=self._headers(), timeout=10)
        return resp.json() if resp.status_code == 200 else {"error": resp.text}

    def list_webhooks(self):
        """List registered webhooks."""
        resp = requests.get(f"{self.base_url}/webhooks",
                            headers=self._headers(), timeout=10)
        return resp.json() if resp.status_code == 200 else {"error": resp.text}
