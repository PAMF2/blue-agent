from agent import Agent
from server import _process


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
