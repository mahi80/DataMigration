"""Reproducible synthetic fixtures. Never uses or resets an existing database."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import yaml

from .spec import MigrationSpec


SEED_SQL = """
CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
CREATE TABLE products(id INTEGER PRIMARY KEY, sku TEXT NOT NULL, unit_price_cents INTEGER NOT NULL);
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, placed_at TEXT NOT NULL);
CREATE TABLE order_items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL);
INSERT INTO customers VALUES(1,'  Asha Verma  ','ASHA@EXAMPLE.INVALID'),(2,'Ravi Shah','RAVI@EXAMPLE.INVALID');
INSERT INTO products VALUES(10,'WIDGET-A',12500),(20,'WIDGET-B',24900);
INSERT INTO orders VALUES(100,1,'2026-11-01T10:00:00'),(200,2,'2026-11-02T11:30:00');
INSERT INTO order_items VALUES(1001,100,10,2),(1002,100,20,1),(2001,200,10,3);
"""

STAR_SQL = """
CREATE TABLE dim_customer(customer_key INTEGER PRIMARY KEY, customer_name TEXT NOT NULL);
INSERT INTO dim_customer SELECT id, trim(name) FROM customers;
CREATE TABLE dim_product(product_key INTEGER PRIMARY KEY, sku TEXT NOT NULL);
INSERT INTO dim_product SELECT id, sku FROM products;
CREATE TABLE fact_sales(sales_line_key INTEGER PRIMARY KEY, customer_key INTEGER NOT NULL,
product_key INTEGER NOT NULL, quantity INTEGER NOT NULL, sales_amount_cents INTEGER NOT NULL, sale_date TEXT NOT NULL);
INSERT INTO fact_sales
SELECT i.id AS sales_line_key, o.customer_id AS customer_key, i.product_id AS product_key,
       i.quantity, i.quantity*p.unit_price_cents AS sales_amount_cents,
       substr(o.placed_at,1,10) AS sale_date
FROM order_items i JOIN orders o ON i.order_id=o.id JOIN products p ON p.id=i.product_id;
"""


def seed_demo(directory: str | Path, *, star: bool = False, delta: bool = False) -> tuple[MigrationSpec, Path]:
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source, target = directory / "source.sqlite", directory / "target.sqlite"
    if source.exists() or target.exists() or (directory / "delta").exists():
        raise FileExistsError("Demo directory already contains data; choose a new --directory")
    with sqlite3.connect(source) as db:
        db.executescript(SEED_SQL)
        if star:
            db.executescript(STAR_SQL)
    os.environ["MIGRATION_DEMO_SOURCE_URI"] = f"sqlite:///{source}"
    os.environ["MIGRATION_DEMO_TARGET_URI"] = str(directory / "delta") if delta else f"sqlite:///{target}"
    if star:
        tables = [
            {"source": "dim_customer", "target": "dim_customer", "keys": ["customer_key"]},
            {"source": "dim_product", "target": "dim_product", "keys": ["product_key"]},
            {"source": "fact_sales", "target": "fact_sales", "keys": ["sales_line_key"], "depends_on": ["dim_customer", "dim_product"]},
        ]
    else:
        tables = [
            {"source": "customers", "target": "customers", "keys": ["customer_id"], "columns": [
                {"source": "id", "target": "customer_id", "data_type": "integer", "nullable": False},
                {"source": "name", "target": "full_name", "data_type": "string", "transform": "trim", "nullable": False},
                {"source": "email", "target": "email", "data_type": "string", "transform": "lower", "nullable": False},
            ]},
            {"source": "products", "target": "products", "keys": ["id"]},
            {"source": "orders", "target": "orders", "keys": ["id"], "depends_on": ["customers"]},
            {"source": "order_items", "target": "order_items", "keys": ["id"], "depends_on": ["orders", "products"]},
        ]
    spec = MigrationSpec.model_validate({
        "name": "synthetic-star-demo" if star else "synthetic-oltp-demo",
        "source": {"kind": "sqlite", "url_env": "MIGRATION_DEMO_SOURCE_URI"},
        "target": {"kind": "delta" if delta else "sqlite", "url_env": "MIGRATION_DEMO_TARGET_URI"},
        "workload": "star_schema" if star else "oltp",
        "tables": tables,
        "policy": {"source_quiesced": True, "batch_size": 2},
    })
    path = directory / "migration.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    (directory / "source_uri.txt").write_text(os.environ["MIGRATION_DEMO_SOURCE_URI"] + "\n")
    (directory / "target_uri.txt").write_text(os.environ["MIGRATION_DEMO_TARGET_URI"] + "\n")
    return spec, path
