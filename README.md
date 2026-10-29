# NetLink Bot

Telegram bot for corporate secure access management.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
python -m bot.main
```

## NetLink 2.0 pilot enrollment (optional, disabled by default)

The bot can additionally enroll users into the NetLink 2.0 Control Plane
(new device identity + signed config flow). The feature is fully OFF unless
ALL of the following are set (see `.env.example`):

- `NETLINK_V2_ENABLED=true`
- `NETLINK_CONTROL_PLANE_URL` — Control Plane base URL
- `NETLINK_CONTROL_PLANE_INTERNAL_API_KEY` — internal API key (env only,
  never logged, never written to the bot DB)
- `NETLINK_V2_PILOT_TELEGRAM_IDS` — comma-separated allowlist; an empty list
  means NOBODY has access (no wildcards)

When disabled there is no v2 UI and the bot makes no Control Plane requests;
legacy behavior is unchanged. When enabled, only allowlisted (pilot) users
see an extra "Подключить NetLink 2.0" action. Control Plane failures never
affect the legacy flows: the pilot user simply sees a "temporarily
unavailable" message.
