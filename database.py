"""
SQLite database for nutrition tracking
"""

import sqlite3
import os
from datetime import date, timedelta

DB_PATH = os.environ.get("DB_PATH", "nutrition.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                items TEXT,
                calories REAL DEFAULT 0,
                protein REAL DEFAULT 0,
                fat REAL DEFAULT 0,
                carbs REAL DEFAULT 0,
                day_type TEXT DEFAULT 'deficit',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON food_entries(date)")
        conn.commit()


def add_food_entry(date: str, description: str, items: str,
                   calories: float, protein: float, fat: float,
                   carbs: float, day_type: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO food_entries (date, description, items, calories, protein, fat, carbs, day_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, description, items, calories, protein, fat, carbs, day_type)
        )
        conn.commit()


def get_daily_totals(date: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 
                COALESCE(SUM(calories), 0) as calories,
                COALESCE(SUM(protein), 0) as protein,
                COALESCE(SUM(fat), 0) as fat,
                COALESCE(SUM(carbs), 0) as carbs
               FROM food_entries WHERE date = ?""",
            (date,)
        ).fetchone()
        return dict(row) if row else {"calories": 0, "protein": 0, "fat": 0, "carbs": 0}


def get_daily_entries(date: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM food_entries WHERE date = ? ORDER BY created_at",
            (date,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(days: int = 7) -> list:
    """Returns daily totals for last N days (excluding today)"""
    results = []
    today = date.today()

    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        d_str = d.isoformat()

        with get_conn() as conn:
            row = conn.execute(
                """SELECT 
                    COALESCE(SUM(calories), 0) as calories,
                    COALESCE(SUM(protein), 0) as protein,
                    COALESCE(SUM(fat), 0) as fat,
                    COALESCE(SUM(carbs), 0) as carbs,
                    day_type
                   FROM food_entries WHERE date = ?""",
                (d_str,)
            ).fetchone()

            if row and row['calories'] > 0:
                results.append({
                    "date": d_str,
                    "calories": row['calories'],
                    "protein": row['protein'],
                    "fat": row['fat'],
                    "carbs": row['carbs'],
                    "day_type": row['day_type'] or ("maintenance" if d.weekday() >= 5 else "deficit")
                })

    return results
