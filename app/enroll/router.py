from __future__ import annotations

from fastapi import APIRouter, Query

from app.enroll.schemas import CapacityAdjust, ClassCreate, ConfirmRequest, CourseCreate, DropRequest, QuotaAdjust, ReservationApply, TransferRequest
from app.enroll.service import EnrollmentService

router = APIRouter(prefix="/api/enroll", tags=["实训报名预留"])


def service() -> EnrollmentService:
    return EnrollmentService()


@router.post("/courses", status_code=201)
def create_course(payload: CourseCreate, actor: str = Query(..., min_length=1)):
    return service().create_course(payload.model_dump(), actor)


@router.get("/courses")
def list_courses():
    return {"items": service().list_courses()}


@router.put("/courses/{course_id}/capacity")
def adjust_course_capacity(course_id: int, payload: CapacityAdjust, actor: str = Query(..., min_length=1)):
    return service().adjust_course_capacity(course_id, payload.model_dump(), actor)


@router.post("/classes", status_code=201)
def create_class(payload: ClassCreate, actor: str = Query(..., min_length=1)):
    return service().create_class(payload.model_dump(), actor)


@router.get("/classes")
def list_classes(course_code: str | None = None):
    return {"items": service().list_classes(course_code)}


@router.get("/classes/{class_id}")
def get_class(class_id: int):
    return service().get_class(class_id)


@router.put("/classes/{class_id}/quota")
def adjust_class_quota(class_id: int, payload: QuotaAdjust, actor: str = Query(..., min_length=1)):
    return service().adjust_class_quota(class_id, payload.model_dump(), actor)


@router.get("/classes/{class_id}/promotions")
def list_class_promotions(class_id: int):
    return {"items": service().class_promotions(class_id)}


@router.post("/reservations", status_code=201)
def apply(payload: ReservationApply):
    return service().apply(payload.model_dump())


@router.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: int):
    return service().get_reservation(reservation_id)


@router.post("/reservations/{reservation_id}/confirm")
def confirm(reservation_id: int, payload: ConfirmRequest):
    return service().confirm(reservation_id, payload.actor)


@router.post("/reservations/{reservation_id}/drop")
def drop(reservation_id: int, payload: DropRequest):
    return service().drop(reservation_id, payload.actor, payload.reason)


@router.post("/reservations/{reservation_id}/transfer")
def transfer(reservation_id: int, payload: TransferRequest):
    return service().transfer(reservation_id, payload.model_dump())


@router.get("/students/{student_id}/reservations")
def student_reservations(student_id: str):
    return {"items": service().student_reservations(student_id)}


@router.get("/promotions/{promotion_id}")
def get_promotion(promotion_id: int):
    return service().get_promotion(promotion_id)


@router.post("/maintenance/expire-pending")
def expire_pending(actor: str = Query(default="enrollment-janitor", min_length=1)):
    return service().expire_pending(actor)


@router.get("/summary")
def summary():
    return service().summary()
