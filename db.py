import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])

def get_cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            mode TEXT DEFAULT 'note',
            updated_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT NOT NULL,
            due_date DATE DEFAULT CURRENT_DATE,
            done BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            description TEXT NOT NULL,
            tx_date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_sets (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            currency TEXT DEFAULT 'TWD',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tx_date DATE DEFAULT CURRENT_DATE;
    """)
    cur.execute("""
        ALTER TABLE transactions ADD COLUMN IF NOT EXISTS account_set_id INTEGER DEFAULT NULL;
    """)
    cur.execute("""
        ALTER TABLE todos ADD COLUMN IF NOT EXISTS due_time TIME DEFAULT NULL;
    """)
    cur.execute("""
        ALTER TABLE user_state ADD COLUMN IF NOT EXISTS push_time TIME DEFAULT NULL;
    """)
    cur.execute("""
        ALTER TABLE user_state ADD COLUMN IF NOT EXISTS active_account_set_id INTEGER DEFAULT NULL;
    """)
    conn.commit()
    cur.close()
    conn.close()
