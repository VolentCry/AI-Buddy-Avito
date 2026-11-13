import secrets

import psycopg
from flask import current_app
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    pending_question TEXT,
    confirmed_resource TEXT,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days'
);
CREATE TABLE IF NOT EXISTS access_requests (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    access_level TEXT NOT NULL CHECK (access_level = 'read'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'granted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, resource_id, access_level)
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_user_id ON messages (user_id, id);
"""


def connect():
    return psycopg.connect(
        current_app.config["DATABASE_URL"],
        row_factory=dict_row,
        connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=3000",
    )


def initialize():
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ai-buddy-schema'))")
        conn.execute(SCHEMA)
        if not conn.execute("SELECT to_regclass('user_access') AS table_name").fetchone()["table_name"]:
            conn.execute("""
                CREATE TABLE user_access (
                    user_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    access_level TEXT NOT NULL CHECK (access_level = 'read'),
                    PRIMARY KEY (user_id, resource_id, access_level)
                );
                INSERT INTO user_access (user_id, resource_id, access_level)
                SELECT user_id, resource_id, access_level FROM access_requests WHERE status = 'granted';
            """)
        conn.execute("""
            ALTER TABLE user_access ADD COLUMN IF NOT EXISTS status TEXT NOT NULL
                DEFAULT 'granted' CHECK (status IN ('missing', 'granted'));
            ALTER TABLE user_access ALTER COLUMN status DROP DEFAULT;
            INSERT INTO user_access (user_id, resource_id, access_level, status)
            VALUES ('intern-pregranted', 'catalog-dev', 'read', 'granted') ON CONFLICT DO NOTHING;
        """)


def new_session(user_id):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO browser_sessions (id, user_id, csrf_token) VALUES (%s, %s, %s) RETURNING *",
            (secrets.token_urlsafe(32), user_id, secrets.token_urlsafe(32)),
        ).fetchone()


def get_session(session_id):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM browser_sessions WHERE id = %s AND expires_at > now()",
            (session_id,),
        ).fetchone()


def set_clarification(session_id, question, resource):
    with connect() as conn:
        conn.execute(
            "UPDATE browser_sessions SET pending_question = %s, confirmed_resource = %s WHERE id = %s",
            (question, resource, session_id),
        )


def history(user_id):
    with connect() as conn:
        return conn.execute(
            "SELECT role, content, details FROM "
            "(SELECT * FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT 100) recent ORDER BY id",
            (user_id,),
        ).fetchall()


def save_exchange(user_id, question, response):
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (%s, 'user', %s)",
            (user_id, question),
        )
        conn.execute(
            "INSERT INTO messages (user_id, role, content, details) VALUES (%s, 'assistant', %s, %s)",
            (user_id, response["answer"], Jsonb({
                "sources": response["sources"], "generation": response["generation"],
                "action": response["action"],
            })),
        )
