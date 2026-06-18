import os
import json
import logging
import contextlib
import psycopg
from psycopg.rows import dict_row
from datetime import datetime

logger = logging.getLogger(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    """Opens a direct connection. IMPORTANT: every call site is responsible
    for closing this — see get_conn_ctx() below for a safer alternative that
    guarantees cleanup even if the query raises an exception."""
    return psycopg.connect(DATABASE_URL, sslmode="require", connect_timeout=10)

@contextlib.contextmanager
def get_conn_ctx():
    """Preferred way to get a DB connection going forward: guarantees the
    connection is closed even if the query inside raises. Usage:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(...)
    Existing call sites still use the older get_conn() + manual close()
    pattern; see the audit note in count_active_since() below for why that
    pattern leaks connections on the exception path and should be migrated
    incrementally to this context manager as functions are touched."""
    conn = psycopg.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL set — using in-memory fallback only")
        return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
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
                admin_takeover BOOLEAN DEFAULT FALSE,
                escalated BOOLEAN DEFAULT FALSE,
                escalation_reason TEXT,
                escalated_at TIMESTAMPTZ
            )""")
            # Add columns if upgrading an existing table that predates this feature
            for col, ddl in [
                ("escalated", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS escalated BOOLEAN DEFAULT FALSE"),
                ("escalation_reason", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS escalation_reason TEXT"),
                ("escalated_at", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ"),
            ]:
                cur.execute(ddl)
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
            cur.execute("""
            CREATE TABLE IF NOT EXISTS reply_codes (
                code TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS escalation_history (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                name TEXT,
                reason TEXT,
                escalated_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ,
                resolved_by TEXT
            )""")
            # One-time backfill: any conversation that was escalated before this
            # history table existed (escalated_at is set on conversations) but has
            # no matching row here yet gets copied in, so old escalations are
            # visible in the history tab too. Currently-open ones (escalated=TRUE)
            # backfill with resolved_at left NULL so they show as "open".
            cur.execute("""
                INSERT INTO escalation_history (phone, name, reason, escalated_at, resolved_at, resolved_by)
                SELECT c.phone, c.name, c.escalation_reason, c.escalated_at,
                       CASE WHEN c.escalated = FALSE THEN c.escalated_at ELSE NULL END,
                       CASE WHEN c.escalated = FALSE THEN 'admin (backfilled)' ELSE NULL END
                FROM conversations c
                WHERE c.escalated_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM escalation_history h WHERE h.phone = c.phone
                  )
            """)
            conn.commit()
        logger.info("✅ Database initialized")
        seed_quick_replies()
    except Exception as e:
        logger.error(f"DB init error: {e}")

def seed_quick_replies():
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
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
    except Exception as e:
        logger.error(f"seed_quick_replies error: {e}")

# ── Messages ──────────────────────────────────────────────────────────────────
def log_message(phone, message, direction="inbound", sender="bot"):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (phone, message, direction, sender, read_flag) VALUES (%s,%s,%s,%s,%s)",
                (phone, message, direction, sender, direction == "outbound")
            )
            conn.commit()
    except Exception as e:
        logger.error(f"log_message: {e}")

def get_messages(limit=300, phone=None):
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if phone:
                cur.execute("SELECT * FROM messages WHERE phone=%s ORDER BY timestamp ASC", (phone,))
            else:
                cur.execute("SELECT * FROM messages ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_messages: {e}"); return []

def get_messages_for_phones(phones, per_phone_limit=200):
    """
    Batched alternative to calling get_messages() once per phone.
    Fetches messages for every phone in `phones` with a single query
    (instead of N separate round-trips), then groups them in Python.

    Each phone's message list is capped at `per_phone_limit` most-recent
    messages (still enough to compute unread/unread_media counts and grab
    the last message for the dashboard, without pulling unbounded history
    for every conversation on every poll).
    """
    if not DATABASE_URL or not phones: return {}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            # Window function caps rows per phone server-side, so we still only
            # pull what's needed even for conversations with very long history.
            cur.execute("""
                SELECT phone, direction, message, read_flag, timestamp
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY phone ORDER BY timestamp DESC
                    ) AS rn
                    FROM messages
                    WHERE phone = ANY(%s)
                ) ranked
                WHERE rn <= %s
                ORDER BY phone, timestamp ASC
            """, (list(phones), per_phone_limit))
            rows = [dict(r) for r in cur.fetchall()]
        grouped = {p: [] for p in phones}
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
            grouped.setdefault(r["phone"], []).append(r)
        return grouped
    except Exception as e:
        logger.error(f"get_messages_for_phones: {e}"); return {p: [] for p in phones}

def mark_messages_read(phone):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE messages SET read_flag=TRUE WHERE phone=%s AND direction='inbound'", (phone,))
            conn.commit()
    except Exception as e:
        logger.error(f"mark_messages_read: {e}")

def count_unread():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound' AND read_flag=FALSE")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_unread: {e}"); return 0

def total_message_count():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM messages")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"total_message_count: {e}"); return 0

# ── Conversations ─────────────────────────────────────────────────────────────
def get_history(phone):
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT history FROM conversations WHERE phone=%s", (phone,))
            row = cur.fetchone()
            return row[0] if row else []
    except Exception as e:
        logger.error(f"get_history: {e}"); return []

def save_history(phone, history):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO conversations (phone, history, last_seen) VALUES (%s,%s,NOW())
                ON CONFLICT (phone) DO UPDATE SET history=EXCLUDED.history, last_seen=NOW()
            """, (phone, json.dumps(history)))
            conn.commit()
    except Exception as e:
        logger.error(f"save_history: {e}")

