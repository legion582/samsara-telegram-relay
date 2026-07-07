"""
Samsara -> Telegram safety alert relay.

Receives webhook POSTs from Samsara, formats safety-related events,
and forwards them as messages to a Telegram channel via a bot.

Required environment variables:
  TELEGRAM_BOT_TOKEN   - token from @BotFather
  TELEGRAM_CHAT_ID     - target channel/chat id (e.g. -1001234567890)
  SAMSARA_WEBHOOK_SECRET - (optional but recommended) shared secret Samsara
                            sends back so you can verify the request is genuine
"""

import os
import hmac
import base64
import hashlib
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("samsara-telegram-relay")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SAMSARA_WEBHOOK_SECRET = os.environ.get("SAMSARA_WEBHOOK_SECRET")  # optional

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Event types you care about. These must exactly match the event
# subscriptions you enabled in Samsara's webhook configuration
# (Settings > Webhooks > Edit Webhook > Event Subscriptions).
SAFETY_EVENT_TYPES = {
    "SevereSpeedingStarted",
    "SevereSpeedingEnded",
    "SpeedingEventStarted",
    "SpeedingEventEnded",
    "EngineFaultOn",
    "EngineFaultOff",
    "PredictiveMaintenanceAlert",
}


def verify_signature(raw_body: bytes, timestamp: str, signature_header: str) -> bool:
    """
    Verify the request actually came from Samsara using the shared secret.

    Per Samsara's webhook docs (https://developers.samsara.com/docs/webhooks):
      - The secret key shown on the webhook config page is Base64 encoded
        and must be decoded before use.
      - The signature is HMAC-SHA256 over the message: "v1:<timestamp>:<body>"
        where <timestamp> is the X-Samsara-Timestamp header value.
      - The X-Samsara-Signature header value looks like "v1=<hex signature>".

    Skips verification if no secret is configured.
    """
    if not SAMSARA_WEBHOOK_SECRET:
        return True
    if not signature_header or not timestamp:
        return False
    if not signature_header.startswith("v1="):
        return False

    provided_signature = signature_header[len("v1="):]

    try:
        decoded_secret = base64.b64decode(SAMSARA_WEBHOOK_SECRET)
    except Exception:
        log.error("SAMSARA_WEBHOOK_SECRET is not valid base64")
        return False

    message = f"v1:{timestamp}:{raw_body.decode('utf-8')}".encode("utf-8")
    computed = hmac.new(decoded_secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, provided_signature)


def format_message(payload: dict) -> str:
    """Turn a Samsara webhook payload into a readable Telegram message."""
    event_type = payload.get("eventType", "Unknown event")
    data = payload.get("data", {})

    vehicle = data.get("vehicle", {}).get("name") or "Unknown vehicle"
    driver = data.get("driver", {}).get("name") or "Unknown driver"
    happened_at = payload.get("eventTime", "Unknown time")

    lines = [
        f"🚨 *Samsara Safety Alert*",
        f"*Type:* {event_type}",
        f"*Vehicle:* {vehicle}",
        f"*Driver:* {driver}",
        f"*Time:* {happened_at}",
    ]

    # Include a link to the event/dashcam clip if Samsara provides one
    if data.get("url"):
        lines.append(f"[View details]({data['url']})")

    return "\n".join(lines)


def send_to_telegram(text: str):
    resp = requests.post(
        TELEGRAM_API_URL,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
        timeout=10,
    )
    if not resp.ok:
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()


@app.route("/samsara-webhook", methods=["POST"])
def samsara_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Samsara-Signature", "")
    timestamp = request.headers.get("X-Samsara-Timestamp", "")

    if not verify_signature(raw_body, timestamp, signature):
        log.warning("Rejected webhook with invalid signature")
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    event_type = payload.get("eventType")

    log.info("Received Samsara event: %s", event_type)

    if event_type not in SAFETY_EVENT_TYPES:
        # Not a safety event we care about — acknowledge and ignore.
        return jsonify({"status": "ignored"}), 200

    try:
        message = format_message(payload)
        send_to_telegram(message)
    except Exception as exc:
        log.exception("Failed to relay event to Telegram")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "forwarded"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    missing = [
        name
        for name, val in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    try:
        decoded_secret = base64.b64decode(SAMSARA_WEBHOOK_SECRET)
    except Exception:
        log.error("SAMSARA_WEBHOOK_SECRET is not valid base64")
        return False

    message = f"v1:{timestamp}:{raw_body.decode('utf-8')}".encode("utf-8")
    computed = hmac.new(decoded_secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, provided_signature)


def format_message(payload: dict) -> str:
    """Turn a Samsara webhook payload into a readable Telegram message."""
    event_type = payload.get("eventType", "Unknown event")
    data = payload.get("data", {})

    vehicle = data.get("vehicle", {}).get("name", "Unknown vehicle")
    driver = data.get("driver", {}).get("name", "Unknown driver")
    happened_at = payload.get("eventTime", "Unknown time")

    lines = [
        f"🚨 *Samsara Safety Alert*",
        f"*Type:* {event_type}",
        f"*Vehicle:* {vehicle}",
        f"*Driver:* {driver}",
        f"*Time:* {happened_at}",
    ]

    # Include a link to the event/dashcam clip if Samsara provides one
    if "url" in data:
        lines.append(f"[View details]({data['url']})")

    return "\n".join(lines)


def send_to_telegram(text: str):
    resp = requests.post(
        TELEGRAM_API_URL,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
        timeout=10,
    )
    if not resp.ok:
        log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()


@app.route("/samsara-webhook", methods=["POST"])
def samsara_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Samsara-Signature", "")
    timestamp = request.headers.get("X-Samsara-Timestamp", "")

    if not verify_signature(raw_body, timestamp, signature):
        log.warning("Rejected webhook with invalid signature")
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    event_type = payload.get("eventType")

    log.info("Received Samsara event: %s", event_type)

    if event_type not in SAFETY_EVENT_TYPES:
        # Not a safety event we care about — acknowledge and ignore.
        return jsonify({"status": "ignored"}), 200

    try:
        message = format_message(payload)
        send_to_telegram(message)
    except Exception as exc:
        log.exception("Failed to relay event to Telegram")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "forwarded"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    missing = [
        name
        for name, val in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
