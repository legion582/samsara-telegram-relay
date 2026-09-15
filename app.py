"""
Samsara -> Telegram relay for Western Cargo
- Verifies Samsara webhook signatures
- Sends instant text alerts to a Telegram group (geofence entry, stopped vehicle, safety events)
- For selected safety behaviors (speeding, rolling stop, etc.), retrieves dashcam video
  via the Samsara Media Retrieval API and sends the clip to the group when it's ready.

Required environment variables (Render -> Environment):
  SAMSARA_SIGNING_SECRET   webhook signing secret from Samsara (base64 string as shown)
  SAMSARA_API_TOKEN        API token with: Read Safety Events & Scores, Read Camera Media,
                           Write Media Retrieval, Read Media Retrieval
  TELEGRAM_BOT_TOKEN       your bot token (keep it ONLY here - never in code)
  TELEGRAM_CHAT_ID         group chat id, e.g. -1001234567890

Optional:
  DATABASE_URL             Render Postgres URL. If set, pending video jobs survive
                           restarts/redeploys. If not set, jobs are held in memory
                           (a redeploy during the wait window will drop them).
  VIDEO_BEHAVIORS          comma-separated keywords that trigger video retrieval.
                           Default: "speeding,rolling stop,stop sign,ran red light,harsh"
  VIDEO_DELAY_MINUTES      how long to wait before requesting footage (default 5)
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, abort

app = Flask(__name__)

SAMSARA_SIGNING_SECRET = os.environ.get("SAMSARA_SIGNING_SECRET", "")
SAMSARA_API_TOKEN = os.environ.get("SAMSARA_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
VIDEO_DELAY_MINUTES = int(os.environ.get("VIDEO_DELAY_MINUTES", "5"))

VIDEO_BEHAVIORS = [
    s.strip().lower()
    for s in os.environ.get(
        "VIDEO_BEHAVIORS",
        "speeding,rolling stop,stop sign,ran red light,harsh",
    ).split(",")
    if s.strip()
]

LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "America/New_York"))  # Eastern time

# Skip "...Ended" events by default - the Started alert already told the group
SKIP_ENDED = os.environ.get("SKIP_ENDED", "true").lower() == "true"

# Friendly labels + emojis for common event types
EVENT_STYLES = [
    ("severespeeding", "🚨🏎 SEVERE SPEEDING"),
    ("speeding",       "⚠️🏎 Speeding"),
    ("harshbrak",      "⚠️🛑 Harsh Braking"),
    ("harshaccel",     "⚠️💨 Harsh Acceleration"),
    ("harshturn",      "⚠️↩️ Harsh Turn"),
    ("rolling stop",   "🚫🛑 Rolling Stop"),
    ("stop sign",      "🚫🛑 Ran Stop Sign"),
    ("red light",      "🚦❌ Ran Red Light"),
    ("crash",          "💥 POSSIBLE CRASH"),
    ("geofence entry", "🚧 ENTERED RESTRICTED ZONE"),
    ("geofence exit",  "🚧 Left Zone"),
    ("geofence",       "🚧 Restricted Zone Alert"),
    ("stopped",        "🅿️⏱ Stopped 15+ min"),
    ("stops moving",   "🅿️⏱ Stopped 15+ min"),
    ("low bridge",     "🌉⚠️ LOW BRIDGE AHEAD"),
    ("idle",           "🅿️⏱ Idling Alert"),
    ("phone",          "📵 Phone Use While Driving"),
    ("seatbelt",       "🔴 No Seatbelt"),
    ("tailgating",     "↔️ Tailgating"),
    ("following",      "↔️ Following Too Close"),
]

# Dedupe: don't send the same (vehicle, label) twice within this many seconds
DEDUPE_SECONDS = int(os.environ.get("DEDUPE_SECONDS", "120"))
_recent_alerts = {}
_recent_lock = threading.Lock()

SAMSARA_API = "https://api.samsara.com"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024  # 50 MB bot upload limit


# ---------------------------------------------------------------------------
# Job store: Postgres if DATABASE_URL is set, otherwise in-memory fallback
# ---------------------------------------------------------------------------

_mem_jobs = []
_mem_lock = threading.Lock()
_pg_conn = None


def _pg():
    """Return a live psycopg2 connection, reconnecting if needed."""
    global _pg_conn
    import psycopg2

    if _pg_conn is None or _pg_conn.closed:
        _pg_conn = psycopg2.connect(DATABASE_URL)
        _pg_conn.autocommit = True
    return _pg_conn


def init_store():
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set - video jobs held in memory only.")
        return
    with _pg().cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS video_jobs (
                id SERIAL PRIMARY KEY,
                vehicle_id TEXT,
                vehicle_name TEXT,
                behavior TEXT,
                event_time TIMESTAMPTZ,
                next_check TIMESTAMPTZ,
                retrieval_id TEXT,
                attempts INT DEFAULT 0,
                status TEXT DEFAULT 'pending'
            )
            """
        )