def touch_active_user(phone, name=None):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
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
            conn.commit()
    except Exception as e:
        logger.error(f"touch_active_user: {e}")

def get_all_conversations():
    if not DATABASE_URL: return {}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("""
                SELECT phone, name, history, last_seen, admin_takeover,
                       escalated, escalation_reason
                FROM conversations ORDER BY last_seen DESC
            """)
            rows = cur.fetchall()
        result = {}
        for r in rows:
            result[r["phone"]] = {
                "phone": r["phone"], "name": r["name"],
                "history": r["history"] or [],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else "",
                "admin_takeover": r["admin_takeover"],
                "escalated": r.get("escalated", False),
                "escalation_reason": r.get("escalation_reason"),
            }
        return result
    except Exception as e:
        logger.error(f"get_all_conversations: {e}"); return {}

def get_active_user_phones():
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT phone FROM conversations")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_active_user_phones: {e}"); return []

def count_total_users():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM conversations")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_total_users: {e}"); return 0

def count_active_since(hours):
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM conversations WHERE last_seen > NOW() - make_interval(hours => %s)",
                (int(hours),)
            )
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_active_since: {e}"); return 0

def set_admin_takeover(phone, value):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO conversations (phone, admin_takeover) VALUES (%s,%s)
                ON CONFLICT (phone) DO UPDATE SET admin_takeover=EXCLUDED.admin_takeover
            """, (phone, value))
            conn.commit()
    except Exception as e:
        logger.error(f"set_admin_takeover: {e}")

def is_admin_takeover(phone):
    if not DATABASE_URL: return False
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT admin_takeover FROM conversations WHERE phone=%s", (phone,))
            row = cur.fetchone()
            return row[0] if row else False
    except Exception as e:
        logger.error(f"is_admin_takeover: {e}"); return False

def get_takeover_phones():
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT phone FROM conversations WHERE admin_takeover=TRUE")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_takeover_phones: {e}"); return []

def count_takeovers():
    return len(get_takeover_phones())

# ── Escalations ───────────────────────────────────────────────────────────────
def set_escalated(phone, reason):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO conversations (phone, escalated, escalation_reason, escalated_at)
                VALUES (%s, TRUE, %s, NOW())
                ON CONFLICT (phone) DO UPDATE SET
                    escalated=TRUE, escalation_reason=EXCLUDED.escalation_reason, escalated_at=NOW()
            """, (phone, reason))
            # Look up name for the history row, if we have one on file
            cur.execute("SELECT name FROM conversations WHERE phone=%s", (phone,))
            row = cur.fetchone()
            name = row[0] if row else None
            cur.execute("""
                INSERT INTO escalation_history (phone, name, reason, escalated_at)
                VALUES (%s, %s, %s, NOW())
            """, (phone, name, reason))
            conn.commit()
    except Exception as e:
        logger.error(f"set_escalated: {e}")

