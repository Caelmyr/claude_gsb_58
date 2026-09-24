"""竞赛管理 API（创建、倒计时、封榜配置、报名审核）。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin, get_current_user
from backend.storage import read_json, atomic_write_json, locked_update, list_files
from backend.utils import now_iso, gen_id, frozen_now
from backend.judge.ranking import contest_status, contest_elapsed, reset_contest_scores

contests_bp = Blueprint("contests", __name__)


def _load(contest_id):
    return read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))


def _path(contest_id):
    return os.path.join(config.CONTESTS_DIR, f"{contest_id}.json")


def find_registration(contest, user_id):
    """在竞赛报名记录中查找指定用户，未报名返回 None。"""
    if not user_id:
        return None
    for r in contest.get("registrations", []):
        if r.get("user_id") == user_id:
            return r
    return None


def can_enter_contest(contest, user):
    """需报名竞赛：仅审核通过的用户（或管理员）可进入提交代码。"""
    if not contest.get("require_approval"):
        return True
    if user and user.get("role") == "admin":
        return True
    if not user:
        return False
    reg = find_registration(contest, user["id"])
    return bool(reg and reg.get("status") == "approved")


def _decorate(c, user=None):
    if not c:
        return None
    out = dict(c)
    out["status"] = contest_status(c)
    out["elapsed"] = contest_elapsed(c)
    out["frozen_now"] = frozen_now(c)
    out["require_approval"] = bool(c.get("require_approval"))
    # 报名明细不随竞赛信息下发：普通用户只能看到自己的报名状态，
    # 管理员通过专用接口获取完整列表，这里仅给统计数字。
    out.pop("registrations", None)
    if user and user.get("role") == "admin":
        regs = c.get("registrations", [])
        out["registration_stats"] = {
            "total": len(regs),
            "pending": sum(1 for r in regs if r.get("status") == "pending"),
            "approved": sum(1 for r in regs if r.get("status") == "approved"),
        }
    out["my_registration"] = find_registration(c, user["id"]) if user else None
    out["can_submit"] = can_enter_contest(c, user)
    return out


def list_all(user=None):
    contests = []
    for cid in list_files(config.CONTESTS_DIR):
        c = _load(cid)
        if c:
            contests.append(_decorate(c, user))
    contests.sort(key=lambda c: c.get("start_time", ""))
    return contests


@contests_bp.get("/contests")
def get_contests():
    current = get_current_user()
    is_admin = current and current.get("role") == "admin"
    contests = list_all(current)
    if not is_admin:
        contests = [c for c in contests if c.get("visble", True)]
    return ok({"total": len(contests), "items": contests})


@contests_bp.get("/contests/<contest_id>")
def get_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    user = get_current_user()
    if not c.get("visble", True) and (user is None or user.get("role") != "admin"):
        return err("竞赛不存在", 404)
    return ok(_decorate(c, user))


@contests_bp.post("/contests")
@require_admin
def create_contest():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return err("竞赛标题不能为空", 400)
    contest_id = data.get("id") or gen_id("c")
    c = {
        "id": contest_id,
        "title": title,
        "description": data.get("description", ""),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "freeze_time": data.get("freeze_time"),
        "freeze_enabled": bool(data.get("freeze_enabled", False)),
        "mode": data.get("mode", "acm"),
        "problems": data.get("problems", []),
        "visible": data.get("visible", True),
        "require_approval": bool(data.get("require_approval", False)),
        "registrations": [],
        "created_at": now_iso(),
    }
    atomic_write_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"), c)
    return ok(_decorate(c, request.user))


@contests_bp.put("/contests/<contest_id>")
@require_admin
def update_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    data = request.get_json(silent=True) or {}
    for key in ("title", "description", "start_time", "end_time", "freeze_time",
                "mode", "problems"):
        if key in data:
            c[key] = data[key]
    if "freeze_enabled" in data:
        c["freeze_enabled"] = bool(data["freeze_enabled"])
    if "visible" in data:
        c["visible"] = bool(data["visible"])
    if "require_approval" in data:
        c["require_approval"] = bool(data["require_approval"])
    if "title" in data and not (data["title"] or "").strip():
        return err("竞赛标题不能为空", 400)
    atomic_write_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"), c)
    return ok(_decorate(c, request.user))


@contests_bp.delete("/contests/<contest_id>")
@require_admin
def delete_contest(contest_id):
    p = os.path.join(config.CONTESTS_DIR, f"{contest_id}.json")
    if not os.path.exists(p):
        return err("竞赛不存在", 404)
    os.remove(p)
    reset_contest_scores(contest_id)
    return ok()


@contests_bp.post("/contests/<contest_id>/reset-scores")
@require_admin
def reset_scores(contest_id):
    reset_contest_scores(contest_id)
    return ok()


# ---- 报名与审核 ----
@contests_bp.post("/contests/<contest_id>/register")
@require_auth
def register_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    if not c.get("visble", True) and request.user.get("role") != "admin":
        return err("竞赛不存在", 404)
    if not c.get("require_approval"):
        return err("该竞赛无需报名，可直接参加", 400)
    if contest_status(c) == "ended":
        return err("竞赛已结束，报名已截止", 400)

    user = request.user
    result = {}

    def _upd(data):
        regs = data.setdefault("registrations", [])
        existing = next((r for r in regs if r.get("user_id") == user["id"]), None)
        if existing:
            result["reg"] = existing
            result["created"] = False
            if existing.get("status") == "rejected":
                # 被拒绝后允许重新报名，重新进入待审核
                existing["status"] = "pending"
                existing["note"] = ""
                existing["registered_at"] = now_iso()
                existing["reviewed_at"] = None
                existing["reviewed_by"] = None
                result["created"] = True
            return data
        reg = {
            "user_id": user["id"],
            "username": user.get("username", ""),
            "nickname": user.get("nickname") or user.get("username", ""),
            "status": "pending",
            "note": "",
            "registered_at": now_iso(),
            "reviewed_at": None,
            "reviewed_by": None,
        }
        regs.append(reg)
        result["reg"] = reg
        result["created"] = True
        return data

    locked_update(_path(contest_id), _upd, default=dict(c))
    reg = result["reg"]
    if not result["created"]:
        msg = {"pending": "你已报名，正在等待管理员审核",
               "approved": "你的报名已通过审核"}.get(reg.get("status"), "你已报名该竞赛")
        return ok(reg, message=msg)
    return ok(reg, message="报名成功，请等待管理员审核")


@contests_bp.post("/contests/<contest_id>/unregister")
@require_auth
def unregister_contest(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    removed = {}

    def _upd(data):
        regs = data.get("registrations", [])
        kept = [r for r in regs if r.get("user_id") != request.user["id"]]
        removed["yes"] = len(kept) != len(regs)
        data["registrations"] = kept
        return data

    locked_update(_path(contest_id), _upd, default=dict(c))
    if not removed.get("yes"):
        return err("你尚未报名该竞赛", 400)
    return ok(message="已取消报名")


@contests_bp.get("/contests/<contest_id>/registrations")
@require_admin
def list_registrations(contest_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    regs = list(c.get("registrations", []))
    status = request.args.get("status")
    if status:
        regs = [r for r in regs if r.get("status") == status]
    regs.sort(key=lambda r: r.get("registered_at", ""))
    return ok({"total": len(regs), "items": regs})


@contests_bp.put("/contests/<contest_id>/registrations/<user_id>")
@require_admin
def review_registration(contest_id, user_id):
    c = _load(contest_id)
    if not c:
        return err("竞赛不存在", 404)
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("approved", "rejected", "pending"):
        return err("无效的审核状态", 400)
    note = (data.get("note") or "").strip()
    found = {}

    def _upd(d):
        for r in d.get("registrations", []):
            if r.get("user_id") == user_id:
                r["status"] = status
                r["note"] = note
                r["reviewed_at"] = now_iso()
                r["reviewed_by"] = request.user.get("username", "")
                found["reg"] = r
                break
        return d

    locked_update(_path(contest_id), _upd, default=dict(c))
    if "reg" not in found:
        return err("该用户未报名此竞赛", 404)
    return ok(found["reg"])