def add_job(vehicle_id, vehicle_name, behavior, event_time):
    next_check = datetime.now(timezone.utc) + timedelta(minutes=VIDEO_DELAY_MINUTES)
    if DATABASE_URL:
        with _pg().cursor() as cur:
            cur.execute(
                "INSERT INTO video_jobs (vehicle_id, vehicle_name, behavior, event_time, next_check)"
                " VALUES (%s, %s, %s, %s, %s)",
                (vehicle_id, vehicle_name, behavior, event_time, next_check),
            )
    else:
        with _mem_lock:
            _mem_jobs.append(
                {
                    "id": len(_mem_jobs) + 1,
                    "vehicle_id": vehicle_id,
                    "vehicle_name": vehicle_name,
                    "behavior": behavior,
                    "event_time": event_time,
                    "next_check": next_check,
                    "retrieval_id": None,
                    "attempts": 0,
                    "status": "pending",
                }
            )


def due_jobs():
    now = datetime.now(timezone.utc)
    if DATABASE_URL:
        with _pg().cursor() as cur:
            cur.execute(
                "SELECT id, vehicle_id, vehicle_name, behavior, event_time, retrieval_id, attempts"
                " FROM video_jobs WHERE status IN ('pending','requested') AND next_check <= %s",
                (now,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0], "vehicle_id": r[1], "vehicle_name": r[2],
                "behavior": r[3], "event_time": r[4],
                "retrieval_id": r[5], "attempts": r[6],
            }
            for r in rows
        ]
    with _mem_lock:
        return [dict(j) for j in _mem_jobs
                if j["status"] in ("pending", "requested") and j["next_check"] <= now]


def update_job(job_id, **fields):
    if DATABASE_URL:
        sets = ", ".join(f"{k} = %s" for k in fields)
        with _pg().cursor() as cur:
            cur.execute(
                f"UPDATE video_jobs SET {sets} WHERE id = %s",
                list(fields.values()) + [job_id],
            )
    else:
        with _mem_lock:
            for j in _mem_jobs:
                if j["id"] == job_id:
                    j.update(fields)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_send_text(text):
    try:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            print("Telegram sendMessage failed:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram sendMessage error:", e)


def tg_send_video(url, caption):
    """Download the clip and upload to Telegram; fall back to sending the link."""
    try:
        vid = requests.get(url, timeout=120)
        vid.raise_for_status()
        if len(vid.content) <= TELEGRAM_MAX_UPLOAD:
            r = requests.post(
                f"{TELEGRAM_API}/sendVideo",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"video": ("clip.mp4", vid.content, "video/mp4")},
                timeout=180,
            )
            if r.ok:
                return True
            print("Telegram sendVideo failed:", r.status_code, r.text[:300])
        # Too big or upload failed: send the (8-hour) link instead
        tg_send_text(f"{caption}\nVideo (link expires in 8h): {url}")
        return True
    except Exception as e:
        print("tg_send_video error:", e)
        tg_send_text(f"{caption}\nVideo (link expires in 8h): {url}")
        return True


# ---------------------------------------------------------------------------
# Samsara signature verification
# ---------------------------------------------------------------------------

def verify_signature(req):
    timestamp = req.headers.get("X-Samsara-Timestamp", "")
    signature = req.headers.get("X-Samsara-Signature", "")
    if not timestamp or not signature:
        return False
    secret = base64.b64decode(SAMSARA_SIGNING_SECRET)
    message = b"v1:" + timestamp.encode() + b":" + req.get_data()
    expected = "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Webhook parsing
# ---------------------------------------------------------------------------

def dig(d, *keys, default=None):
    """Safely walk nested dicts and lists."""
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        elif isinstance(d, list) and isinstance(k, int) and len(d) > k:
            d = d[k]
        else:
            return default
    return d


def find_all(node, wanted_key, _depth=0):
    """Recursively find every dict stored under `wanted_key` anywhere in the payload."""
    results = []
    if _depth > 8:
        return results
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == wanted_key and isinstance(v, dict):
                results.append(v)
            results.extend(find_all(v, wanted_key, _depth + 1))
    elif isinstance(node, list):
        for item in node:
            results.extend(find_all(item, wanted_key, _depth + 1))
    return results


