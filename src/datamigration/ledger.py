"""Durable receipts for replayable data batches; separate from graph checkpoints."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS batches (
            run_id TEXT NOT NULL, plan_hash TEXT NOT NULL, table_name TEXT NOT NULL,
            batch_no INTEGER NOT NULL, digest TEXT NOT NULL, row_count INTEGER NOT NULL,
            committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, plan_hash, table_name, batch_no))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS approvals (
            run_id TEXT NOT NULL, plan_hash TEXT NOT NULL, reviewer TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, plan_hash))""")
        self.db.commit()

    def approved(self, run_id: str, plan_hash: str) -> bool:
        return self.db.execute("SELECT 1 FROM approvals WHERE run_id=? AND plan_hash=?", (run_id, plan_hash)).fetchone() is not None

    def approve(self, run_id: str, plan_hash: str, reviewer: str) -> None:
        if not reviewer.strip():
            raise ValueError("Reviewer identity is required")
        self.db.execute("INSERT OR IGNORE INTO approvals(run_id,plan_hash,reviewer) VALUES(?,?,?)", (run_id, plan_hash, reviewer))
        self.db.commit()

    def completed(self, run_id: str, plan_hash: str, table: str, batch: int, digest: str) -> bool:
        row = self.db.execute("SELECT digest FROM batches WHERE run_id=? AND plan_hash=? AND table_name=? AND batch_no=?", (run_id, plan_hash, table, batch)).fetchone()
        if row and row[0] != digest:
            raise ValueError("Previously committed batch differs from staged data; start a reviewed new run")
        return row is not None

    def complete(self, run_id: str, plan_hash: str, table: str, batch: int, digest: str, rows: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO batches(run_id,plan_hash,table_name,batch_no,digest,row_count) VALUES(?,?,?,?,?,?)", (run_id, plan_hash, table, batch, digest, rows))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
