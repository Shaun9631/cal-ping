"""
cal-ping: opens public booking pages in a headless browser, reads the times
they offer, and pings Discord/Telegram when a new opening shows up inside a
date window. Read-only; never clicks a time or books anything.
All targets (link template, entries) come from config / APP_CONFIG.

Usage:
  python ping.py            # run forever, checking every N seconds
  python ping.py --once     # check once and exit (for cron / GitHub Actions)
  python ping.py --once --silent  # record what's open now without alerting
  python ping.py --test     # send a test notification and exit
  python ping.py --show     # print what's open right now, no alerts
  python ping.py --earliest # earliest opening per entry (any date), JSON

Secrets (env vars or a .env file next to this script):
  DISCORD_WEBHOOK_URL
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

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


def to_local(utc_str):
    """'2027-02-03T16:30:00Z' -> ('2027-02-03', '10:30') in US Central time."""
    dt = datetime.strptime(utc_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(CFG.get("timezone", "America/Chicago")))
    except Exception:  # no tz database (bare Windows): CST, fine for Nov-Mar
        local = dt.astimezone(timezone(timedelta(hours=-6)))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def fetch_slots_page(browser, doc, start, scrolls=12, end=None):
    """Open the real booking page like a visitor would and read the times it offers.

    Returns every slot the page loaded (any date). Read-only: it answers the
    insurance question and looks at the list; it never clicks a time.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    out = {}
    for vt, method in doc["visit_types"]:
        url = CFG["link_template"].format(vt=vt, npi=doc["npi"], dept=doc["dept"], d=start)
        ctx = browser.new_context(user_agent=UA, locale="en-US", timezone_id=CFG.get("timezone", "America/Chicago"))
        page = ctx.new_page()
        responses = []
        page.on("response", lambda r: responses.append(r) if "GetSlots" in r.url else None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:  # insurance question (not every page asks it)
                page.get_by_text(CFG.get("insurance", "Other"), exact=True).first.click(timeout=25000)
                page.get_by_role("button", name=re.compile(r"^Continue")).filter(visible=True).first.click(timeout=10000)
            except PWTimeout:
                pass
            for _ in range(40):  # wait up to ~20s for the first batch of times
                if responses:
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(2500)
            parsed = 0

            def absorb():
                nonlocal parsed
                for r in responses[parsed:]:
                    try:
                        data = r.json()
                    except Exception:
                        continue
                    for sol in data.get("Solutions") or []:
                        for s in sol.get("Slots") or []:
                            if not s.get("DisplayDateTimeUtc"):
                                continue
                            d, t = to_local(s["DisplayDateTimeUtc"])
                            out[(d, t, method)] = {"date": d, "start": t, "method": method, "vt": vt}
                parsed = len(responses)

            absorb()
            # Keep scrolling (which loads further dates) until the list reaches past `end`,
            # so the whole window is read every time and slots don't flicker in and out.
            stalls = 0
            for _ in range(scrolls):
                if end and out and max(k[0] for k in out) > end:
                    break
                before = len(responses)
                page.mouse.wheel(0, 30000)
                page.wait_for_timeout(3000)
                absorb()
                stalls = stalls + 1 if len(responses) == before else 0
                if stalls >= 2:
                    break
            if not responses:
                print(f"  ! {doc['name']} ({method}): page showed no time list", flush=True)
        finally:
            ctx.close()
    return list(out.values())


def fetch_slots(browser, doc, start, end):
    """Slots for one entry between start and end (YYYY-MM-DD), straight from the booking page."""
    return [s for s in fetch_slots_page(browser, doc, start, end=end) if start <= s["date"] <= end]


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
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for doc in sorted(cfg["items"], key=lambda d: d["rank"]):
                if doc["rank"] > max_rank:
                    continue
                try:
                    slots = fetch_slots(browser, doc, start, end)
                except Exception as e:  # page hiccup: log and move on
                    print(f"  ! {doc['name']}: {e}", flush=True)
                    continue
                if slots:
                    found[doc["name"]] = (doc, sorted(slots, key=lambda s: (s["date"], s["start"])))
                time.sleep(random.uniform(1.0, 3.0))  # be polite between entries
        finally:
            browser.close()
    return found


def earliest(cfg):
    """{name: (doc, first_slot_or_None)} — earliest opening from today, any date."""
    from playwright.sync_api import sync_playwright
    today = date.today().isoformat()
    res = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for doc in sorted(cfg["items"], key=lambda d: d["rank"]):
                try:
                    slots = sorted((s for s in fetch_slots_page(browser, doc, today, scrolls=0) if s["date"] >= today),
                                   key=lambda s: (s["date"], s["start"]))
                    res[doc["name"]] = (doc, slots)
                except Exception as e:
                    print(f"  ! {doc['name']}: {e}", flush=True)
                    res[doc["name"]] = (doc, None)
        finally:
            browser.close()
    return res


def build_message(cfg, new_by_doc):
    lines = [f"🔔 New opening(s) before {cfg.get('current_booking', 'your current one')}!"]
    for name, (doc, slots) in sorted(new_by_doc.items(), key=lambda kv: kv[1][0]["rank"]):
        total = cfg.get("total_ranked")
        rank = f"#{doc['rank']} of {total}" if total else f"#{doc['rank']}"
        lines.append(f"\n**{rank} · {name}** — {doc['where']}")
        if doc.get("rating"):
            lines.append(f"  ⭐ {doc['rating']}")
        lines.append(f"  🔁 Referrals: {doc.get('referrals') or 'no info'}")
        if doc.get("note"):
            lines.append(f"  📝 {doc['note']}")
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


def run_once(cfg, alert=True, silent=False):
    # seen = {slot_key: unix time it was last seen open}
    now = time.time()
    state = load_json("state.json", {"seen": {}})
    seen = state["seen"]
    if isinstance(seen, list):  # older format: treat everything in it as seen just now
        seen = {k: now for k in seen}
    found = check(cfg)
    stamp = datetime.now().strftime("%H:%M:%S")

    # Alert only for slots never seen before, or ones that vanished (someone booked) and were
    # gone for a while before reappearing (a cancellation). Slots that simply stay open, or
    # blink out for a check or two because a page loaded slowly, never re-alert.
    realert_after = cfg.get("realert_after_hours", 12) * 3600
    key = lambda doc, s: f"{doc['npi']}|{s['date']}|{s['start']}|{s['method']}"
    new_by_doc = {}
    for name, (doc, slots) in found.items():
        fresh = [s for s in slots if now - seen.get(key(doc, s), 0) > realert_after]
        if fresh:
            new_by_doc[name] = (doc, fresh)

    summary = ", ".join(f"#{d['rank']} {n.split(',')[0]}: {len(s)}" for n, (d, s) in found.items()) or "nothing"
    print(f"[{stamp}] open in window -> {summary}", flush=True)

    if alert and new_by_doc and not silent:
        notify(build_message(cfg, new_by_doc))
    elif silent and new_by_doc:
        print(f"  (silent) recorded {sum(len(v[1]) for v in new_by_doc.values())} slot(s) without alerting", flush=True)

    for d, sl in found.values():
        for s in sl:
            seen[key(d, s)] = now
    today = date.today().isoformat()
    state["seen"] = {k: t for k, t in seen.items() if k.split("|")[1] >= today}  # drop past dates
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
    if "--earliest" in args:
        res = earliest(cfg)
        print(json.dumps({n: [doc["rank"], sl[:3] if sl else sl] for n, (doc, sl) in res.items()}, indent=1))
        return
    if "--show" in args:
        found = check(cfg)
        if not found:
            print("Nothing open in the window right now.")
        for name, (doc, slots) in sorted(found.items(), key=lambda kv: kv[1][0]["rank"]):
            print(f"#{doc['rank']} {name}: " + ", ".join(pretty_time(s["date"], s["start"]) for s in slots))
        return
    if "--once" in args:
        run_once(cfg, silent="--silent" in args)
        return

    # --loop-minutes N: keep checking for N minutes then exit (one GitHub "shift").
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--loop-minutes="):
            limit = time.time() + float(a.split("=", 1)[1]) * 60
    silent_first = "--silent" in args

    print(f"Watching {len(cfg['items'])} entries for {cfg['window_start']} .. {cfg['window_end']} "
          f"every ~{cfg['check_every_seconds']}s." + (" Ctrl+C to stop." if limit is None else ""), flush=True)
    while True:
        if date.today().isoformat() > cfg["window_end"]:
            print("Window has passed — stopping.", flush=True)
            open(os.path.join(HERE, "WINDOW_OVER"), "w").close()  # tells the workflow not to restart
            return
        try:
            run_once(cfg, silent=silent_first)
        except Exception as e:
            print(f"  ! check failed: {e}", flush=True)
        silent_first = False
        base = cfg["check_every_seconds"]
        pause = base + random.uniform(-0.2, 0.3) * base
        if limit is not None and time.time() + pause > limit:
            print("Shift over.", flush=True)
            return
        time.sleep(pause)


if __name__ == "__main__":
    main()
