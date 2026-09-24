"""竞赛报名与审核 API。

需报名模式（contest.registration_required = True）：
  1. 用户先报名（pending）；
  2. 管理员审批（approved / rejected）；
  3. 仅 approved 的用户可在该竞赛下提交代码（管理员不受限）。

存储（沿用按竞赛/用户分片的风格）：
  data/registrations/{contest_id}/{user_id}.json   每个用户一份报名分片
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.storage import read_json, atomic_write_json, locked_update, list_files
from backend.utils import now_iso

registrations_bp = Blueprint("registrations", __name__)

# 报名状态
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def _reg_dir(contest_id):
    return os.path.join(config.REGISTRATIONS_DIR, contest_id)


def _reg_path(contest_id, user_id):
    return os.path.join(_reg_dir(contest_id), f"{user_id}.json")


def _load_contest(contest_id):
    return read_json(os.path.join(config.CONTESTS_DIR, f"{contest_id}.json"))


def get_registration(contest_id, user_id):
    """读取某用户在某竞赛的报名记录，未报名返回 None。"""
    if not user_id:
        return None
    return read_json(_reg_path(contest_id, user_id))


def list_registrations(contest_id):
    """列出某竞赛的全部报名记录（按报名时间排序）。"""
    regs = []
    for uid in list_files(_reg_dir(contest_id)):
        r = read_json(_reg_path(contest_id, uid))
        if r:
            regs.append(r)
    regs.sort(key=lambda r: r.get("applied_at", ""))
    return regs


def can_participate(contest, user):
    """检查用户是否可参加（提交）该竞赛，返回 (是否允许, 提示信息)。"""
    if not contest.get("registration_required"):
        return True, ""
    if user and user.get("role") == "admin":
        return True, ""
    reg = get_registration(contest["id"], user["id"]) if user else None
    if reg is None:
        return False, "该竞赛需先报名并通过审核后才能提交"
    status = reg.get("status")
    if status == STATUS_APPROVED:
        return True, ""
    if status == STATUS_PENDING:
        return False, "您的报名正在审核中，通过后才能提交"
    return False, "您的报名未通过审核，无法参加该竞赛"


@registrations_bp.post("/contests/<contest_id>/register")
@require_auth
def register(contest_id):
    contest = _load_contest(contest_id)
    if not contest or not contest.get("visble", True):
        return err("竞赛不存在", 404)
    if not contest.get("registration_required"):
        return err("该竞赛无需报名，可直接参加", 400)

    user = request.user
    existing = get_registration(contest_id, user["id"])
    if existing and existing.get("status") == STATUS_PENDING:
        return err("您已报名，正在等待审核", 400)
    if existing and existing.get("status") == STATUS_APPROVED:
        return err("您已通过审核，无需重复报名", 400)

    data = request.get_json(silent=True) or {}

    def _upd(_old):
        return {
            "contest_id": contest_id,
            "user_id": user["id"],
            "username": user.get("username", ""),
            "nickname": user.get("nickname", ""),
            "note": str(data.get("note") or "")[:200],
            "status": STATUS_PENDING,
            "applied_at": now_iso(),
            "reviewed_at": None,
            "reviewed_by": None,
        }

    # 未报名或被拒绝后重新报名：重置为待审核
    reg = locked_update(_reg_path(contest_id, user["id"]), _upd, default=None)
    return ok(reg)


@registrations_bp.delete("/contests/<contest_id>/register")
@require_auth
def unregister(contest_id):
    path = _reg_path(contest_id, request.user["id"])
    reg = read_json(path)
    if not reg:
        return err("您尚未报名该竞赛", 404)
    if reg.get("status") != STATUS_PENDING:
        return err("当前状态不可取消报名", 400)
    os.remove(path)
    return ok()


@registrations_bp.get("/contests/<contest_id>/register")
@require_auth
def my_registration(contest_id):
    reg = get_registration(contest_id, request.user["id"])
    return ok({"status": reg.get("status") if reg else None, "registration": reg})


@registrations_bp.get("/contests/<contest_id>/registrations")
@require_admin
def admin_list(contest_id):
    if not _load_contest(contest_id):
        return err("竞赛不存在", 404)
    regs = list_registrations(contest_id)
    summary = {
        "total": len(regs),
        "pending": sum(1 for r in regs if r.get("status") == STATUS_PENDING),
        "approved": sum(1 for r in regs if r.get("status") == STATUS_APPROVED),
        "rejected": sum(1 for r in regs if r.get("status") == STATUS_REJECTED),
    }
    return ok({"items": regs, "summary": summary})


@registrations_bp.post("/contests/<contest_id>/registrations/<user_id>/review")
@require_admin
def review(contest_id, user_id):
    if not _load_contest(contest_id):
        return err("竞赛不存在", 404)
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("approve", "reject"):
        return err("action 必须为 approve 或 reject", 400)
    if not os.path.exists(_reg_path(contest_id, user_id)):
        return err("该用户未报名此竞赛", 404)

    def _upd(reg):
        if reg is None:
            return reg
        reg["status"] = STATUS_APPROVED if action == "approve" else STATUS_REJECTED
        reg["reviewed_at"] = now_iso()
        reg["reviewed_by"] = request.user.get("username", "")
        return reg

    reg = locked_update(_reg_path(contest_id, user_id), _upd, default=None)
    if reg is None:
        return err("该用户未报名此竞赛", 404)
    return ok(reg)
