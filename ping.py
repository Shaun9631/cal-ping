"""
cal-ping: watches a public availability API and pings Discord/Telegram when
a new opening shows up inside a date window. Read-only; never books anything.
All targets (API url, link template, entries) come from config / APP_CONFIG.

Usage:
  python ping.py            # run forever, checking every N seconds
  python ping.py --once     # check once and exit (for cron / GitHub Actions)
  python ping.py --test     # send a test notification and exit
  python ping.py --show     # print what's open right now, no alerts

Secrets (env vars or a .env file next to this script):
  DISCORD_WEBHOOK_URL
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
"""
import json
import os
import random
import sys
import time
import urllib.request
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(name, default):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(name, data):
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def post_json(url, payload, timeout=20):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}


def fetch_slots(doc, start, end):
    """Return list of slots for one entry between start and end (YYYY-MM-DD)."""
    payload = {
        "provider": doc["npi"],
        "providerType": "NPI",
        "providerGroup": "pcg",
        "startDate": start,
        "endDate": end,
        "visitTypes": [{"id": vt, "method": m} for vt, m in doc["visit_types"]],
        "departments": [{"id": doc["dept"], "type": "External"}],
        "facets": ["date"],
        "limit": 100,
        "lookahead": False,
    }
    data = post_json(CFG["api_url"], payload)
    out = []
    for res in data.get("_result", []):
        for s in (res.get("availability") or {}).get("slots", []) or []:
            if not (start <= s["date"] <= end):
                continue
            out.append({
                "date": s["date"],
                "start": s["start"],
                "method": s.get("visit_method", "in_person"),
                "vt": s.get("visit_type") or doc["visit_types"][0][0],
            })
    return out


def pretty_time(d, t):
    dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


CFG = {}


def check(cfg):
    """Return {name: (doc, [slots])} for everything open in the window."""
    today = date.today().isoformat()
    start = max(cfg["window_start"], today)
    end = cfg["window_end"]
    found = {}
    if start > end:
        return found
    max_rank = cfg.get("alert_max_rank", 99)
    for doc in sorted(cfg["items"], key=lambda d: d["rank"]):
        if doc["rank"] > max_rank:
            continue
        try:
            slots = fetch_slots(doc, start, end)
        except Exception as e:  # network hiccup: log and move on
            print(f"  ! {doc['name']}: {e}", flush=True)
            continue
        if slots:
            found[doc["name"]] = (doc, sorted(slots, key=lambda s: (s["date"], s["start"])))
        time.sleep(random.uniform(1.0, 3.0))  # be polite between entries
    return found


def build_message(cfg, new_by_doc):
    lines = [f"🔔 New opening(s) before {cfg.get('current_booking', 'your current one')}!"]
    for name, (doc, slots) in sorted(new_by_doc.items(), key=lambda kv: kv[1][0]["rank"]):
        lines.append(f"\n#{doc['rank']} {name} — {doc['where']}")
        for s in slots[:6]:
            vid = " (video)" if s["method"] == "video" else ""
            lines.append(f"  • {pretty_time(s['date'], s['start'])}{vid}")
        if len(slots) > 6:
            lines.append(f"  • …and {len(slots) - 6} more")
        first = slots[0]
        lines.append("  Book: " + cfg["link_template"].format(vt=first["vt"], npi=doc["npi"], dept=doc["dept"], d=first["date"]))
    lines.append(f"\nBooking is up to you. If you switch, cancel {cfg.get('current_booking', 'the old one')}.")
    return "\n".join(lines)


def notify(text):
    sent = False
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            for i in range(0, len(text), 1900):  # Discord 2000-char limit
                post_json(hook, {"content": text[i:i + 1900]})
            sent = True
        except Exception as e:
            print(f"  ! Discord failed: {e}", flush=True)
    btok, chan = os.environ.get("DISCORD_BOT_TOKEN"), os.environ.get("DISCORD_CHANNEL_ID")
    if btok and chan:
        try:
            for i in range(0, len(text), 1900):
                req = urllib.request.Request(
                    f"https://discord.com/api/v10/channels/{chan}/messages",
                    data=json.dumps({"content": text[i:i + 1900]}).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bot {btok}",
                             "User-Agent": "DiscordBot (cal-ping, 1.0)"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:
            print(f"  ! Discord bot failed: {e}", flush=True)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            for i in range(0, len(text), 4000):
                post_json(f"https://api.telegram.org/bot{tok}/sendMessage",
                          {"chat_id": chat, "text": text[i:i + 4000], "disable_web_page_preview": True})
            sent = True
        except Exception as e:
            print(f"  ! Telegram failed: {e}", flush=True)
    if not sent:
        print("  (no Discord/Telegram configured — printing instead)\n" + text, flush=True)


def run_once(cfg, alert=True):
    state = load_json("state.json", {"seen": []})
    seen = set(state["seen"])
    found = check(cfg)
    stamp = datetime.now().strftime("%H:%M:%S")

    new_by_doc = {}
    for name, (doc, slots) in found.items():
        fresh = [s for s in slots if f"{doc['npi']}|{s['date']}|{s['start']}|{s['method']}" not in seen]
        if fresh:
            new_by_doc[name] = (doc, fresh)

    summary = ", ".join(f"#{d['rank']} {n.split(',')[0]}: {len(s)}" for n, (d, s) in found.items()) or "nothing"
    print(f"[{stamp}] open in window -> {summary}", flush=True)

    if alert and new_by_doc:
        notify(build_message(cfg, new_by_doc))

    # Remember only currently-open slots, so a slot that disappears and comes back re-alerts.
    state["seen"] = sorted(
        f"{d['npi']}|{s['date']}|{s['start']}|{s['method']}" for d, sl in found.values() for s in sl
    )
    if alert:
        save_json("state.json", state)
    return found


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_env()
    # On GitHub Actions the config comes from the APP_CONFIG secret so the public repo
    # never contains your entries/dates. Locally it falls back to config.json.
    cfg = json.loads(os.environ["APP_CONFIG"]) if os.environ.get("APP_CONFIG") else load_json("config.json", None)
    if cfg is None:
        sys.exit("config.json not found")
    CFG.update(cfg)
    args = set(sys.argv[1:])

    if "--test" in args:
        notify("✅ cal-ping test message — notifications are working.")
        return
    if "--show" in args:
        found = check(cfg)
        if not found:
            print("Nothing open in the window right now.")
        for name, (doc, slots) in sorted(found.items(), key=lambda kv: kv[1][0]["rank"]):
            print(f"#{doc['rank']} {name}: " + ", ".join(pretty_time(s["date"], s["start"]) for s in slots))
        return
    if "--once" in args:
        run_once(cfg)
        return

    print(f"Watching {len(cfg['items'])} entries for {cfg['window_start']} .. {cfg['window_end']} "
          f"every ~{cfg['check_every_seconds']}s. Ctrl+C to stop.", flush=True)
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            print(f"  ! check failed: {e}", flush=True)
        if date.today().isoformat() > cfg["window_end"]:
            print("Window has passed — stopping.")
            return
        base = cfg["check_every_seconds"]
        time.sleep(base + random.uniform(-0.2, 0.3) * base)


if __name__ == "__main__":
    main()
