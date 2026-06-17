import os
import json
import logging
import psycopg
from psycopg.rows import dict_row
from datetime import datetime

logger = logging.getLogger(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    return psycopg.connect(DATABASE_URL, sslmode="require")

def init_db():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL set — using in-memory fallback only")
        return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            message TEXT,
            direction TEXT,
            sender TEXT DEFAULT 'bot',
            read_flag BOOLEAN DEFAULT FALSE,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT PRIMARY KEY,
            name TEXT,
            history JSONB DEFAULT '[]',
            last_seen TIMESTAMPTZ DEFAULT NOW(),
            admin_takeover BOOLEAN DEFAULT FALSE
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS faq_counts (
            keyword TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_counts (
            day TEXT NOT NULL,
            metric TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (day, metric)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS response_times (
            id SERIAL PRIMARY KEY,
            seconds REAL,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id SERIAL PRIMARY KEY,
            message TEXT,
            recipients JSONB,
            sent INTEGER,
            failed INTEGER,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS quick_replies (
            id SERIAL PRIMARY KEY,
            title TEXT,
            body TEXT
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.commit(); cur.close(); conn.close()
        logger.info("✅ Database initialized")
        seed_quick_replies()
    except Exception as e:
        logger.error(f"DB init error: {e}")

def seed_quick_replies():
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM quick_replies")
        if cur.fetchone()[0] == 0:
            defaults = [
                ("Fee reminder", "Dear Parent, kindly note that school fees for Term II 2026 are due. Total: Ksh 17,000. Pay via M-Pesa Paybill 777643, Account: ADM number. Minimum 60% on Reporting Day."),
                ("Trip reminder", "Dear Parent, please note that educational trip fees are due. Kindly pay promptly to secure your child's spot."),
                ("Meeting notice", "Dear Parent, you are invited to the upcoming Parental Engagement Day. Please check the school calendar for your child's grade date."),
                ("Half term", "Dear Parent, Half Term holiday runs from 24th to 28th June 2026. School resumes on Monday 30th June 2026."),
            ]
            for title, body in defaults:
                cur.execute("INSERT INTO quick_replies (title, body) VALUES (%s, %s)", (title, body))
            conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"seed_quick_replies error: {e}")

# ── Messages ──────────────────────────────────────────────────────────────────
def log_message(phone, message, direction="inbound", sender="bot"):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (phone, message, direction, sender, read_flag) VALUES (%s,%s,%s,%s,%s)",
            (phone, message, direction, sender, direction == "outbound")
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"log_message: {e}")

def get_messages(limit=300, phone=None):
    if not DATABASE_URL: return []
    try:
        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)
        if phone:
            cur.execute("SELECT * FROM messages WHERE phone=%s ORDER BY timestamp ASC", (phone,))
        else:
            cur.execute("SELECT * FROM messages ORDER BY timestamp DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_messages: {e}"); return []

def mark_messages_read(phone):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE messages SET read_flag=TRUE WHERE phone=%s AND direction='inbound'", (phone,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"mark_messages_read: {e}")

def count_unread():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound' AND read_flag=FALSE")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"count_unread: {e}"); return 0

def total_message_count():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"total_message_count: {e}"); return 0

# ── Conversations ─────────────────────────────────────────────────────────────
def get_history(phone):
    if not DATABASE_URL: return []
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT history FROM conversations WHERE phone=%s", (phone,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row[0] if row else []
    except Exception as e:
        logger.error(f"get_history: {e}"); return []

def save_history(phone, history):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO conversations (phone, history, last_seen) VALUES (%s,%s,NOW())
            ON CONFLICT (phone) DO UPDATE SET history=EXCLUDED.history, last_seen=NOW()
        """, (phone, json.dumps(history)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"save_history: {e}")

def touch_active_user(phone, name=None):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        if name:
            cur.execute("""
                INSERT INTO conversations (phone, name, last_seen) VALUES (%s,%s,NOW())
                ON CONFLICT (phone) DO UPDATE SET last_seen=NOW(), name=COALESCE(EXCLUDED.name, conversations.name)
            """, (phone, name))
        else:
            cur.execute("""
                INSERT INTO conversations (phone, last_seen) VALUES (%s,NOW())
                ON CONFLICT (phone) DO UPDATE SET last_seen=NOW()
            """, (phone,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"touch_active_user: {e}")

def get_all_conversations():
    if not DATABASE_URL: return {}
    try:
        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT phone, name, history, last_seen, admin_takeover FROM conversations ORDER BY last_seen DESC")
        rows = cur.fetchall(); cur.close(); conn.close()
        result = {}
        for r in rows:
            result[r["phone"]] = {
                "phone": r["phone"], "name": r["name"],
                "history": r["history"] or [],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else "",
                "admin_takeover": r["admin_takeover"],
            }
        return result
    except Exception as e:
        logger.error(f"get_all_conversations: {e}"); return {}

def get_active_user_phones():
    if not DATABASE_URL: return []
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT phone FROM conversations")
        rows = [r[0] for r in cur.fetchall()]; cur.close(); conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_active_user_phones: {e}"); return []

def count_total_users():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM conversations")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"count_total_users: {e}"); return 0

def count_active_since(hours):
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM conversations WHERE last_seen > NOW() - INTERVAL '{hours} hours'")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"count_active_since: {e}"); return 0

def set_admin_takeover(phone, value):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO conversations (phone, admin_takeover) VALUES (%s,%s)
            ON CONFLICT (phone) DO UPDATE SET admin_takeover=EXCLUDED.admin_takeover
        """, (phone, value))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"set_admin_takeover: {e}")

