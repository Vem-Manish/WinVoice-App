# contacts_store.py

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "winvoice.db"


# -------------------------
# DB init (auto-create)
# -------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE
        )
    """)
    conn.commit()
    conn.close()


# -------------------------
# CRUD operations
# -------------------------

def add_contact(name: str, email: str):
    if not name or not email:
        return "❌ Name and email are required."

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO contacts (name, email) VALUES (?, ?)",
            (name.strip(), email.strip())
        )
        conn.commit()
        return f"✅ Added contact: {name} ({email})"
    except sqlite3.IntegrityError:
        return f"⚠️ Contact with email {email} already exists."
    finally:
        conn.close()


def delete_contact(email: str):
    if not email:
        return "❌ Email is required to delete a contact."

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM contacts WHERE email = ?",
        (email.strip(),)
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()

    if deleted:
        return f"🗑️ Deleted contact with email {email}"
    else:
        return f"⚠️ No contact found with email {email}"


def get_all_contacts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, email FROM contacts ORDER BY name")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "📭 No contacts found."

    return "\n".join(f"- {name} ({email})" for name, email in rows)


def find_contact(name: str):
    if not name:
        return []

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, email FROM contacts WHERE LOWER(name) LIKE ?",
        (f"%{name.lower()}%",)
    )
    results = cursor.fetchall()
    conn.close()

    return results
