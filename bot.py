import sys
import os
import logging
import asyncio
import io
import urllib.parse
import sqlite3
import re
from datetime import datetime

# UTF-8 encoding support for console/logs
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import requests
import qrcode
from aiohttp import web
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatJoinRequest,
    ChatMemberUpdated,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- CONFIGURATION -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8613221309:AAHkdjfnEk7Br_w-IMqh9ai4KZJuFSlZWoM")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8710308658"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))  # For Render port binding

# In-memory states for admin interactive flows: {admin_id: {"action": "...", "step": "...", "data": {...}}}
ADMIN_STATES = {}
# In-memory states for user flows: {user_id: {"action": "..."}}
USER_STATES = {}

# ----------------- DATABASE SETUP -----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            credits INTEGER DEFAULT 1,
            total_earned INTEGER DEFAULT 1,
            total_used INTEGER DEFAULT 0,
            referred_by INTEGER,
            last_claimed TEXT,
            is_banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add columns if table already exists without them
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_claimed TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except Exception:
        pass

    # Join Requests tracking table (records pending join requests sent by users)
    c.execute("""
        CREATE TABLE IF NOT EXISTS join_requests (
            user_id INTEGER,
            chat_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, chat_id)
        )
    """)

    # Force channels table (Supports multiple channels with public, private, request modes)
    c.execute("""
        CREATE TABLE IF NOT EXISTS force_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            chat_id TEXT UNIQUE,
            invite_link TEXT,
            channel_type TEXT DEFAULT 'public',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Promo Codes table
    c.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            credits INTEGER,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Promo Redemptions tracking table
    c.execute("""
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            code TEXT,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, code)
        )
    """)

    # Connected Groups table (Tracking groups where bot is added as Admin)
    c.execute("""
        CREATE TABLE IF NOT EXISTS connected_groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            added_by INTEGER,
            member_count INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            invite_link TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Settings table
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Payments table
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            credits INTEGER,
            utr TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Default settings setup
    defaults = {
        "log_channel": "-1003946288718",         # Channel/Group ID for data & logs e.g. -100xxxx
        "data_channel": "-1003946288718",        # Dedicated Telegram Data Backup channel
        "upi_id": "pawank63599-1@okicici",
        "plan_10_credits": "50",
        "plan_50_credits": "250",
        "plan_100_credits": "500",
        "redeem_min_refers": "5",
        "redeem_denial_msg": "bhai jab tum refer karoge hi nahi to kyu de hum tumhe redeem code? Isliye tum use nahi kar sakte bhai! Pehle refer karo phir code claim karo.",
        "group_min_members": "100",
        "referral_reward": "5",
        "support_username": "@YOURSOLUTIONs_BOT",
        "youtube_channel_link": "https://youtube.com/@toxicexploit?si=6evZKvA2IxUhv9U4"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Migrate any legacy single force_channel setting into force_channels table
    try:
        c.execute("SELECT value FROM settings WHERE key = 'force_channel'")
        row = c.fetchone()
        if row and row["value"]:
            legacy_ch = row["value"]
            c.execute("SELECT value FROM settings WHERE key = 'force_invite_link'")
            link_row = c.fetchone()
            legacy_link = link_row["value"] if link_row and link_row["value"] else (f"https://t.me/{legacy_ch.replace('@', '')}" if legacy_ch.startswith("@") else "")
            c.execute("SELECT value FROM settings WHERE key = 'force_channel_type'")
            type_row = c.fetchone()
            legacy_type = type_row["value"] if type_row and type_row["value"] else "public"
            c.execute("INSERT OR IGNORE INTO force_channels (name, chat_id, invite_link, channel_type) VALUES (?, ?, ?, ?)",
                      (legacy_ch, legacy_ch, legacy_link, legacy_type))
            # Clear legacy setting to avoid duplicates
            c.execute("UPDATE settings SET value = '' WHERE key = 'force_channel'")
    except Exception as e:
        logger.error(f"Migration error: {e}")

    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# ----------------- FORCE CHANNELS DATABASE OPS -----------------
def get_all_force_channels():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM force_channels ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_force_channel(name: str, chat_id: str, invite_link: str, channel_type: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO force_channels (name, chat_id, invite_link, channel_type) VALUES (?, ?, ?, ?)",
              (name, chat_id, invite_link, channel_type))
    conn.commit()
    conn.close()

def remove_force_channel(channel_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM force_channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()

# ----------------- CONNECTED GROUPS DATABASE OPS -----------------
def register_or_update_group(chat_id: int, title: str, username: str = None, added_by: int = None, member_count: int = 0, invite_link: str = None):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM connected_groups WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if row:
        c.execute("""
            UPDATE connected_groups 
            SET title = ?, username = COALESCE(?, username), member_count = ?, 
                invite_link = COALESCE(?, invite_link), updated_at = ?
            WHERE chat_id = ?
        """, (title, username, member_count, invite_link, now_str, chat_id))
    else:
        c.execute("""
            INSERT INTO connected_groups (chat_id, title, username, added_by, member_count, invite_link, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, title, username, added_by, member_count, invite_link, now_str))
    conn.commit()
    conn.close()

def get_all_connected_groups():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM connected_groups ORDER BY updated_at DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_connected_group(chat_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM connected_groups WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def set_group_ban_status(chat_id: int, is_banned: bool, reason: str = ""):
    conn = get_db()
    c = conn.cursor()
    ban_val = 1 if is_banned else 0
    c.execute("UPDATE connected_groups SET is_banned = ?, ban_reason = ? WHERE chat_id = ?", (ban_val, reason, chat_id))
    conn.commit()
    conn.close()

def update_group_invite_link(chat_id: int, invite_link: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE connected_groups SET invite_link = ? WHERE chat_id = ?", (invite_link, chat_id))
    conn.commit()
    conn.close()

# ----------------- PROMO / REDEEM CODES OPS -----------------
def create_promo_code(code: str, credits: int, max_uses: int = 1):
    conn = get_db()
    c = conn.cursor()
    clean_code = code.strip().upper()
    c.execute("INSERT OR REPLACE INTO promo_codes (code, credits, max_uses, used_count) VALUES (?, ?, ?, 0)",
              (clean_code, credits, max_uses))
    conn.commit()
    conn.close()

def get_all_promo_codes():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM promo_codes ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_promo_code(promo_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
    conn.commit()
    conn.close()

def redeem_promo_code_for_user(user_id: int, code: str):
    """
    Validates eligibility and claims promo code.
    Returns: (success: bool, credits_added: int, message: str)
    """
    clean_code = code.strip().upper()
    conn = get_db()
    c = conn.cursor()

    # 1. Check user referral count
    c.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (user_id,))
    ref_count = c.fetchone()["count"]

    try:
        min_refers = int(get_setting("redeem_min_refers", "5"))
    except Exception:
        min_refers = 5

    denial_msg = get_setting("redeem_denial_msg", "bhai jab tum refer karoge hi nahi to kyu de hum tumhe redeem code? Isliye tum use nahi kar sakte bhai! Pehle refer karo phir code claim karo.")

    if ref_count < min_refers:
        conn.close()
        return (False, 0, f"🚫 *aᴄᴄᴇss ᴅᴇɴɪᴇᴅ!*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{denial_msg}\n\n• *Aapke Verified Referrals:* `{ref_count}`\n• *Required Referrals:* `{min_refers}`")

    # 2. Check if promo code exists
    c.execute("SELECT * FROM promo_codes WHERE code = ?", (clean_code,))
    promo = c.fetchone()
    if not promo:
        conn.close()
        return (False, 0, "❌ *Invalid Promo Code!* Aisa koi code exist nahi karta.")

    # 3. Check if user already claimed this code
    c.execute("SELECT 1 FROM promo_redemptions WHERE user_id = ? AND code = ?", (user_id, clean_code))
    if c.fetchone():
        conn.close()
        return (False, 0, "⚠️ *Already Redeemed!* Aap ye code pehle hi claim kar chuke hain.")

    # 4. Check if max uses reached
    if promo["used_count"] >= promo["max_uses"]:
        conn.close()
        return (False, 0, "⌛ *Code Expired!* Is code ki maximum limit poori ho chuki hai.")

    # 5. Successfully Redeem: increment used_count, add record, give credits
    credits_to_give = promo["credits"]
    c.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?", (promo["id"],))
    c.execute("INSERT INTO promo_redemptions (user_id, code) VALUES (?, ?)", (user_id, clean_code))
    c.execute("UPDATE users SET credits = credits + ?, total_earned = total_earned + ? WHERE user_id = ?",
              (credits_to_give, credits_to_give, user_id))
    conn.commit()
    conn.close()

    return (True, credits_to_give, f"🎉 *ᴄᴏᴅᴇ rᴇᴅᴇᴇᴍᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ!*\n\n• Code: `{clean_code}`\n• Credits Added: `+{credits_to_give} Credits` 💎")

def get_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def register_user(user_id: int, username: str, first_name: str, ref_id: int = None):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    existing = c.fetchone()
    is_new = False
    rewarded_referrer = None

    if not existing:
        is_new = True
        initial_credits = 1
        referred_by = None
        if ref_id and ref_id != user_id:
            c.execute("SELECT * FROM users WHERE user_id = ?", (ref_id,))
            referrer = c.fetchone()
            if referrer:
                referred_by = ref_id
                rewarded_referrer = dict(referrer)
                # Give dynamic referral bonus points to referrer
                try:
                    c.execute("SELECT value FROM settings WHERE key = 'referral_reward'")
                    r_row = c.fetchone()
                    ref_bonus = int(r_row["value"]) if r_row and r_row["value"] else 5
                except Exception:
                    ref_bonus = 5
                c.execute("""
                    UPDATE users 
                    SET credits = credits + ?, total_earned = total_earned + ? 
                    WHERE user_id = ?
                """, (ref_bonus, ref_bonus, ref_id))
                rewarded_referrer["bonus_awarded"] = ref_bonus

        c.execute("""
            INSERT INTO users (user_id, username, first_name, credits, total_earned, total_used, referred_by, is_banned)
            VALUES (?, ?, ?, ?, ?, 0, ?, 0)
        """, (user_id, username or "", first_name or "", initial_credits, initial_credits, referred_by))
        conn.commit()
    conn.close()
    return {"is_new": is_new, "rewarded_referrer": rewarded_referrer}

def update_user_credits(user_id: int, amount: int, is_usage: bool = False):
    conn = get_db()
    c = conn.cursor()
    if is_usage:
        c.execute("UPDATE users SET credits = credits + ?, total_used = total_used + ? WHERE user_id = ?", 
                  (amount, abs(amount), user_id))
    else:
        c.execute("UPDATE users SET credits = credits + ?, total_earned = total_earned + ? WHERE user_id = ?", 
                  (amount, max(0, amount), user_id))
    conn.commit()
    conn.close()

def set_user_ban(user_id: int, banned: bool):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))
    conn.commit()
    conn.close()

# ----------------- HELPER FUNCTIONS & DATA BACKUP / RESTORE -----------------
DATA_BACKUP_CHANNEL = os.getenv("DATA_CHANNEL", "-1003946288718")

async def backup_db_to_telegram(bot, reason: str = "Auto Backup"):
    """Backs up the SQLite database directly as a document to the private Telegram Data Channel and pins it."""
    ch = get_setting("data_channel", DATA_BACKUP_CHANNEL)
    if not ch:
        return
    try:
        if not os.path.exists(DB_PATH):
            return
        
        now_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")
        caption = (
            f"📦 <b>TG2NUM DATABASE SNAPSHOT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Trigger:</b> <code>{reason}</code>\n"
            f"• <b>Timestamp:</b> <code>{now_str}</code>\n"
            f"• <b>Storage:</b> Permanent Telegram Cloud\n"
            f"🛡️ <i>Live cloud backup safe from Render restarts!</i>"
        )
        with open(DB_PATH, "rb") as f:
            sent_msg = await bot.send_document(
                chat_id=ch,
                document=f,
                filename="database.db",
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        # Pin the latest backup message so it can always be restored upon cold start / Render redeploy
        try:
            await bot.pin_chat_message(chat_id=ch, message_id=sent_msg.message_id, disable_notification=True)
        except Exception as pe:
            logger.debug(f"Note: Pinning backup message: {pe}")
        logger.info(f"Database successfully backed up to Telegram channel {ch}")
    except Exception as e:
        logger.error(f"Failed to backup database to Telegram channel {ch}: {e}")

async def restore_db_from_telegram(bot):
    """Restores the latest database backup from the Telegram private channel if Render container restarted."""
    ch = get_setting("data_channel", DATA_BACKUP_CHANNEL)
    if not ch:
        return
    try:
        chat = await bot.get_chat(ch)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            logger.info("No pinned database backup found in Telegram storage channel.")
            return

        file_id = pinned.document.file_id
        tg_file = await bot.get_file(file_id)
        
        # Download backup
        backup_tmp = DB_PATH + ".downloaded"
        await tg_file.download_to_drive(custom_path=backup_tmp)

        # Validate that downloaded file is a valid sqlite3 database
        test_conn = sqlite3.connect(backup_tmp)
        test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        test_conn.close()

        # Safely replace DB_PATH
        if os.path.exists(DB_PATH):
            try:
                os.remove(DB_PATH + ".old")
            except Exception:
                pass
            try:
                os.rename(DB_PATH, DB_PATH + ".old")
            except Exception:
                pass
        os.replace(backup_tmp, DB_PATH)
        logger.info(f"✅ Successfully restored database from Telegram Cloud Storage channel ({ch})!")
        # Re-initialize/verify DB
        init_db()
    except Exception as e:
        logger.error(f"Could not restore database from Telegram channel {ch}: {e}")

async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str):
    log_channel = get_setting("log_channel", DATA_BACKUP_CHANNEL)
    if log_channel:
        try:
            await context.bot.send_message(chat_id=log_channel, text=message, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send log to channel {log_channel}: {e}")

def record_join_request(user_id: int, chat_id: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO join_requests (user_id, chat_id) VALUES (?, ?)", (user_id, str(chat_id)))
    conn.commit()
    conn.close()

def has_sent_join_request(user_id: int, chat_id: str) -> bool:
    conn = get_db()
    c = conn.cursor()
    # Normalize chat_id matching (with or without -100 prefix)
    clean_id = str(chat_id)
    c.execute("SELECT 1 FROM join_requests WHERE user_id = ? AND (chat_id = ? OR chat_id = ?)",
              (user_id, clean_id, clean_id.replace("-100", "-")))
    row = c.fetchone()
    conn.close()
    return bool(row)

async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when bot status changes in a chat (e.g. added as admin to group)."""
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    if chat.type not in ["group", "supergroup"]:
        return

    new_status = result.new_chat_member.status
    if new_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        try:
            m_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            m_count = 0
        added_by = result.from_user.id if result.from_user else None
        register_or_update_group(chat.id, chat.title or "Group", chat.username, added_by, m_count)
        
        # Send welcoming & rules alert in group
        min_m = get_setting("group_min_members", "100")
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🎉 *TG2NUM BOT ACTIVATED IN THIS GROUP!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• *Group Title:* {chat.title}\n"
                    f"• *Total Members:* `{m_count}` Users\n\n"
                    f"🚀 *Unlimited Free Search Rules:*\n"
                    f"1. Group me kam se kam *{min_m}+ Real Members* hona zaroori hai.\n"
                    f"2. Unlimited Search sirf **Group Owner & Admins** ke liye active hai.\n"
                    f"3. Seedha koi bhi 9-10 digit Telegram ID is chat me send karein lookup ke liye!\n\n"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send welcome to group {chat.id}: {e}")

        # Send Audit Log to Bot Owner / Log channel
        await send_log(
            context,
            f"👥 <b>Bot Added as Admin in Group:</b>\n"
            f"• Title: <code>{chat.title}</code>\n"
            f"• Chat ID: <code>{chat.id}</code>\n"
            f"• Members: <code>{m_count}</code>\n"
            f"• Added By: {result.from_user.mention_html() if result.from_user else 'Unknown'}"
        )

async def chat_join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by Telegram whenever a user sends a join request to a channel where bot is admin."""
    req = update.chat_join_request
    if not req:
        return
    user = req.from_user
    chat = req.chat
    record_join_request(user.id, str(chat.id))
    logger.info(f"Join request received and recorded for user {user.id} in chat {chat.id} ({chat.title})")

async def check_single_channel_joined(ch: dict, user_id: int, bot) -> bool:
    """Checks if a user has joined or submitted request for a single channel."""
    target = ch["chat_id"]
    ch_type = ch.get("channel_type", "public")

    # 1. Join request channel: if pending request exists in DB, verified immediately
    if ch_type == "request":
        if has_sent_join_request(user_id, target):
            return True

    # 2. Check chat member status via Telegram API
    try:
        member = await bot.get_chat_member(chat_id=target, user_id=user_id)
        if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return True
        if ch_type == "request" and member.status == ChatMemberStatus.RESTRICTED:
            return True
        return False
    except Exception as e:
        err_str = str(e).lower()
        if "user not found" in err_str or "participant_id_invalid" in err_str:
            if ch_type == "request" and has_sent_join_request(user_id, target):
                return True
            return False
        logger.error(f"Error checking channel {target} for {user_id}: {e}")
        return True  # If error is on Telegram/channel permission side, avoid blocking user falsely

async def get_unjoined_channels(user_id: int, bot) -> list:
    """Returns list of channels that user has NOT joined yet. Parallelized for ultra-fast verification."""
    if user_id == ADMIN_ID:
        return []

    channels = get_all_force_channels()
    if not channels:
        return []

    # Run member checks concurrently across all channels for maximum speed
    results = await asyncio.gather(*(check_single_channel_joined(ch, user_id, bot) for ch in channels))
    return [ch for ch, joined in zip(channels, results) if not joined]

async def is_user_joined(user_id: int, bot) -> bool:
    unjoined = await get_unjoined_channels(user_id, bot)
    return len(unjoined) == 0

def get_force_channels_keyboard(unjoined_channels: list) -> InlineKeyboardMarkup:
    """Builds inline keyboard with clickable 'Join' buttons in 2x2 layout + Check Joined."""
    join_btns = []

    for ch in unjoined_channels:
        link = ch.get("invite_link")
        if not link:
            ch_id = ch["chat_id"]
            link = f"https://t.me/{ch_id.replace('@', '')}" if ch_id.startswith("@") else f"https://t.me/{ch_id}"
        join_btns.append(InlineKeyboardButton("Join", url=link, style="primary"))

    # YouTube Channel button for show-off / branding (pure show-off, doesn't block verification)
    yt_link = get_setting("youtube_channel_link", "https://youtube.com/@toxicexploit?si=6evZKvA2IxUhv9U4")
    if yt_link:
        join_btns.append(InlineKeyboardButton("Join", url=yt_link, style="primary"))

    # Layout buttons: If exactly 5 buttons -> Row 1 (2), Row 2 (1), Row 3 (2)
    keyboard = []
    n = len(join_btns)
    if n == 5:
        keyboard.append(join_btns[0:2])  # Row 1: 2 buttons
        keyboard.append(join_btns[2:3])  # Row 2: 1 button
        keyboard.append(join_btns[3:5])  # Row 3: 2 buttons
    else:
        for i in range(0, n, 2):
            keyboard.append(join_btns[i:i + 2])

    # Full width Check Joined button
    keyboard.append([InlineKeyboardButton("✅ Check Joined / Verified", callback_data="check_joined", style="primary")])
    return InlineKeyboardMarkup(keyboard)

def get_main_keyboard(is_owner: bool = False):
    keyboard = [
        [KeyboardButton("⚡ ꜱᴇᴀʀᴄʜ ᴛᴇʟᴇɢʀᴀᴍ ɴᴜᴍʙᴇʀ ⚡", style="primary")],
        [KeyboardButton("🚀 ᴜɴʟɪᴍɪᴛᴇᴅ ғrᴇᴇ sᴇarᴄʜ", style="primary")],
        [KeyboardButton("👤 ᴍʏ ᴘʀᴏꜰɪʟᴇ", style="primary"), KeyboardButton("🎁 ʀᴇꜰᴇʀ & ᴇᴀʀɴ", style="primary")],
        [KeyboardButton("💎 ʙᴜʏ ᴠɪᴘ ᴄʀᴇᴅɪᴛꜱ", style="primary"), KeyboardButton("🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ (+1)", style="primary")],
        [KeyboardButton("🎟️ rᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ", style="primary"), KeyboardButton("📞 ꜱᴜᴘᴘᴏʀᴛ", style="primary")]
    ]
    if is_owner:
        keyboard.append([KeyboardButton("👑 ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ", style="primary")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ----------------- HANDLERS -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    ref_id = None
    if args and args[0].isdigit():
        ref_id = int(args[0])

    reg_result = register_user(user.id, user.username, user.first_name, ref_id)
    is_new = reg_result["is_new"]
    referrer_info = reg_result["rewarded_referrer"]

    # If new user registered, send detailed notification to Admin & Referrer
    if is_new:
        now_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")
        user_uname_str = f"@{user.username}" if user.username else "No Username"

        # 1. Notify Referrer (Short, Crisp, Pro Format)
        if referrer_info:
            ref_target_id = referrer_info["user_id"]
            bonus_amt = referrer_info.get("bonus_awarded", 5)
            new_balance = referrer_info["credits"] + bonus_amt
            u_ident = f"@{user.username}" if user.username else user.first_name
            ref_notify_msg = (
                f"🎉 *New Referral Joined! (+{bonus_amt} Credits)*\n"
                f"• User: {u_ident} (`{user.id}`)\n"
                f"• Balance: `{new_balance}` Credits 💎"
            )
            try:
                await context.bot.send_message(
                    chat_id=ref_target_id,
                    text=ref_notify_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to send referral notification to {ref_target_id}: {e}")

        # 2. Notify Admin Directly (Short, Clean, Pro Format)
        admin_alert = (
            f"🔔 *New User:* {user.mention_markdown()} (`{user.id}`) | Ref: `{ref_id or 'Direct'}`"
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_alert,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send new user alert to admin: {e}")

        # 3. Channel log
        await send_log(context, f"🟢 <b>New User Registered:</b>\n• Identity: {user.mention_html()} (<code>{user.id}</code>)\n• Referral Origin: <code>{ref_id or 'Direct'}</code>\n• Time: <code>{now_str}</code>")

    u = get_user(user.id)
    if u and u["is_banned"] == 1:
        await update.message.reply_text("🚫 *Your access has been suspended by Administration.*", parse_mode=ParseMode.MARKDOWN)
        return

    # Check force channel join
    unjoined = await get_unjoined_channels(user.id, context.bot)
    if unjoined:
        keyboard = get_force_channels_keyboard(unjoined)
        await update.message.reply_text(
            "⚠️ *Access Required: Channel Membership*\n\n"
            "Bot use karne ke liye neeche diye gaye sabhi official channels ko join karein.\n\n"
            "Join karne ke baad **✅ Check Joined / Verified** par click karein:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    ref_rew = get_setting("referral_reward", "5")
    welcome_text = (
        f"👋 *ᴡᴇʟᴄᴏᴍᴇ, {user.first_name}!* 🇮🇳\n\n"
        "╔══════════════════════════════════════╗\n"
        "  🏛 *ᴛɢ2ɴᴜᴍ ᴇɴᴛᴇʀᴘʀɪsᴇ ɪɴᴛᴇʟʟɪɢᴇɴᴄᴇ* \n"
        "  _aᴅᴠaɴᴄᴇᴅ ᴏsɪɴᴛ • ɪᴅᴇɴᴛɪᴛʏ ʟᴏᴏᴋᴜᴘ ᴇɴɢɪɴᴇ_\n"
        "╚══════════════════════════════════════╝\n\n"
        "🔹 *sᴇᴄᴜrɪᴛʏ ᴛɪᴇr:* `ᴠᴇrɪғɪᴇᴅ sᴛaɴᴅarᴅ`\n"
        "🔹 *aᴄᴛɪᴠᴇ aʟʟᴏᴄaᴛɪᴏɴ:* `1 ɪɴɪᴛɪaʟ ᴄrᴇᴅɪᴛ`\n"
        f"🔹 *rᴇғᴇrraʟ rᴇᴡarᴅ:* `+{ref_rew} ᴄrᴇᴅɪᴛs / ғrɪᴇɴᴅ`\n"
        "🔹 *ᴅaᴛa aᴄᴄᴜraᴄʏ:* `ᴅɪrᴇᴄᴛ rᴇɢɪsᴛrʏ ᴍaᴛᴄʜ`\n\n"
        "⚡ *ǫᴜɪᴄᴋ sᴛarᴛ:* ɴᴇᴇᴄʜᴇ ᴍᴇɴᴜ sᴇ ᴏᴘᴛɪᴏɴ sᴇʟᴇᴄᴛ ᴋarᴇɪɴ ya direct *ᴛᴇʟᴇɢraᴍ ɴᴜᴍᴇrɪᴄaʟ ɪᴅ* sᴇɴᴅ ᴋarᴇɪɴ:"
    )
    is_owner = (user.id == ADMIN_ID)
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(is_owner=is_owner), parse_mode=ParseMode.MARKDOWN)

# Helper function to render User Info display
async def show_user_info_card(bot, chat_id, target_id: int):
    u = get_user(target_id)
    if not u:
        await bot.send_message(chat_id=chat_id, text=f"❌ User with ID `{target_id}` not found.", parse_mode=ParseMode.MARKDOWN)
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name, username, created_at FROM users WHERE referred_by = ?", (target_id,))
    refs = c.fetchall()
    conn.close()

    user_link = f"[@{u['username']}](https://t.me/{u['username']})" if u["username"] else f"[{u['first_name']}](tg://user?id={target_id})"
    inviter_link = f"[{u['referred_by']}](tg://user?id={u['referred_by']})" if u["referred_by"] else "Direct (None)"
    ban_status = "🚫 BANNED" if u["is_banned"] == 1 else "🟢 ACTIVE"

    text = (
        f"🔎 *USER DETAILS FOR:* {user_link}\n"
        f"• *Telegram ID:* `{target_id}`\n"
        f"• *Status:* {ban_status}\n"
        f"• *Current Credits:* `{u['credits']}` 💎\n"
        f"• *Total Earned:* `{u['total_earned']}`\n"
        f"• *Total Used:* `{u['total_used']}`\n"
        f"• *Invited By:* {inviter_link}\n\n"
        f"👥 *Users Referred by this user ({len(refs)}):*\n"
    )
    for r in refs[:15]:
        r_link = f"[@{r['username']}](https://t.me/{r['username']})" if r["username"] else f"[{r['first_name']}](tg://user?id={r['user_id']})"
        text += f"• {r_link} (`{r['user_id']}`)\n"
    if len(refs) > 15:
        text += f"...and {len(refs) - 15} more.\n"

    direct_chat_url = f"https://t.me/{u['username']}" if u["username"] else f"tg://user?id={target_id}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Open Chat with User", url=direct_chat_url)]
    ])

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# Helper function to render Force Channels Admin List Menu
async def show_force_channels_admin_menu(target_message, is_callback: bool = False):
    channels = get_all_force_channels()

    msg = "📢 *FORCE CHANNELS MANAGEMENT CONSOLE*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if not channels:
        msg += "⚠️ *No Force Channels Configured Yet.*\nUsers can use the bot without joining any channel.\n"
    else:
        msg += f"Total Active Channels: *{len(channels)}*\n\n"
        for idx, ch in enumerate(channels, 1):
            type_badge = "🌐 PUBLIC" if ch["channel_type"] == "public" else ("🔒 PRIVATE" if ch["channel_type"] == "private" else "📨 REQUEST")
            msg += (
                f"*{idx}. {ch['name']}* [{type_badge}]\n"
                f"   • *Chat ID:* `{ch['chat_id']}`\n"
                f"   • *Link:* [Open Channel Link]({ch['invite_link']})\n\n"
            )

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nAdd new channels or remove existing channels below:"

    buttons = [
        [
            InlineKeyboardButton("➕ Add Public Channel", callback_data="fch_add_public"),
            InlineKeyboardButton("➕ Add Private Channel", callback_data="fch_add_private")
        ],
        [
            InlineKeyboardButton("➕ Add Join Request Channel", callback_data="fch_add_request")
        ]
    ]

    # If channels exist, provide direct remove buttons
    if channels:
        buttons.append([InlineKeyboardButton("🗑 Remove a Channel", callback_data="fch_remove_menu")])

    buttons.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")])

    kb = InlineKeyboardMarkup(buttons)
    if is_callback:
        await target_message.edit_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await target_message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# Helper function to render Promo / Redeem Codes Admin Console
async def show_promo_admin_menu(target_message, is_callback: bool = False):
    promos = get_all_promo_codes()
    min_ref = get_setting("redeem_min_refers", "5")
    denial_msg = get_setting("redeem_denial_msg", "bhai jab tum refer karoge hi nahi to kyu de hum tumhe redeem code? Isliye tum use nahi kar sakte bhai! Pehle refer karo phir code claim karo.")

    msg = (
        "🎟️ *PROMO & REDEEM CODES MANAGEMENT*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• *Minimum Referrals Required:* `{min_ref}` Referrals\n"
        f"• *Denial Notice:* _{denial_msg}_\n"
        "────────────────────────────────────────\n"
    )

    if not promos:
        msg += "⚠️ *No Promo Codes Active Currently.*\nGenerate a code below to distribute credits to users!\n"
    else:
        msg += f"Active Codes ({len(promos)} total):\n\n"
        for idx, p in enumerate(promos, 1):
            status = "🔴 EXPIRED" if p["used_count"] >= p["max_uses"] else "🟢 ACTIVE"
            msg += (
                f"*{idx}. Code:* `{p['code']}` [{status}]\n"
                f"   • *Credits:* `+{p['credits']} 💎`\n"
                f"   • *Usage:* `{p['used_count']}/{p['max_uses']}` Claims\n\n"
            )

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nSelect an action below:"

    buttons = [
        [
            InlineKeyboardButton("➕ Create Promo Code", callback_data="adm_promo_create"),
            InlineKeyboardButton("🗑 Delete a Code", callback_data="adm_promo_del_menu")
        ],
        [
            InlineKeyboardButton("⚙️ Set Min Referrals", callback_data="adm_promo_setmin"),
            InlineKeyboardButton("✏️ Set Denial Notice", callback_data="adm_promo_setmsg")
        ],
        [
            InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")
        ]
    ]

    kb = InlineKeyboardMarkup(buttons)
    if is_callback:
        await target_message.edit_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await target_message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# Helper function to render Connected Groups Admin Console
async def show_connected_groups_admin_menu(target_message, is_callback: bool = False):
    groups = get_all_connected_groups()
    min_m = get_setting("group_min_members", "100")

    msg = (
        "👥 *CONNECTED GROUPS & ACCESS CONSOLE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• *Rule Requirement:* Minimum `{min_m}+ Members` in group\n"
        "• *Privilege:* Only Group Owner & Admins receive Unlimited Free Searches\n"
        "────────────────────────────────────────\n"
    )

    if not groups:
        msg += "⚠️ *No Groups Connected Yet.*\nWhen users add this bot as Admin to their group, it will automatically register here.\n"
    else:
        msg += f"Active Connected Groups ({len(groups)} total):\n\n"
        for idx, g in enumerate(groups, 1):
            status = "🔴 BANNED / RESTRICTED" if g["is_banned"] == 1 else "🟢 ACTIVE"
            uname = f"@{g['username']}" if g["username"] else "Private Group"
            msg += (
                f"*{idx}. {g['title']}* [{status}]\n"
                f"   • *Chat ID:* `{g['chat_id']}` ({uname})\n"
                f"   • *Members:* `{g['member_count']}` Users\n"
                f"   • *Status Note:* `{g['ban_reason'] if g['is_banned'] == 1 and g['ban_reason'] else 'Normal Access'}`\n\n"
            )

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nSelect an option below to manage a group:"

    buttons = []
    if groups:
        for g in groups:
            st_icon = "🟢" if g["is_banned"] == 0 else "🔴"
            buttons.append([InlineKeyboardButton(f"{st_icon} Manage: {g['title'][:25]}", callback_data=f"adm_grp_mgr_{g['chat_id']}")])

    buttons.append([InlineKeyboardButton("⚙️ Set Min Members Requirement", callback_data="adm_grp_setmin")])
    buttons.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")])

    kb = InlineKeyboardMarkup(buttons)
    if is_callback:
        await target_message.edit_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await target_message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "check_joined":
        unjoined = await get_unjoined_channels(user_id, context.bot)
        if not unjoined:
            chat_id = query.message.chat.id if query.message else user_id
            await query.edit_message_text("✅ *Dhanyawad! Membership successfully verified.*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(
                chat_id=chat_id,
                text="Aap ab bot use kar sakte hain! Kripya neeche button se select karein ya Telegram ID bhejein:",
                reply_markup=get_main_keyboard(is_owner=(user_id == ADMIN_ID))
            )
        else:
            await query.answer("❌ Verification pending. Kripya sabhi channel join karein!", show_alert=True)
            # Refresh with updated buttons for remaining unjoined channels
            keyboard = get_force_channels_keyboard(unjoined)
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception:
                pass

    elif query.data == "buy_vip_menu":
        p10 = get_setting("plan_10_credits", "50")
        p50 = get_setting("plan_50_credits", "250")
        p100 = get_setting("plan_100_credits", "500")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⚡ Tier I — ₹10 ({p10} Credits)", callback_data="buy_10")],
            [InlineKeyboardButton(f"🔥 Tier II — ₹50 ({p50} Credits)", callback_data="buy_50")],
            [InlineKeyboardButton(f"👑 Tier III — ₹100 ({p100} Credits)", callback_data="buy_100")]
        ])
        await query.message.reply_text(
            "💎 *EXECUTIVE QUERY ALLOCATIONS*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Select a pre-configured tier to initiate instant QR settlement:",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data.startswith("buy_"):
        plan = query.data.split("_")[1]
        upi_id = get_setting("upi_id", "merchant@upi")
        
        amounts = {
            "10": (10, int(get_setting("plan_10_credits", "50"))),
            "50": (50, int(get_setting("plan_50_credits", "250"))),
            "100": (100, int(get_setting("plan_100_credits", "500")))
        }
        if plan not in amounts:
            return

        rupees, creds = amounts[plan]

        # Record pending purchase order in DB for this user
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO payments (user_id, amount, credits, utr, status)
            VALUES (?, ?, ?, 'PENDING_SCREENSHOT', 'pending')
        """, (user_id, rupees, creds))
        pay_order_id = c.lastrowid
        conn.commit()
        conn.close()

        upi_url = f"upi://pay?pa={upi_id}&pn=TG2NUM&am={rupees}&cu=INR&tn=User_{user_id}_Credits"

        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        img.save(bio, "PNG")
        bio.seek(0)

        caption = (
            f"💎 *VIP ALLOCATION INVOICE*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Package:* `{creds} Query Credits`\n"
            f"• *Settlement Amount:* `₹{rupees} INR`\n"
            f"• *Merchant UPI:* `{upi_id}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📲 *Settlement Instructions:*\n"
            "1. Scan the dynamic QR code above using PhonePe, Paytm, or GPay.\n"
            "2. Complete payment and **send the payment screenshot directly here in this chat**.\n"
            "3. Admin will verify and activate your credits instantly."
        )
        await query.message.reply_photo(photo=bio, caption=caption, parse_mode=ParseMode.MARKDOWN)

    # ----------------- ADMIN PAYMENT APPROVALS -----------------
    elif query.data.startswith("appr_quick_"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Unauthorized!", show_alert=True)
            return
        parts = query.data.split("_")
        target_uid = int(parts[2])
        credits_to_add = int(parts[3])

        update_user_credits(target_uid, credits_to_add)
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n✅ *Status: APPROVED (+{credits_to_add} Credits)*",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"🎉 *Payment Verified & Approved!*\n\n"
                     f"Aapke account me *{credits_to_add} Credits* add kar diye gaye hain. Bot use karna shuru karein!",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to notify user {target_uid}: {e}")

    elif query.data.startswith("rej_quick_"):
        if query.from_user.id != ADMIN_ID:
            await query.answer("Unauthorized!", show_alert=True)
            return
        target_uid = int(query.data.split("_")[2])
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ *Status: REJECTED*",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text="❌ *Payment Verification Failed!*\n\n"
                     "Aapka payment proof reject kar diya gaya hai. Agar koi dikkat hai toh support se sampark karein: @Your_father_786k",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

    # ----------------- ADMIN ACTION CALLBACKS (FULLY INTERACTIVE) -----------------
    elif query.data == "adm_refresh":
        if query.from_user.id == ADMIN_ID:
            await admin_command(update, context)

    elif query.data == "adm_cancel_input":
        if query.from_user.id == ADMIN_ID:
            if user_id in ADMIN_STATES:
                del ADMIN_STATES[user_id]
            await query.edit_message_text("❌ Action cancelled.")
            await admin_command(update, context)

    elif query.data == "adm_toprefs":
        if query.from_user.id == ADMIN_ID:
            await top_referrers_cmd(update, context)

    elif query.data.startswith("ref_tree_"):
        if query.from_user.id == ADMIN_ID:
            target_referrer_id = int(query.data.split("_")[2])
            await show_referrer_details(context.bot, update.effective_chat.id, target_referrer_id, edit_message=query.message)

    elif query.data == "adm_addcreds":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_addcreds_step1"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "➕ *ADD USER CREDITS (Step 1/2)*\n\n"
                "Please enter the **Telegram User ID**:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_remcreds":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_remcreds_step1"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "➖ *DEDUCT USER CREDITS (Step 1/2)*\n\n"
                "Please enter the **Telegram User ID**:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_userinfo":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_userinfo"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "🔎 *INSPECT USER DETAILS*\n\n"
                "Please enter the **Telegram User ID** to view dossier:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    # --- BAN & UNBAN ADMIN CALLBACKS ---
    elif query.data == "adm_ban_menu":
        if query.from_user.id == ADMIN_ID:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Ban User", callback_data="adm_do_ban")],
                [InlineKeyboardButton("✅ Unban User", callback_data="adm_do_unban")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")]
            ])
            await query.message.reply_text("⚙️ *USER ACCESS MANAGEMENT (BAN / UNBAN):*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    elif query.data == "adm_do_ban":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_ban"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "🚫 *BAN USER*\n\n"
                "Please enter the **Telegram User ID** to ban:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_do_unban":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_unban"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "✅ *UNBAN USER*\n\n"
                "Please enter the **Telegram User ID** to unban:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    # --- FORCE CHANNELS LIST & MANAGEMENT CALLBACKS ---
    elif query.data == "adm_setchannel":
        if query.from_user.id == ADMIN_ID:
            await show_force_channels_admin_menu(query.message, is_callback=True)

    elif query.data == "fch_add_public":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "fch_add_public"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="fch_back_to_menu")]])
            await query.message.reply_text(
                "🌐 *ADD PUBLIC FORCE CHANNEL*\n\n"
                "Please send the public channel username (e.g. `@MyChannel`):\n"
                "_(Bot must be added as administrator in the channel)_",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "fch_add_private":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "fch_add_private_step1"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="fch_back_to_menu")]])
            await query.message.reply_text(
                "🔒 *ADD PRIVATE FORCE CHANNEL (Step 1/2)*\n\n"
                "Please send the **Private Channel Numeric Chat ID** (e.g. `-1001234567890`):\n"
                "_(Bot must be added as administrator in the channel)_",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "fch_add_request":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "fch_add_request_step1"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="fch_back_to_menu")]])
            await query.message.reply_text(
                "📨 *ADD JOIN REQUEST CHANNEL (Step 1/2)*\n\n"
                "Please send the **Channel Numeric Chat ID** (e.g. `-1001234567890`):\n"
                "_(In this mode, once user requests to join, verification is granted!)_",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "fch_remove_menu":
        if query.from_user.id == ADMIN_ID:
            channels = get_all_force_channels()
            if not channels:
                await query.answer("No channels to remove!", show_alert=True)
                return
            kb_rows = []
            for ch in channels:
                kb_rows.append([InlineKeyboardButton(f"❌ Delete: {ch['name']}", callback_data=f"fch_del_{ch['id']}")])
            kb_rows.append([InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_setchannel")])
            await query.edit_message_text(
                "🗑 *SELECT CHANNEL TO REMOVE:*\n\nClick on the channel button below that you wish to remove from force join verification:",
                reply_markup=InlineKeyboardMarkup(kb_rows),
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data.startswith("fch_del_"):
        if query.from_user.id == ADMIN_ID:
            ch_db_id = int(query.data.split("_")[2])
            remove_force_channel(ch_db_id)
            await query.answer("✅ Channel removed successfully!", show_alert=True)
            await show_force_channels_admin_menu(query.message, is_callback=True)

    elif query.data == "fch_back_to_menu":
        if query.from_user.id == ADMIN_ID:
            if user_id in ADMIN_STATES:
                del ADMIN_STATES[user_id]
            await show_force_channels_admin_menu(query.message, is_callback=True)

    elif query.data == "adm_setlog":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_setlog"}
            cur = get_setting("log_channel", "Not Set")
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                f"📑 *AUDIT LOG CHANNEL*\n\n"
                f"• Current: `{cur}`\n\n"
                "Please enter the **Log Channel/Group ID** (e.g. `-100xxxxxxxxxx`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_setupi":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_setupi"}
            cur = get_setting("upi_id", "merchant@upi")
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                f"💳 *SET UPI GATEWAY ID*\n\n"
                f"• Current UPI: `{cur}`\n\n"
                "Please enter the new UPI address (e.g. `yourname@upi`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_setprice":
        if query.from_user.id == ADMIN_ID:
            p10 = get_setting("plan_10_credits", "50")
            p50 = get_setting("plan_50_credits", "250")
            p100 = get_setting("plan_100_credits", "500")

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Modify ₹10 Plan ({p10} cr)", callback_data="price_edit_10")],
                [InlineKeyboardButton(f"Modify ₹50 Plan ({p50} cr)", callback_data="price_edit_50")],
                [InlineKeyboardButton(f"Modify ₹100 Plan ({p100} cr)", callback_data="price_edit_100")],
                [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")]
            ])
            await query.message.reply_text(
                "🏷 *TIER PRICING CONFIGURATION*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nChoose which package rate to update:",
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data.startswith("price_edit_"):
        if query.from_user.id == ADMIN_ID:
            plan = query.data.split("_")[2]
            ADMIN_STATES[user_id] = {"action": "interactive_setprice", "plan": plan}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                f"🏷 *UPDATE ₹{plan} PLAN CREDITS*\n\n"
                f"Please enter the new credits amount for ₹{plan} package:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_broadcast":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "interactive_broadcast"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                "📣 *SEND GLOBAL BROADCAST*\n\n"
                "Please type or forward your announcement message to broadcast to all users:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    # ----------------- ADMIN PROMO / REDEEM CODE CALLBACKS -----------------
    elif query.data == "adm_promo_menu":
        if query.from_user.id == ADMIN_ID:
            await show_promo_admin_menu(query.message, is_callback=True)

    elif query.data == "adm_promo_create":
        if query.from_user.id == ADMIN_ID:
            ADMIN_STATES[user_id] = {"action": "promo_create_step1"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_promo_menu")]])
            await query.message.reply_text(
                "🎟️ *CREATE NEW PROMO CODE (Step 1/3)*\n\n"
                "Please enter the **Promo Code text** (e.g. `WELCOME50` or `BONUS10`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_promo_del_menu":
        if query.from_user.id == ADMIN_ID:
            promos = get_all_promo_codes()
            if not promos:
                await query.answer("No promo codes exist to delete!", show_alert=True)
                return
            del_buttons = []
            for p in promos:
                del_buttons.append([InlineKeyboardButton(f"🗑 Delete: {p['code']} ({p['credits']} cr)", callback_data=f"adm_pdel_{p['id']}")])
            del_buttons.append([InlineKeyboardButton("🔙 Back to Promo Menu", callback_data="adm_promo_menu")])
            await query.message.edit_text(
                "🗑 *DELETE PROMO CODE*\n\nSelect a promo code from below to permanently remove it:",
                reply_markup=InlineKeyboardMarkup(del_buttons),
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data.startswith("adm_pdel_"):
        if query.from_user.id == ADMIN_ID:
            pid = int(query.data.split("_")[2])
            delete_promo_code(pid)
            await query.answer("✅ Promo code deleted successfully!", show_alert=True)
            await show_promo_admin_menu(query.message, is_callback=True)

    elif query.data == "adm_promo_setmin":
        if query.from_user.id == ADMIN_ID:
            cur_min = get_setting("redeem_min_refers", "5")
            ADMIN_STATES[user_id] = {"action": "promo_set_min"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_promo_menu")]])
            await query.message.reply_text(
                f"⚙️ *SET MINIMUM REFERRALS FOR REDEEM CODE*\n\n"
                f"• Current requirement: `{cur_min}` verified referrals\n\n"
                "Please enter the new number of referrals required to redeem codes (e.g. `5`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_promo_setmsg":
        if query.from_user.id == ADMIN_ID:
            cur_msg = get_setting("redeem_denial_msg", "bhai jab tum refer karoge hi nahi to kyu de hum tumhe redeem code? Isliye tum use nahi kar sakte bhai! Pehle refer karo phir code claim karo.")
            ADMIN_STATES[user_id] = {"action": "promo_set_msg"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_promo_menu")]])
            await query.message.reply_text(
                f"✏️ *CUSTOMIZE REDEEM DENIAL NOTICE*\n\n"
                f"• Current notice:\n_{cur_msg}_\n\n"
                "Please type the new denial message that users with insufficient referrals will receive:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    # ----------------- ADMIN CONNECTED GROUPS CALLBACKS -----------------
    elif query.data == "adm_groups_menu":
        if query.from_user.id == ADMIN_ID:
            await show_connected_groups_admin_menu(query.message, is_callback=True)

    elif query.data.startswith("adm_grp_mgr_"):
        if query.from_user.id == ADMIN_ID:
            gid = int(query.data.split("_")[3])
            g = get_connected_group(gid)
            if not g:
                await query.answer("Group not found!", show_alert=True)
                return

            status = "🔴 BANNED / RESTRICTED" if g["is_banned"] == 1 else "🟢 ACTIVE"
            uname = f"@{g['username']}" if g["username"] else "Private Group"
            g_msg = (
                f"👥 *MANAGE GROUP: {g['title']}*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• *Chat ID:* `{g['chat_id']}`\n"
                f"• *Username:* {uname}\n"
                f"• *Members:* `{g['member_count']}` Users\n"
                f"• *Status:* {status}\n"
            )
            if g["is_banned"] == 1:
                g_msg += f"• *Ban Reason:* _{g['ban_reason'] or 'No reason specified'}_\n"
            if g["invite_link"]:
                g_msg += f"• *Invite Link:* [Open Group Link]({g['invite_link']})\n"

            g_msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nChoose an action:"

            mgr_buttons = [
                [InlineKeyboardButton("🔗 Get / Refresh Invite Link", callback_data=f"adm_grp_link_{gid}")]
            ]
            if g["is_banned"] == 0:
                mgr_buttons.append([InlineKeyboardButton("🚫 Restrict / Ban Group Access", callback_data=f"adm_grp_ban_{gid}")])
            else:
                mgr_buttons.append([InlineKeyboardButton("✅ Unban / Restore Access", callback_data=f"adm_grp_unban_{gid}")])

            mgr_buttons.append([InlineKeyboardButton("🔙 Back to Groups List", callback_data="adm_groups_menu")])

            await query.message.edit_text(g_msg, reply_markup=InlineKeyboardMarkup(mgr_buttons), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif query.data.startswith("adm_grp_link_"):
        if query.from_user.id == ADMIN_ID:
            gid = int(query.data.split("_")[3])
            link = None
            try:
                # Try creating or fetching invite link from Telegram API
                chat_obj = await context.bot.get_chat(gid)
                link = chat_obj.invite_link
                if not link:
                    link = await context.bot.export_chat_invite_link(gid)
            except Exception as e:
                logger.error(f"Error fetching invite link for group {gid}: {e}")

            if link:
                update_group_invite_link(gid, link)
                await query.answer("✅ Invite link generated!", show_alert=True)
                g = get_connected_group(gid)
                await query.message.reply_text(
                    f"🔗 *GROUP INVITE LINK GENERATED*\n\n"
                    f"• *Group:* {g['title']}\n"
                    f"• *Link:* {link}\n\n"
                    "Admin can use this link to inspect group members & activity.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.answer("❌ Bot has no permission to create invite link in that group!", show_alert=True)

    elif query.data.startswith("adm_grp_ban_"):
        if query.from_user.id == ADMIN_ID:
            gid = int(query.data.split("_")[3])
            g = get_connected_group(gid)
            ADMIN_STATES[user_id] = {"action": "grp_ban_reason", "chat_id": gid, "title": g["title"] if g else "Group"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_grp_mgr_{gid}")]])
            await query.message.reply_text(
                f"🚫 *RESTRICT GROUP ACCESS: {g['title'] if g else gid}*\n\n"
                "Please type the **Ban Reason** to notify the group:\n"
                "_(e.g. 'Group members are inactive / fake. Unlimited search has been disabled.')_",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data.startswith("adm_grp_unban_"):
        if query.from_user.id == ADMIN_ID:
            gid = int(query.data.split("_")[3])
            set_group_ban_status(gid, False, "")
            await query.answer("✅ Group unbanned successfully!", show_alert=True)
            # Send notification directly into group
            try:
                await context.bot.send_message(
                    chat_id=gid,
                    text=(
                        "✅ *GROUP ACCESS RESTORED*\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Bot Owner has restored Unlimited Free Search access for this group.\n"
                        "Group Owner and Admins can now resume searches!\n\n"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to send unban alert to group {gid}: {e}")

            await show_connected_groups_admin_menu(query.message, is_callback=True)

    elif query.data == "adm_grp_setmin":
        if query.from_user.id == ADMIN_ID:
            cur_min = get_setting("group_min_members", "100")
            ADMIN_STATES[user_id] = {"action": "grp_set_min_members"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_groups_menu")]])
            await query.message.reply_text(
                f"⚙️ *SET MINIMUM GROUP MEMBERS REQUIREMENT*\n\n"
                f"• Current requirement: `{cur_min}+ Members`\n\n"
                "Please enter the minimum number of members a group must have for unlimited search (e.g. `100`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_set_ref_reward":
        if query.from_user.id == ADMIN_ID:
            cur_rew = get_setting("referral_reward", "5")
            ADMIN_STATES[user_id] = {"action": "interactive_set_ref_reward"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                f"🎁 *SET REFERRAL BONUS REWARD*\n\n"
                f"• Current Reward: `+{cur_rew} Credits per Friend`\n\n"
                "Aap refer karne par kitne credits dena chahte hain? (e.g. `2`, `3`, `5`, ya `10`):\n\n"
                "_(Enter the credits amount to award per successful referral)_",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_set_support":
        if query.from_user.id == ADMIN_ID:
            cur_sup = get_setting("support_username", "@YOURSOLUTIONs_BOT")
            ADMIN_STATES[user_id] = {"action": "interactive_set_support"}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await query.message.reply_text(
                f"📞 *SET SUPPORT ADMIN USERNAME*\n\n"
                f"• Current Support: `{cur_sup}`\n\n"
                "Please enter the new Support username or bot handle (e.g. `@YOURSOLUTIONs_BOT`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )

    elif query.data == "adm_claims_log":
        if query.from_user.id == ADMIN_ID:
            conn = get_db()
            c = conn.cursor()
            today_date_str = datetime.now().strftime("%Y-%m-%d")
            c.execute("""
                SELECT user_id, username, first_name, last_claimed 
                FROM users 
                WHERE last_claimed LIKE ? 
                ORDER BY last_claimed DESC 
                LIMIT 20
            """, (f"{today_date_str}%",))
            claimers = c.fetchall()
            conn.close()

            log_msg = f"🎁 *TODAY'S DAILY CLAIMS LOG ({len(claimers)} users)*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            if not claimers:
                log_msg += "No users have claimed daily reward today yet.\n"
            else:
                for idx, cl in enumerate(claimers, 1):
                    uname = f"@{cl['username']}" if cl["username"] else cl["first_name"]
                    t_str = cl["last_claimed"].split(" ")[1] if " " in cl["last_claimed"] else cl["last_claimed"]
                    log_msg += f"*{idx}.* {uname} (`{cl['user_id']}`) at `{t_str}`\n"
            
            back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh", style="primary")]])
            await query.message.edit_text(log_msg, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)

    # Result action callbacks
    elif query.data.startswith("cp_"):
        num = query.data.split("_")[1]
        await query.answer(f"Copied: +{num}", show_alert=True)

    elif query.data == "del_res":
        try:
            await query.message.delete()
            await query.answer("Result message deleted!")
        except Exception:
            await query.answer("Could not delete message.")

# ----------------- PAYMENT SCREENSHOT PHOTO HANDLER -----------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id)
    if not u:
        register_user(user.id, user.username, user.first_name)
        u = get_user(user.id)

    if u["is_banned"] == 1:
        await update.message.reply_text("🚫 *Your access has been suspended.*", parse_mode=ParseMode.MARKDOWN)
        return

    photo_file_id = update.message.photo[-1].file_id
    caption_user = update.message.caption or ""

    # Check user's latest pending purchase order from DB
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM payments 
        WHERE user_id = ? AND status = 'pending' 
        ORDER BY id DESC LIMIT 1
    """, (user.id,))
    pending_order = c.fetchone()
    conn.close()

    if pending_order:
        amt = pending_order["amount"]
        creds = pending_order["credits"]
        pay_id = pending_order["id"]
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Approve ₹{amt} (+{creds} Credits)", callback_data=f"appr_quick_{user.id}_{creds}")],
            [InlineKeyboardButton("❌ Reject Payment", callback_data=f"rej_quick_{user.id}")]
        ])
        admin_caption = (
            "📸 *NEW PAYMENT PROOF RECEIVED!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *User:* {user.mention_markdown()} (`{user.id}`)\n"
            f"• *Selected Tier Amount:* `₹{amt} INR`\n"
            f"• *Allocation Due:* `+{creds} Credits`\n"
            f"• *Current Balance:* `{u['credits']}` Credits\n"
            f"• *User Note:* `{caption_user or 'None'}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Click Approve to credit **{creds} Credits** instantly:"
        )
    else:
        # Fallback if user uploaded screenshot directly without picking tier
        p10 = get_setting("plan_10_credits", "50")
        p50 = get_setting("plan_50_credits", "250")
        p100 = get_setting("plan_100_credits", "500")

        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ ₹10 (+{p10} cr)", callback_data=f"appr_quick_{user.id}_{p10}"),
                InlineKeyboardButton(f"✅ ₹50 (+{p50} cr)", callback_data=f"appr_quick_{user.id}_{p50}"),
                InlineKeyboardButton(f"✅ ₹100 (+{p100} cr)", callback_data=f"appr_quick_{user.id}_{p100}")
            ],
            [
                InlineKeyboardButton("❌ Reject Payment", callback_data=f"rej_quick_{user.id}")
            ]
        ])
        admin_caption = (
            "📸 *NEW PAYMENT SCREENSHOT RECEIVED!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *User:* {user.mention_markdown()} (`{user.id}`)\n"
            f"• *Current Credits:* `{u['credits']}`\n"
            f"• *User Note:* `{caption_user or 'None'}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Select the tier to approve credits, or Reject:"
        )

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_file_id,
            caption=admin_caption,
            reply_markup=admin_kb,
            parse_mode=ParseMode.MARKDOWN
        )

        await update.message.reply_text(
            "✅ *Payment Screenshot Received!*\n\n"
            "Admin is reviewing your transaction proof. Your credits will be added as soon as verified.",
            parse_mode=ParseMode.MARKDOWN
        )
        await send_log(context, f"💳 <b>Payment Screenshot Uploaded:</b>\n• User: {user.mention_html()} (<code>{user.id}</code>)")

    except Exception as e:
        logger.error(f"Error handling photo from {user.id}: {e}")
        await update.message.reply_text("⚠️ Could not process screenshot. Please try again.")

# ----------------- TEXT MESSAGE HANDLER -----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Check Ban Status
    u = get_user(user.id)
    if u and u["is_banned"] == 1:
        await update.message.reply_text("🚫 *Your access has been suspended by Administration.*", parse_mode=ParseMode.MARKDOWN)
        return

    # Check Admin Interactive Button State Machine
    if user.id == ADMIN_ID and user.id in ADMIN_STATES:
        state = ADMIN_STATES[user.id]
        action = state.get("action")

        # 1. Add Credits (Step 1: ID, Step 2: Amount)
        if action == "interactive_addcreds_step1":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid numerical User ID:")
                return
            ADMIN_STATES[user.id] = {"action": "interactive_addcreds_step2", "target_id": int(text)}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await update.message.reply_text(f"User ID `{text}` selected.\n\nNow enter the **Credits Amount** to add:", reply_markup=cancel_kb, parse_mode=ParseMode.MARKDOWN)
            return

        elif action == "interactive_addcreds_step2":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid number of credits:")
                return
            amt = int(text)
            target_id = state.get("target_id")
            update_user_credits(target_id, amt)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ Successfully added `+{amt}` credits to User `{target_id}`.", parse_mode=ParseMode.MARKDOWN)
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🎁 Admin has credited *{amt} credits* to your account!", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
            return

        # 2. Deduct Credits (Step 1: ID, Step 2: Amount)
        elif action == "interactive_remcreds_step1":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid numerical User ID:")
                return
            ADMIN_STATES[user.id] = {"action": "interactive_remcreds_step2", "target_id": int(text)}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel_input")]])
            await update.message.reply_text(f"User ID `{text}` selected.\n\nNow enter the **Credits Amount** to deduct:", reply_markup=cancel_kb, parse_mode=ParseMode.MARKDOWN)
            return

        elif action == "interactive_remcreds_step2":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid number of credits:")
                return
            amt = int(text)
            target_id = state.get("target_id")
            update_user_credits(target_id, -amt)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ Successfully deducted `-{amt}` credits from User `{target_id}`.", parse_mode=ParseMode.MARKDOWN)
            return

        # 3. Inspect User
        elif action == "interactive_userinfo":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid numerical User ID:")
                return
            target_id = int(text)
            del ADMIN_STATES[user.id]
            await show_user_info_card(context.bot, user.id, target_id)
            return

        # 4. Ban User
        elif action == "interactive_ban":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid numerical User ID:")
                return
            target_id = int(text)
            set_user_ban(target_id, True)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"🚫 User `{target_id}` has been **BANNED** successfully.", parse_mode=ParseMode.MARKDOWN)
            return

        # 5. Unban User
        elif action == "interactive_unban":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid numerical User ID:")
                return
            target_id = int(text)
            set_user_ban(target_id, False)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ User `{target_id}` has been **UNBANNED** successfully.", parse_mode=ParseMode.MARKDOWN)
            return

        # 6. Set UPI
        elif action == "interactive_setupi":
            upi = text.strip()
            set_setting("upi_id", upi)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ UPI Gateway ID updated to: `{upi}`", parse_mode=ParseMode.MARKDOWN)
            return

        # 7. Set Tier Price
        elif action == "interactive_setprice":
            if not text.isdigit():
                await update.message.reply_text("⚠️ Please enter a valid number of credits:")
                return
            plan = state.get("plan")
            set_setting(f"plan_{plan}_credits", text)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ ₹{plan} package rate updated to `{text}` credits.", parse_mode=ParseMode.MARKDOWN)
            return

        # 8. Set Audit Log Channel
        elif action == "interactive_setlog":
            ch = text.strip()
            set_setting("log_channel", ch)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(f"✅ Audit Log Channel updated to: `{ch}`", parse_mode=ParseMode.MARKDOWN)
            return

        # 9. Global Broadcast
        elif action == "interactive_broadcast":
            del ADMIN_STATES[user.id]
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE is_banned = 0")
            all_users = c.fetchall()
            conn.close()

            sent = 0
            failed = 0
            status_msg = await update.message.reply_text(f"🚀 Broadcasting message to {len(all_users)} users...")
            for row in all_users:
                try:
                    await context.bot.send_message(chat_id=row["user_id"], text=text, parse_mode=ParseMode.MARKDOWN)
                    sent += 1
                    await asyncio.sleep(0.04)
                except Exception:
                    failed += 1
            await status_msg.edit_text(f"✅ *Broadcast Finished!*\n• Sent: `{sent}`\n• Failed/Blocked: `{failed}`", parse_mode=ParseMode.MARKDOWN)
            return

        # 10. Multi Force Channels Addition Flows
        elif action == "fch_add_public":
            ch = text.strip()
            if not ch.startswith("@") and not ch.startswith("-100"):
                ch = f"@{ch}"
            
            # Auto-resolve username into real Chat ID and Channel Title via Telegram API
            try:
                chat_obj = await context.bot.get_chat(ch)
                resolved_chat_id = str(chat_obj.id)
                ch_title = chat_obj.title or ch
                link = f"https://t.me/{chat_obj.username}" if chat_obj.username else (f"https://t.me/{ch.replace('@', '')}")
            except Exception as e:
                resolved_chat_id = ch
                ch_title = ch
                link = f"https://t.me/{ch.replace('@', '')}"
                logger.warning(f"Could not auto-fetch chat ID for {ch}: {e}")

            add_force_channel(name=ch_title, chat_id=resolved_chat_id, invite_link=link, channel_type="public")
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"✅ *Public Force Channel Connected!*\n"
                f"• Title: *{ch_title}*\n"
                f"• Auto Chat ID: `{resolved_chat_id}`\n"
                f"• Link: `{link}`\n"
                f"• Mode: `PUBLIC`",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_force_channels_admin_menu(update.message, is_callback=False)
            return

        elif action == "fch_add_private_step1":
            ADMIN_STATES[user.id] = {"action": "fch_add_private_step2", "chat_id": text}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="fch_back_to_menu")]])
            await update.message.reply_text(
                "🔒 *ADD PRIVATE CHANNEL (Step 2/2)*\n\n"
                f"Saved Chat ID: `{text}`\n\n"
                "Now send the **Permanent Invite Link** (e.g. `https://t.me/+AbCdEfGhIjK`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif action == "fch_add_private_step2":
            chat_id = state.get("chat_id")
            invite_link = text
            add_force_channel(name=f"Private Channel", chat_id=chat_id, invite_link=invite_link, channel_type="private")
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                "✅ *Private Force Channel Added!*\n"
                f"• Chat ID: `{chat_id}`\n"
                f"• Invite Link: `{invite_link}`\n"
                f"• Mode: `PRIVATE`",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_force_channels_admin_menu(update.message, is_callback=False)
            return

        elif action == "fch_add_request_step1":
            ADMIN_STATES[user.id] = {"action": "fch_add_request_step2", "chat_id": text}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="fch_back_to_menu")]])
            await update.message.reply_text(
                "📨 *ADD JOIN REQUEST CHANNEL (Step 2/2)*\n\n"
                f"Saved Chat ID: `{text}`\n\n"
                "Now send the **Join Request Link** (e.g. `https://t.me/+AbCdEfGhIjK`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif action == "fch_add_request_step2":
            chat_id = state.get("chat_id")
            invite_link = text
            add_force_channel(name=f"Join Request Channel", chat_id=chat_id, invite_link=invite_link, channel_type="request")
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                "✅ *Join Request Force Channel Added!*\n"
                f"• Chat ID: `{chat_id}`\n"
                f"• Request Link: `{invite_link}`\n"
                f"• Mode: `JOIN REQUEST` (Pending requests are verified!)",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_force_channels_admin_menu(update.message, is_callback=False)
            return

        # Promo Creation States
        elif action == "promo_create_step1":
            clean_code = text.strip().upper()
            if not clean_code:
                await update.message.reply_text("⚠️ Please enter a valid Promo Code:")
                return
            ADMIN_STATES[user.id] = {"action": "promo_create_step2", "code": clean_code}
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_promo_menu")]])
            await update.message.reply_text(
                f"Promo Code `{clean_code}` recorded!\n\n"
                "*(Step 2/3)* Enter the **Credits Amount** to award (e.g. `10` or `50`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif action == "promo_create_step2":
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text("⚠️ Please enter a positive integer for credits:")
                return
            creds = int(text)
            ADMIN_STATES[user.id]["credits"] = creds
            ADMIN_STATES[user.id]["action"] = "promo_create_step3"
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_promo_menu")]])
            await update.message.reply_text(
                f"Credits Award: `+{creds} 💎`\n\n"
                "*(Step 3/3)* Enter the **Maximum Total Uses / Redemptions** allowed (e.g. `100` or `1`):",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif action == "promo_create_step3":
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text("⚠️ Please enter a positive integer for max uses:")
                return
            max_uses = int(text)
            code = state["code"]
            creds = state["credits"]
            create_promo_code(code=code, credits=creds, max_uses=max_uses)
            await update.message.reply_text(
                "🎉 *PROMO CODE CREATED SUCCESSFULLY!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• *Code:* `{code}`\n"
                f"• *Credits:* `+{creds} 💎`\n"
                f"• *Max Claims:* `{max_uses}` Users",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_promo_admin_menu(update.message, is_callback=False)
            return

        # Promo Min Referrals Setting
        elif action == "promo_set_min":
            if not text.isdigit() or int(text) < 0:
                await update.message.reply_text("⚠️ Please enter a valid number (e.g. 5 or 0):")
                return
            set_setting("redeem_min_refers", str(text))
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"✅ *Requirement Updated!*\n\nUsers now need at least `{text}` verified referrals to redeem promo codes.",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_promo_admin_menu(update.message, is_callback=False)
            return

        # Promo Denial Message Setting
        elif action == "promo_set_msg":
            set_setting("redeem_denial_msg", text)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"✅ *Denial Notice Updated Successfully!*\n\nNew notice:\n_{text}_",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_promo_admin_menu(update.message, is_callback=False)
            return

        # Connected Group Ban Reason & Notification
        elif action == "grp_ban_reason":
            gid = state.get("chat_id")
            title = state.get("title", "Group")
            ban_reason = text.strip()
            set_group_ban_status(gid, True, ban_reason)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"🚫 *Group Access Restricted Successfully!*\n\n"
                f"• *Group:* {title} (`{gid}`)\n"
                f"• *Reason:* {ban_reason}\n\n"
                "Group alert notification dispatched.",
                parse_mode=ParseMode.MARKDOWN
            )
            # Send notification directly into group
            try:
                await context.bot.send_message(
                    chat_id=gid,
                    text=(
                        "⚠️ *GROUP ACCESS RESTRICTED BY BOT OWNER*\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"• *Reason:* {ban_reason}\n\n"
                        "Unlimited free search facility has been suspended for this group until further review.\n\n"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to send ban alert to group {gid}: {e}")

            await show_connected_groups_admin_menu(update.message, is_callback=False)
            return

        # Connected Group Min Members Requirement Setting
        elif action == "grp_set_min_members":
            if not text.isdigit() or int(text) < 1:
                await update.message.reply_text("⚠️ Please enter a valid number of members (e.g. 100):")
                return
            set_setting("group_min_members", str(text))
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"✅ *Requirement Updated!*\n\nGroups must now have at least `{text}+ members` to activate unlimited searches.",
                parse_mode=ParseMode.MARKDOWN
            )
            await show_connected_groups_admin_menu(update.message, is_callback=False)
            return

        # Referral Bonus Setting
        elif action == "interactive_set_ref_reward":
            if not text.isdigit() or int(text) < 0:
                await update.message.reply_text("⚠️ Please enter a valid number of credits (e.g. 2, 3, 5, or 10):")
                return
            set_setting("referral_reward", str(text))
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"🎉 *Referral Reward Updated!*\n\n"
                f"Ab har naye dost ko invite karne par user ko **+{text} Search Credits** milenge.\n"
                "Ye rule turant sabhi naye users ke referral registrations par apply ho gaya hai!",
                parse_mode=ParseMode.MARKDOWN
            )
            await admin_command(update, context)
            return

        # Support Handle Setting
        elif action == "interactive_set_support":
            new_sup = text.strip()
            if not new_sup.startswith("@"):
                new_sup = f"@{new_sup}"
            set_setting("support_username", new_sup)
            del ADMIN_STATES[user.id]
            await update.message.reply_text(
                f"✅ *Support Handle Updated!*\n\n"
                f"Ab support button par click karne par users seedha `{new_sup}` par redirect honge.",
                parse_mode=ParseMode.MARKDOWN
            )
            await admin_command(update, context)
            return

    # Check Membership on text input
    unjoined = await get_unjoined_channels(user.id, context.bot)
    if unjoined:
        await start_command(update, context)
        return

    # Check User States (e.g. Waiting to enter promo code)
    if user.id in USER_STATES:
        u_state = USER_STATES[user.id]
        if u_state.get("action") == "waiting_redeem_code":
            del USER_STATES[user.id]
            # Cancel command or button check
            if text in ["❌ Cancel", "/cancel", "Cancel"]:
                await update.message.reply_text("🚫 Redemption process cancelled.", reply_markup=get_main_keyboard(is_owner=(user.id == ADMIN_ID)))
                return

            # Execute redemption
            success, creds_given, res_msg = redeem_promo_code_for_user(user.id, text)
            full_reply = (
                f"{res_msg}\n\n"
            )
            await update.message.reply_text(full_reply, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(is_owner=(user.id == ADMIN_ID)))
            return

    # 0. Redeem Code Menu Button
    if text in ["🎟️ rᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ", "🎟️ Redeem Code", "/redeem"]:
        USER_STATES[user.id] = {"action": "waiting_redeem_code"}
        min_ref = get_setting("redeem_min_refers", "5")
        prompt_text = (
            "🎟️ *rᴇᴅᴇᴇᴍ ᴘrᴏᴍᴏ ᴄᴏᴅᴇ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Kripya apna **Promo / Redeem Code** yahan chat me type karke bhejein:\n\n"
            f"⚠️ *Notice:* Redeem code use karne ke liye kam se kam *{min_ref} Verified Referrals* hona anivarya hai.\n\n"
            "_(Type karein apna code ya cancel karne ke liye 'Cancel' bhejein)_"
        )
        await update.message.reply_text(prompt_text, parse_mode=ParseMode.MARKDOWN)
        return

    # Unlimited Free Search Instructions & Terms Button
    if text in ["🚀 ᴜɴʟɪᴍɪᴛᴇᴅ ғrᴇᴇ sᴇarᴄʜ", "Unlimited Free Search", "/unlimited"]:
        bot_me = await context.bot.get_me()
        min_m = get_setting("group_min_members", "100")
        # Telegram deep-link to directly prompt group selection dialog to add bot as admin
        add_bot_url = f"https://t.me/{bot_me.username}?startgroup=true&admin=post_messages+delete_messages+invite_users"

        terms_msg = (
            "🚀 *ᴜɴʟɪᴍɪᴛᴇᴅ ғrᴇᴇ sᴇarᴄʜ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Apne group me Unlimited Free Search chalane ke liye:\n"
            f"• Group me kam se kam *{min_m}+ Real Members* hone chahiye.\n"
            "• Unlimited Search sirf **Group Owner & Admins** ke liye active hogi.\n"
            "• Fake/dead members wale groups ka access suspend kar diya jayega.\n\n"
            "Neeche button se bot ko apne group me Admin banayein 👇"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Bot to Your Group", url=add_bot_url, style="primary")]
        ])
        await update.message.reply_text(terms_msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # 1. My Profile Button
    if text in ["👤 ᴍʏ ᴘʀᴏꜰɪʟᴇ", "👤 My Profile"]:
        if not u:
            register_user(user.id, user.username, user.first_name)
            u = get_user(user.id)

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (user.id,))
        ref_count = c.fetchone()["count"]
        conn.close()

        ref_by = f"`{u['referred_by']}`" if u["referred_by"] else "None (Direct)"

        profile_text = (
            "╔═══════════════════════════════════╗\n"
            "   👤 *usᴇr ɪᴅᴇɴᴛɪᴛʏ & ǫᴜᴏᴛa ᴘrᴏғɪʟᴇ* \n"
            "╚═══════════════════════════════════╝\n\n"
            "┌── 🏷 *aᴄᴄᴏᴜɴᴛ ɪᴅᴇɴᴛɪғɪᴇrs*\n"
            f"│ • *ɴaᴍᴇ:* {user.first_name}\n"
            f"│ • *ᴛᴇʟᴇɢraᴍ ɪᴅ:* `{user.id}`\n"
            f"│ • *ɪɴᴠɪᴛᴇᴅ ᴏrɪɢɪɴ:* {ref_by}\n"
            "└──\n\n"
            "┌── 💎 *ᴡaʟʟᴇᴛ & usaɢᴇ ᴍᴇᴛrɪᴄs*\n"
            f"│ • *aᴠaɪʟaʙʟᴇ ʙaʟaɴᴄᴇ:* `{u['credits']}` ᴄrᴇᴅɪᴛs 💎\n"
            f"│ • *ʟɪғᴇᴛɪᴍᴇ ᴇarɴᴇᴅ :* `{u['total_earned']}` ᴄrᴇᴅɪᴛs\n"
            f"│ • *ᴛᴏᴛaʟ sᴇarᴄʜᴇs   :* `{u['total_used']}` ᴇxᴇᴄᴜᴛᴇᴅ\n"
            f"│ • *aᴄᴛɪᴠᴇ ɴᴇᴛᴡᴏrᴋ   :* `{ref_count}` rᴇғᴇrrᴇᴅ 👥\n"
            "└──\n\n"
            "💡 *ᴛɪᴘ:* Use '🎁 ʀᴇꜰᴇʀ & ᴇᴀʀɴ' to expand your query credits automatically."
        )
        await update.message.reply_text(profile_text, parse_mode=ParseMode.MARKDOWN)
        return

    # 2. Refer & Earn Button
    if text in ["🎁 ʀᴇꜰᴇʀ & ᴇᴀʀɴ", "🔗 Refer & Earn"]:
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user.id}"

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (user.id,))
        total_refs = c.fetchone()["count"]
        conn.close()

        ref_bonus_setting = int(get_setting("referral_reward", "5"))
        refer_text = (
            "🎁 *rᴇғᴇr & ᴇarɴ ғrᴇᴇ ᴄrᴇᴅɪᴛs*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Har dost ko invite karne par aapko milenge:\n"
            f"👉 *{ref_bonus_setting} ғrᴇᴇ sᴇarᴄʜ ᴄrᴇᴅɪᴛs!*\n\n"
            f"👥 *ᴛᴏᴛaʟ rᴇғᴇrrᴇᴅ:* `{total_refs}` ғrɪᴇɴᴅs\n"
            f"💎 *ᴇarɴᴇᴅ ʙʏ rᴇғᴇr:* `{total_refs * ref_bonus_setting}` ᴄrᴇᴅɪᴛs\n\n"
            f"📌 *ʏᴏᴜr ɪɴᴠɪᴛᴇ ʟɪɴᴋ:*\n`{ref_link}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Is link ko apne dosto aur groups me share karein!"
        )
        await update.message.reply_text(refer_text, parse_mode=ParseMode.MARKDOWN)
        return

    # 3. Buy VIP Credits Button
    if text in ["💎 ʙᴜʏ ᴠɪᴘ ᴄʀᴇᴅɪᴛꜱ", "💎 Buy VIP Credits"]:
        p10 = get_setting("plan_10_credits", "50")
        p50 = get_setting("plan_50_credits", "250")
        p100 = get_setting("plan_100_credits", "500")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⚡ ₹10 = {p10} Credits", callback_data="buy_10", style="primary")],
            [InlineKeyboardButton(f"🔥 ₹50 = {p50} Credits", callback_data="buy_50", style="primary")],
            [InlineKeyboardButton(f"👑 ₹100 = {p100} Credits", callback_data="buy_100", style="primary")]
        ])
        await update.message.reply_text(
            "💎 *SELECT VIP CREDIT PACKAGE:*\n\n"
            "Apni choice ke package par click karein, aapko direct QR code aur payment instructions mil jayenge:",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 4. Search Trigger
    if text in ["⚡ ꜱᴇᴀʀᴄʜ ᴛᴇʟᴇɢʀᴀᴍ ɴᴜᴍʙᴇʀ ⚡", "🔍 Search Telegram Number"]:
        await update.message.reply_text(
            "🛰 *sᴇarᴄʜ ᴛᴇʟᴇɢraᴍ ɴᴜᴍᴇrɪᴄaʟ ɪᴅ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send the numerical user ID to initiate lookup:\n\n"
            "• *Format:* `XXXXXXXXXX` (e.g., `1234567890`)",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 5. Statistics
    if text in ["📊 ʟɪᴠᴇ ꜱᴛᴀᴛꜱ", "📊 Statistics"]:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as users, SUM(total_used) as searches FROM users")
        stats = c.fetchone()
        conn.close()

        total_users = stats["users"] or 0
        total_searches = stats["searches"] or 0

        await update.message.reply_text(
            f"📊 *ʙᴏᴛ ʟɪᴠᴇ sᴛaᴛɪsᴛɪᴄs:*\n\n"
            f"• *Total Users:* `{total_users}`\n"
            f"• *Total Searches Processed:* `{total_searches}`\n"
            f"• *System Status:* `Online 🟢`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 6. How To Use
    if text == "ℹ️ ʜᴏᴡ ᴛᴏ ᴜꜱᴇ":
        ref_bonus_str = get_setting("referral_reward", "5")
        how_to_text = (
            "ℹ️ *ʜᴏᴡ ᴛᴏ ᴜsᴇ ᴛʜɪs ʙᴏᴛ*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ *Telegram ID Enter Karein:* Koi bhi 9-10 digit Telegram ID send karein.\n"
            "2️⃣ *Phone Number & Intelligence details:* Bot automatically database se report nikaal kar send karega.\n"
            "3️⃣ *Refund Policy:* Agar koi number nahi milta to aapka Credit deduct nahi hoga (Refund ho jayega).\n"
            f"4️⃣ *Free Credits:* '🎁 ʀᴇꜰᴇʀ & ᴇᴀʀɴ' se apne dosto ko link bhejein, har refer par {ref_bonus_str} Credits milenge!"
        )
        await update.message.reply_text(how_to_text, parse_mode=ParseMode.MARKDOWN)
        return

    # 7. Support
    if text in ["📞 ꜱᴜᴘᴘᴏʀᴛ", "📞 Support", "/support"]:
        sup_user = get_setting("support_username", "@YOURSOLUTIONs_BOT").strip()
        sup_clean = sup_user.replace("@", "")
        sup_url = f"https://t.me/{sup_clean}"
        sup_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Support Admin", url=sup_url, style="primary")]
        ])
        await update.message.reply_text(
            "📞 *24/7 ᴄᴜsᴛᴏᴍᴇr sᴜᴘᴘᴏrᴛ*\n\n"
            "Kisi bhi problem, inquiry, ya VIP bulk credit purchase ke liye support admin se contact karein:\n"
            f"👉 *Support Handle:* @{sup_clean}",
            reply_markup=sup_kb,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 8. Daily Claim Button (+1 Credit every 24h if user has >= 1 referral)
    if text in ["🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ (+1)", "Daily Claim"]:
        if not u:
            register_user(user.id, user.username, user.first_name)
            u = get_user(user.id)

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE referred_by = ?", (user.id,))
        ref_count = c.fetchone()["count"]

        if ref_count < 1:
            bot_info = await context.bot.get_me()
            ref_link = f"https://t.me/{bot_info.username}?start={user.id}"
            conn.close()
            await update.message.reply_text(
                "🔒 *DAILY REWARD LOCKED*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Daily Free Credit claim karne ke liye aapke account me kam se kam **1 Verified Referral** hona zaroori hai.\n\n"
                f"📌 *Apna Invite Link share karein:*\n`{ref_link}`\n\n"
                "Jaise hi 1 dost join karega, aapka Daily Claim unlock ho jayega!",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        last_claimed_str = u["last_claimed"]
        now = datetime.now()
        can_claim = True
        hours_left = 0
        mins_left = 0

        if last_claimed_str:
            try:
                last_dt = datetime.strptime(last_claimed_str, "%Y-%m-%d %H:%M:%S")
                diff = now - last_dt
                if diff.total_seconds() < 86400:
                    can_claim = False
                    hours_left = int((86400 - diff.total_seconds()) // 3600)
                    mins_left = int(((86400 - diff.total_seconds()) % 3600) // 60)
            except Exception:
                pass

        if not can_claim:
            conn.close()
            await update.message.reply_text(
                f"⏳ *ALREADY CLAIMED TODAY*\n\n"
                f"Aapne aaj ka reward claim kar liya hai.\n"
                f"Agla claim: **{hours_left} Hours, {mins_left} Minutes** baad unlock hoga.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE users SET credits = credits + 1, total_earned = total_earned + 1, last_claimed = ? WHERE user_id = ?", (now_str, user.id))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            "🎉 *DAILY REWARD CLAIMED!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Aapke account me **+1 Free Search Credit** add kar diya gaya hai!\n"
            f"• Current Balance: `{u['credits'] + 1}` Credits 💎\n\n"
            "Kal dobara aakar apna reward claim karein!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 9. Admin Control Panel Button (Owner Only)
    if text in ["👑 ᴀᴅᴍɪɴ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ", "Admin", "/admin"]:
        if is_admin(user.id):
            await admin_command(update, context)
            return

    # Otherwise treat as ID query
    await process_search_query(update, context, text)

# ----------------- SEARCH LOOKUP PROCESSOR -----------------
async def process_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query_id: str):
    user = update.effective_user
    chat = update.effective_chat
    is_group = chat.type in ["group", "supergroup"]
    is_bot_owner = (user.id == ADMIN_ID)
    is_group_unlimited = False

    u = get_user(user.id)
    if not u:
        register_user(user.id, user.username, user.first_name)
        u = get_user(user.id)

    # ----------------- IN-GROUP SEARCH VALIDATIONS -----------------
    if is_group:
        # Auto register/update connected group in DB
        try:
            m_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            m_count = 0
        register_or_update_group(chat.id, chat.title or "Group", chat.username, user.id, m_count)

        g_data = get_connected_group(chat.id)
        if g_data and g_data.get("is_banned") == 1:
            ban_reason = g_data.get("ban_reason") or "Unlimited search suspended for this group."
            await update.message.reply_text(
                f"🚫 *GROUP ACCESS RESTRICTED*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{ban_reason}",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Check Member Count Requirement
        try:
            min_m = int(get_setting("group_min_members", "100"))
        except Exception:
            min_m = 100

        if m_count < min_m:
            await update.message.reply_text(
                "⚠️ *INSUFFICIENT GROUP MEMBERS*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Group me Unlimited Free Search unlock karne ke liye kam se kam *{min_m}+ Real Members* hone chahiye.\n"
                f"• Current Members: `{m_count}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Check if caller is Group Owner or Admin
        is_group_admin = False
        try:
            chat_member = await context.bot.get_chat_member(chat.id, user.id)
            if chat_member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                is_group_admin = True
        except Exception as e:
            logger.error(f"Error checking group admin status: {e}")

        # Unlimited Free Search is exclusive to Group Owner and Group Admins (or Bot Owner)
        if is_group_admin or is_bot_owner:
            is_group_unlimited = True
        else:
            is_group_unlimited = False

    # ----------------- QUOTA & BALANCE CHECK -----------------
    if not is_bot_owner and not is_group_unlimited and u["credits"] < 1:
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user.id}"
        ref_bonus_str = get_setting("referral_reward", "5")
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Purchase VIP Allocation", callback_data="buy_vip_menu")],
            [InlineKeyboardButton("📤 Share Referral Invite", url=f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={urllib.parse.quote('Access TG2NUM Intelligence Bot:')}")]
        ])

        notice_text = (
            "🔒 *ACCESS RESTRICTION: INSUFFICIENT QUOTA*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Your account currently has *0 Active Query Credits*.\n\n"
            "To replenish your query allowance:\n"
            f"• *Option A — Referral Expansion:* Invite associates using your referral link. Each verified registration grants `+{ref_bonus_str} Query Credits`.\n"
            f"🔗 *Referral Link:* `{ref_link}`\n\n"
            "• *Option B — VIP Upgrade:* Procure immediate high-volume query allocations via verified UPI gateway."
        )
        await update.message.reply_text(notice_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # Deduct credit only if NOT bot owner and NOT in unlimited group mode
    should_deduct_credit = (not is_bot_owner and not is_group_unlimited)

    if should_deduct_credit:
        update_user_credits(user.id, -1, is_usage=True)

    status_msg = await update.message.reply_text("🛰 *[■□□□□□□□□□] 10%* — Query Dispatched to OSINT Gateway...", parse_mode=ParseMode.MARKDOWN)

    base_url = "https://altaf-api-maker.base44.app/functions/lookup?tool=tgnum&key=ToxicAdmin&q="
    full_url = base_url + urllib.parse.quote(query_id)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        try:
            await status_msg.edit_text("🛰 *[■■■■■□□□□□] 50%* — Handshaking with DB registry...", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

        response = requests.get(full_url, headers=headers, timeout=25)

        try:
            await status_msg.edit_text("🛰 *[■■■■■■■■□□] 85%* — Decrypting identity metadata...", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

        if response.status_code != 200:
            logger.error(f"HTTP Error {response.status_code}: {response.text}")
            if should_deduct_credit:
                update_user_credits(user.id, 1, is_usage=False)
            await status_msg.edit_text("⚠️ Server busy hai ya koi response nahi mila. Kripya thodi der baad koshish karein.")
            return

        try:
            res = response.json()
        except ValueError:
            logger.error(f"JSON Parse Error. Raw: {response.text}")
            if should_deduct_credit:
                update_user_credits(user.id, 1, is_usage=False)
            await status_msg.edit_text("❌ Is ID ka koi record nahi mila.")
            return

        # Check for structured dict data
        if isinstance(res, dict) and isinstance(res.get("data"), dict) and res["data"].get("phone_number"):
            data = res["data"]
            tg_id = data.get("telegram_id", query_id)
            phone = str(data.get("phone_number", "N/A")).strip()
            clean_digits = "".join([c for c in phone if c.isdigit()])
            country = data.get("country", "N/A")

            msg = (
                "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "  📡 *ᴛᴇʟᴇɢraᴍ ɪɴᴛᴇʟʟɪɢᴇɴᴄᴇ rᴇᴘᴏrᴛ*\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "┌── 👤 *ɪᴅᴇɴᴛɪᴛʏ ᴅᴏssɪᴇr*\n"
                f"│ • *ᴛᴇʟᴇɢraᴍ ɪᴅ*   : `{tg_id}`\n"
                f"│ • *ᴘʜᴏɴᴇ ɴᴜᴍʙᴇr*  : `{phone}`\n"
                f"│ • *ᴏrɪɢɪɴ / sᴛaᴛᴇ*: `{country}`\n"
                "└──"
            )

            action_buttons = []
            row1 = []
            if clean_digits:
                row1.append(InlineKeyboardButton("💬 Open in WhatsApp", url=f"https://wa.me/{clean_digits}"))
            action_buttons.append(row1) if row1 else None
            action_buttons.append([
                InlineKeyboardButton("📋 Copy Number", callback_data=f"cp_{clean_digits}"),
                InlineKeyboardButton("🗑 Delete Result", callback_data="del_res")
            ])
            kb = InlineKeyboardMarkup(action_buttons)

            await status_msg.delete()
            await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            await send_log(context, f"🔍 <b>Search Success:</b>\n• User: {user.mention_html()}\n• Query ID: <code>{query_id}</code>\n• Phone: <code>{phone}</code>")
            return

        # Check for 'result' field in new API or 'raw' field
        result_text = None
        if isinstance(res, dict):
            if res.get("result"):
                result_text = str(res["result"])
            elif res.get("raw"):
                result_text = str(res["raw"])

        if result_text:
            # Check for failure strings
            if any(fail_word in result_text.lower() for fail_word in ["failed to fetch", "not found", "error", "timed out", "http", "base44", "key="]):
                if should_deduct_credit:
                    update_user_credits(user.id, 1, is_usage=False)
                await status_msg.edit_text("❌ Is ID ka koi record nahi mila.")
                return

            # Extract details from text format
            phone_match = re.search(r"PHONE NUMBER\s*:\s*([^\n\r]+)", result_text, re.IGNORECASE)
            tg_match = re.search(r"TELEGRAM ID\s*:\s*([^\n\r]+)", result_text, re.IGNORECASE)
            country_match = re.search(r"COUNTRY\s*:\s*([^\n\r]+)", result_text, re.IGNORECASE)

            if phone_match:
                extracted_phone = phone_match.group(1).strip()
                extracted_tg = tg_match.group(1).strip() if tg_match else query_id
                extracted_country = country_match.group(1).strip() if country_match else "INDIA"
                clean_digits = "".join([c for c in extracted_phone if c.isdigit()])

                msg = (
                    "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    "  📡 *ᴛᴇʟᴇɢraᴍ ɪɴᴛᴇʟʟɪɢᴇɴᴄᴇ rᴇᴘᴏrᴛ*\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "┌── 👤 *ɪᴅᴇɴᴛɪᴛʏ ᴅᴏssɪᴇr*\n"
                    f"│ • *ᴛᴇʟᴇɢraᴍ ɪᴅ*   : `{extracted_tg}`\n"
                    f"│ • *ᴘʜᴏɴᴇ ɴᴜᴍʙᴇr*  : `{extracted_phone}`\n"
                    f"│ • *ᴏrɪɢɪɴ / sᴛaᴛᴇ*: `{extracted_country}`\n"
                    "└──"
                )

                action_buttons = []
                row1 = []
                if clean_digits:
                    row1.append(InlineKeyboardButton("💬 Open in WhatsApp", url=f"https://wa.me/{clean_digits}"))
                action_buttons.append(row1) if row1 else None
                action_buttons.append([
                    InlineKeyboardButton("📋 Copy Number", callback_data=f"cp_{clean_digits}"),
                    InlineKeyboardButton("🗑 Delete Result", callback_data="del_res")
                ])
                kb = InlineKeyboardMarkup(action_buttons)

                await status_msg.delete()
                await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                await send_log(context, f"🔍 <b>Search Success:</b>\n• User: {user.mention_html()}\n• Query ID: <code>{extracted_tg}</code>\n• Phone: <code>{extracted_phone}</code>")
                return

            # Clean raw output fallback
            cleaned_lines = []
            for line in result_text.splitlines():
                if any(bad in line for bad in ["DATA SOURCE", "PRIVATE SECURE DB", "Powered by", "Your_father_786k", "http", "base44", "developer"]):
                    continue
                cleaned_lines.append(line)
            clean_output = "\n".join(cleaned_lines).strip()
            if clean_output:
                del_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete Result", callback_data="del_res")]])
                await status_msg.delete()
                await update.message.reply_text(clean_output, reply_markup=del_kb)
                await send_log(context, f"🔍 <b>Search Success:</b>\n• User: {user.mention_html()}\n• Query: <code>{query_id}</code>")
                return

        if should_deduct_credit:
            update_user_credits(user.id, 1, is_usage=False)
        await status_msg.edit_text("❌ Is ID ka koi record nahi mila.")

    except Exception as e:
        logger.error(f"Search internal exception: {e}")
        if should_deduct_credit:
            update_user_credits(user.id, 1, is_usage=False)
        await status_msg.edit_text("❌ Is ID ka koi record nahi mila.")

# ----------------- ADMIN COMMANDS -----------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin interactive dashboard with full button controls."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total, SUM(CASE WHEN credits > 0 THEN 1 ELSE 0 END) as active, SUM(CASE WHEN credits <= 0 THEN 1 ELSE 0 END) as zero FROM users")
    stats = c.fetchone()
    conn.close()

    ch_list = get_all_force_channels()
    log_ch = get_setting("log_channel", "Not Configured")
    upi = get_setting("upi_id", "merchant@upi")
    groups_list = get_all_connected_groups()
    active_groups_cnt = sum(1 for g in groups_list if g["is_banned"] == 0)
    cur_ref_reward = get_setting("referral_reward", "5")
    sup_user = get_setting("support_username", "@YOURSOLUTIONs_BOT")

    # Get Today's Daily Claim Count
    conn = get_db()
    c = conn.cursor()
    today_date_str = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT COUNT(*) as claims_today FROM users WHERE last_claimed LIKE ?", (f"{today_date_str}%",))
    today_claims = c.fetchone()["claims_today"]
    conn.close()

    admin_msg = (
        "👑 *EXECUTIVE ADMIN CONTROL CONSOLE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 *Total Registered Entities:* `{stats['total']}`\n"
        f"🟢 *Active Quota Holders:* `{stats['active']}`\n"
        f"🔴 *Zero Quota Users:* `{stats['zero']}`\n"
        f"🎁 *Daily Claims Today:* `{today_claims}` Users\n"
        "────────────────────────────────────────\n"
        f"🎁 *Referral Bonus Reward:* `+{cur_ref_reward} Credits/Friend`\n"
        f"📞 *Support Handle:* `{sup_user}`\n"
        f"📢 *Force Channels Active:* `{len(ch_list)} Channels`\n"
        f"👥 *Connected Groups:* `{len(groups_list)} Total ({active_groups_cnt} Active)`\n"
        f"📑 *Log Audit Channel:* `{log_ch}`\n"
        f"💳 *Merchant Gateway UPI:* `{upi}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select an operation from the button console below:"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Credits", callback_data="adm_addcreds", style="primary"),
            InlineKeyboardButton("➖ Deduct Credits", callback_data="adm_remcreds", style="primary")
        ],
        [
            InlineKeyboardButton("🚫 Ban / Unban User", callback_data="adm_ban_menu", style="primary"),
            InlineKeyboardButton("🔎 Inspect User", callback_data="adm_userinfo", style="primary")
        ],
        [
            InlineKeyboardButton("🏆 Top Referrers", callback_data="adm_toprefs", style="primary"),
            InlineKeyboardButton("🎁 Set Referral Bonus", callback_data="adm_set_ref_reward", style="primary")
        ],
        [
            InlineKeyboardButton("📞 Set Support Handle", callback_data="adm_set_support", style="primary"),
            InlineKeyboardButton("🎁 Daily Claims Log", callback_data="adm_claims_log", style="primary")
        ],
        [
            InlineKeyboardButton("📢 Force Channels List & Config", callback_data="adm_setchannel", style="primary"),
            InlineKeyboardButton("👥 Connected Groups", callback_data="adm_groups_menu", style="primary")
        ],
        [
            InlineKeyboardButton("💳 Set UPI Gateway", callback_data="adm_setupi", style="primary"),
            InlineKeyboardButton("🏷 Tier Pricing", callback_data="adm_setprice", style="primary")
        ],
        [
            InlineKeyboardButton("📑 Audit Log Channel", callback_data="adm_setlog", style="primary"),
            InlineKeyboardButton("📣 Global Broadcast", callback_data="adm_broadcast", style="primary")
        ],
        [
            InlineKeyboardButton("🎟️ Promo / Redeem Codes", callback_data="adm_promo_menu", style="primary")
        ],
        [
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="adm_refresh", style="primary")
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(admin_msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(admin_msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = int(context.args[0])
    set_user_ban(uid, True)
    await update.message.reply_text(f"🚫 User `{uid}` has been **BANNED** successfully.", parse_mode=ParseMode.MARKDOWN)

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = int(context.args[0])
    set_user_ban(uid, False)
    await update.message.reply_text(f"✅ User `{uid}` has been **UNBANNED** successfully.", parse_mode=ParseMode.MARKDOWN)

async def add_credits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Usage: `/addcredits <user_id> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = int(args[0])
    amt = int(args[1])
    update_user_credits(uid, amt)
    await update.message.reply_text(f"✅ Added `{amt}` credits to user `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    try:
        await context.bot.send_message(chat_id=uid, text=f"🎁 Admin has credited *{amt} credits* to your account!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

async def remove_credits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Usage: `/removecredits <user_id> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = int(args[0])
    amt = int(args[1])
    update_user_credits(uid, -amt)
    await update.message.reply_text(f"✅ Removed `{amt}` credits from user `{uid}`.", parse_mode=ParseMode.MARKDOWN)

async def set_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await show_force_channels_admin_menu(update.message, is_callback=False)
        return
    ch = context.args[0].strip()
    link = f"https://t.me/{ch.replace('@', '')}" if ch.startswith("@") else ""
    add_force_channel(ch, ch, link, "public")
    await update.message.reply_text(f"✅ Force channel added to list: `{ch}`", parse_mode=ParseMode.MARKDOWN)

async def set_log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setlog -100xxxxxxx`", parse_mode=ParseMode.MARKDOWN)
        return
    ch = context.args[0].strip()
    set_setting("log_channel", ch)
    await update.message.reply_text(f"✅ Log channel updated to: `{ch}`", parse_mode=ParseMode.MARKDOWN)

async def set_upi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setupi example@upi`", parse_mode=ParseMode.MARKDOWN)
        return
    upi = context.args[0].strip()
    set_setting("upi_id", upi)
    await update.message.reply_text(f"✅ UPI ID set to: `{upi}`", parse_mode=ParseMode.MARKDOWN)

async def set_price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2 or args[0] not in ["10", "50", "100"]:
        await update.message.reply_text("Usage: `/setprice <10|50|100> <credits>`", parse_mode=ParseMode.MARKDOWN)
        return
    plan = args[0]
    creds = args[1]
    set_setting(f"plan_{plan}_credits", creds)
    await update.message.reply_text(f"✅ ₹{plan} package updated to `{creds}` credits.", parse_mode=ParseMode.MARKDOWN)

async def show_referrer_details(bot, chat_id, referrer_id: int, edit_message=None):
    """Shows full details of a referrer and lists every single user they have invited."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (referrer_id,))
    ref_user = c.fetchone()

    c.execute("SELECT user_id, first_name, username, created_at FROM users WHERE referred_by = ? ORDER BY created_at DESC", (referrer_id,))
    invited_users = c.fetchall()
    conn.close()

    if not ref_user:
        text = f"❌ User `{referrer_id}` not found."
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
        return

    ref_uname = f"@{ref_user['username']}" if ref_user["username"] else ref_user["first_name"]
    ref_link = f"[@{ref_user['username']}](https://t.me/{ref_user['username']})" if ref_user["username"] else f"[{ref_user['first_name']}](tg://user?id={referrer_id})"

    msg = (
        "╔══════════════════════════════════════╗\n"
        "   🏆 *REFERRAL TREE & NETWORK DOSSIER* \n"
        "╚══════════════════════════════════════╝\n\n"
        f"👑 *REFERRER PROFILE:*\n"
        f"• *User:* {ref_link}\n"
        f"• *Telegram ID:* `{referrer_id}`\n"
        f"• *Total Friends Invited:* `{len(invited_users)}`\n"
        f"• *Bonus Credits Earned:* `{len(invited_users) * 5}` 💎\n"
        f"• *Available Balance:* `{ref_user['credits']}` Credits\n"
        "────────────────────────────────────────\n"
        f"👥 *USERS REFERRED BY {ref_uname.upper()} ({len(invited_users)}):*\n"
    )

    if not invited_users:
        msg += "_No users have been referred by this account yet._\n"
    else:
        for idx, u in enumerate(invited_users, 1):
            u_name = f"[@{u['username']}](https://t.me/{u['username']})" if u["username"] else f"[{u['first_name']}](tg://user?id={u['user_id']})"
            u_date = u["created_at"] or "N/A"
            msg += f" {idx}. {u_name} (`{u['user_id']}`) — `{u_date}`\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    direct_chat_url = f"https://t.me/{ref_user['username']}" if ref_user["username"] else f"tg://user?id={referrer_id}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Open Chat with Referrer", url=direct_chat_url)],
        [InlineKeyboardButton("🔙 Back to Top Referrers", callback_data="adm_toprefs")]
    ])

    if edit_message:
        await edit_message.edit_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def top_referrers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays top referrers leaderboard with interactive click buttons to view invited users."""
    if not is_admin(update.effective_user.id):
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.first_name, u.username, COUNT(r.user_id) as ref_count 
        FROM users u
        JOIN users r ON r.referred_by = u.user_id 
        GROUP BY u.user_id 
        ORDER BY ref_count DESC 
        LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()

    msg = (
        "🏆 *TOP REFERRERS LEADERBOARD*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not rows:
        msg += "⚠️ *Abhi tak kisi user ne kisi ko refer nahi kiya hai.*"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")]])
    else:
        msg += "Neeche kisi bhi user par click karein unki complete **Referred Users List** dekhne ke liye:\n\n"
        kb_buttons = []
        for idx, r in enumerate(rows, 1):
            uname = f"@{r['username']}" if r["username"] else r["first_name"]
            msg += f"*{idx}.* `{uname}` (`{r['user_id']}`) — *{r['ref_count']} Referrals*\n"
            kb_buttons.append([InlineKeyboardButton(f"👤 {idx}. {uname} ({r['ref_count']} Refs)", callback_data=f"ref_tree_{r['user_id']}")])

        kb_buttons.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_refresh")])
        kb = InlineKeyboardMarkup(kb_buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def user_info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/userinfo <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    target_id = int(context.args[0])
    await show_user_info_card(context.bot, update.effective_chat.id, target_id)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg_text = update.message.text.partition(" ")[2].strip()
    if not msg_text:
        await update.message.reply_text("Usage: `/broadcast <your_message_here>`", parse_mode=ParseMode.MARKDOWN)
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = c.fetchall()
    conn.close()

    sent = 0
    failed = 0
    status_msg = await update.message.reply_text(f"🚀 Broadcasting to {len(users)} users...")

    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=msg_text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1

    await status_msg.edit_text(f"✅ *Broadcast Finished!*\n• Sent: `{sent}`\n• Failed/Blocked: `{failed}`", parse_mode=ParseMode.MARKDOWN)

# ----------------- DUMMY WEB SERVER FOR RENDER -----------------
async def health_check(request):
    return web.Response(text="Bot is running healthy!")

async def start_web_server():
    try:
        app = web.Application()
        app.router.add_get("/", health_check)
        app.router.add_get("/health", health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Render Web Server started on port {PORT}")
    except OSError as e:
        logger.warning(f"Web server port {PORT} notice: {e} (Safe to ignore on local Windows machine)")
    except Exception as e:
        logger.warning(f"Web server notice: {e}")

# ----------------- MAIN EXECUTION -----------------
def main():
    init_db()
    print(f"🚀 Starting TG2NUM PRO Bot (Admin ID: {ADMIN_ID})...")

    # Hook web server and cloud DB restore into Application post_init
    async def post_init(application):
        asyncio.create_task(start_web_server())
        # Automatically restore the latest database from Telegram cloud storage channel if freshly deployed
        await restore_db_from_telegram(application.bot)
        # Immediately upload a fresh snapshot of current database to Telegram channel
        await backup_db_to_telegram(application.bot, reason="Startup Initial Backup")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands (Optional fallback, now fully supported via interactive button flows)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("addcredits", add_credits_cmd))
    app.add_handler(CommandHandler("removecredits", remove_credits_cmd))
    app.add_handler(CommandHandler("setchannel", set_channel_cmd))
    app.add_handler(CommandHandler("setlog", set_log_cmd))
    app.add_handler(CommandHandler("setupi", set_upi_cmd))
    app.add_handler(CommandHandler("setprice", set_price_cmd))
    app.add_handler(CommandHandler("topreferrers", top_referrers_cmd))
    app.add_handler(CommandHandler("userinfo", user_info_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Photo Handler (Payment proof screenshots)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Join Request Handler (Receives when user requests to join private/request channel)
    app.add_handler(ChatJoinRequestHandler(chat_join_request_handler))

    # Bot Status Handler in Groups (Auto register group when bot added as admin)
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # Callback Queries
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # All text messages / Menu buttons / ID searches / Interactive Admin inputs
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule regular automatic database backups to the Telegram data channel every 5 minutes
    async def periodic_backup_job(context: ContextTypes.DEFAULT_TYPE):
        await backup_db_to_telegram(context.bot, reason="Periodic 5-Min Auto Backup")

    if app.job_queue:
        # Run 10 seconds after startup, then every 5 minutes (300 seconds)
        app.job_queue.run_repeating(periodic_backup_job, interval=300, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()
