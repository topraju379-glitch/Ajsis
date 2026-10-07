#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         WhatsApp Auto-Message Bot via Telegram               ║
║         Multi-Account | Auto Welcome | APK Sender            ║
╚══════════════════════════════════════════════════════════════╝

REQUIREMENTS:
    pip install python-telegram-bot==20.7 selenium webdriver-manager qrcode pillow

USAGE:
    python whatsapp_bot.py

FEATURES:
    - Multiple WhatsApp accounts connect via QR code
    - Auto send 3 messages to every NEW contact (1 time only)
    - Message 1: Custom welcome text
    - Message 2: APK file
    - Message 3: Custom closing text
    - 2 second gap between messages
    - SQLite database - tracks who already received messages
    - Full Telegram dashboard with live stats
"""

import asyncio
import logging
import sqlite3
import os
import json
import time
import threading
import base64
import io
from datetime import datetime
from pathlib import Path

# ─── Telegram Bot ───────────────────────────────────────────────────────────
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ─── Selenium for WhatsApp Web ───────────────────────────────────────────────
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager

import qrcode
from PIL import Image

# ════════════════════════════════════════════════════════════════════════════
#                           CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8500472231:AAG6EqQ6ADZJtMyL2I7xuqip0Yj7j-11bZw"
ADMIN_CHAT_ID = 7553216258

# Folders
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "whatsapp_bot.db"
SESSIONS_DIR = BASE_DIR / "wa_sessions"
APK_PATH = BASE_DIR / "app.apk"  # Place your APK here
LOGS_DIR = BASE_DIR / "logs"

SESSIONS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ConversationHandler states
(
    WAITING_ACCOUNT_NAME,
    WAITING_MSG1,
    WAITING_MSG3,
    WAITING_APK,
) = range(4)

# ════════════════════════════════════════════════════════════════════════════
#                           DATABASE SETUP
# ════════════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            session_path TEXT,
            status TEXT DEFAULT 'disconnected',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            contact_phone TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success INTEGER DEFAULT 1,
            UNIQUE(account_id, contact_phone)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Default settings
    defaults = {
        "msg1": "👋 Hello! Aapka swagat hai. Yeh humara special offer hai aapke liye!",
        "msg3": "✅ Koi bhi sawaal ho toh bata dena. Hum hamesha available hain! 😊",
        "apk_path": str(APK_PATH),
        "msg_delay": "2",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_all_accounts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, status, last_seen FROM accounts ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def add_account(name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    session_path = str(SESSIONS_DIR / name)
    try:
        c.execute(
            "INSERT INTO accounts (name, session_path, status) VALUES (?, ?, 'connecting')",
            (name, session_path)
        )
        conn.commit()
        account_id = c.lastrowid
        conn.close()
        return account_id, session_path
    except sqlite3.IntegrityError:
        conn.close()
        return None, None

def update_account_status(account_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE accounts SET status=?, last_seen=? WHERE id=?",
        (status, datetime.now(), account_id)
    )
    conn.commit()
    conn.close()

def already_sent(account_id, phone):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM sent_log WHERE account_id=? AND contact_phone=?",
        (account_id, phone)
    )
    exists = c.fetchone() is not None
    conn.close()
    return exists

def mark_sent(account_id, phone, success=True):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO sent_log (account_id, contact_phone, success) VALUES (?, ?, ?)",
            (account_id, phone, 1 if success else 0)
        )
        conn.commit()
    except Exception:
        pass
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM accounts")
    total_accounts = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM accounts WHERE status='active'")
    active_accounts = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM sent_log WHERE success=1")
    total_sent = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT contact_phone) FROM sent_log")
    unique_contacts = c.fetchone()[0]

    c.execute("""
        SELECT a.name, COUNT(s.id) as sent_count
        FROM accounts a
        LEFT JOIN sent_log s ON a.id = s.account_id
        GROUP BY a.id
        ORDER BY sent_count DESC
    """)
    per_account = c.fetchall()

    c.execute("""
        SELECT COUNT(*) FROM sent_log
        WHERE date(sent_at) = date('now')
    """)
    today_sent = c.fetchone()[0]

    conn.close()
    return {
        "total_accounts": total_accounts,
        "active_accounts": active_accounts,
        "total_sent": total_sent,
        "unique_contacts": unique_contacts,
        "per_account": per_account,
        "today_sent": today_sent,
    }

# ════════════════════════════════════════════════════════════════════════════
#                        WHATSAPP MANAGER
# ════════════════════════════════════════════════════════════════════════════

class WhatsAppAccount:
    def __init__(self, account_id, name, session_path):
        self.account_id = account_id
        self.name = name
        self.session_path = session_path
        self.driver = None
        self.status = "disconnected"
        self.monitor_thread = None
        self.running = False

    def get_driver(self):
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,800")
        options.add_argument(f"--user-data-dir={self.session_path}")
        options.add_argument("--profile-directory=Default")
        # Uncomment for headless (server use):
        # options.add_argument("--headless=new")

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver

    def get_qr_code(self):
        """Open WhatsApp Web and capture QR code as image bytes"""
        try:
            self.driver = self.get_driver()
            self.driver.get("https://web.whatsapp.com")

            # Wait for QR code
            wait = WebDriverWait(self.driver, 60)
            qr_element = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "canvas[aria-label='Scan me!'], div[data-testid='qrcode']"))
            )
            time.sleep(2)

            # Screenshot the QR area
            screenshot = self.driver.get_screenshot_as_png()
            img = Image.open(io.BytesIO(screenshot))

            # Crop to QR area
            loc = qr_element.location
            size = qr_element.size
            dpr = self.driver.execute_script("return window.devicePixelRatio")
            x = int(loc['x'] * dpr)
            y = int(loc['y'] * dpr)
            w = int(size['width'] * dpr)
            h = int(size['height'] * dpr)
            padding = 30
            qr_img = img.crop((x - padding, y - padding, x + w + padding, y + h + padding))

            img_bytes = io.BytesIO()
            qr_img.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            return img_bytes, None
        except Exception as e:
            return None, str(e)

    def wait_for_login(self, timeout=120):
        """Wait until WhatsApp Web is fully logged in"""
        try:
            wait = WebDriverWait(self.driver, timeout)
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div[data-testid='chat-list'], #side, div[aria-label='Chat list']")
                )
            )
            return True
        except TimeoutException:
            return False

    def is_logged_in(self):
        try:
            self.driver.find_element(
                By.CSS_SELECTOR, "div[data-testid='chat-list'], #side"
            )
            return True
        except Exception:
            return False

    def send_text_message(self, phone, message):
        """Send text message to a phone number"""
        try:
            url = f"https://web.whatsapp.com/send?phone={phone}&text={message}"
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 30)

            # Wait for send button or input box
            send_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "span[data-icon='send'], button[aria-label='Send']")
                )
            )
            time.sleep(1)
            send_btn.click()
            time.sleep(2)
            return True
        except Exception as e:
            logging.error(f"[{self.name}] send_text_message error: {e}")
            return False

    def send_file_message(self, phone, file_path, caption=""):
        """Send file/APK to a phone number"""
        try:
            url = f"https://web.whatsapp.com/send?phone={phone}"
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 30)

            # Wait for page load
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div[data-testid='conversation-compose-box-input'], footer")
                )
            )
            time.sleep(2)

            # Click attachment button
            attach_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "div[data-testid='attach-button-plus'], span[data-icon='plus']")
                )
            )
            attach_btn.click()
            time.sleep(1)

            # Find file input
            file_input = self.driver.find_element(
                By.CSS_SELECTOR, "input[accept][type='file']"
            )
            file_input.send_keys(str(file_path))
            time.sleep(2)

            # Add caption if any
            if caption:
                try:
                    caption_box = self.driver.find_element(
                        By.CSS_SELECTOR, "div[data-testid='media-caption-input-container'] div[contenteditable]"
                    )
                    caption_box.send_keys(caption)
                    time.sleep(1)
                except Exception:
                    pass

            # Send
            send_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "span[data-icon='send'], div[data-testid='send']")
                )
            )
            send_btn.click()
            time.sleep(3)
            return True
        except Exception as e:
            logging.error(f"[{self.name}] send_file_message error: {e}")
            return False

    def get_new_chats(self):
        """Get list of new/unread chats with phone numbers"""
        contacts = []
        try:
            # Look for unread badges
            chats = self.driver.find_elements(
                By.CSS_SELECTOR, "div[data-testid='cell-frame-container']"
            )
            for chat in chats[:20]:  # Check first 20 chats
                try:
                    # Check for unread badge
                    badge = chat.find_elements(
                        By.CSS_SELECTOR, "span[data-testid='icon-unread-count']"
                    )
                    if badge:
                        # Get phone/name
                        title = chat.find_element(
                            By.CSS_SELECTOR, "span[data-testid='cell-frame-title']"
                        ).text
                        contacts.append(title)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"[{self.name}] get_new_chats error: {e}")
        return contacts

    def start_monitoring(self, bot_app):
        """Start background thread to monitor for new messages"""
        self.running = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(bot_app,),
            daemon=True
        )
        self.monitor_thread.start()

    def _monitor_loop(self, bot_app):
        """Monitor loop - checks for new contacts every 10 seconds"""
        logging.info(f"[{self.name}] Monitor loop started")

        while self.running:
            try:
                if self.driver and self.is_logged_in():
                    update_account_status(self.account_id, "active")
                    self.status = "active"

                    # Navigate to main page
                    if "web.whatsapp.com" not in self.driver.current_url:
                        self.driver.get("https://web.whatsapp.com")
                        time.sleep(5)

                    # Get all unread chats
                    self._check_and_send_messages()
                else:
                    self.status = "disconnected"
                    update_account_status(self.account_id, "disconnected")

            except Exception as e:
                logging.error(f"[{self.name}] Monitor error: {e}")

            time.sleep(10)

    def _check_and_send_messages(self):
        """Check for new contacts and send auto messages"""
        try:
            # Get all chat items
            wait = WebDriverWait(self.driver, 10)
            chat_list = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#pane-side, div[aria-label='Chat list']")
                )
            )
            chats = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div[data-testid='cell-frame-container']"
            )

            for chat in chats:
                try:
                    # Check unread
                    unread = chat.find_elements(
                        By.CSS_SELECTOR, "span[data-testid='icon-unread-count']"
                    )
                    if not unread:
                        continue

                    # Click on chat
                    chat.click()
                    time.sleep(1.5)

                    # Get phone from URL
                    current_url = self.driver.current_url
                    phone = None
                    if "phone=" in current_url:
                        phone = current_url.split("phone=")[1].split("&")[0]
                    else:
                        # Try to get from chat header
                        try:
                            header = self.driver.find_element(
                                By.CSS_SELECTOR,
                                "div[data-testid='conversation-info-header'] span[dir='auto']"
                            )
                            phone = header.text.replace("+", "").replace(" ", "").replace("-", "")
                        except Exception:
                            pass

                    if not phone:
                        continue

                    # Check if already sent
                    if already_sent(self.account_id, phone):
                        continue

                    logging.info(f"[{self.name}] New contact: {phone} - Sending messages...")

                    # Send 3 messages
                    delay = float(get_setting("msg_delay") or 2)
                    msg1 = get_setting("msg1") or ""
                    msg3 = get_setting("msg3") or ""
                    apk = get_setting("apk_path") or str(APK_PATH)

                    success = True

                    # Message 1
                    if msg1:
                        ok = self.send_text_message(phone, msg1)
                        success = success and ok
                        time.sleep(delay)

                    # Message 2 - APK
                    if os.path.exists(apk):
                        ok = self.send_file_message(phone, apk, "📦 Download karein")
                        success = success and ok
                        time.sleep(delay)
                    else:
                        logging.warning(f"APK not found at: {apk}")

                    # Message 3
                    if msg3:
                        ok = self.send_text_message(phone, msg3)
                        success = success and ok
                        time.sleep(delay)

                    # Mark as sent
                    mark_sent(self.account_id, phone, success)

                    logging.info(f"[{self.name}] Messages sent to {phone} | Success: {success}")

                except Exception as e:
                    logging.error(f"[{self.name}] Chat processing error: {e}")
                    continue

        except Exception as e:
            logging.error(f"[{self.name}] Check messages error: {e}")

    def disconnect(self):
        self.running = False
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        update_account_status(self.account_id, "disconnected")
        self.status = "disconnected"


# Global WhatsApp manager
class WAManager:
    def __init__(self):
        self.accounts: dict[int, WhatsAppAccount] = {}

    def get_account(self, account_id) -> WhatsAppAccount | None:
        return self.accounts.get(account_id)

    def add_account(self, account_id, name, session_path):
        acc = WhatsAppAccount(account_id, name, session_path)
        self.accounts[account_id] = acc
        return acc

    def remove_account(self, account_id):
        acc = self.accounts.pop(account_id, None)
        if acc:
            acc.disconnect()


wa_manager = WAManager()


# ════════════════════════════════════════════════════════════════════════════
#                        TELEGRAM BOT HANDLERS
# ════════════════════════════════════════════════════════════════════════════

def admin_only(func):
    """Decorator to restrict to admin only"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_CHAT_ID:
            await update.effective_message.reply_text("⛔ Access denied.")
            return
        return await func(update, context)
    return wrapper


