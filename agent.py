"""
agent.py — Claude tool-use agent loop for iMessage conversations.

Manages conversation history, lead profiles, and runs a configurable
tool-use loop that feels like texting a real person.

Usage:
    from agent import Agent

    agent = Agent(
        system_prompt="you are a helpful assistant over text...",
        tools=[...],
        tool_handler=my_handler,
    )
    response = agent.run("+15551234567", "hey whats up")
"""
import json
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from blooio import BlooioClient


class Agent:
    """Conversational AI agent over iMessage with tool-use support."""

    def __init__(
        self,
        system_prompt,
        tools=None,
        tool_handler=None,
        fire_and_forget_tools=None,
        db_path="data/agent.db",
        model="claude-sonnet-4-20250514",
        max_iterations=5,
        max_history=30,
        history_ttl_hours=72,
        message_split_delay=1.5,
        blooio_api_key=None,
    ):
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.tool_handler = tool_handler or (lambda name, args, phone: json.dumps({"error": "no handler"}))
        self.fire_and_forget_tools = fire_and_forget_tools or set()
        self.model = model
        self.max_iterations = max_iterations
        self.max_history = max_history
        self.history_ttl_hours = history_ttl_hours
        self.message_split_delay = message_split_delay
        self.db_path = db_path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        if blooio_api_key:
            self.blooio = BlooioClient(api_key=blooio_api_key)
        else:
            self.blooio = None

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ttl_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone);

            CREATE TABLE IF NOT EXISTS profiles (
                phone TEXT PRIMARY KEY,
                name TEXT,
                facts TEXT DEFAULT '[]',
                status TEXT DEFAULT 'new',
                opted_out BOOLEAN DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                text TEXT NOT NULL,
                direction TEXT DEFAULT 'inbound',
                processed BOOLEAN DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_queue_pending ON message_queue(processed, phone);

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

    # ── Conversation History ────────────────────────────────────────────
    def add_message(self, phone, role, content):
        phone = BlooioClient.format_phone(phone)
        ttl = (datetime.now(timezone.utc) + timedelta(hours=self.history_ttl_hours)).isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO conversations (phone, role, content, created_at, ttl_at) VALUES (?, ?, ?, ?, ?)",
            (phone, role, content, self._now(), ttl))
        conn.commit()
        conn.close()

    def get_history(self, phone, limit=None):
        phone = BlooioClient.format_phone(phone)
        limit = limit or self.max_history
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE phone = ? ORDER BY created_at DESC LIMIT ?",
            (phone, limit)).fetchall()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def cleanup_expired(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM conversations WHERE ttl_at < ?", (self._now(),))
        conn.commit()
        conn.close()

    # ── Profiles ────────────────────────────────────────────────────────
    def get_profile(self, phone):
        phone = BlooioClient.format_phone(phone)
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM profiles WHERE phone = ?", (phone,)).fetchone()
        conn.close()
        if row:
            profile = dict(row)
            profile["facts"] = json.loads(profile.get("facts", "[]"))
            profile["metadata"] = json.loads(profile.get("metadata", "{}"))
            return profile
        return None

    def upsert_profile(self, phone, **kwargs):
        phone = BlooioClient.format_phone(phone)
        # Serialize complex types
        for k in ("facts", "metadata"):
            if k in kwargs and not isinstance(kwargs[k], str):
                kwargs[k] = json.dumps(kwargs[k])
        now = self._now()
        conn = self._get_conn()
        existing = conn.execute("SELECT phone FROM profiles WHERE phone = ?", (phone,)).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(f"UPDATE profiles SET {sets}, updated_at = ? WHERE phone = ?",
                         list(kwargs.values()) + [now, phone])
        else:
            kwargs.setdefault("created_at", now)
            kwargs["updated_at"] = now
            kwargs["phone"] = phone
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            conn.execute(f"INSERT INTO profiles ({cols}) VALUES ({placeholders})", list(kwargs.values()))
        conn.commit()
        conn.close()

    def remember_fact(self, phone, fact):
        profile = self.get_profile(phone) or {}
        facts = profile.get("facts", [])
        if fact not in facts:
            facts.append(fact)
        self.upsert_profile(phone, facts=facts)

    # ── Agent Loop ──────────────────────────────────────────────────────
    def run(self, phone, user_message):
        """Run the Claude tool-use loop for a conversation. Returns response text or None."""
        phone = BlooioClient.format_phone(phone)

        # Check opt-out
        profile = self.get_profile(phone)
        if profile and profile.get("opted_out"):
            return None

        self.add_message(phone, "user", user_message)
        history = self.get_history(phone)

        # Build system prompt with profile context
        system = self.system_prompt
        if profile:
            facts = profile.get("facts", [])
            if facts:
                system += "\n\nwhat we know about this contact:\n" + "\n".join(f"- {f}" for f in facts)
            if profile.get("name"):
                system += f"\ntheir name is {profile['name']}"

        # Get Claude client
        client = self._get_client()
        if not client:
            return None

        messages = history.copy()
        response_texts = []

        for _ in range(self.max_iterations):
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                tools=self.tools,
                messages=messages,
            )

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    response_texts.append(block.text.strip())

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break

            tool_results = []
            has_data_tool = False
            for tool_use in tool_uses:
                result = self.tool_handler(tool_use.name, tool_use.input, phone)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result,
                })
                if tool_use.name not in self.fire_and_forget_tools:
                    has_data_tool = True

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            if not has_data_tool:
                break

        full_response = "\n".join(response_texts)
        if full_response:
            self.add_message(phone, "assistant", full_response)

        return full_response

    def _get_client(self):
        try:
            import anthropic
            return anthropic.Anthropic()
        except Exception:
            return None

    # ── Send Reply ──────────────────────────────────────────────────────
    def send_reply(self, phone, text):
        """Send reply via Blooio, splitting on --- for natural multi-message feel."""
        if not text or not self.blooio:
            return []

        import time
        phone = BlooioClient.format_phone(phone)
        parts = [p.strip() for p in text.split("---") if p.strip()]
        results = []
        for i, part in enumerate(parts):
            results.append(self.blooio.send(phone, part))
            if i < len(parts) - 1:
                time.sleep(self.message_split_delay)
        return results

    # ── Status ──────────────────────────────────────────────────────────
    def get_status(self, phone=None):
        conn = self._get_conn()
        stats = {
            "total_conversations": conn.execute("SELECT COUNT(DISTINCT phone) FROM conversations").fetchone()[0],
            "total_messages": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "queue_pending": conn.execute("SELECT COUNT(*) FROM message_queue WHERE processed = 0").fetchone()[0],
            "profiles_total": conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0],
            "opted_out": conn.execute("SELECT COUNT(*) FROM profiles WHERE opted_out = 1").fetchone()[0],
        }
        if phone:
            phone = BlooioClient.format_phone(phone)
            stats["profile"] = self.get_profile(phone)
            stats["recent_messages"] = self.get_history(phone, limit=5)
        conn.close()
        return stats
