"""Migrate a SQLAlchemy ORM-managed source to a separate database.

Run from the repository root with the base package installed. The ORM creates
only this synthetic fixture. The migration connector reflects actual tables;
it does not import application models or automatically translate ORM hooks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from datamigration.spec import MigrationSpec
from datamigration.workflow import RunManager


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".demo/orm")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source_url = f"sqlite:///{directory}/source.sqlite"
    os.environ["ORM_SOURCE_URL"] = source_url
    os.environ["ORM_TARGET_URL"] = f"sqlite:///{directory}/target.sqlite"
    engine = create_engine(source_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Customer(id=1, name="Asha Verma", email="asha@example.invalid"),
            Customer(id=2, name="Ravi Shah", email="ravi@example.invalid"),
        ])
        session.commit()
    engine.dispose()
    spec = MigrationSpec.model_validate({
        "name": "synthetic-orm-source",
        "source": {"kind": "sqlite", "url_env": "ORM_SOURCE_URL"},
        "target": {"kind": "sqlite", "url_env": "ORM_TARGET_URL"},
        "tables": [{"source": "customers", "target": "customers", "keys": ["id"]}],
        "policy": {"source_quiesced": True},
    })
    manager = RunManager(directory / "state")
    plan = manager.start(spec, "orm-example")
    result = manager.decide("orm-example", approved=True, plan_hash=plan["plan_hash"],
                            reviewer="synthetic-orm-example-only")
    print(json.dumps({"synthetic_data_only": True, "run": result}, indent=2))


if __name__ == "__main__":
    main()
