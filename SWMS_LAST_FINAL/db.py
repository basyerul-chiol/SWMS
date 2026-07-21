import os
from contextlib import contextmanager

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error

load_dotenv()


def get_db_connection():
    """Create and return a new MySQL connection using values from .env."""
    try:
        return mysql.connector.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "swms_db"),
            autocommit=False,
        )
    except Error as error:
        print(f"MySQL connection error: {error}")
        return None


@contextmanager
def database_cursor(dictionary=True):
    """Yield a cursor and safely commit/rollback/close the connection."""
    connection = get_db_connection()
    if connection is None:
        raise RuntimeError("Unable to connect to MySQL.")

    cursor = connection.cursor(dictionary=dictionary)
    try:
        yield connection, cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def test_database_connection():
    connection = get_db_connection()
    if connection is None:
        print("❌ Database connection failed.")
        return False

    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT VERSION() AS version")
        result = cursor.fetchone()
        print("✅ Database connection successful.")
        print(f"MySQL version: {result['version']}")
        print(f"Database: {os.getenv('DB_NAME', 'swms_db')}")
        cursor.close()
        return True
    except Error as error:
        print(f"❌ Database query failed: {error}")
        return False
    finally:
        if connection.is_connected():
            connection.close()


if __name__ == "__main__":
    test_database_connection()
