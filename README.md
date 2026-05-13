# riolu

Riolu is a private Telegram intel scraper that pulls recent items from:

- <https://hellogithub.com/>
- <https://talkback.sh/>

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Set `TELEGRAM_TOKEN` in `.env` using the token Telegram gives you.

To restrict access, set `ALLOWED_CHAT_IDS` to a comma-separated list of chat IDs.

## Run

```bash
riolu
```

## Telegram Commands

- `/start` - show the command summary
- `/help` - show the command summary
- `/latest [limit]` - scrape both sources
- `/hellogithub [limit]` - scrape HelloGitHub
- `/talkback [limit]` - scrape Talkback

`limit` defaults to `DEFAULT_LIMIT` and is capped at 10.
