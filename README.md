# Samsara → Telegram Safety Alert Relay

Small Flask app that receives Samsara webhook events and forwards
safety-related ones to a Telegram channel.

## 1. Create your Telegram bot

1. Message **@BotFather** on Telegram, run `/newbot`, follow the prompts.
2. Save the bot token it gives you 8888992002:AAFcfUGpio6PeSPt0Z9sNK7fhlXgH9GStJ0
(looks like `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`).
3. Create (or use an existing) Telegram **channel**.
4. Add your bot to the channel as an **administrator** (needed for it to post).
5. Get the channel's chat ID:
   - Post any message in the channel.
   - Forward that message to **@userinfobot** (or **@RawDataBot**) — it will show you the channel's numeric ID, -1004496290237
which looks like `-1001234567890`.

## 2. Configure environment variables

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRstuVWXyz"
export TELEGRAM_CHAT_ID="-1001234567890"
export SAMSARA_WEBHOOK_SECRET="your-shared-secret"   # optional but recommended
```

## 3. Run locally to test

```bash
pip install -r requirements.txt
python app.py
```

This starts a server on `http://localhost:5000`. Samsara needs a public
HTTPS URL to send webhooks to, so for local testing use a tunnel tool
like `ngrok`:

```bash
ngrok http 5000
```

Use the `https://xxxx.ngrok.app/samsara-webhook` URL it gives you in the next step.

## 4. Deploy somewhere public (pick one)

Any host that can run a Python/Flask app works. Easiest options:
- **Render.com** – connect your repo, set env vars in the dashboard, it auto-deploys.
- **Railway.app** – same idea, very quick free tier.
- **Fly.io** – `fly launch` then `fly deploy`.
- A small VPS with `gunicorn app:app` behind nginx.

Whatever you choose, you'll end up with a public URL like:
`https://your-app.onrender.com/samsara-webhook`

## 5. Configure the Samsara webhook

1. In Samsara: **Settings → Webhooks → Add Webhook**.
2. URL: your public `/samsara-webhook` endpoint from step 4.
3. Select the event types you want to trigger it — for safety notifications
   look for things like Harsh Event, Safety Score Changed, Dashcam Safety
   Event, Driver Safety Alert (exact names depend on your Samsara plan/features).
4. If Samsara gives you a signing secret, put it in `SAMSARA_WEBHOOK_SECRET`.

## 6. Test it

Trigger a test event from Samsara (most webhook setups have a "Send test
event" button), or wait for a real safety event. You should see a
formatted message appear in your Telegram channel within seconds.

## Customizing

- Edit `SAFETY_EVENT_TYPES` in `app.py` to match the exact event type names
  Samsara sends for your account (check the payload of a test event to confirm).
- Edit `format_message()` to change what shows up in the Telegram message.
