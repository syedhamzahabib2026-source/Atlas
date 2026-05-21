# Slack setup for Atlas (Phase 3)

## 1. Create Slack app

1. https://api.slack.com/apps → Create New App
2. Enable **Socket Mode** → generate `App-Level Token` with `connections:write` → `SLACK_APP_TOKEN`
3. **OAuth & Permissions** → Bot Token Scopes:
   - `chat:write`
   - `commands`
   - `app_mentions:read`
4. Install to workspace → copy `SLACK_BOT_TOKEN`
5. **Slash Commands** → Create `/atlas`:
   - Request URL: placeholder (Socket Mode handles commands)
   - Description: Control Atlas remotely
   - Usage hint: `start <prompt> | stop | status | sessions | logs`

## 2. Environment

```env
ATLAS_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL_ID=C...
SLACK_ALLOWED_USER_IDS=U123,U456
SLACK_ALLOWED_CHANNEL_IDS=C...
```

Empty `SLACK_ALLOWED_*` lists = no restriction on that dimension.

## 3. Run Atlas

```bash
python main.py
```

Invite the bot to your channel. Use `/atlas start Fix the auth bug in login.py`.

## 4. Blocked workflow

When Atlas blocks a task, reply **in the task thread**. Atlas re-queues the task with your clarification appended to the prompt.
