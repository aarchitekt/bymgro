from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import storage

APP_VERSION = "1.7"

storage.init_db()

app = FastAPI(title="bymgro")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def uid(x_user_id: Optional[str]) -> str:
    if not x_user_id:
        raise HTTPException(400, "Missing X-User-Id header")
    storage.get_or_create_user(x_user_id)
    return x_user_id


# ---------- schemas ----------

class InitIn(BaseModel):
    display_name: Optional[str] = None


class ProfileIn(BaseModel):
    name: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    goal: Optional[str] = None


class PlanExerciseIn(BaseModel):
    name: str
    muscle: Optional[str] = None
    kind: str = "weight"
    target_sets: int = 3
    unit: str = "kg"
    last_weight: Optional[float] = None
    last_reps: Optional[float] = None


class PlanIn(BaseModel):
    push: List[PlanExerciseIn]
    pull: List[PlanExerciseIn]


class SetIn(BaseModel):
    exercise_name: str
    set_index: int
    weight: Optional[float] = None
    reps: Optional[float] = None
    value_text: Optional[str] = None
    logged: bool = True


class StartSessionIn(BaseModel):
    day_type: Optional[str] = None


class FinishSessionIn(BaseModel):
    duration_min: Optional[float] = None
    bodyweight_kg: Optional[float] = None


class NutritionIn(BaseModel):
    date: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None


class SupplementIn(BaseModel):
    name: str


class SupplementToggleIn(BaseModel):
    date: Optional[str] = None
    taken: bool = True


class HabitIn(BaseModel):
    date: Optional[str] = None
    alcohol: bool = False
    smoke: bool = False
    drugs: bool = False


class AddFriendIn(BaseModel):
    code: str


class LoginIn(BaseModel):
    username: str
    password: str


def today() -> str:
    return datetime.date.today().isoformat()


# ---------- API: identity ----------

