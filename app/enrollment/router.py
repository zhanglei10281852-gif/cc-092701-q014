from __future__ import annotations

from fastapi import APIRouter, Query

from app.enrollment.schemas import (
    CancelRequest,
    CapacityAdjust,
    ClassCreate,
    ConfirmRequest,
    CourseCreate,
    ExpireRequest,
    ReservationRequest,
    TransferRequest,
)
from app.enrollment.service import PRIORITY_RULE, EnrollmentService

router = APIRouter(prefix="/api/enrollment", tags=["实训报名预留与候补"])


def service() -> EnrollmentService:
    return EnrollmentService()


@router.get("/meta/rules")
def promotion_rules() -> dict:
    """说明一次递补使用的优先规则与释放来源取值。"""
    return {
        "waitlist_priority_rule": PRIORITY_RULE,
        "release_order": {
            "cancel": "退课立即释放，随后递补该班候补",
            "expire": "过期扫描按 reserved_expires_at、enrollment id 升序依次释放，随后统一递补",
            "transfer": "转班先按 transfer 释放原班级名额，再占用目标班级名额，并递补原班候补",
            "capacity_increase": "班级或课程扩容不释放占用，但会重新评估候补队列",
        },
        "version_boundary": "每个 release/promote 事件记录课程与班级的 version_before/version_after，版本边界以批次事件流为准",
    }


@router.post("/courses", status_code=201)
def create_course(payload: CourseCreate, actor: str = Query(..., min_length=1)):
    return service().create_course(payload.model_dump(), actor)


@router.get("/courses")
def list_courses():
    return {"items": service().list_courses()}


@router.get("/courses/{course_code}")
def course_detail(course_code: str):
    return service().course_detail(course_code)


@router.post("/courses/{course_code}/classes", status_code=201)
def create_class(course_code: str, payload: ClassCreate, actor: str = Query(..., min_length=1)):
    data = payload.model_dump()
    data["course_code"] = course_code
    return service().create_class(data, actor)


@router.put("/courses/{course_code}/capacity")
def adjust_capacity(
    course_code: str,
    payload: CapacityAdjust,
    class_code: str | None = Query(default=None, min_length=1, max_length=64),
):
    return service().adjust_capacity(course_code, class_code, payload.model_dump())


@router.post("/reservations", status_code=202)
def reserve(payload: ReservationRequest):
    return service().reserve(payload.model_dump())


@router.post("/confirmations", status_code=200)
def confirm(payload: ConfirmRequest):
    return service().confirm(payload.model_dump())


@router.post("/cancellations", status_code=200)
def cancel(payload: CancelRequest):
    return service().cancel(payload.model_dump())


@router.post("/transfers", status_code=202)
def transfer(payload: TransferRequest):
    return service().transfer(payload.model_dump())


@router.post("/expire-unconfirmed", status_code=200)
def expire_unconfirmed(payload: ExpireRequest):
    return service().expire_unconfirmed(payload.actor)


@router.get("/enrollments")
def list_enrollments(
    class_id: int | None = None,
    student_key: str | None = None,
    status: str | None = Query(default=None, pattern="^(reserved|confirmed|waitlisted|cancelled|expired|transferred)$"),
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_enrollments(class_id=class_id, student_key=student_key, status=status, limit=limit)}


@router.get("/enrollments/{enrollment_id}/trace")
def enrollment_trace(enrollment_id: int):
    return service().enrollment_trace(enrollment_id)


@router.get("/courses/{course_code}/classes/{class_code}/waitlist")
def class_waitlist(course_code: str, class_code: str):
    return service().waitlist(course_code, class_code)


@router.get("/events/batches")
def list_batches(limit: int = Query(default=50, ge=1, le=500)):
    return service().list_batches(limit)


@router.get("/events/batches/{batch_key}")
def batch_trace(batch_key: str):
    """管理追溯：一次批次中每次递补的优先规则、释放来源与版本边界。"""
    return service().batch_trace(batch_key)