def find_value(node, wanted_keys, _depth=0):
    """Recursively find the first non-empty value stored under any of `wanted_keys`."""
    if _depth > 8:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if k in wanted_keys and v not in (None, "", {}):
                return v
        for v in node.values():
            found = find_value(v, wanted_keys, _depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_value(item, wanted_keys, _depth + 1)
            if found is not None:
                return found
    return None


_vehicle_name_cache = {}


def lookup_vehicle_name(vehicle_id):
    """If the webhook didn't include the truck name, ask the Samsara API for it."""
    if not vehicle_id or not SAMSARA_API_TOKEN:
        return None
    if vehicle_id in _vehicle_name_cache:
        return _vehicle_name_cache[vehicle_id]
    try:
        r = requests.get(f"{SAMSARA_API}/fleet/vehicles/{vehicle_id}",
                         headers=samsara_headers(), timeout=10)
        if r.ok:
            name = dig(r.json(), "data", "name")
            if name:
                _vehicle_name_cache[vehicle_id] = name
                return name
    except Exception as e:
        print("vehicle lookup error:", e)
    return None


def pretty_label(raw):
    """Match an event type/description to a friendly emoji label."""
    text = (raw or "").lower()
    squashed = text.replace(" ", "")
    for keyword, label in EVENT_STYLES:
        if keyword in text or keyword.replace(" ", "") in squashed:
            return label
    # Fallback: split CamelCase into words, e.g. SpeedingEventStarted -> Speeding Event Started
    words = []
    current = ""
    for ch in raw or "Alert":
        if ch.isupper() and current:
            words.append(current)
            current = ch
        else:
            current += ch
    if current:
        words.append(current)
    return "🔔 " + " ".join(words)


def extract_alert(payload):
    """
    Pull the useful fields out of a Samsara alert webhook.
    Samsara has a few payload shapes (Alert, AlertIncident, safety events);
    this searches the whole payload recursively so nothing is missed.
    """
    event_type = payload.get("eventType", "")
    data = payload.get("data") or payload.get("event") or payload

    info = {
        "kind": event_type,
        "vehicle_id": None,
        "vehicle_name": None,
        "driver_name": None,
        "behavior": None,
        "lat": None,
        "lng": None,
        "description": None,
        "event_time": payload.get("eventTime") or payload.get("eventMs"),
    }

    # Condition / trigger description
    info["description"] = (
        find_value(data, {"alertConditionDescription", "configurationDescription"})
        or find_value(data, {"description"})
        or find_value(data, {"eventType", "type"})
        or event_type
    )

    # Vehicle - check every "vehicle"/"device"/"asset" object anywhere in the payload
    for v in (find_all(data, "vehicle") + find_all(data, "device")
              + find_all(data, "asset")):
        info["vehicle_id"] = info["vehicle_id"] or v.get("id")
        info["vehicle_name"] = info["vehicle_name"] or v.get("name")
    if not info["vehicle_id"]:
        info["vehicle_id"] = find_value(data, {"vehicleId", "assetId", "deviceId"})
    if not info["vehicle_name"] and info["vehicle_id"]:
        info["vehicle_name"] = lookup_vehicle_name(str(info["vehicle_id"]))

    # Driver
    for d in find_all(data, "driver"):
        info["driver_name"] = info["driver_name"] or d.get("name")
    if not info["driver_name"]:
        info["driver_name"] = find_value(data, {"driverName"})

    # Behavior label (safety events)
    info["behavior"] = find_value(data, {"behaviorLabel", "behaviorLabels"})
    if isinstance(info["behavior"], list):
        info["behavior"] = ", ".join(
            b.get("label", str(b)) if isinstance(b, dict) else str(b)
            for b in info["behavior"]
        )

    # Location - any location/gps/address object with coordinates
    for loc in (find_all(data, "location") + find_all(data, "gps")
                + find_all(data, "address")):
        info["lat"] = info["lat"] or loc.get("latitude") or loc.get("lat")
        info["lng"] = info["lng"] or loc.get("longitude") or loc.get("lng")
    if not info["lat"]:
        info["lat"] = find_value(data, {"latitude"})
        info["lng"] = find_value(data, {"longitude"})

    return info


def wants_video(info):
    text = " ".join(filter(None, [info.get("behavior"), info.get("description")])).lower()
    return any(k in text for k in VIDEO_BEHAVIORS)


def local_time_str(event_time=None):
    """Format the event time in Texas time, e.g. '09/14 08:18 PM CT'."""
    t = None
    try:
        if isinstance(event_time, str):
            t = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        elif isinstance(event_time, (int, float)):
            t = datetime.fromtimestamp(event_time / 1000, tz=timezone.utc)
    except Exception:
        t = None
    if t is None:
        t = datetime.now(timezone.utc)
    return t.astimezone(LOCAL_TZ).strftime("%m/%d %I:%M %p ET")


def format_alert(info):
    label = pretty_label(info.get("behavior") or info.get("description")
                         or info.get("kind"))
    lines = [f"<b>{label}</b>"]
    if info.get("vehicle_name"):
        lines.append(f"🚛 Truck: <b>{info['vehicle_name']}</b>")
    elif info.get("vehicle_id"):
        lines.append(f"🚛 Truck ID: {info['vehicle_id']}")
    if info.get("driver_name"):
        lines.append(f"👤 Driver: <b>{info['driver_name']}</b>")
    if info.get("lat") and info.get("lng"):
        lines.append(
            f'📍 <a href="https://www.google.com/maps?q={info["lat"]},{info["lng"]}">Open location on map</a>'
        )
    lines.append(f"🕐 {local_time_str(info.get('event_time'))}")
    return "\n".join(lines)


def is_duplicate(info):
    """True if we already sent an alert for this (vehicle, event) very recently."""
    key = (str(info.get("vehicle_id") or info.get("vehicle_name") or "?"),
           pretty_label(info.get("behavior") or info.get("description") or ""))
    now = time.time()
    with _recent_lock:
        # drop old entries
        for k in [k for k, v in _recent_alerts.items() if now - v > DEDUPE_SECONDS]:
            del _recent_alerts[k]
        if key in _recent_alerts:
            return True
        _recent_alerts[key] = now
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return "ok", 200


def enrich_safety_event(info):
    """
    Samsara's safety-event webhooks only say 'a safety event occurred' - the
    behavior (harsh braking, following distance, ...) and location must be
    fetched from the Safety Events API. Retries a few times because the event
    can take up to ~1 min to appear in the API after the webhook fires.
    """
    if not SAMSARA_API_TOKEN:
        return
    for attempt in range(4):  # ~0s, 15s, 30s, 45s
        if attempt:
            time.sleep(15)
        try:
            now = datetime.now(timezone.utc)
            params = {
                "startTime": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            r = requests.get(f"{SAMSARA_API}/fleet/safety-events",
                             headers=samsara_headers(), params=params, timeout=15)
            if not r.ok:
                print("safety-events fetch failed:", r.status_code, r.text[:200])
                continue
            events = dig(r.json(), "data", default=[]) or []
            best = None
            for ev in events:
                v = find_all(ev, "vehicle")
                v_id = str(v[0].get("id")) if v else str(find_value(ev, {"vehicleId"}) or "")
                v_name = (v[0].get("name") if v else None) or ""
                if (info.get("vehicle_id") and v_id == str(info["vehicle_id"])) or \
                   (info.get("vehicle_name") and v_name == info["vehicle_name"]):
                    best = ev  # events are time-ordered; keep the last match
            if best:
                labels = find_value(best, {"behaviorLabels", "behaviorLabel"})
                if isinstance(labels, list):
                    names = [b.get("name") or b.get("label", "") if isinstance(b, dict)
                             else str(b) for b in labels]
                    info["behavior"] = ", ".join(n for n in names if n) or info["behavior"]
                elif labels:
                    info["behavior"] = str(labels)
                for loc in find_all(best, "location"):
                    info["lat"] = info["lat"] or loc.get("latitude") or loc.get("lat")
                    info["lng"] = info["lng"] or loc.get("longitude") or loc.get("lng")
                if not info.get("driver_name"):
                    d = find_all(best, "driver")
                    if d:
                        info["driver_name"] = d[0].get("name")
                if info.get("behavior"):
                    return
        except Exception as e:
            print("enrich error:", e)


def process_alert(info):
    """Runs in a background thread so the webhook can answer Samsara instantly."""
    try:
        desc = (info.get("description") or "").lower()
        if "safety event" in desc and not info.get("behavior"):
            enrich_safety_event(info)

        tg_send_text(format_alert(info))

        if wants_video(info) and info.get("vehicle_id"):
            event_time = info.get("event_time")
            try:
                if isinstance(event_time, str):
                    event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                elif isinstance(event_time, (int, float)):
                    event_time = datetime.fromtimestamp(event_time / 1000, tz=timezone.utc)
                else:
                    event_time = datetime.now(timezone.utc)
            except Exception:
                event_time = datetime.now(timezone.utc)
            add_job(info["vehicle_id"], info.get("vehicle_name") or "?",
                    info.get("behavior") or info.get("description") or "violation",
                    event_time)
    except Exception as e:
        print("process_alert error:", e)


@app.route("/webhook", methods=["POST"])
def webhook():
    if SAMSARA_SIGNING_SECRET and not verify_signature(request):
        abort(401)

    payload = request.get_json(silent=True) or {}
    event_type = payload.get("eventType", "")

    if event_type == "Ping":
        return "", 200

    info = extract_alert(payload)

    # Skip "...Ended" events - the group already saw the Started alert
    raw_text = " ".join(filter(None, [
        str(info.get("kind") or ""), str(info.get("description") or "")])).lower()
    if SKIP_ENDED and "ended" in raw_text:
        return "", 200

    # Skip duplicates (e.g. Speeding + SevereSpeeding firing for the same moment)
    if is_duplicate(info):
        return "", 200

    # Enrich + send in the background so Samsara gets an instant 200 OK
    threading.Thread(target=process_alert, args=(info,), daemon=True).start()

    return "", 200


# ---------------------------------------------------------------------------
# Background worker: request + poll dashcam footage
# ---------------------------------------------------------------------------

def samsara_headers():
    return {"Authorization": f"Bearer {SAMSARA_API_TOKEN}"}


def request_media(job):
    """Ask Samsara for ~20s of forward dashcam video around the event."""
    start = job["event_time"] - timedelta(seconds=8)
    end = job["event_time"] + timedelta(seconds=12)
    body = {
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vehicleId": str(job["vehicle_id"]),
        "inputs": ["dashcamForward"],
        "mediaType": "videoLowRes",   # low-res keeps files under Telegram's 50MB cap
    }
    r = requests.post(f"{SAMSARA_API}/cameras/media/retrieval",
                      headers=samsara_headers(), json=body, timeout=30)
    if r.ok:
        rid = dig(r.json(), "data", "retrievalId")
        print(f"Job {job['id']}: media requested, retrievalId={rid}")
        return rid
    print(f"Job {job['id']}: media request failed {r.status_code} {r.text[:300]}")
    return None


def poll_media(job):
    """Check whether the requested clip is ready; send it if so. Returns True when done."""
    r = requests.get(f"{SAMSARA_API}/cameras/media/retrieval",
                     headers=samsara_headers(),
                     params={"retrievalId": job["retrieval_id"]}, timeout=30)
    if not r.ok:
        print(f"Job {job['id']}: poll failed {r.status_code} {r.text[:300]}")
        return False
    for m in dig(r.json(), "data", "media", default=[]) or []:
        if m.get("status", "").lower() == "available":
            url = dig(m, "urlInfo", "url") or m.get("url")
            if url:
                caption = (f"🎥 {job['behavior']} — {job['vehicle_name']} "
                           f"({job['event_time'].strftime('%m/%d %H:%M UTC')})")
                tg_send_video(url, caption)
                return True
    return False


def worker_loop():
    while True:
        try:
            for job in due_jobs():
                attempts = job["attempts"] + 1
                if attempts > 20:  # ~ up to a few hours of retries, then give up
                    update_job(job["id"], status="failed")
                    tg_send_text(
                        f"⚠️ Could not retrieve video for {job['behavior']} — "
                        f"{job['vehicle_name']}. Camera may be offline; "
                        f"check Samsara Video Library."
                    )
                    continue
                if not job["retrieval_id"]:
                    rid = request_media(job)
                    if rid:
                        update_job(job["id"], retrieval_id=rid, status="requested",
                                   attempts=attempts,
                                   next_check=datetime.now(timezone.utc) + timedelta(minutes=2))
                    else:
                        update_job(job["id"], attempts=attempts,
                                   next_check=datetime.now(timezone.utc) + timedelta(minutes=5))
                else:
                    if poll_media(job):
                        update_job(job["id"], status="done", attempts=attempts)
                    else:
                        update_job(job["id"], attempts=attempts,
                                   next_check=datetime.now(timezone.utc) + timedelta(minutes=3))
        except Exception as e:
            print("worker error:", e)
        time.sleep(60)


init_store()
threading.Thread(target=worker_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
