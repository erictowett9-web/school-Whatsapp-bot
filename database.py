import os
import json
import logging
import contextlib
import psycopg
from psycopg.rows import dict_row
from datetime import datetime

logger = logging.getLogger(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Connection pool ────────────────────────────────────────────────────────────
# psycopg_pool is already in requirements.txt. Using a pool means we keep a
# small number of live connections open and reuse them across requests, rather
# than opening + closing a connection on every single DB call. At even modest
# traffic (dozens of parents messaging simultaneously) this makes a measurable
# difference in both latency and DB server load.
_pool = None

def _init_pool():
    global _pool
    if not DATABASE_URL:
        return
    try:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            kwargs={"sslmode": "require", "connect_timeout": 10},
            open=False,
        )
        _pool.open(wait=True, timeout=15)
        logger.info("✅ Connection pool initialized (min=2, max=10)")
    except Exception as e:
        logger.warning(f"⚠️ psycopg_pool unavailable ({e}) — falling back to per-request connections")
        _pool = None

def get_conn():
    """Legacy direct connection — use get_conn_ctx() for new code."""
    return psycopg.connect(DATABASE_URL, sslmode="require", connect_timeout=10)

@contextlib.contextmanager
def get_conn_ctx():
    """Preferred way to get a DB connection: borrows from the pool when
    available (fast), falls back to a direct connection otherwise.
    Guarantees cleanup even if the query raises an exception."""
    if _pool is not None:
        with _pool.connection() as conn:
            yield conn
    else:
        conn = psycopg.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
        try:
            yield conn
        finally:
            conn.close()

def init_db():
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL set — using in-memory fallback only")
        return
    _init_pool()
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
            cur.execute("""
            CREATE TABLE IF NOT EXISTS school_info (
                key TEXT PRIMARY KEY,
                value TEXT,
                label TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                detail TEXT,
                phone TEXT,
                performed_by TEXT DEFAULT 'admin',
                timestamp TIMESTAMPTZ DEFAULT NOW()
            )""")
            conn.commit()
        logger.info("✅ Database initialized")
        seed_quick_replies()
        seed_school_info()
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

