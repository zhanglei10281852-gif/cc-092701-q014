from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class CourseCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    total_capacity: int = Field(ge=0, le=100000)


class CapacityAdjust(BaseModel):
    capacity: int = Field(ge=0, le=100000)
    reason: str = Field(min_length=2, max_length=1000)


class ClassCreate(BaseModel):
    course_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    quota: int = Field(ge=0, le=100000)
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "ClassCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("课程周期的结束时间必须晚于开始时间")
        return self


class QuotaAdjust(BaseModel):
    quota: int = Field(ge=0, le=100000)
    reason: str = Field(min_length=2, max_length=1000)


class ReservationApply(BaseModel):
    class_code: str = Field(min_length=2, max_length=64)
    student_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=6, max_length=160)
    confirm_ttl_hours: int = Field(default=48, ge=1, le=168)


class ConfirmRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class DropRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class TransferRequest(BaseModel):
    to_class_code: str = Field(min_length=2, max_length=64)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)
