from agent import Agent
from server import _process, create_app


def test_processing_message_marks_only_its_queue_row_processed(tmp_path):
    agent = Agent(system_prompt="test", db_path=str(tmp_path / "agent.db"))
    agent.run = lambda _phone, _text: None
    conn = agent._get_conn()
    conn.execute(
        "INSERT INTO message_queue (phone, text, direction, created_at) VALUES (?, ?, ?, ?)",
        ("+15551234567", "first", "inbound", agent._now()))
    first_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO message_queue (phone, text, direction, created_at) VALUES (?, ?, ?, ?)",
        ("+15551234567", "second", "inbound", agent._now()))
    conn.commit()
    conn.close()

    _process(agent, "+15551234567", "first", first_id)

    conn = agent._get_conn()
    pending = conn.execute(
        "SELECT text FROM message_queue WHERE processed = 0").fetchall()
    conn.close()
    assert [row["text"] for row in pending] == ["second"]


def test_webhook_dispatches_each_committed_queue_id(tmp_path, monkeypatch):
    agent = Agent(system_prompt="test", db_path=str(tmp_path / "agent.db"))
    agent.run = lambda _phone, _text: None
    dispatched = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            dispatched.append(self)

    monkeypatch.setattr("server.threading.Thread", DeferredThread)
    client = create_app(agent).test_client()
    phone = "+15551234567"
    for text in ("first", "second"):
        response = client.post("/webhook", json={"from": phone, "text": text})
        assert response.status_code == 200
        assert response.get_json() == {"status": "queued"}

    conn = agent._get_conn()
    rows = conn.execute(
        "SELECT id, text, processed FROM message_queue ORDER BY id").fetchall()
    conn.close()
    assert [(row["text"], row["processed"]) for row in rows] == [
        ("first", 0), ("second", 0)]
    assert len(dispatched) == 2
    for thread, row in zip(dispatched, rows):
        assert thread.target is _process
        assert thread.args == (agent, phone, row["text"], row["id"])
        assert thread.daemon is True

    dispatched[0].target(*dispatched[0].args)

    conn = agent._get_conn()
    states = conn.execute(
        "SELECT id, processed FROM message_queue ORDER BY id").fetchall()
    conn.close()
    assert [(row["id"], row["processed"]) for row in states] == [
        (rows[0]["id"], 1), (rows[1]["id"], 0)]