def seed_school_info():
    """Seed the school_info table with verified Sally-Ann School data — June 2026.
    Uses DO UPDATE so corrected values overwrite any wrong placeholder data
    already in the DB from earlier deploys. Safe to re-run on every restart."""
    defaults = [
        # ── DAY FEES (Term 2 figures used as the standard per-term rate) ──
        ("fee_pp1",       "Term 1: Ksh 14,500 | Term 2 & 3: Ksh 13,500 each | Annual: Ksh 41,500",  "PP1 day fees 2026"),
        ("fee_pp2",       "Ksh 13,500 per term | Annual: Ksh 40,500",                                 "PP2 day fees 2026"),
        ("fee_grade_1",   "Term 1: Ksh 15,500 + Ksh 3,500 books = Ksh 19,000 | Term 2 & 3: Ksh 17,000 each | Annual: Ksh 53,000", "Grade 1 day fees 2026"),
        ("fee_grade_2",   "Term 1: Ksh 15,500 + Ksh 1,000 books = Ksh 16,500 | Term 2 & 3: Ksh 17,000 each | Annual: Ksh 50,500", "Grade 2 day fees 2026"),
        ("fee_grade_3",   "Term 1: Ksh 16,500 + Ksh 1,000 books = Ksh 17,500 | Term 2 & 3: Ksh 18,000 each | Annual: Ksh 53,500", "Grade 3 day fees 2026"),
        ("fee_grade_4",   "Term 1: Ksh 16,500 + Ksh 1,000 books = Ksh 17,500 | Term 2 & 3: Ksh 18,000 each | Annual: Ksh 53,500", "Grade 4 day fees 2026"),
        ("fee_grade_5",   "Term 1: Ksh 16,500 + Ksh 1,000 books = Ksh 17,500 | Term 2 & 3: Ksh 18,000 each | Annual: Ksh 53,500", "Grade 5 day fees 2026"),
        ("fee_grade_6_9_day", "Grade 6–9 are boarding only. No day scholar option for these grades.", "Grade 6–9 day fees 2026"),
        # ── BOARDING FEES ──
        ("fee_grade_6_boarding",  "Term 1: Ksh 25,000 + Ksh 1,000 books = Ksh 26,000 | Term 2 & 3: Ksh 26,500 each | Annual: Ksh 79,000", "Grade 6 boarding fees 2026"),
        ("fee_grade_7_boarding",  "Term 1: Ksh 26,500 + Ksh 1,000 books = Ksh 27,500 | Term 2 & 3: Ksh 28,000 each | Annual: Ksh 83,500", "Grade 7 boarding fees 2026"),
        ("fee_grade_8_boarding",  "Term 1: Ksh 26,500 + Ksh 1,000 books = Ksh 27,500 | Term 2 & 3: Ksh 28,000 each | Annual: Ksh 83,500", "Grade 8 boarding fees 2026"),
        ("fee_grade_9_boarding",  "Term 1: Ksh 28,000 + Ksh 1,000 books = Ksh 29,000 | Term 2: Ksh 28,000 | Term 3: Ksh 25,000 | Annual: Ksh 82,000", "Grade 9 boarding fees 2026"),
        # ── OTHER FEE ITEMS ──
        ("fee_admission", "2,000", "New admission fee (Ksh)"),
        ("fee_minimum_percent", "60", "Minimum % payable on Reporting Day"),
        ("fee_ict", "1,500", "ICT/Coding & Robotics per term (Ksh) — included in school fees"),
        # ── PAYMENT DETAILS — FEES ──
        ("pay_mpesa_paybill", "777643",        "M-Pesa Paybill (fees)"),
        ("pay_kcb",           "1135294917",    "KCB account"),
        ("pay_equity",        "0530291926992", "Equity account"),
        ("pay_equity_paybill","247247",         "Equity Paybill"),
        ("pay_coop",          "01148786054900","Coop Bank account"),
        ("pay_chai_sacco",    "1083225",        "Chai Sacco account"),
        # ── PAYMENT DETAILS — TRIPS ──
        ("trip_paybill",        "328585",           "M-Pesa Paybill (trips)"),
        ("trip_account_format", "111444#ADM number","Trip payment account format"),
        # ── TRIPS TERM II 2026 ──
        ("trip_grade_4", "Maasai Mara — Ksh 2,500 (deadline 22nd June 2026)",                    "Grade 4 trip"),
        ("trip_grade_5", "Nakuru — TBC",                                                           "Grade 5 trip"),
        ("trip_grade_6", "Naivasha — Ksh 3,500",                                                   "Grade 6 trip"),
        ("trip_grade_7", "Nairobi — Ksh 5,000",                                                    "Grade 7 trip"),
        ("trip_grade_8", "Mombasa — Ksh 15,000 (deposit Ksh 5,000 before 30th June 2026)",       "Grade 8 trip"),
        # ── BUS ROUTES ──
        ("bus_kapkatet",   "Koitabai 2300, Daraja Sita 1950, Factory 1850, Town 1600, Chematich 1850, Kapkatolonyi 1250, Kaptote 1150, DC Jct 950", "Kapkatet route fares/month"),
        ("bus_litein",     "Town/St Kizitos 950, Factory Gate 1050, Kwa Soi/Joyland 1150, Imarisha 1150, Kusumek 1600",                              "Litein route fares/month"),
        ("bus_tebesonik",  "Lalagin 1250, Kiptewit Jct 1500, Cheborge 1600, Korongoi 1700, Bokoiyot/Factory 2300",                                   "Tebesonik route fares/month"),
        ("bus_chemosot",   "Cheluget 1250, Chelilis/Chesingoro 1600, Kaminjeiwet/Getarwet Jct 1700",                                                  "Chemosot route fares/month"),
        ("bus_mogogosiek", "Murram 2600, Mogogosiek 2500, Boito Kaptien Rd 1850, Boito Shopping 1600, Chemoiben 1400, DC Residence 1050",             "Mogogosiek route fares/month"),
        # ── TERM DATES ──
        ("term_half_term", "24th–28th June 2026. School resumes Monday 30th June.", "Half term dates"),
        ("parental_days",  "Grade 5: May 16 | Grade 4: May 23 | Grade 3: May 30 | Grade 2: Jun 6 | Grade 1: Jun 13 | PP1&PP2: Jun 20", "Parental engagement days"),
        # ── SCHOOL CONTACT ──
        ("school_phone",   "0727839424",               "School phone number"),
        ("school_email",   "sas@sallyannschool.sc.ke", "School email"),
        ("school_address", "P.O Box 401-20210, Litein", "Postal address"),
        # ── ADMISSIONS ──
        ("admissions_form_link", "https://docs.google.com/forms/d/e/1FAIpQLSemf1iZghMpupJ98AeCqyMSdUfqqsqyPmaTdnmtm9Pc2LLkFg/viewform?usp=publish-editor", "Google Form link for admissions"),
    ]
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            for key, value, label in defaults:
                cur.execute("""
                    INSERT INTO school_info (key, value, label)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, label=EXCLUDED.label, updated_at=NOW()
                """, (key, value, label))
            conn.commit()
        logger.info(f"✅ School info seeded/updated: {len(defaults)} entries")
    except Exception as e:
        logger.error(f"seed_school_info error: {e}")

