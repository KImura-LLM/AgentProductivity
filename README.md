# Personal Productivity AI Agent

Telegram-first productivity agent for Notion, TickTick, and OpenAI.

## Setup

1. Copy `.env.example` to `.env` and fill in tokens.
2. Configure Notion databases with `NOTION_DATABASES_JSON` or the legacy `NOTION_TASKS_DATABASE_ID`.
3. Install dependencies:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

4. For TickTick OAuth:

```bash
agent-productivity-cli doctor
agent-productivity-cli ticktick-auth-url
agent-productivity-cli ticktick-exchange-code <code-from-redirect>
```

5. Run the bot:

```bash
agent-productivity
```

## Docker

```bash
docker compose up -d --build
```

The container reads `.env` and stores local OAuth/pending-action state in `.state/`.

## Commands

`/today`, `/briefing`, `/week`, `/review`, `/project <name>`, `/life`, `/add`, `/done`, `/reschedule`, `/focus`, `/stuck`, `/settings`.

Write operations create a pending confirmation first. Reply `да` to execute or `нет` / `отмена` to cancel.
