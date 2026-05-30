#!/usr/bin/env python3
"""
V2Ray Config Collector – نسخه اصلاح‌شده
پشتیبانی از:
- جستجوی عمیق (پیمایش صفحات تاریخچه کانال)
- اعمال فیلتر پروتکل حین جمع‌آوری برای رسیدن به limit دقیق
- پارس بهتر آدرس/پورت (IPv6، Shadowsocks، VMess)
- رفع باگ‌های جزئی و افزایش پایداری
"""

import os
import sys
import json
import time
import re
import base64
import socket
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from typing import List, Set, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# ========== Environment variables ==========
MODE = os.getenv("MODE", "collect")
TEMP_CHANNEL = os.getenv("TEMP_CHANNEL", "").strip()
TEMP_LIMIT = int(os.getenv("TEMP_LIMIT", 5))
GLOBAL_LIMIT_OVERRIDE = int(os.getenv("GLOBAL_LIMIT_OVERRIDE", 0))
PROTOCOL_FILTER = os.getenv("PROTOCOL_FILTER", "").strip()
MAX_TOTAL_CONFIGS = int(os.getenv("MAX_TOTAL_CONFIGS", 0))
LIVE_TEST = os.getenv("LIVE_TEST", "false").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"
COMMIT_CHANGES = os.getenv("COMMIT_CHANGES", "true").lower() == "true"

# File paths
CHANNELS_FILE = "channels.json"
DB_FILE = "collected.json"
SUBSCRIPTION_FILE = "subscription.txt"
SCANS_DIR = "scans"

# ========== Helper functions ==========
def log(msg: str, level: str = "INFO") -> None:
    if VERBOSE or level != "DEBUG":
        print(f"[{level}] {msg}")

def load_json(filepath: str, default: any = None) -> any:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(filepath: str, data: any) -> None:
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ========== Universal config extraction ==========
PROTOCOL_PATTERN = re.compile(
    r'(vmess://\S+|vless://\S+|trojan://\S+|ss://\S+|hysteria2?://\S+|tuic://\S+)',
    re.IGNORECASE
)

def extract_configs(text: str) -> List[str]:
    configs = []
    matches = PROTOCOL_PATTERN.findall(text)
    for match in matches:
        clean = match.rstrip('.,;:!?؟،؛"\'()[]{}<>')
        if clean:
            configs.append(clean)
    return configs