def get_school_info():
    """Returns all school info as a dict of key→value."""
    if not DATABASE_URL: return {}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM school_info ORDER BY key")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.error(f"get_school_info: {e}"); return {}

def set_school_info(key, value):
    """Update a single school info value from the dashboard."""
    if not DATABASE_URL: return False
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO school_info (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"set_school_info: {e}"); return False

def get_all_school_info_with_labels():
    """Returns all school info rows including labels for the dashboard editor."""
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value, label, updated_at FROM school_info ORDER BY key")
            rows = cur.fetchall()
        return [{"key": r[0], "value": r[1], "label": r[2],
                 "updated_at": r[3].isoformat() if r[3] else ""} for r in rows]
    except Exception as e:
        logger.error(f"get_all_school_info_with_labels: {e}"); return []

# ── Audit log ─────────────────────────────────────────────────────────────────
def log_audit(action, detail=None, phone=None, performed_by="admin"):
    """Record an admin action for the audit trail."""
    if not DATABASE_URL: return
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO audit_log (action, detail, phone, performed_by) VALUES (%s,%s,%s,%s)",
                (action, detail, phone, performed_by)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"log_audit: {e}")

def get_audit_log(limit=200):
    """Returns recent audit log entries, most recent first."""
    if not DATABASE_URL: return []
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, action, detail, phone, performed_by, timestamp
                FROM audit_log ORDER BY timestamp DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [{"id": r[0], "action": r[1], "detail": r[2],
                 "phone": r[3], "performed_by": r[4],
                 "timestamp": r[5].isoformat() if r[5] else ""} for r in rows]
    except Exception as e:
        logger.error(f"get_audit_log: {e}"); return []

def get_reports_data():
    """Returns aggregated data for the reports tab."""
    if not DATABASE_URL: return {}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            # Messages last 30 days by day
            cur.execute("""
                SELECT DATE(timestamp) as day, direction, COUNT(*) as cnt
                FROM messages
                WHERE timestamp > NOW() - INTERVAL '30 days'
                GROUP BY day, direction ORDER BY day DESC
            """)
            daily = cur.fetchall()
            # Bot vs human reply split
            cur.execute("""
                SELECT sender, COUNT(*) FROM messages
                WHERE direction='outbound' AND timestamp > NOW() - INTERVAL '30 days'
                GROUP BY sender
            """)
            senders = cur.fetchall()
            # Escalation stats
            cur.execute("""
                SELECT
                  COUNT(*) as total,
                  COUNT(resolved_at) as resolved,
                  AVG(EXTRACT(EPOCH FROM (resolved_at - escalated_at))/60) as avg_minutes
                FROM escalation_history
                WHERE escalated_at > NOW() - INTERVAL '30 days'
            """)
            esc = cur.fetchone()
            # Top FAQ keywords
            cur.execute("SELECT keyword, count FROM faq_counts ORDER BY count DESC LIMIT 10")
            faq = cur.fetchall()
            # Avg response time
            cur.execute("SELECT AVG(seconds) FROM response_times")
            avg_rt = cur.fetchone()
        return {
            "daily": [{"day": str(r[0]), "direction": r[1], "count": r[2]} for r in daily],
            "senders": {r[0]: r[1] for r in senders},
            "escalations": {
                "total": esc[0] if esc else 0,
                "resolved": esc[1] if esc else 0,
                "avg_minutes": round(float(esc[2]), 1) if esc and esc[2] else None
            },
            "faq": [{"keyword": r[0], "count": r[1]} for r in faq],
            "avg_response_seconds": round(float(avg_rt[0]), 1) if avg_rt and avg_rt[0] else None
        }
    except Exception as e:
        logger.error(f"get_reports_data: {e}"); return {}

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

def count_unread_for_phone(phone):
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE phone=%s AND direction='inbound' AND read_flag=FALSE",
                (phone,)
            )
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_unread_for_phone: {e}"); return 0

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
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM conversations WHERE admin_takeover=TRUE")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_takeovers: {e}"); return 0

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
    if not DATABASE_URL: return 0
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM conversations WHERE escalated=TRUE")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"count_escalated: {e}"); return 0

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

# ══════════════════════════════════════════════════════════════════════════════
# DATA RETENTION / DELETION
# ══════════════════════════════════════════════════════════════════════════════
# Built for Kenya's Data Protection Act (2019) compliance: parents have a
# right to request deletion of their data, and data shouldn't be kept
# indefinitely with no purpose. None of this runs automatically — every
# function here is meant to be triggered manually from the admin dashboard
# (or a future scheduled job, once the school decides on a retention period).

