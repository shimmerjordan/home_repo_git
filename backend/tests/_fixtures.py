"""共用测试夹具: 一个内存 sqlite + 一份有"易混对"的真实感库存。

易混对是关键: 充电宝/充电器、洗手液/洗发水、螺丝刀/螺丝 —— 用户抱怨的
"存入新物品时错错误匹配到已有物品" 全发生在这类名字相近的组合上。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base


def make_session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, future=True)()


# (name, aliases, category, quantity, 位置路径)
ITEMS = [
    ("充电宝", "移动电源", "数码", 1, "我家/书房/书桌1"),
    ("充电器", "充电头", "数码", 2, "我家/书房/书桌1"),
    ("洗手液", "", "日用", 1, "我家/卫生间/洗漱柜"),
    ("螺丝刀", "", "工具", 1, "我家/书房"),
    ("螺丝", "螺钉", "工具", 30, "我家/书房/工具箱"),
    ("卷尺", "", "工具", 1, "我家/书房"),
    ("电池", "五号电池", "日用", 8, "我家/书房/书桌1"),
    ("电池", "五号电池", "日用", 4, "我家/卫生间/洗漱柜"),   # 同名两处
    ("抽纸", "纸巾", "日用", 0, "我家/卫生间/洗漱柜"),        # 库存为 0
]

LOCATIONS = [
    "我家", "我家/书房", "我家/书房/书桌1", "我家/书房/工具箱",
    "我家/卫生间", "我家/卫生间/洗漱柜",
]


def seed(db):
    """建位置树 + 物品。返回 {名称或路径: 对象} 方便断言。"""
    by_path: dict[str, models.Location] = {}
    for path in LOCATIONS:
        parts = path.split("/")
        parent = by_path.get("/".join(parts[:-1])) if len(parts) > 1 else None
        loc = models.Location(
            name=parts[-1],
            kind="home" if len(parts) == 1 else ("room" if len(parts) == 2 else "box"),
            parent_id=parent.id if parent else None,
        )
        db.add(loc)
        db.flush()
        by_path[path] = loc
    items: list[models.Item] = []
    for name, aliases, cat, qty, path in ITEMS:
        it = models.Item(name=name, aliases=aliases, category=cat,
                         quantity=qty, location_id=by_path[path].id)
        db.add(it)
        db.flush()
        items.append(it)
    db.commit()
    return by_path, items


def item_by(items, name, loc_name=None):
    for it in items:
        if it.name == name and (loc_name is None or (it.location and it.location.name == loc_name)):
            return it
    raise AssertionError(f"fixture 里没有 {name} @ {loc_name}")