# ========== Improved address/port parsing ==========
def parse_address_port(config: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract host and port from any proxy config URI. Returns (host, port) or (None, None)."""
    try:
        if config.startswith("ss://"):
            # Support both legacy base64 and SIP002 format
            # Remove fragment and params first
            main_part = config.split("#")[0].split("?")[0]
            # Check if it's base64 encoded (starts with ss:// and then base64 without @)
            if "@" not in main_part[5:]:
                # Legacy format: ss://base64(...)
                b64 = main_part[5:]
                padding = 4 - len(b64) % 4
                if padding != 4:
                    b64 += "=" * padding
                decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                # decoded format: method:password@host:port
                method_pw, server = decoded.rsplit("@", 1)
                host, port_str = server.rsplit(":", 1)
                return host.strip(), int(port_str)
            else:
                # SIP002: ss://method:password@host:port
                rest = main_part[5:]  # remove "ss://"
                method_pw, server = rest.split("@", 1)
                host, port_str = server.rsplit(":", 1)
                return host.strip(), int(port_str)
        elif config.startswith("trojan://"):
            parsed = urlparse(config)
            return parsed.hostname, parsed.port or 443
        elif config.startswith("hysteria") or config.startswith("tuic://"):
            parsed = urlparse(config)
            return parsed.hostname, parsed.port or 443
        elif config.startswith("vless://") or config.startswith("vmess://"):
            if config.startswith("vmess://"):
                b64 = config[8:].split("#")[0].split("?")[0]
                # Correct base64 padding
                missing_padding = len(b64) % 4
                if missing_padding:
                    b64 += "=" * (4 - missing_padding)
                data = json.loads(base64.b64decode(b64).decode("utf-8"))
                return data["add"], int(data["port"])
            else:  # vless://
                rest = config[8:]
                # rest is like UUID@host:port?params...
                if "@" not in rest:
                    return None, None
                userinfo, hostport = rest.split("@", 1)
                hostport = hostport.split("?")[0].split("#")[0]
                # support IPv6 like [::1]:12345
                if hostport.startswith("["):
                    # IPv6
                    host, port_str = hostport.rsplit("]:", 1)
                    host = host[1:]  # remove '['
                    return host, int(port_str)
                else:
                    host, port_str = hostport.rsplit(":", 1)
                    return host, int(port_str)
    except Exception as e:
        if VERBOSE:
            log(f"Failed to parse address/port for config: {config[:60]}... Error: {e}", "DEBUG")
    return None, None

def test_config_live(config: str) -> bool:
    host, port = parse_address_port(config)
    if not host or not port:
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def filter_by_protocol(configs: List[str], allowed: str) -> List[str]:
    if not allowed:
        return configs
    allowed_set = set(p.strip().lower() for p in allowed.split(","))
    return [c for c in configs if c.split("://")[0].lower() in allowed_set]

# ========== Deep channel scraping ==========
def fetch_channel_posts(username: str, max_posts: Optional[int] = None) -> List[str]:
    """
    Fetch message texts from a Telegram public channel.
    Will paginate until max_posts is reached or no more pages.
    """
    base_url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    posts = []
    next_page = base_url

    while True:
        try:
            resp = requests.get(next_page, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log(f"Error fetching {next_page}: {e}", "ERROR")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.select(".tgme_widget_message_wrap")
        if not messages:
            break

        for msg in messages:
            text_div = msg.select_one(".tgme_widget_message_text")
            if text_div:
                posts.append(text_div.get_text(separator="\n"))
                if max_posts and len(posts) >= max_posts:
                    return posts

        # Find "before" parameter for next page
        last_msg = messages[-1]
        before_attr = last_msg.get("data-before")
        if not before_attr:
            break
        next_page = f"{base_url}?before={before_attr}"

    return posts

# ========== Create default files if missing ==========
if not os.path.exists(CHANNELS_FILE):
    default_channels = [{"username": "free_v2ray_configs", "limit": 10}]
    save_json(CHANNELS_FILE, default_channels)
    log(f"Created default {CHANNELS_FILE}. Please edit it.", "WARN")
if not os.path.exists(DB_FILE):
    save_json(DB_FILE, {})
    log(f"Created empty {DB_FILE}.", "WARN")

# ========== Load channels ==========
channels = load_json(CHANNELS_FILE, [])
if TEMP_CHANNEL:
    channels.append({"username": TEMP_CHANNEL, "limit": TEMP_LIMIT})
    log(f"Temporary channel added: {TEMP_CHANNEL} (limit={TEMP_LIMIT})")

if not channels:
    log("No channels defined. Exiting.", "ERROR")
    sys.exit(1)

# ========== Load database ==========
if MODE == "fresh":
    db = {}
    log("Fresh mode: ignoring previous database.")
else:
    db = load_json(DB_FILE, {})

# ========== Collect configs ==========
new_configs_added = []

for ch in channels:
    username = ch["username"]
    limit = GLOBAL_LIMIT_OVERRIDE if GLOBAL_LIMIT_OVERRIDE > 0 else ch.get("limit", 10)
    log(f"Processing channel {username} (limit={limit})")

    collected = []  # unique configs from this channel that match protocol filter
    posts = fetch_channel_posts(username)  # get all posts (pagination), no post limit
    for text in posts:
        cfgs = extract_configs(text)
        cfgs = filter_by_protocol(cfgs, PROTOCOL_FILTER)  # apply filter immediately
        for cfg in cfgs:
            if cfg not in collected:
                collected.append(cfg)
                if len(collected) >= limit:
                    break
        if len(collected) >= limit:
            break

    log(f"Collected {len(collected)} configs from {username} after filtering")
    now = int(time.time())
    for cfg in collected:
        if cfg not in db:
            db[cfg] = now
            new_configs_added.append(cfg)
            log(f"  New config: {cfg[:60]}...")

log(f"Total new configs this run: {len(new_configs_added)}")

# ========== Enforce total limit ==========
if MAX_TOTAL_CONFIGS > 0 and len(db) > MAX_TOTAL_CONFIGS:
    sorted_items = sorted(db.items(), key=lambda x: x[1])
    db = dict(sorted_items[-MAX_TOTAL_CONFIGS:])
    log(f"Removed oldest configs. Total now: {len(db)}")

# ========== Optional live test ==========
valid_configs = list(db.keys())
if LIVE_TEST:
    log("Running live connection tests...")
    alive = []
    dead = 0
    for cfg in valid_configs:
        if test_config_live(cfg):
            alive.append(cfg)
        else:
            dead += 1
    log(f"Live test results: {len(alive)} alive, {dead} dead")
    valid_configs = alive

# ========== Build subscription ==========
if valid_configs:
    plain = "\n".join(valid_configs)
    b64 = base64.b64encode(plain.encode()).decode()
    with open(SUBSCRIPTION_FILE, "w", encoding="utf-8") as f:
        f.write(b64)
    log(f"Subscription created with {len(valid_configs)} configs.")
else:
    with open(SUBSCRIPTION_FILE, "w", encoding="utf-8") as f:
        f.write("")
    log("No valid configs for subscription.", "WARN")

# ========== Save database ==========
if MODE != "test":
    save_json(DB_FILE, db)
else:
    log("Test mode: database not saved.")

# ========== Save scan snapshot ==========
if new_configs_added and MODE != "test":
    os.makedirs(SCANS_DIR, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan_file = os.path.join(SCANS_DIR, f"scan_{timestamp_str}.txt")
    with open(scan_file, "w", encoding="utf-8") as f:
        f.write("\n".join(new_configs_added))
    log(f"Scan saved: {scan_file}")