def main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Account", callback_data="menu_add"),
            InlineKeyboardButton("📊 Status", callback_data="menu_status"),
        ],
        [
            InlineKeyboardButton("✏️ Set Message 1", callback_data="menu_msg1"),
            InlineKeyboardButton("✏️ Set Message 3", callback_data="menu_msg3"),
        ],
        [
            InlineKeyboardButton("📦 Change APK", callback_data="menu_apk"),
            InlineKeyboardButton("🔄 Refresh", callback_data="menu_refresh"),
        ],
        [
            InlineKeyboardButton("📋 All Accounts", callback_data="menu_accounts"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def dashboard_text():
    stats = get_stats()
    accounts = get_all_accounts()

    acc_lines = ""
    for acc in accounts:
        aid, name, status, last_seen = acc
        emoji = "🟢" if status == "active" else "🔴" if status == "disconnected" else "🟡"
        acc_lines += f"\n  {emoji} <b>{name}</b> — {status}"

    # Per account sent count
    per_acc = ""
    for name, cnt in stats["per_account"]:
        per_acc += f"\n  • {name}: {cnt} sent"

    msg = f"""
╔══════════════════════════════╗
║   📱 WhatsApp Bot Dashboard  ║
╚══════════════════════════════╝

📊 <b>Stats</b>
  ├ Total Accounts: {stats['total_accounts']}
  ├ Active Now: {stats['active_accounts']}
  ├ Total Messages Sent: {stats['total_sent']}
  ├ Unique Contacts: {stats['unique_contacts']}
  └ Today Sent: {stats['today_sent']}

📱 <b>Accounts</b>{acc_lines if acc_lines else "\n  No accounts added yet"}

📤 <b>Per Account Stats</b>{per_acc if per_acc else "\n  No data yet"}

⚙️ <b>Current Settings</b>
  ├ Message Delay: {get_setting('msg_delay')}s
  ├ APK: {"✅ Set" if os.path.exists(get_setting('apk_path') or '') else "❌ Not found"}
  ├ Msg 1: {"✅ Set" if get_setting('msg1') else "❌ Not set"}
  └ Msg 3: {"✅ Set" if get_setting('msg3') else "❌ Not set"}

🕒 Last updated: {datetime.now().strftime('%H:%M:%S')}
"""
    return msg


@admin_only
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>WhatsApp Auto-Message Bot</b>\n\nSelect an option from the menu below:",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )


@admin_only
async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        dashboard_text(),
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id != ADMIN_CHAT_ID:
        await query.message.reply_text("⛔ Access denied.")
        return

    data = query.data

    # ── REFRESH / MAIN MENU ──────────────────────────────────────────────
    if data in ("menu_refresh", "back_main"):
        await query.message.edit_text(
            dashboard_text(),
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )

    # ── STATUS ────────────────────────────────────────────────────────────
    elif data == "menu_status":
        stats = get_stats()
        accounts = get_all_accounts()

        lines = []
        for acc in accounts:
            aid, name, status, last_seen = acc
            emoji = "🟢" if status == "active" else "🔴" if status == "disconnected" else "🟡"
            ls = last_seen[:19] if last_seen else "Never"
            lines.append(f"{emoji} <b>{name}</b>\n   Status: {status}\n   Last Seen: {ls}")

        status_text = "\n\n".join(lines) if lines else "No accounts added yet."

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Status", callback_data="menu_status")],
            [InlineKeyboardButton("◀️ Back", callback_data="back_main")],
        ])
        await query.message.edit_text(
            f"📊 <b>Account Status</b>\n\n{status_text}\n\n"
            f"📨 Total Sent Today: <b>{stats['today_sent']}</b>\n"
            f"📨 Total Ever: <b>{stats['total_sent']}</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── ADD ACCOUNT ───────────────────────────────────────────────────────
    elif data == "menu_add":
        context.user_data["state"] = "waiting_account_name"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
        ])
        await query.message.edit_text(
            "➕ <b>Add New WhatsApp Account</b>\n\n"
            "Account ka naam bhejo (e.g., <code>Account1</code>):",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return WAITING_ACCOUNT_NAME

    # ── SET MESSAGE 1 ─────────────────────────────────────────────────────
    elif data == "menu_msg1":
        current = get_setting("msg1")
        context.user_data["state"] = "waiting_msg1"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
        ])
        await query.message.edit_text(
            f"✏️ <b>Set Message 1</b>\n\n"
            f"Current:\n<i>{current}</i>\n\n"
            f"Naya Message 1 bhejo:",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── SET MESSAGE 3 ─────────────────────────────────────────────────────
    elif data == "menu_msg3":
        current = get_setting("msg3")
        context.user_data["state"] = "waiting_msg3"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
        ])
        await query.message.edit_text(
            f"✏️ <b>Set Message 3</b>\n\n"
            f"Current:\n<i>{current}</i>\n\n"
            f"Naya Message 3 bhejo:",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── CHANGE APK ────────────────────────────────────────────────────────
    elif data == "menu_apk":
        current_apk = get_setting("apk_path")
        exists = os.path.exists(current_apk) if current_apk else False
        context.user_data["state"] = "waiting_apk"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")]
        ])
        await query.message.edit_text(
            f"📦 <b>Change APK File</b>\n\n"
            f"Current APK: <code>{current_apk}</code>\n"
            f"Status: {'✅ File exists' if exists else '❌ File not found'}\n\n"
            f"Naya APK file Telegram pe bhejo (.apk):",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── ALL ACCOUNTS ──────────────────────────────────────────────────────
    elif data == "menu_accounts":
        accounts = get_all_accounts()
        if not accounts:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="back_main")]])
            await query.message.edit_text(
                "📋 No accounts added yet.\nUse ➕ Add Account to add one.",
                reply_markup=kb
            )
            return

        buttons = []
        for acc in accounts:
            aid, name, status, _ = acc
            emoji = "🟢" if status == "active" else "🔴"
            buttons.append([
                InlineKeyboardButton(
                    f"{emoji} {name} ({status})",
                    callback_data=f"acc_detail_{aid}"
                )
            ])
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="back_main")])
        await query.message.edit_text(
            "📋 <b>All WhatsApp Accounts</b>\n\nSelect an account:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )

    # ── ACCOUNT DETAIL ────────────────────────────────────────────────────
    elif data.startswith("acc_detail_"):
        aid = int(data.split("_")[-1])
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, name, status, session_path, last_seen FROM accounts WHERE id=?", (aid,))
        acc = c.fetchone()
        c.execute("SELECT COUNT(*) FROM sent_log WHERE account_id=?", (aid,))
        sent = c.fetchone()[0]
        conn.close()

        if not acc:
            await query.answer("Account not found")
            return

        aid, name, status, session_path, last_seen = acc
        wa_acc = wa_manager.get_account(aid)
        live_status = wa_acc.status if wa_acc else status

        emoji = "🟢" if live_status == "active" else "🔴"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Remove Account", callback_data=f"acc_remove_{aid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="menu_accounts")],
        ])
        await query.message.edit_text(
            f"📱 <b>Account: {name}</b>\n\n"
            f"{emoji} Status: {live_status}\n"
            f"📨 Messages Sent: {sent}\n"
            f"🕒 Last Seen: {last_seen or 'Never'}\n"
            f"📁 Session: <code>{session_path}</code>",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── REMOVE ACCOUNT ────────────────────────────────────────────────────
    elif data.startswith("acc_remove_"):
        aid = int(data.split("_")[-1])
        wa_manager.remove_account(aid)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))
        conn.commit()
        conn.close()

        await query.message.edit_text(
            "✅ Account removed successfully.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="back_main")]
            ])
        )

    # ── QR SCANNED CONFIRM ────────────────────────────────────────────────
    elif data.startswith("qr_confirm_"):
        aid = int(data.split("_")[-1])
        wa_acc = wa_manager.get_account(aid)
        if not wa_acc:
            await query.message.reply_text("❌ Account not found.")
            return

        await query.message.reply_text("⏳ Login check kar raha hoon... (30 seconds)")

        loop = asyncio.get_event_loop()
        logged_in = await loop.run_in_executor(None, wa_acc.wait_for_login, 60)

        if logged_in:
            update_account_status(aid, "active")
            wa_acc.status = "active"
            wa_acc.start_monitoring(None)
            await query.message.reply_text(
                f"✅ <b>WhatsApp Connected!</b>\n\n"
                f"Account <b>{wa_acc.name}</b> ab active hai.\n"
                f"Naye contacts ko auto messages bheje jayenge! 🚀",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
        else:
            await query.message.reply_text(
                "❌ Login failed. QR code expire ho gaya hoga.\n"
                "Dobara ➕ Add Account try karein.",
                reply_markup=main_menu_keyboard()
            )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all text/file messages based on current state"""
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        return

    state = context.user_data.get("state")
    msg = update.message

    # ── WAITING FOR ACCOUNT NAME ─────────────────────────────────────────
    if state == "waiting_account_name":
        name = msg.text.strip() if msg.text else ""
        if not name:
            await msg.reply_text("❌ Valid naam bhejo.")
            return

        context.user_data.clear()
        account_id, session_path = add_account(name)

        if not account_id:
            await msg.reply_text(
                f"❌ '{name}' naam ka account already exists.\nDusra naam try karein."
            )
            return

        await msg.reply_text(
            f"⏳ <b>{name}</b> ke liye WhatsApp Web khol raha hoon...\n"
            f"QR code aane mein 15-30 seconds lag sakte hain.",
            parse_mode="HTML"
        )

        # Open WhatsApp Web in background
        wa_acc = wa_manager.add_account(account_id, name, session_path)

        loop = asyncio.get_event_loop()
        qr_bytes, error = await loop.run_in_executor(None, wa_acc.get_qr_code)

        if error or not qr_bytes:
            await msg.reply_text(
                f"❌ Error: {error}\n\nChrome/ChromeDriver installed hai? Retry karein.",
                reply_markup=main_menu_keyboard()
            )
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ QR Scan kar liya", callback_data=f"qr_confirm_{account_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
        ])

        await msg.reply_photo(
            photo=InputFile(qr_bytes, filename="qr.png"),
            caption=(
                f"📱 <b>WhatsApp QR Code - {name}</b>\n\n"
                f"1. WhatsApp > Linked Devices > Link a Device\n"
                f"2. Is QR code ko scan karein\n"
                f"3. Scan karne ke baad niche button dabao ✅\n\n"
                f"⚠️ QR code 60 seconds mein expire ho jata hai"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )

    # ── WAITING FOR MESSAGE 1 ────────────────────────────────────────────
    elif state == "waiting_msg1":
        text = msg.text
        if not text:
            await msg.reply_text("❌ Text message bhejo.")
            return
        set_setting("msg1", text)
        context.user_data.clear()
        await msg.reply_text(
            f"✅ Message 1 set ho gaya!\n\n<i>{text}</i>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )

    # ── WAITING FOR MESSAGE 3 ────────────────────────────────────────────
    elif state == "waiting_msg3":
        text = msg.text
        if not text:
            await msg.reply_text("❌ Text message bhejo.")
            return
        set_setting("msg3", text)
        context.user_data.clear()
        await msg.reply_text(
            f"✅ Message 3 set ho gaya!\n\n<i>{text}</i>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )

    # ── WAITING FOR APK ──────────────────────────────────────────────────
    elif state == "waiting_apk":
        if msg.document:
            doc = msg.document
            if not doc.file_name.endswith(".apk"):
                await msg.reply_text("❌ Sirf .apk file bhejo.")
                return

            await msg.reply_text("⏳ APK download ho raha hai...")
            file = await context.bot.get_file(doc.file_id)
            apk_save_path = BASE_DIR / doc.file_name
            await file.download_to_drive(str(apk_save_path))

            set_setting("apk_path", str(apk_save_path))
            context.user_data.clear()
            await msg.reply_text(
                f"✅ APK updated!\n<code>{apk_save_path}</code>",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
        else:
            await msg.reply_text("❌ .apk file bhejo (document ke roop mein).")

    # ── DEFAULT ──────────────────────────────────────────────────────────
    else:
        await msg.reply_text(
            "👋 Main menu:",
            reply_markup=main_menu_keyboard()
        )


# ════════════════════════════════════════════════════════════════════════════
#                              MAIN
# ════════════════════════════════════════════════════════════════════════════

def setup_logging():
    log_file = LOGS_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )


def main():
    setup_logging()
    init_db()

    logging.info("═" * 60)
    logging.info("  WhatsApp Auto-Message Bot Starting...")
    logging.info("═" * 60)

    # Restore previously active accounts
    accounts = get_all_accounts()
    restored = 0
    for acc_row in accounts:
        aid, name, status, last_seen = acc_row
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_path FROM accounts WHERE id=?", (aid,))
        row = c.fetchone()
        conn.close()
        if row and os.path.exists(row[0]):
            # Don't auto-reconnect, user needs to do QR again
            update_account_status(aid, "disconnected")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("dashboard", dashboard_handler))
    app.add_handler(CommandHandler("status", dashboard_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        filters.TEXT | filters.Document.ALL,
        message_handler
    ))

    logging.info(f"Bot started! Admin: {ADMIN_CHAT_ID}")
    logging.info("Telegram pe /start karein")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