def preview_inactive_data(days):
    """Dry-run: shows what WOULD be deleted for conversations inactive for
    more than `days`, without deleting anything. Use this before calling
    delete_inactive_data() so the admin can review the list first."""
    if not DATABASE_URL: return {"phones": [], "message_count": 0}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("""
                SELECT phone, name, last_seen FROM conversations
                WHERE last_seen < NOW() - make_interval(days => %s)
                ORDER BY last_seen ASC
            """, (int(days),))
            phones_info = [dict(r) for r in cur.fetchall()]
            for p in phones_info:
                if p.get("last_seen"): p["last_seen"] = p["last_seen"].isoformat()
            phone_list = [p["phone"] for p in phones_info]
            if phone_list:
                cur.execute("SELECT COUNT(*) FROM messages WHERE phone = ANY(%s)", (phone_list,))
                msg_count = cur.fetchone()["count"]
            else:
                msg_count = 0
        return {"conversations": phones_info, "message_count": msg_count}
    except Exception as e:
        logger.error(f"preview_inactive_data: {e}"); return {"conversations": [], "message_count": 0}

def delete_inactive_data(days, confirm=False):
    """Permanently deletes messages, conversation history, and escalation
    history for any parent whose conversation has been inactive for more
    than `days`. Requires confirm=True as a safety check against accidental
    calls. Does NOT run on a schedule — must be explicitly triggered."""
    if not DATABASE_URL: return {"deleted_conversations": 0, "deleted_messages": 0}
    if not confirm:
        logger.warning("delete_inactive_data called without confirm=True — no action taken")
        return {"deleted_conversations": 0, "deleted_messages": 0}
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT phone FROM conversations
                WHERE last_seen < NOW() - make_interval(days => %s)
            """, (int(days),))
            phones = [r[0] for r in cur.fetchall()]
            if not phones:
                return {"deleted_conversations": 0, "deleted_messages": 0}

            cur.execute("DELETE FROM messages WHERE phone = ANY(%s)", (phones,))
            deleted_messages = cur.rowcount
            cur.execute("DELETE FROM escalation_history WHERE phone = ANY(%s)", (phones,))
            cur.execute("DELETE FROM conversations WHERE phone = ANY(%s)", (phones,))
            deleted_conversations = cur.rowcount
            conn.commit()
        logger.info(
            f"Data retention deletion: removed {deleted_conversations} conversations "
            f"and {deleted_messages} messages (inactive > {days} days)"
        )
        return {"deleted_conversations": deleted_conversations, "deleted_messages": deleted_messages}
    except Exception as e:
        logger.error(f"delete_inactive_data: {e}")
        return {"deleted_conversations": 0, "deleted_messages": 0}

def delete_parent_data(phone, confirm=False):
    """Permanently deletes ALL data for a single parent's phone number —
    messages, conversation/history, escalation history, reply codes. Use
    this when a parent explicitly requests deletion of their data (a right
    under Kenya's Data Protection Act). Requires confirm=True."""
    if not DATABASE_URL: return False
    if not confirm:
        logger.warning(f"delete_parent_data({phone}) called without confirm=True — no action taken")
        return False
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM escalation_history WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM conversations WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM reply_codes WHERE phone=%s", (phone,))
            conn.commit()
        logger.info(f"Deleted all data for {phone} per data deletion request")
        return True
    except Exception as e:
        logger.error(f"delete_parent_data: {e}"); return False

def export_parent_data(phone):
    """Returns all stored data for a single parent — for fulfilling a Data
    Protection Act data-access/export request, or before deleting on request
    so the school keeps a record of what was given to the parent."""
    if not DATABASE_URL: return None
    try:
        with get_conn_ctx() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute("SELECT * FROM conversations WHERE phone=%s", (phone,))
            conv = cur.fetchone()
            cur.execute("SELECT phone, message, direction, sender, timestamp FROM messages WHERE phone=%s ORDER BY timestamp ASC", (phone,))
            msgs = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM escalation_history WHERE phone=%s ORDER BY escalated_at ASC", (phone,))
            escalations = [dict(r) for r in cur.fetchall()]
        for m in msgs:
            if m.get("timestamp"): m["timestamp"] = m["timestamp"].isoformat()
        for e in escalations:
            if e.get("escalated_at"): e["escalated_at"] = e["escalated_at"].isoformat()
            if e.get("resolved_at"): e["resolved_at"] = e["resolved_at"].isoformat()
        conv_dict = dict(conv) if conv else None
        if conv_dict and conv_dict.get("last_seen"):
            conv_dict["last_seen"] = conv_dict["last_seen"].isoformat()
        if conv_dict and conv_dict.get("escalated_at"):
            conv_dict["escalated_at"] = conv_dict["escalated_at"].isoformat()
        return {"conversation": conv_dict, "messages": msgs, "escalation_history": escalations}
    except Exception as e:
        logger.error(f"export_parent_data: {e}"); return None
