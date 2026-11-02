"""Schema upgrades for databases created by an earlier version of the app.

db.create_all() creates missing tables but never alters existing ones, so new
nullable columns are added here, and tables whose constraints changed are
rebuilt by app.models.migrations. Existing rows and data are kept.
"""

import os

from flask import current_app
from sqlalchemy import inspect, text

from app.extensions import db
from app.models.migrations import rebuild_tables

ADDED_COLUMNS = {
    "planner_profiles": [
        ("business_category", "VARCHAR(80)"),
        ("business_address", "VARCHAR(255)"),
        ("pacra_number", "VARCHAR(40)"),
        ("pacra_reference", "VARCHAR(60)"),
        ("pacra_document", "VARCHAR(255)"),
    ],
}


def upgrade_schema():
    added = []
    if db.engine.dialect.name == "sqlite":
        rebuilt = rebuild_tables(db.engine, db.metadata, os.path.join(current_app.instance_path, "backups"))
        for step in rebuilt:
            current_app.logger.warning("Schema upgrade: %s", step)
        added.extend(rebuilt)
    inspector = inspect(db.engine)
    with db.engine.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns:
                if name not in existing:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}'))
                    added.append(f"{table}.{name}")
    return added
