"""RealSense ID face enrollment, authentication and the host-side roster (name + role)."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..face_roster import (FaceRoster, clean_name, clean_role, new_user_id, status_text)
from ..services import Services
from .deps import get_services, set_meta, timed_meta

router = APIRouter()
_LOCKS: dict[int, asyncio.Lock] = {}


def _face(services: Services):
    if services.face_auth is None:
        raise HTTPException(503, "realsense_id_unavailable")
    return services.face_auth


def _roster(services: Services) -> FaceRoster:
    if services.face_roster is None:
        services.face_roster = FaceRoster(services.settings.face_roster)
    return services.face_roster


def _lock(services: Services) -> asyncio.Lock:
    """One Face ID operation at a time: enrolment is several sensor sessions in a row."""
    return _LOCKS.setdefault(id(services), asyncio.Lock())


def _conflict(code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}, **extra}, 409)


def _who(roster: FaceRoster, user_id: str | None) -> dict:
    entry = roster.get(user_id) if user_id else None
    if entry:
        return {"name": entry["name"], "role": entry["role"]}
    return {"name": user_id, "role": "unassigned" if user_id else None}


def _hints(result: dict) -> list[dict]:
    out = []
    for h in result.get("hints") or []:
        out.append({**h, "text": status_text(h.get("status", ""))})
    return out


def _final_text(result: dict) -> str:
    status = result.get("status", "")
    if status != "Success":
        return status_text(status)
    return "Success"


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body


@router.get("/v1/face/users")
async def users(services: Annotated[Services, Depends(get_services)]):
    sensor = await _face(services).users()
    roster = _roster(services).all()
    out = []
    for uid in sorted(set(sensor) | set(roster)):
        e = roster.get(uid) or {}
        out.append({"user_id": uid, "name": e.get("name", uid),
                    "role": e.get("role", "unassigned"), "enrolled_at": e.get("enrolled_at"),
                    "on_sensor": uid in sensor})
    out.sort(key=lambda u: (u["role"] != "driver", u["name"].casefold()))
    return {"users": out, "method": "realsense_id"}


@router.post("/v1/face/enroll")
async def enroll(request: Request, services: Annotated[Services, Depends(get_services)]):
    body = await _json(request)
    try:
        name = clean_name(body.get("name"))
        role = clean_role(body.get("role"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    confirm = body.get("confirm") is True
    face = _face(services)
    roster = _roster(services)
    t0 = time.perf_counter()
    async with _lock(services):
        if roster.find(name, role):
            return _conflict("duplicate", f"Already enrolled as {name} ({role})")
        old_drivers = roster.drivers() if role == "driver" else []
        if old_drivers and not confirm:
            first = roster.get(old_drivers[0])
            return _conflict(
                "driver_exists",
                f"A driver ({first['name']}) is already enrolled - confirm to replace them",
                existing={"name": first["name"], "role": first["role"]})
        try:
            probe = await face.authenticate()
            if probe.get("authenticated") and probe.get("user_id"):
                who = _who(roster, probe["user_id"])
                return _conflict("duplicate", f"Already enrolled as {who['name']} ({who['role']})",
                                 existing=who)
            user_id = new_user_id(name)
            result = await face.enroll(user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if result.get("status") == "DuplicateFaceprints":
            return _conflict("duplicate", "Already enrolled - this face is already in the camera")
        replaced = []
        if result.get("enrolled"):
            roster.add(user_id, name, role)
            for old in old_drivers:
                try:
                    await face.remove_user(old)
                except (RuntimeError, ValueError):
                    continue
                roster.remove(old)
                replaced.append(old)
    result = {**result, "user_id": user_id if result.get("enrolled") else None, "name": name,
              "role": role, "replaced": replaced, "message": (
                  f"Enrolled {name} ({role})" if result.get("enrolled") else _final_text(result)),
              "hints": _hints(result)}
    meta = set_meta(request, timed_meta("realsense-id", "f455", t0,
                                        float(result.get("latency_ms", 0.0))))
    return {**result, "meta": meta}


@router.post("/v1/face/authenticate")
async def authenticate(request: Request, services: Annotated[Services, Depends(get_services)]):
    t0 = time.perf_counter()
    face = _face(services)
    async with _lock(services):
        try:
            result = await face.authenticate()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    who = _who(_roster(services), result.get("user_id"))
    if result.get("authenticated"):
        message = f"Authenticated: {who['name']} · {str(who['role']).capitalize()}"
    else:
        message = _final_text(result)
    meta = set_meta(request, timed_meta("realsense-id", "f455", t0,
                                        float(result.get("latency_ms", 0.0))))
    return {**result, **who, "message": message, "hints": _hints(result), "meta": meta}


@router.delete("/v1/face/users/{user_id}")
async def remove_user(user_id: str, services: Annotated[Services, Depends(get_services)]):
    face = _face(services)
    roster = _roster(services)
    async with _lock(services):
        try:
            on_sensor = user_id in await face.users()
            if not on_sensor and roster.get(user_id) is None:
                raise HTTPException(404, f"unknown user '{user_id}'")
            result = await face.remove_user(user_id) if on_sensor else {
                "deleted": True, "user_id": user_id, "method": "realsense_id"}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if result.get("deleted"):
            roster.remove(user_id)
    return result


@router.delete("/v1/face/users")
async def remove_all(services: Annotated[Services, Depends(get_services)]):
    face = _face(services)
    roster = _roster(services)
    async with _lock(services):
        try:
            ids = await face.users()
            failed = []
            for uid in ids:
                if not (await face.remove_user(uid)).get("deleted"):
                    failed.append(uid)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not failed:
            roster.clear()
        else:
            for uid in set(ids) - set(failed):
                roster.remove(uid)
    return {"deleted": not failed, "count": len(ids) - len(failed), "failed": failed,
            "method": "realsense_id"}
