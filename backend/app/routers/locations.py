import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services.inventory import serialize_location

router = APIRouter(prefix="/api/locations", tags=["locations"])


def _payload_to_db(data: dict) -> dict:
    if "geometry" in data and data["geometry"] is not None:
        if isinstance(data["geometry"], dict):
            data["geometry"] = json.dumps(data["geometry"], ensure_ascii=False)
    return data


@router.get("", response_model=list[schemas.LocationOut])
def list_locations(db: Session = Depends(get_db)):
    rows = db.query(models.Location).order_by(models.Location.id).all()
    return [serialize_location(r) for r in rows]


@router.post("", response_model=schemas.LocationOut)
def create_location(payload: schemas.LocationCreate, db: Session = Depends(get_db)):
    data = _payload_to_db(payload.model_dump())
    loc = models.Location(**data)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return serialize_location(loc)


@router.patch("/{loc_id}", response_model=schemas.LocationOut)
def update_location(loc_id: int, payload: schemas.LocationUpdate, db: Session = Depends(get_db)):
    loc = db.get(models.Location, loc_id)
    if not loc:
        raise HTTPException(404, "location not found")
    data = _payload_to_db(payload.model_dump(exclude_unset=True))
    for k, v in data.items():
        setattr(loc, k, v)
    db.commit()
    db.refresh(loc)
    return serialize_location(loc)


@router.delete("/{loc_id}")
def delete_location(loc_id: int, db: Session = Depends(get_db)):
    loc = db.get(models.Location, loc_id)
    if not loc:
        raise HTTPException(404, "location not found")
    for it in list(loc.items):
        it.location_id = None
    for child in list(loc.children):
        child.parent_id = loc.parent_id
    db.delete(loc)
    db.commit()
    return {"ok": True}