def is_admin_takeover(phone):
    if not DATABASE_URL: return False
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT admin_takeover FROM conversations WHERE phone=%s", (phone,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row[0] if row else False
    except Exception as e:
        logger.error(f"is_admin_takeover: {e}"); return False

def get_takeover_phones():
    if not DATABASE_URL: return []
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT phone FROM conversations WHERE admin_takeover=TRUE")
        rows = [r[0] for r in cur.fetchall()]; cur.close(); conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_takeover_phones: {e}"); return []

def count_takeovers():
    return len(get_takeover_phones())

# ── FAQ counts ────────────────────────────────────────────────────────────────
def increment_faq(keyword):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO faq_counts (keyword, count) VALUES (%s,1)
            ON CONFLICT (keyword) DO UPDATE SET count=faq_counts.count+1
        """, (keyword,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"increment_faq: {e}")

def get_faq_counts():
    if not DATABASE_URL: return {}
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT keyword, count FROM faq_counts")
        rows = dict(cur.fetchall()); cur.close(); conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_faq_counts: {e}"); return {}

# ── Daily counts (messages today/yesterday, bot vs admin replies) ─────────────
def increment_daily(day, metric):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO daily_counts (day, metric, count) VALUES (%s,%s,1)
            ON CONFLICT (day, metric) DO UPDATE SET count=daily_counts.count+1
        """, (day, metric))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"increment_daily: {e}")

def get_daily_count(day, metric):
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT count FROM daily_counts WHERE day=%s AND metric=%s", (day, metric))
        row = cur.fetchone(); cur.close(); conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"get_daily_count: {e}"); return 0

def get_daily_total(day):
    """Sum of inbound + outbound_bot + outbound_admin for a given day."""
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(count),0) FROM daily_counts WHERE day=%s", (day,))
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"get_daily_total: {e}"); return 0

def get_total_inbound_all_time():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(count),0) FROM daily_counts WHERE metric='inbound'")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"get_total_inbound_all_time: {e}"); return 0

# ── Response times ───────────────────────────────────────────────────────────
def add_response_time(seconds):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO response_times (seconds) VALUES (%s)", (seconds,))
        # Keep table small — delete old rows beyond 500
        cur.execute("""
            DELETE FROM response_times WHERE id NOT IN (
                SELECT id FROM response_times ORDER BY id DESC LIMIT 500
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"add_response_time: {e}")

def get_avg_response_time():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT AVG(seconds) FROM response_times")
        row = cur.fetchone(); cur.close(); conn.close()
        return round(row[0], 1) if row and row[0] else 0
    except Exception as e:
        logger.error(f"get_avg_response_time: {e}"); return 0

# ── Broadcasts ────────────────────────────────────────────────────────────────
def save_broadcast(message, recipients, sent, failed):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO broadcasts (message, recipients, sent, failed) VALUES (%s,%s,%s,%s)",
            (message, json.dumps(recipients), sent, failed)
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"save_broadcast: {e}")

def get_broadcast_history():
    if not DATABASE_URL: return []
    try:
        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM broadcasts ORDER BY timestamp DESC")
        rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_broadcast_history: {e}"); return []

def count_broadcasts():
    if not DATABASE_URL: return 0
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM broadcasts")
        n = cur.fetchone()[0]; cur.close(); conn.close()
        return n
    except Exception as e:
        logger.error(f"count_broadcasts: {e}"); return 0

# ── Quick replies ─────────────────────────────────────────────────────────────
def get_quick_replies():
    if not DATABASE_URL: return []
    try:
        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM quick_replies ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_quick_replies: {e}"); return []

def add_quick_reply(title, body):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO quick_replies (title, body) VALUES (%s,%s)", (title, body))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"add_quick_reply: {e}")

def delete_quick_reply(qid):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM quick_replies WHERE id=%s", (qid,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"delete_quick_reply: {e}")

# ── Settings (bot pause flag) ───────────────────────────────────────────────────
def get_setting(key, default=None):
    if not DATABASE_URL: return default
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row[0] if row else default
    except Exception as e:
        logger.error(f"get_setting: {e}"); return default

def set_setting(key, value):
    if not DATABASE_URL: return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings (key, value) VALUES (%s,%s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, (key, value))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"set_setting: {e}")

def is_bot_paused():
    return get_setting("bot_paused", "false") == "true"

def set_bot_paused(value: bool):
    set_setting("bot_paused", "true" if value else "false")

# ── Activity items for activity log ─────────────────────────────────────────
def get_activity_items(limit=100):
    """Returns latest message per phone, with status derived from last message."""
    if not DATABASE_URL: return []
    try:
        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("""
            SELECT DISTINCT ON (m.phone) m.phone, m.message, m.direction, m.sender, m.timestamp, c.name
            FROM messages m
            LEFT JOIN conversations c ON c.phone = m.phone
            ORDER BY m.phone, m.timestamp DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[:limit]
    except Exception as e:
        logger.error(f"get_activity_items: {e}"); return []
