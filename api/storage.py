#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite storage for Samarth Insta Info."""

import os
import sqlite3
import json
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "insta_history.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ts TEXT NOT NULL,
            followers INTEGER,
            following INTEGER,
            posts INTEGER,
            bio TEXT,
            full_name TEXT,
            profile_pic TEXT,
            is_verified INTEGER,
            is_private INTEGER,
            external_url TEXT,
            UNIQUE(username, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_username_ts ON snapshots(username, ts);

        CREATE TABLE IF NOT EXISTS profiles (
            username TEXT PRIMARY KEY,
            user_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            last_payload TEXT
        );
        """)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_snapshot(data, ts):
    with db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO snapshots
            (username, ts, followers, following, posts, bio, full_name,
             profile_pic, is_verified, is_private, external_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("username"), ts,
            data.get("followers"), data.get("following"), data.get("posts"),
            data.get("biography"), data.get("full_name"),
            data.get("profile_pic"), 1 if data.get("is_verified") else 0,
            1 if data.get("is_private") else 0,
            data.get("external_url"),
        ))


def save_profile_meta(username, meta, ts):
    with db() as conn:
        existing = conn.execute(
            "SELECT first_seen FROM profiles WHERE username=?", (username,)
        ).fetchone()
        first = existing["first_seen"] if existing else ts
        conn.execute("""
            INSERT OR REPLACE INTO profiles
            (username, user_id, first_seen, last_seen, last_payload)
            VALUES (?, ?, ?, ?, ?)
        """, (username, meta.get("user_id"), first, ts, json.dumps(meta)))


def get_history(username, limit=90):
    with db() as conn:
        rows = conn.execute("""
            SELECT ts, followers, following, posts
            FROM snapshots
            WHERE username = ?
            ORDER BY ts DESC
            LIMIT ?
        """, (username, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


init_db()
