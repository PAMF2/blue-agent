"""
example.py — Example: Build a custom agent using Blue Agent.

This shows how to create a domain-specific conversational agent
with custom tools over iMessage.
"""
import json
import os
from agent import Agent
from server import create_app


# ── Define your tools ───────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "lookup_info",
        "description": "Look up information about a topic the user is asking about.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "Save an important fact about this contact for future conversations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The fact to remember"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "schedule_callback",
        "description": "The contact wants a callback. Save their preferred time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preferred_time": {"type": "string"},
                "topic": {"type": "string"},
            },
        },
    },
    {
        "name": "opt_out",
        "description": "Contact wants to stop receiving messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
        },
    },
]


# ── Tool handler ────────────────────────────────────────────────────────────
def handle_tool(tool_name, args, phone):
    """Handle tool calls from Claude. Return JSON string."""

    if tool_name == "lookup_info":
        # Replace with your actual data source
        return json.dumps({
            "result": f"Here's what I found about '{args['query']}': [your data here]",
            "source": "your_database",
        })

    elif tool_name == "remember":
        agent.remember_fact(phone, args["fact"])
        return json.dumps({"saved": True})

    elif tool_name == "schedule_callback":
        # Replace with your notification system
        print(f"CALLBACK REQUESTED: {phone} wants to talk about {args.get('topic', '?')}")
        return json.dumps({"scheduled": True})

    elif tool_name == "opt_out":
        agent.upsert_profile(phone, opted_out=True)
        return json.dumps({"opted_out": True})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ── Create agent ────────────────────────────────────────────────────────────
agent = Agent(
    system_prompt="""you are a helpful assistant over text. you sound like a real person texting.

rules:
- write in lowercase, casual tone
- keep messages short (2-4 sentences)
- use --- to split into multiple text messages
- never say you're an AI or bot
- if you can't help, offer to have someone call them""",

    tools=TOOLS,
    tool_handler=handle_tool,
    fire_and_forget_tools={"remember", "opt_out"},
    db_path="data/example_agent.db",
    blooio_api_key=os.environ.get("BLOOIO_API_KEY"),
)


# ── Run server ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = create_app(agent)
    print("Example agent starting on port 8787")
    print("Webhook: http://localhost:8787/webhook")
    app.run(host="0.0.0.0", port=8787)
