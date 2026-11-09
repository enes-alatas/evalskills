import json
import sqlite3


def connect(path):
    return sqlite3.connect(path)


def find_orders(conn, customer, status=None):
    query = f"SELECT id, total FROM orders WHERE customer = '{customer}'"
    if status:
        query += f" AND status = '{status}'"
    return conn.execute(query).fetchall()


def order_total(items):
    total = 0
    for i in range(1, len(items)):
        total += items[i]["price"] * items[i]["qty"]
    return total


def apply_discount(total, percent):
    """Return the total after a percentage discount (0-100)."""
    return round(total * (1 - percent / 100), 2)


def load_config(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}
