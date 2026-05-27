#!/usr/bin/env python3
import os, sys, json, time, re, base64, socket
from datetime import datetime
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# ========== تنظیمات از محیط ==========
MODE = os.getenv("MODE", "collect")
TEMP_CHANNEL = os.getenv("TEMP_CHANNEL", "").strip()
TEMP_LIMIT = int(os.getenv("TEMP_LIMIT", 5))
GLOBAL_LIMIT_OVERRIDE = int(os.getenv("GLOBAL_LIMIT_OVERRIDE", 0))
PROTOCOL_FILTER = os.getenv("PROTOCOL_FILTER", "").strip()
MAX_TOTAL_CONFIGS = int(os.getenv("MAX_TOTAL_CONFIGS", 0))
LIVE_TEST = os.getenv("LIVE_TEST", "false").lower() == "true"
VERBOSE = os.getenv("VERBOSE", "false").lower() == "true"
COMMIT_CHANGES = os.getenv("COMMIT_CHANGES", "true").lower() == "true"

# فایل‌های دائمی
CHANNELS_FILE = "channels.json"
DB_FILE = "collected.json"
SUBSCRIPTION_FILE = "subscription.txt"
SCANS_DIR = "scans"

# ========== توابع کمکی ==========
def log(msg, level="INFO"):
    if VERBOSE or level != "DEBUG":
        print(f"[{level}] {msg}")

def load_json(filepath, default=None):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# الگوهای تشخیص کانفیگ
CONFIG_PATTERNS = [
    r'(vmess://[A-Za-z0-9+/_\-]+={0,2})',
    r'(vless://[A-Za-z0-9+/_\-]+={0,2})',
    r'(trojan://[A-Za-z0-9+/_\-]+={0,2})',
    r'(ss://[A-Za-z0-9+/_\-]+={0,2})',
    r'(hysteria2?://[A-Za-z0-9+/_\-]+={0,2})',
    r'(tuic://[A-Za-z0-9+/_\-]+={0,2})',
]

def extract_configs(text):
    configs = set()
    for pattern in CONFIG_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            clean = m.strip().rstrip('"').rstrip("'").rstrip('>')
            if clean:
                configs.add(clean)
    return list(configs)

def fetch_channel_posts(username):
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log(f"خطا در دریافت کانال {username}: {e}", "ERROR")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")
    texts = []
    for msg in messages:
        text_div = msg.select_one(".tgme_widget_message_text")
        if text_div:
            texts.append(text_div.get_text(separator="\n"))
    return texts

def parse_address_port(config):
    if config.startswith("ss://"):
        try:
            rest = config.split("@", 1)[-1].split("#")[0].split("?")[0]
            host, port = rest.rsplit(":", 1)
            return host, int(port)
        except:
            pass
    elif config.startswith("trojan://"):
        parsed = urlparse(config)
        return parsed.hostname, parsed.port or 443
    elif config.startswith("hysteria"):
        parsed = urlparse(config)
        return parsed.hostname, parsed.port or 443
    elif config.startswith("tuic://"):
        parsed = urlparse(config)
        return parsed.hostname, parsed.port or 443
    elif config.startswith("vless://") or config.startswith("vmess://"):
        try:
            if config.startswith("vmess://"):
                b64 = config[8:].split("#")[0].split("?")[0]
                data = json.loads(base64.b64decode(b64 + "==").decode())
                return data["add"], int(data["port"])
            elif config.startswith("vless://"):
                rest = config[8:].split("@", 1)[1]
                host = rest.split(":")[0]
                port = rest.split(":")[1].split("?")[0]
                return host, int(port)
        except:
            pass
    return None, None

def test_config_live(config):
    host, port = parse_address_port(config)
    if not host or not port:
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def filter_by_protocol(configs, allowed):
    if not allowed:
        return configs
    allowed_set = set(p.strip().lower() for p in allowed.split(","))
    filtered = []
    for c in configs:
        proto = c.split("://")[0].lower() if "://" in c else ""
        if proto in allowed_set:
            filtered.append(c)
    return filtered

