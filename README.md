# Blue Agent

AI-powered conversational agent over iMessage. Built on [Blooio](https://blooio.com) for messaging and Claude for intelligence.

Text a phone number → AI agent responds naturally → qualifies leads, answers questions, books calls. The person on the other end thinks they're texting a real human.

## Architecture

```
Contact texts your Blooio number
    ↓ instant webhook
Flask server validates + enqueues
    ↓ background thread
Claude tool-use loop (up to 5 iterations)
    ↓
Reply sent via Blooio (split across messages for natural feel)
```

## Quick Start

```bash
pip install -r requirements.txt

# Set your API keys
export BLOOIO_API_KEY="your_key"
export ANTHROPIC_API_KEY="your_key"

# Start the server
python server.py --port 8787

# Expose publicly (pick one)
ngrok http 8787
# or: cloudflared tunnel --url http://localhost:8787

# Register the webhook
python server.py --register-webhook https://your-domain.com/webhook --api-key $BLOOIO_API_KEY
```

## Build Your Own Agent

```python
from agent import Agent
from server import create_app

# Define tools Claude can use
tools = [
    {
        "name": "check_inventory",
        "description": "Check if a product is in stock",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
            },
            "required": ["product"],
        },
    },
]

# Handle tool calls
def handle_tool(name, args, phone):
    if name == "check_inventory":
        return '{"in_stock": true, "quantity": 5}'

# Create agent
agent = Agent(
    system_prompt="you are a helpful shop assistant over text...",
    tools=tools,
    tool_handler=handle_tool,
    blooio_api_key="your_key",
)

# Run
app = create_app(agent)
app.run(port=8787)
```

## Features

- **Instant response** — Blooio webhooks, not polling
- **Conversational** — Claude tool-use loop adapts to what the contact says
- **Persistent memory** — conversation history (configurable TTL) + permanent contact profiles
- **Natural feel** — messages split on `---`, sent with delays between parts
- **HMAC verification** — webhook signatures validated
- **Contact profiles** — remember facts, track status, opt-out handling
- **Fire-and-forget tools** — tools like "remember" execute without re-calling Claude

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server status + stats |
| `/webhook` | POST | Blooio inbound webhook |
| `/send` | POST | Manual send (testing) |
| `/conversation/<phone>` | GET | View conversation history |

## Agent Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `system_prompt` | required | The agent's personality and instructions |
| `tools` | `[]` | Claude tool definitions |
| `tool_handler` | required | Function handling tool calls: `(name, args, phone) → json_str` |
| `fire_and_forget_tools` | `set()` | Tool names that don't loop back to Claude |
| `model` | `claude-sonnet-4-20250514` | Claude model to use |
| `max_iterations` | `5` | Max tool-use loop iterations |
| `max_history` | `30` | Messages to include as context |
| `history_ttl_hours` | `72` | Conversation history expiry |
| `message_split_delay` | `1.5` | Seconds between multi-part messages |
| `db_path` | `data/agent.db` | SQLite database path |

## How It Works

1. **Webhook receives message** → validates HMAC → enqueues in SQLite
2. **Background thread** picks up message → loads conversation history + contact profile
3. **Claude runs** with system prompt + history + tools (max 5 iterations)
4. **Tools execute** — data tools loop back to Claude, fire-and-forget tools don't
5. **Response sent** via Blooio — split on `---` with natural delays
6. **State saved** — conversation history + any profile updates

Each inbound message has its own SQLite queue ID, passed to its background
processor. After processing and any reply send return without an exception,
only that queue row is marked processed; other pending messages from the same
phone remain pending.

## License

MIT
