from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CourseCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    term_label: str = Field(min_length=1, max_length=80)
    starts_at: str = Field(min_length=10, max_length=40, description="ISO 8601，例如 2026-09-01T08:00:00+00:00")
    ends_at: str = Field(min_length=10, max_length=40)
    total_capacity: int = Field(ge=0, le=1000000)
    reservation_seconds: int = Field(default=86400, ge=60, le=31536000)


class ClassCreate(BaseModel):
    course_code: str | None = Field(default=None, min_length=2, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    starts_at: str = Field(min_length=10, max_length=40)
    ends_at: str = Field(min_length=10, max_length=40)
    capacity: int = Field(ge=0, le=100000)


class CapacityAdjust(BaseModel):
    course_capacity: int | None = Field(default=None, ge=0, le=1000000)
    class_capacity: int | None = Field(default=None, ge=0, le=100000)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class ReservationRequest(BaseModel):
    course_code: str = Field(min_length=2, max_length=64)
    class_code: str = Field(min_length=1, max_length=64)
    student_key: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_key: str = Field(min_length=6, max_length=160)


class ConfirmRequest(BaseModel):
    enrollment_id: int = Field(ge=1)
    student_key: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=6, max_length=160)


class CancelRequest(BaseModel):
    enrollment_id: int = Field(ge=1)
    student_key: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)


class TransferRequest(BaseModel):
    enrollment_id: int = Field(ge=1)
    student_key: str = Field(min_length=1, max_length=80)
    target_course_code: str = Field(min_length=2, max_length=64)
    target_class_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)


class ExpireRequest(BaseModel):
    actor: str = Field(default="expiry-worker", min_length=1, max_length=120)


EventType = Literal["reserve", "confirm", "release", "promote", "promote_skip", "transfer", "capacity_adjust", "expire_scan"]