# ========== ساخت فایل‌های پیش‌فرض در صورت عدم وجود ==========
if not os.path.exists(CHANNELS_FILE):
    default_channels = [
        {"username": "free_v2ray_configs", "limit": 10}
    ]
    save_json(CHANNELS_FILE, default_channels)
    log(f"⚠️ فایل {CHANNELS_FILE} وجود نداشت. یک نمونه با کانال پیش‌فرض ساخته شد. لطفاً آن را ویرایش کنید.")
if not os.path.exists(DB_FILE):
    save_json(DB_FILE, {})
    log(f"⚠️ فایل {DB_FILE} وجود نداشت. یک دیتابیس خالی ایجاد شد.")

# ========== بارگذاری کانال‌ها ==========
channels = load_json(CHANNELS_FILE, [])
if TEMP_CHANNEL:
    channels.append({"username": TEMP_CHANNEL, "limit": TEMP_LIMIT})
    log(f"کانال موقت اضافه شد: {TEMP_CHANNEL} با محدودیت {TEMP_LIMIT}")

if not channels:
    log("هیچ کانالی تعریف نشده است.", "ERROR")
    sys.exit(1)

# ========== بارگذاری دیتابیس ==========
if MODE == "fresh":
    db = {}
    log("حالت fresh: دیتابیس قبلی نادیده گرفته شد.")
else:
    db = load_json(DB_FILE, {})

# ========== جمع‌آوری کانفیگ‌ها ==========
new_configs_added = []

for ch in channels:
    username = ch["username"]
    limit = GLOBAL_LIMIT_OVERRIDE if GLOBAL_LIMIT_OVERRIDE > 0 else ch.get("limit", 10)
    log(f"پردازش کانال {username} (limit={limit})")
    posts = fetch_channel_posts(username)
    collected = []
    for text in posts:
        if len(collected) >= limit:
            break
        cfgs = extract_configs(text)
        for cfg in cfgs:
            if cfg not in collected:
                collected.append(cfg)
            if len(collected) >= limit:
                break
    collected = filter_by_protocol(collected, PROTOCOL_FILTER)
    now = int(time.time())
    for cfg in collected:
        if cfg not in db:
            db[cfg] = now
            new_configs_added.append(cfg)
            log(f"  کانفیگ جدید: {cfg[:60]}...")

log(f"تعداد کانفیگ‌های جدید این اجرا: {len(new_configs_added)}")

# ========== محدودیت تعدادی ==========
if MAX_TOTAL_CONFIGS > 0 and len(db) > MAX_TOTAL_CONFIGS:
    sorted_items = sorted(db.items(), key=lambda x: x[1])
    db = dict(sorted_items[-MAX_TOTAL_CONFIGS:])
    log(f"حذف کانفیگ‌های قدیمی. تعداد نهایی: {len(db)}")

# ========== تست زنده بودن ==========
valid_configs = list(db.keys())
if LIVE_TEST:
    log("انجام تست زنده بودن کانفیگ‌ها...")
    alive = []
    dead = 0
    for cfg in valid_configs:
        if test_config_live(cfg):
            alive.append(cfg)
        else:
            dead += 1
    log(f"تست زنده: {len(alive)} فعال, {dead} مرده")
    valid_configs = alive

# ========== تولید فایل اشتراک ==========
if valid_configs:
    plain = "\n".join(valid_configs)
    b64 = base64.b64encode(plain.encode()).decode()
    with open(SUBSCRIPTION_FILE, "w", encoding="utf-8") as f:
        f.write(b64)
    log(f"فایل اشتراک با {len(valid_configs)} کانفیگ ساخته شد.")
else:
    with open(SUBSCRIPTION_FILE, "w", encoding="utf-8") as f:
        f.write("")
    log("هیچ کانفیگ معتبری برای اشتراک وجود ندارد.", "WARN")

# ========== ذخیره دیتابیس ==========
if MODE != "test":
    save_json(DB_FILE, db)
else:
    log("حالت test: دیتابیس ذخیره نشد.")

# ========== ذخیره فایل اسکن جدید ==========
if new_configs_added and MODE != "test":
    os.makedirs(SCANS_DIR, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan_file = os.path.join(SCANS_DIR, f"scan_{timestamp_str}.txt")
    with open(scan_file, "w", encoding="utf-8") as f:
        f.write("\n".join(new_configs_added))
    log(f"فایل اسکن جدید ذخیره شد: {scan_file}")