def clear_escalated(phone, resolved_by="admin"):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE conversations SET escalated=FALSE WHERE phone=%s", (phone,))
            # Mark the most recent open history row for this phone as resolved
            cur.execute("""
                UPDATE escalation_history SET resolved_at=NOW(), resolved_by=%s
                WHERE id = (
                    SELECT id FROM escalation_history
                    WHERE phone=%s AND resolved_at IS NULL
                    ORDER BY escalated_at DESC LIMIT 1
                )
            """, (resolved_by, phone))
            conn.commit()
    except Exception as e:
        logger.error(f"clear_escalated: {e}")

def get_escalated_conversations():
    """Returns list of dicts for conversations currently flagged as escalated."""
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("""
                SELECT phone, name, escalation_reason, escalated_at
                FROM conversations WHERE escalated=TRUE
                ORDER BY escalated_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("escalated_at"): r["escalated_at"] = r["escalated_at"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_escalated_conversations: {e}"); return []

def count_escalated():
    return len(get_escalated_conversations())

def get_escalation_history(limit=100):
    """Returns past escalations (both resolved and still-open), most recent first."""
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("""
                SELECT phone, name, reason, escalated_at, resolved_at, resolved_by
                FROM escalation_history
                ORDER BY escalated_at DESC
                LIMIT %s
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("escalated_at"): r["escalated_at"] = r["escalated_at"].isoformat()
            if r.get("resolved_at"): r["resolved_at"] = r["resolved_at"].isoformat()
            r["status"] = "resolved" if r.get("resolved_at") else "open"
            if r["status"] == "resolved" and r.get("escalated_at") and r.get("resolved_at"):
                from datetime import datetime as _dt
                try:
                    e = _dt.fromisoformat(r["escalated_at"]); res = _dt.fromisoformat(r["resolved_at"])
                    r["resolution_minutes"] = round((res - e).total_seconds() / 60, 1)
                except Exception:
                    r["resolution_minutes"] = None
            else:
                r["resolution_minutes"] = None
        return rows
    except Exception as e:
        logger.error(f"get_escalation_history: {e}"); return []

# ── FAQ counts ────────────────────────────────────────────────────────────────
def increment_faq(keyword):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO faq_counts (keyword, count) VALUES (%s,1)
                ON CONFLICT (keyword) DO UPDATE SET count=faq_counts.count+1
            """, (keyword,))
            conn.commit()
    except Exception as e:
        logger.error(f"increment_faq: {e}")

def get_faq_counts():
    if not DATABASE_URL: return {}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT keyword, count FROM faq_counts")
            return dict(cur.fetchall())
    except Exception as e:
        logger.error(f"get_faq_counts: {e}"); return {}

# ── Daily counts (messages today/yesterday, bot vs admin replies) ─────────────
def increment_daily(day, metric):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO daily_counts (day, metric, count) VALUES (%s,%s,1)
                ON CONFLICT (day, metric) DO UPDATE SET count=daily_counts.count+1
            """, (day, metric))
            conn.commit()
    except Exception as e:
        logger.error(f"increment_daily: {e}")

def get_daily_count(day, metric):
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count FROM daily_counts WHERE day=%s AND metric=%s", (day, metric))
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"get_daily_count: {e}"); return 0

def get_daily_total(day):
    """Sum of inbound + outbound_bot + outbound_admin for a given day."""
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(count),0) FROM daily_counts WHERE day=%s", (day,))
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"get_daily_total: {e}"); return 0

def get_total_inbound_all_time():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(count),0) FROM daily_counts WHERE metric='inbound'")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"get_total_inbound_all_time: {e}"); return 0

# ── Response times ───────────────────────────────────────────────────────────
def add_response_time(seconds):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO response_times (seconds) VALUES (%s)", (seconds,))
            # Keep table small — delete old rows beyond 500
            cur.execute("""
                DELETE FROM response_times WHERE id NOT IN (
                    SELECT id FROM response_times ORDER BY id DESC LIMIT 500
                )
            """)
            conn.commit()
    except Exception as e:
        logger.error(f"add_response_time: {e}")

