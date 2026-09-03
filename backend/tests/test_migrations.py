"""migrations.run_all 必须幂等: 启动与备份恢复都会调它, 跑两遍不能报错、索引不能重复。"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text

from _fixtures import make_session  # noqa: F401  (sys.path 注入)
from app import migrations
from app.database import Base


def _index_names(engine, table):
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA index_list({table})"))}


class EnsureIndexesTest(unittest.TestCase):
    def test_indexes_created_and_idempotent(self):
        engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(bind=engine)
        # 模拟老库: 建表时没有这两个索引
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_transactions_item_id"))
            conn.execute(text("DROP INDEX IF EXISTS ix_transactions_location_id"))
        self.assertNotIn("ix_transactions_item_id", _index_names(engine, "transactions"))

        migrations.run_all(engine)
        migrations.run_all(engine)   # 第二遍不能抛

        names = _index_names(engine, "transactions")
        self.assertIn("ix_transactions_item_id", names)
        self.assertIn("ix_transactions_location_id", names)


if __name__ == "__main__":
    unittest.main()
