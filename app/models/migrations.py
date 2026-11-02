"""Table rebuilds for SQLite schema changes that ALTER TABLE cannot express.

SQLite cannot change a CHECK constraint or make a NOT NULL column nullable in
place. The affected tables are rebuilt with the documented SQLite procedure:
create the new table, copy every row, drop the old table, rename the new one.
A backup copy of the database is written first and the whole rebuild runs in
one transaction, so a failure leaves the original tables untouched.
"""

import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy.schema import CreateIndex, CreateTable


def _table_sql(cursor, name):
    row = cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row[0] if row else None


def _columns(cursor, name):
    return {row[1]: row for row in cursor.execute(f'PRAGMA table_info("{name}")')}


def _users_needs_rebuild(cursor):
    sql = _table_sql(cursor, "users")
    return sql is not None and "'attendant'" not in sql


def _redemptions_need_rebuild(cursor):
    if _table_sql(cursor, "ticket_redemptions") is None:
        return False
    columns = _columns(cursor, "ticket_redemptions")
    return "event_id" not in columns or columns["ticket_id"][3] == 1


REDEMPTION_OVERRIDES = {
    "result": (
        "CASE result WHEN 'accepted' THEN 'valid' WHEN 'already_redeemed' THEN 'already_used' "
        "WHEN 'not_valid' THEN 'invalid' WHEN 'wrong_event' THEN 'invalid' ELSE result END"
    ),
    "reason": "CASE result WHEN 'not_valid' THEN 'not_eligible' WHEN 'wrong_event' THEN 'wrong_event' ELSE NULL END",
    "event_id": "(SELECT tickets.event_id FROM tickets WHERE tickets.id = ticket_redemptions.ticket_id)",
    "method": "'staff'",
}


def _rebuild(cursor, table, dialect, overrides=None):
    overrides = overrides or {}
    old_columns = list(_columns(cursor, table.name))
    new_columns = [column.name for column in table.columns]
    lost = [name for name in old_columns if name not in new_columns]
    if lost:
        raise RuntimeError(f"Refusing to rebuild {table.name}: columns {', '.join(lost)} are missing from the new schema.")
    temp_name = f"{table.name}__rebuild"
    ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
    prefix = f"CREATE TABLE {table.name} ("
    if not ddl.startswith(prefix):
        raise RuntimeError(f"Unexpected table definition for {table.name}.")
    cursor.execute(f'DROP TABLE IF EXISTS "{temp_name}"')
    cursor.execute(f'CREATE TABLE "{temp_name}" (' + ddl[len(prefix):])
    targets = old_columns + [name for name in overrides if name not in old_columns]
    quoted = [f'"{name}"' for name in targets]
    selects = [overrides.get(name, f'"{name}"') for name in targets]
    cursor.execute(f'INSERT INTO "{temp_name}" ({", ".join(quoted)}) SELECT {", ".join(selects)} FROM "{table.name}"')
    original = cursor.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
    copied = cursor.execute(f'SELECT COUNT(*) FROM "{temp_name}"').fetchone()[0]
    if original != copied:
        raise RuntimeError(f"Copied {copied} of {original} rows from {table.name}; nothing was changed.")
    cursor.execute(f'DROP TABLE "{table.name}"')
    cursor.execute(f'ALTER TABLE "{temp_name}" RENAME TO "{table.name}"')
    for index in table.indexes:
        cursor.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)))
    return copied


def _backup(connection, backup_dir, label):
    path = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not path:
        return None
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_dir, f"{os.path.splitext(os.path.basename(path))[0]}-before-{label}-{stamp}.db")
    destination = sqlite3.connect(target)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return target


def rebuild_tables(engine, metadata, backup_dir):
    """Rebuild tables whose constraints changed. Returns a description of what was done."""
    raw = engine.raw_connection()
    try:
        connection = raw.driver_connection
        cursor = connection.cursor()
        plan = []
        if _users_needs_rebuild(cursor):
            plan.append((metadata.tables["users"], None))
        if _redemptions_need_rebuild(cursor):
            plan.append((metadata.tables["ticket_redemptions"], REDEMPTION_OVERRIDES))
        if not plan:
            return []
        raw.commit()
        backup = _backup(connection, backup_dir, "gate-attendants")
        previous_isolation = connection.isolation_level
        connection.isolation_level = None
        cursor.execute("PRAGMA foreign_keys = OFF")
        done = []
        try:
            baseline = set(cursor.execute("PRAGMA foreign_key_check").fetchall())
            cursor.execute("BEGIN IMMEDIATE")
            try:
                for table, overrides in plan:
                    rows = _rebuild(cursor, table, engine.dialect, overrides)
                    done.append(f"rebuilt {table.name} ({rows} rows kept)")
                broken = set(cursor.execute("PRAGMA foreign_key_check").fetchall()) - baseline
                if broken:
                    raise RuntimeError(f"Rebuild would break {len(broken)} foreign key references; nothing was changed.")
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
        finally:
            cursor.execute("PRAGMA foreign_keys = ON")
            connection.isolation_level = previous_isolation
        if backup:
            done.append(f"backup saved to {backup}")
        return done
    finally:
        raw.close()