def get_avg_response_time():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT AVG(seconds) FROM response_times")
            row = cur.fetchone()
            return round(row[0], 1) if row and row[0] else 0
    except Exception as e:
        logger.error(f"get_avg_response_time: {e}"); return 0

# ── Broadcasts ────────────────────────────────────────────────────────────────
def save_broadcast(message, recipients, sent, failed):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO broadcasts (message, recipients, sent, failed) VALUES (%s,%s,%s,%s)",
                (message, json.dumps(recipients), sent, failed)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"save_broadcast: {e}")

def get_broadcast_history():
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM broadcasts ORDER BY timestamp DESC")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"get_broadcast_history: {e}"); return []

def count_broadcasts():
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM broadcasts")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_broadcasts: {e}"); return 0

# ── Quick replies ─────────────────────────────────────────────────────────────
def get_quick_replies():
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM quick_replies ORDER BY id")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_quick_replies: {e}"); return []

def add_quick_reply(title, body):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO quick_replies (title, body) VALUES (%s,%s)", (title, body))
            conn.commit()
    except Exception as e:
        logger.error(f"add_quick_reply: {e}")

def delete_quick_reply(qid):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM quick_replies WHERE id=%s", (qid,))
            conn.commit()
    except Exception as e:
        logger.error(f"delete_quick_reply: {e}")

# ── Settings (bot pause flag) ───────────────────────────────────────────────────
def get_setting(key, default=None):
    if not DATABASE_URL: return default
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception as e:
        logger.error(f"get_setting: {e}"); return default

def set_setting(key, value):
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO settings (key, value) VALUES (%s,%s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """, (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"set_setting: {e}")

def is_bot_paused():
    return get_setting("bot_paused", "false") == "true"

def set_bot_paused(value: bool):
    set_setting("bot_paused", "true" if value else "false")

# ── Active admin WhatsApp session (sticky reply-by-phone) ──────────────────────
# Once the admin replies with "code: message", we remember which parent they're
# now talking to, so every following message (no code needed) goes to that same
# parent until the admin types "done"/"release" to end the session.
# A timestamp is stored alongside the phone so a forgotten session auto-expires
# rather than silencing the bot for that parent indefinitely.
import time as _time
SESSION_EXPIRY_SECONDS = 4 * 60 * 60  # 4 hours

def set_active_admin_session(phone):
    set_setting("admin_active_session", phone)
    set_setting("admin_active_session_ts", str(_time.time()))

def get_active_admin_session():
    phone = get_setting("admin_active_session", "") or None
    if not phone:
        return None
    ts = get_setting("admin_active_session_ts", "0")
    try:
        age = _time.time() - float(ts)
    except ValueError:
        age = 0
    if age > SESSION_EXPIRY_SECONDS:
        clear_active_admin_session()
        set_admin_takeover(phone, False)
        return None
    return phone

def clear_active_admin_session():
    set_setting("admin_active_session", "")
    set_setting("admin_active_session_ts", "")

# ── Reply codes (let admin reply to parents directly from their own phone) ───
def save_reply_code(code, phone):
    """Map a short code (e.g. '4077') to a parent's full phone number."""
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO reply_codes (code, phone, created_at) VALUES (%s,%s,NOW())
                ON CONFLICT (code) DO UPDATE SET phone=EXCLUDED.phone, created_at=NOW()
            """, (code, phone))
            conn.commit()
    except Exception as e:
        logger.error(f"save_reply_code: {e}")

def get_phone_by_code(code):
    if not DATABASE_URL: return None
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT phone FROM reply_codes WHERE code=%s", (code,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"get_phone_by_code: {e}"); return None

# ── Activity items for activity log ─────────────────────────────────────────
def get_activity_items(limit=100):
    """Returns latest message per phone, with status derived from last message."""
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("""
                SELECT DISTINCT ON (m.phone) m.phone, m.message, m.direction, m.sender, m.timestamp, c.name
                FROM messages m
                LEFT JOIN conversations c ON c.phone = m.phone
                ORDER BY m.phone, m.timestamp DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("timestamp"): r["timestamp"] = r["timestamp"].isoformat()
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[:limit]
    except Exception as e:
        logger.error(f"get_activity_items: {e}"); return []
