"""Additive schema upgrades for databases created before a column existed.

db.create_all() creates missing tables but never alters existing ones, so new
nullable columns are added here. Existing rows and data are left untouched.
"""

from sqlalchemy import inspect, text

from app.extensions import db

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
    inspector = inspect(db.engine)
    added = []
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