@app.get("/api/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.post("/api/auth/init")
def api_init(body: InitIn, x_user_id: str = Header(..., alias="X-User-Id")):
    user = storage.get_or_create_user(x_user_id, body.display_name)
    return user


# Update 1.6: real username/password login, layered on top of the existing
# anonymous X-User-Id header scheme rather than replacing it -- this just
# resolves username+password to a user_id, which the frontend then stores
# in localStorage exactly where the old auto-generated UUID used to live.
# Unknown username = auto-register (new user + credential), same
# low-friction "just works" pattern the anonymous flow already had, except
# now the identity is recoverable across a storage reset as long as you
# remember the username/password.
@app.post("/api/auth/login")
def api_login(body: LoginIn):
    username = body.username.strip()
    password = body.password
    if not username or not password:
        raise HTTPException(400, "Nutzername und Passwort erforderlich")
    existing_uid = storage.verify_login(username, password)
    if existing_uid:
        user = storage.get_or_create_user(existing_uid)
        return {"user_id": existing_uid, "display_name": user.get("display_name")}
    if storage.credential_exists(username):
        raise HTTPException(401, "Falsches Passwort")
    new_uid = str(uuid.uuid4())
    user = storage.get_or_create_user(new_uid)
    storage.create_credential(new_uid, username, password)
    return {"user_id": new_uid, "display_name": user.get("display_name")}


# ---------- API: plan / profile ----------

@app.get("/api/plan")
def api_get_plan(X_User_Id: Optional[str] = Header(None)):
    return storage.get_plan(uid(X_User_Id))


@app.put("/api/plan")
def api_save_plan(plan: PlanIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    return storage.save_plan(u, {"push": [e.dict() for e in plan.push], "pull": [e.dict() for e in plan.pull]})


@app.get("/api/profile")
def api_get_profile(X_User_Id: Optional[str] = Header(None)):
    return storage.get_profile(uid(X_User_Id))


@app.put("/api/profile")
def api_update_profile(profile: ProfileIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    fields = {k: v for k, v in profile.dict().items() if v is not None}
    return storage.update_profile(u, fields)


# ---------- API: workout ----------

@app.get("/api/workout/next")
def api_next_workout(X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    day_type = storage.next_day_type(u)
    plan = storage.get_plan(u)
    return {"day_type": day_type, "exercises": plan.get(day_type, [])}


@app.post("/api/workout/start")
def api_start_workout(body: StartSessionIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    day_type = body.day_type or storage.next_day_type(u)
    if day_type not in ("push", "pull"):
        raise HTTPException(400, "day_type must be 'push' or 'pull'")
    session_id = storage.create_session(u, day_type, today())
    plan = storage.get_plan(u)
    return {"session_id": session_id, "day_type": day_type, "date": today(), "exercises": plan.get(day_type, [])}


@app.post("/api/workout/{session_id}/set")
def api_log_set(session_id: int, body: SetIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    storage.log_set(u, session_id, body.exercise_name, body.set_index, body.weight, body.reps, body.value_text, body.logged)
    return {"ok": True}


@app.post("/api/workout/{session_id}/finish")
def api_finish_workout(session_id: int, body: FinishSessionIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    storage.finish_session(u, session_id, body.duration_min, body.bodyweight_kg)
    gam = storage.gamification_status(u)
    sess = storage.get_session(u, session_id)
    sess["gamification"] = gam
    return sess


@app.get("/api/workout/{session_id}")
def api_get_session(session_id: int, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    sess = storage.get_session(u, session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess


@app.get("/api/history")
def api_history(limit: int = 60, X_User_Id: Optional[str] = Header(None)):
    return storage.get_history(uid(X_User_Id), limit)


@app.get("/api/progress")
def api_progress(X_User_Id: Optional[str] = Header(None)):
    return storage.get_progress(uid(X_User_Id))


# ---------- API: nutrition ----------

@app.get("/api/nutrition")
def api_nutrition_get(date: Optional[str] = None, X_User_Id: Optional[str] = Header(None)):
    return storage.get_nutrition_day(uid(X_User_Id), date or today())


@app.put("/api/nutrition")
def api_nutrition_put(body: NutritionIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    return storage.save_nutrition_day(u, body.date or today(), body.calories, body.protein_g)


@app.get("/api/nutrition/history")
def api_nutrition_history(limit: int = 30, X_User_Id: Optional[str] = Header(None)):
    return storage.get_nutrition_history(uid(X_User_Id), limit)


@app.post("/api/supplements")
def api_add_supplement(body: SupplementIn, X_User_Id: Optional[str] = Header(None)):
    return storage.add_supplement(uid(X_User_Id), body.name)


@app.delete("/api/supplements/{supplement_id}")
def api_delete_supplement(supplement_id: int, X_User_Id: Optional[str] = Header(None)):
    storage.delete_supplement(uid(X_User_Id), supplement_id)
    return {"ok": True}


@app.post("/api/supplements/{supplement_id}/toggle")
def api_toggle_supplement(supplement_id: int, body: SupplementToggleIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    storage.toggle_supplement(u, supplement_id, body.date or today(), body.taken)
    return {"ok": True}


# ---------- API: habits ----------

@app.get("/api/habits")
def api_habits_get(date: Optional[str] = None, X_User_Id: Optional[str] = Header(None)):
    return storage.get_habit_day(uid(X_User_Id), date or today())


@app.put("/api/habits")
def api_habits_put(body: HabitIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    return storage.save_habit_day(u, body.date or today(), body.alcohol, body.smoke, body.drugs)


@app.get("/api/habits/history")
def api_habits_history(limit_days: int = 90, X_User_Id: Optional[str] = Header(None)):
    return storage.get_habit_history(uid(X_User_Id), limit_days)


@app.get("/api/habits/streaks")
def api_habits_streaks(X_User_Id: Optional[str] = Header(None)):
    return storage.clean_streaks(uid(X_User_Id))


# ---------- API: gamification ----------

@app.get("/api/gamification")
def api_gamification(X_User_Id: Optional[str] = Header(None)):
    return storage.gamification_status(uid(X_User_Id))


# ---------- API: social ----------

@app.get("/api/social/friends")
def api_friends(X_User_Id: Optional[str] = Header(None)):
    return storage.list_friends(uid(X_User_Id))


@app.post("/api/social/friends")
def api_add_friend(body: AddFriendIn, X_User_Id: Optional[str] = Header(None)):
    u = uid(X_User_Id)
    try:
        friend = storage.add_friend(u, body.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return friend


@app.delete("/api/social/friends/{friend_user_id}")
def api_remove_friend(friend_user_id: str, X_User_Id: Optional[str] = Header(None)):
    storage.remove_friend(uid(X_User_Id), friend_user_id)
    return {"ok": True}


# ---------- static frontend ----------

if (FRONTEND_DIR / "vendor").exists():
    app.mount("/vendor", StaticFiles(directory=str(FRONTEND_DIR / "vendor")), name="vendor")


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(FRONTEND_DIR / "manifest.json"))


@app.get("/sw.js")
def sw():
    return FileResponse(str(FRONTEND_DIR / "sw.js"), media_type="application/javascript")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
