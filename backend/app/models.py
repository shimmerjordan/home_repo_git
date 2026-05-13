import uuid as _uuid
from datetime import datetime
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base


class Location(Base):
    """A storage location: room or container. Hierarchical via parent_id."""
    __tablename__ = "locations"

    id = Column(Integer, primary_key=True)
    uuid = Column(String(36), unique=True, index=True, nullable=False,
                  default=lambda: str(_uuid.uuid4()))
    name = Column(String(120), nullable=False, index=True)
    kind = Column(String(40), nullable=False, default="room")  # room / box / shelf / drawer
    parent_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    note = Column(Text, default="")
    # 3D placement, JSON: {x,y,z,w,h,d,rot,color,levels,level,slot}; positions relative to parent.
    geometry = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    parent = relationship("Location", remote_side=[id], backref="children")
    items = relationship("Item", back_populates="location")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, index=True)
    aliases = Column(Text, default="")  # comma-separated alternative names
    category = Column(String(80), default="", index=True)
    tags = Column(Text, default="")
    quantity = Column(Integer, default=1)
    price = Column(Float, default=0.0)
    note = Column(Text, default="")
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    # Optional explicit position inside parent location (metres, relative to parent center).
    # If both nulls, Scene3D auto-grids the item.
    pos_x = Column(Float, nullable=True)
    pos_z = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    location = relationship("Location", back_populates="items")
    transactions = relationship("Transaction", back_populates="item", cascade="all, delete-orphan")


class AuditLog(Base):
    """Append-only audit trail of every mutation we make to locations / items /
    items' transactional state. Pairs with /api/audit so the user can scrub back
    through "who changed what when", git-blame style."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.now, index=True)
    entity_type = Column(String(40), index=True)     # 'location' / 'item' / 'transaction'
    entity_id = Column(Integer, index=True, nullable=True)
    entity_name = Column(String(200), default="")
    action = Column(String(40), index=True)          # 'create' / 'update' / 'delete' / 'restore'
    changes = Column(Text, default="")               # JSON: { field: [oldVal, newVal] }
    summary = Column(Text, default="")               # one-line human description
    source = Column(String(40), default="ui")        # 'ui' / 'voice' / 'import' ...


class Transaction(Base):
    """Take-out / put-in records."""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    action = Column(String(20), nullable=False)  # take_out / put_in / adjust
    quantity = Column(Integer, default=1)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now, index=True)

    item = relationship("Item", back_populates="transactions")
    location = relationship("Location")
