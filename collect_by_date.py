#!/usr/bin/env python3
"""
Date-Based Config Collector - Uses channels.json for source list (ignoring limit),
and a simple date_filter_config.json for default days_back.
"""

import os
import sys
import json
import re
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup

# ---------- File paths ----------
CHANNELS_FILE = "channels.json"
DATE_CONFIG_FILE = "date_filter_config.json"
DEFAULT_OUTPUT_FILE = "date_subscription.txt"

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------- Config extraction ----------
PROTOCOL_PATTERN = re.compile(
    r'(vmess://\S+|vless://\S+|trojan://\S+|ss://\S+|hysteria2?://\S+|tuic://\S+)',
    re.IGNORECASE,
)

def extract_configs(text: str) -> List[str]:
    configs = []
    matches = PROTOCOL_PATTERN.findall(text)
    for match in matches:
        clean = match.rstrip('.,;:!?؟،؛"\'()[]{}<>')
        if clean:
            configs.append(clean)
    return configs

# ---------- Telegram scraper ----------
def fetch_messages_in_date_range(username: str, start_date: datetime, end_date: datetime) -> List[dict]:
    base_url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    messages = []
    page_url = base_url
    stop_pagination = False

    log.info(f"Scanning @{username} from {start_date.isoformat()} to {end_date.isoformat()}")

    while not stop_pagination:
        try:
            resp = requests.get(page_url, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Error fetching {page_url}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        msg_wraps = soup.select(".tgme_widget_message_wrap")
        if not msg_wraps:
            log.info("No more messages.")
            break

        for wrap in msg_wraps:
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
            except Exception as e:
                log.warning(f"Failed to parse datetime {msg_dt_str}: {e}")
                continue

            if msg_dt < start_date:
                stop_pagination = True
                break
            if msg_dt > end_date:
                continue

            text_div = wrap.select_one(".tgme_widget_message_text")
            if text_div:
                text = text_div.get_text(separator="\n")
                messages.append({"datetime": msg_dt, "text": text})

        if stop_pagination:
            break

        last_msg = msg_wraps[-1]
        before_attr = last_msg.get("data-before")
        if before_attr:
            page_url = f"{base_url}?before={before_attr}"
        else:
            log.info("No more pages.")
            break

    log.info(f"Found {len(messages)} messages in range for @{username}")
    return messages

# ---------- Main ----------
def main():
    # 1. Days back: from env first, else from date_filter_config.json
    days_back = 3  # fallback default
    if os.getenv("INPUT_DAYS_BACK"):
        days_back = int(os.getenv("INPUT_DAYS_BACK"))
    elif os.path.exists(DATE_CONFIG_FILE):
        try:
            with open(DATE_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            days_back = int(cfg.get("days_back", days_back))
        except Exception as e:
            log.warning(f"Could not read {DATE_CONFIG_FILE}: {e}")

    # 2. Channels: from env override, else from channels.json
    channels_usernames = []
    env_channels = os.getenv("INPUT_CHANNELS", "").strip()
    if env_channels:
        channels_usernames = [ch.strip() for ch in env_channels.split(",") if ch.strip()]
        log.info(f"Using channels from workflow input: {channels_usernames}")
    else:
        if not os.path.exists(CHANNELS_FILE):
            log.error(f"{CHANNELS_FILE} not found. Exiting.")
            sys.exit(1)
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                channels_data = json.load(f)
            # Extract 'username' from each object, ignore 'limit'
            channels_usernames = [ch["username"] for ch in channels_data if "username" in ch]
            log.info(f"Loaded {len(channels_usernames)} channels from {CHANNELS_FILE}")
        except Exception as e:
            log.error(f"Failed to parse {CHANNELS_FILE}: {e}")
            sys.exit(1)

    if not channels_usernames:
        log.error("No channels to scan. Exiting.")
        sys.exit(1)

    output_filename = os.getenv("INPUT_OUTPUT_FILENAME", DEFAULT_OUTPUT_FILE)

    # 3. Date range (UTC)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)
    log.info(f"Collecting configs from {start_date.isoformat()} to {end_date.isoformat()} (last {days_back} days)")

    # 4. Collect
    all_configs: Set[str] = set()
    for channel in channels_usernames:
        messages = fetch_messages_in_date_range(channel, start_date, end_date)
        channel_count = 0
        for msg in messages:
            cfgs = extract_configs(msg["text"])
            if cfgs:
                log.info(f"  📩 {msg['datetime'].strftime('%Y-%m-%d %H:%M')} – {len(cfgs)} config(s)")
                all_configs.update(cfgs)
                channel_count += len(cfgs)
        log.info(f"  ➡️ @{channel}: {channel_count} configs added (total unique so far: {len(all_configs)})")

    # 5. Write output (overwrites previous content)
    if all_configs:
        plain = "\n".join(sorted(all_configs))
        b64 = base64.b64encode(plain.encode()).decode()
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(b64)
        log.info(f"✅ Written {len(all_configs)} unique configs to {output_filename}")
    else:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write("")
        log.warning("❌ No configs found. Output file cleared.")

    # 6. Print raw link (will be committed later)
    repo = os.getenv("GITHUB_REPOSITORY", "user/repo")
    raw_url = f"https://raw.githubusercontent.com/{repo}/main/{output_filename}"
    log.info(f"🔗 Subscription link (after commit): {raw_url}")

if __name__ == "__main__":
    main()
