"""
Простое хранилище для защиты от дублей.
Хранит хэши уже обработанных постов, чтобы не публиковать одно и то же
дважды (например, если несколько источников постят одну новость).
"""
import sqlite3
import hashlib
from pathlib import Path

DB_PATH = Path(__file__).parent / "posts.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id INTEGER,
            source_msg_id INTEGER,
            content_hash TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def _hash_text(text: str) -> str:
    # Нормализуем текст перед хэшированием, чтобы ловить почти-дубли
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_duplicate(text: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    content_hash = _hash_text(text)
    cur = conn.execute(
        "SELECT 1 FROM processed_posts WHERE content_hash = ?", (content_hash,)
    )
    result = cur.fetchone() is not None
    conn.close()
    return result


def mark_processed(source_chat_id: int, source_msg_id: int, text: str):
    conn = sqlite3.connect(DB_PATH)
    content_hash = _hash_text(text)
    try:
        conn.execute(
            "INSERT INTO processed_posts (source_chat_id, source_msg_id, content_hash) "
            "VALUES (?, ?, ?)",
            (source_chat_id, source_msg_id, content_hash),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # уже есть такой хэш — игнорируем
    conn.close()
