#!/usr/bin/env python3
"""
Date-Based Config Collector – اصلاح‌شده: یافتن data-before از هر المان پیام
"""

import os, sys, json, re, base64, logging
from datetime import datetime, timedelta, timezone
from typing import List, Set, Dict
import requests
from bs4 import BeautifulSoup

CHANNELS_FILE = "channels.json"
DATE_CONFIG_FILE = "date_filter_config.json"
DEFAULT_OUTPUT_FILE = "date_subscription.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PROTOCOL_PATTERN = re.compile(
    r'(vmess://\S+|vless://\S+|trojan://\S+|ss://\S+|hysteria2?://\S+|tuic://\S+)',
    re.IGNORECASE,
)

def extract_configs(text: str) -> List[str]:
    return [m.rstrip('.,;:!?؟،؛"\'()[]{}<>') for m in PROTOCOL_PATTERN.findall(text) if m]

def fetch_messages_in_date_range(username: str, start_date: datetime, end_date: datetime) -> List[Dict]:
    base_url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0"}
    messages = []
    page_url = base_url
    page_count = 0

    log.info(f"🔍 @{username}: from {start_date.isoformat()} to {end_date.isoformat()}")

    while True:
        page_count += 1
        log.info(f"📄 Page {page_count}: {page_url}")
        try:
            resp = requests.get(page_url, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"  ❌ Request failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        msg_wraps = soup.select(".tgme_widget_message_wrap")
        if not msg_wraps:
            log.info("  No messages. Stop.")
            break

        log.info(f"  {len(msg_wraps)} message wraps loaded.")
        page_oldest_nonpinned_date = None
        page_has_nonpinned = False
        pinned_count = 0

        for wrap in msg_wraps:
            is_pinned = wrap.select_one(".tgme_widget_message_pinned") is not None
            if is_pinned:
                pinned_count += 1

            time_tag = wrap.select_one("time")
            if not time_tag or not time_tag.has_attr("datetime"):
                continue

            msg_dt_str = time_tag["datetime"]
            try:
                msg_dt = datetime.fromisoformat(msg_dt_str)
                if msg_dt.tzinfo is None:
                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                else:
                    msg_dt = msg_dt.astimezone(timezone.utc)
            except:
                continue

            # Store text if in range
            if start_date <= msg_dt <= end_date:
                text_div = wrap.select_one(".tgme_widget_message_text")
                if text_div:
                    messages.append({"datetime": msg_dt, "text": text_div.get_text(separator="\n")})

            if not is_pinned:
                page_has_nonpinned = True
                if page_oldest_nonpinned_date is None or msg_dt < page_oldest_nonpinned_date:
                    page_oldest_nonpinned_date = msg_dt

        log.info(f"  Pinned: {pinned_count}, collected: {len(messages)}")
        if page_oldest_nonpinned_date:
            log.info(f"  Oldest non-pinned: {page_oldest_nonpinned_date.isoformat()}")

        # Stop if oldest non-pinned is already older than start_date
        if page_has_nonpinned and page_oldest_nonpinned_date < start_date:
            log.info("  🛑 Reached messages older than start_date. Stopping.")
            break

        # Find pagination token: search from the end for any wrap with data-before
        before_token = None
        for wrap in reversed(msg_wraps):
            token = wrap.get("data-before")
            if token:
                before_token = token
                break

        if before_token:
            page_url = f"{base_url}?before={before_token}"
            log.info(f"  ➡️ Next page with before={before_token}")
        else:
            # Last resort: look for a link at the bottom of the page
            older_link = soup.select_one("a[href*='?before=']")
            if older_link:
                before_token = older_link["href"].split("?before=")[-1]
                page_url = f"{base_url}?before={before_token}"
                log.info(f"  🔗 Found before in page link: {before_token}")
            else:
                log.info("  🏁 No data-before in any wrap and no link. End of history.")
                break

    log.info(f"✅ @{username}: pages={page_count}, total collected={len(messages)}")
    return messages

def main():
    # 1. days_back
    days_back = 3
    if os.getenv("INPUT_DAYS_BACK"):
        days_back = int(os.getenv("INPUT_DAYS_BACK"))
    elif os.path.exists(DATE_CONFIG_FILE):
        with open(DATE_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        days_back = int(cfg.get("days_back", days_back))

    # 2. channels
    channels_usernames = []
    env_channels = os.getenv("INPUT_CHANNELS", "").strip()
    if env_channels:
        channels_usernames = [c.strip() for c in env_channels.split(",") if c.strip()]
    elif os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            channels_data = json.load(f)
        channels_usernames = list({c["username"] for c in channels_data if "username" in c})
    else:
        log.error("No channels found.")
        sys.exit(1)

    if not channels_usernames:
        log.error("Channel list empty.")
        sys.exit(1)

    output_filename = os.getenv("INPUT_OUTPUT_FILENAME", DEFAULT_OUTPUT_FILE)

    # 3. Date range (full calendar days)
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days_back - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = now
    log.info(f"📅 Collecting from {start_date.isoformat()} to {end_date.isoformat()} (last {days_back} days)")

    all_configs = set()
    for ch in channels_usernames:
        msgs = fetch_messages_in_date_range(ch, start_date, end_date)
        ch_cfgs = 0
        for m in msgs:
            cfgs = extract_configs(m["text"])
            all_configs.update(cfgs)
            ch_cfgs += len(cfgs)
        log.info(f"📊 @{ch}: {ch_cfgs} configs (unique total: {len(all_configs)})")

    if all_configs:
        plain = "\n".join(sorted(all_configs))
        b64 = base64.b64encode(plain.encode()).decode()
        with open(output_filename, "w") as f:
            f.write(b64)
        log.info(f"✅ Written {len(all_configs)} configs to {output_filename}")
    else:
        with open(output_filename, "w") as f:
            f.write("")
        log.warning("❌ No configs, output cleared.")

    repo = os.getenv("GITHUB_REPOSITORY", "user/repo")
    branch = os.getenv("GITHUB_REF_NAME", "main")
    log.info(f"🔗 Link: https://raw.githubusercontent.com/{repo}/{branch}/{output_filename}")

if __name__ == "__main__":
    main()
