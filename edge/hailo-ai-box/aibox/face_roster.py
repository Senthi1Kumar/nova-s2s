"""Face ID roster: a small id -> {name, role, enrolled_at} map kept on the host.

The F455 owns templates, matching and anti-spoofing. The host only remembers who a sensor user id
belongs to (a display name and a role); no image, embedding or faceprint is ever stored here.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time

ROLES = ("driver", "passenger")
_NAME_MAX = 24

_MESSAGES = {
    "Success": "Success",
    "NoFaceDetected": "No face found - look straight at the camera",
    "NoPersonDetected": "No person found - step in front of the camera",
    "PersonNotFound": "No person found - step in front of the camera",
    "FaceDetected": "Face found - hold still",
    "PersonDetected": "Person found - look at the camera",
    "FaceIsTooFarToTheTop": "Move down a little",
    "FaceIsTooFarToTheBottom": "Move up a little",
    "FaceIsTooFarToTheRight": "Move to your left",
    "FaceIsTooFarToTheLeft": "Move to your right",
    "FaceTiltIsTooUp": "Lower your chin a little",
    "FaceTiltIsTooDown": "Raise your chin a little",
    "FaceTiltIsTooRight": "Straighten your head (tilted right)",
    "FaceTiltIsTooLeft": "Straighten your head (tilted left)",
    "FaceIsNotFrontal": "Look straight at the camera",
    "MultipleFacesDetected": "More than one face - only one person in view",
    "FaceTooClose": "Too close - move back a little",
    "FaceTooFar": "Too far - move closer",
    "Sunglasses": "Remove sunglasses",
    "MedicalMask": "Remove the face mask",
    "Spoof": "Spoof suspected - remove glasses / avoid backlight",
    "Spoof_2D": "Spoof suspected (flat image) - show your real face",
    "Spoof_3D": "Spoof suspected (3D mask) - show your real face",
    "Spoof_LR": "Spoof suspected - remove glasses / avoid backlight",
    "Spoof_Surface": "Spoof suspected - remove glasses / avoid backlight",
    "Spoof_Disparity": "Spoof suspected - remove glasses / avoid backlight",
    "Spoof_Vision": "Spoof suspected - remove glasses / avoid backlight",
    "Spoof_2D_Right": "Spoof suspected (flat image) - show your real face",
    "Spoof_Plane_Disparity": "Spoof suspected (flat image) - show your real face",
    "TooManySpoofs": "Too many spoof attempts - wait and try again",
    "Forbidden": "Face not recognised - not enrolled",
    "InvalidFeatures": "Face not clear - try again in better light",
    "DuplicateFaceprints": "This face is already enrolled",
    "DuplicateUserId": "That user id already exists",
    "DatabaseFull": "Face database is full - delete a user first",
    "Failure": "Face ID failed - try again",
    "Error": "Face ID failed - try again",
    "DeviceError": "Camera error - check the F455 connection",
    "SerialError": "Camera connection error - check the F455 cable",
    "SecurityError": "Camera security error - the F455 is not paired",
    "CameraStarted": "Camera ready",
    "CameraStopped": "Camera stopped",
    "LedFlowSuccess": "Success",
}


def status_text(status: str) -> str:
    """Readable text for an SDK EnrollStatus / AuthenticateStatus name."""
    status = (status or "").strip()
    if status in _MESSAGES:
        return _MESSAGES[status]
    if not status:
        return "Face ID failed - try again"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", status).replace("_", " ")


def clean_name(value) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > _NAME_MAX or not all(ch.isprintable() for ch in name):
        raise ValueError(f"name must be 1-{_NAME_MAX} printable characters")
    return name


def clean_role(value) -> str:
    role = str(value or "").strip().lower()
    if role not in ROLES:
        raise ValueError("role must be 'driver' or 'passenger'")
    return role


def new_user_id(name: str) -> str:
    """A sensor user id (letter first, [A-Za-z0-9_-], max 30) derived from the display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:16].strip("-") or "user"
    if not slug[0].isalpha():
        slug = "u" + slug
    return f"{slug}_{secrets.token_hex(3)}"


class FaceRoster:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        users = data.get("users") if isinstance(data, dict) else None
        return {k: v for k, v in (users or {}).items() if isinstance(v, dict)}

    def _save(self, users: dict[str, dict]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"users": users}, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._load()

    def get(self, user_id: str) -> dict | None:
        with self._lock:
            entry = self._load().get(user_id)
        return {"user_id": user_id, **entry} if entry else None

    def find(self, name: str, role: str) -> str | None:
        key = name.casefold()
        with self._lock:
            for uid, e in self._load().items():
                if e.get("role") == role and str(e.get("name", "")).casefold() == key:
                    return uid
        return None

    def drivers(self) -> list[str]:
        with self._lock:
            return [uid for uid, e in self._load().items() if e.get("role") == "driver"]

    def add(self, user_id: str, name: str, role: str) -> dict:
        entry = {"name": name, "role": role, "enrolled_at": int(time.time())}
        with self._lock:
            users = self._load()
            users[user_id] = entry
            self._save(users)
        return {"user_id": user_id, **entry}

    def remove(self, user_id: str) -> bool:
        with self._lock:
            users = self._load()
            if user_id not in users:
                return False
            del users[user_id]
            self._save(users)
            return True

    def clear(self) -> None:
        with self._lock:
            self._save({})
