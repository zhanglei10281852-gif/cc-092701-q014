from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.enroll.repository import EnrollmentRepository

RULE_FIFO_POSITION = "fifo_position"
RULE_FIFO_DETAIL = "按候补名次升序递补（先报名先递补），跳过与学员已占用时段冲突的候补"
EXPIRY_ACTOR = "system-expiry"


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class EnrollmentService:
    """管理课程总量、班级配额、学员预留、候补递补与转班的可追溯流程。

    容量模型：课程有总量（total_capacity），班级有配额（quota），两层都按
    pending_count（待确认预留）+ confirmed_count（已确认占用）记账。报名先形成
    带确认截止时间的待确认预留；确认在同一事务内原子扣减班级配额与课程总量；
    退课、过期未确认、转班都按“先释放、再递补”的固定顺序处理，候补按名次
    升序递补，每一次递补都记录优先规则、释放来源和当时的版本边界。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = EnrollmentRepository(self.connection)

    # ---- 课程与班级 ----

    def create_course(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            if repository.course_by_code(payload["code"]):
                raise ConflictError("课程编码已存在")
            course = repository.create_course(code=payload["code"], name=payload["name"], total_capacity=payload["total_capacity"], now=now)
            repository.add_event(reservation_id=None, class_id=None, course_id=course["id"], student_id="", action="course_create", actor=actor, reason="", before={}, after=course, now=now)
            return course

    def list_courses(self) -> list[dict[str, Any]]:
        return self.repository.list_courses()

    def adjust_course_capacity(self, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            course = repository.course_by_id(course_id)
            if course is None:
                raise NotFoundError("课程不存在")
            occupied = int(course["pending_count"]) + int(course["confirmed_count"])
            if payload["capacity"] < occupied:
                raise ConflictError("新课程总量低于当前占用，无法调整", context={"occupied": occupied, "pending": course["pending_count"], "confirmed": course["confirmed_count"]})
            before = dict(course)
            connection.execute("UPDATE enroll_courses SET total_capacity=?,version=version+1,updated_at=? WHERE id=?", (payload["capacity"], now, course_id))
            after = dict(repository.course_by_id(course_id))
            repository.add_event(reservation_id=None, class_id=None, course_id=course_id, student_id="", action="capacity_adjust", actor=actor, reason=payload["reason"], before=before, after=after, now=now)
            promotions: list[dict[str, Any]] = []
            if payload["capacity"] > int(before["total_capacity"]):
                promotions = self._promote_course(connection, repository, course_id, actor, {"type": "capacity_expand", "course_id": course_id, "reason": payload["reason"]}, now_value)
            after["promotions"] = promotions
            return after

    def create_class(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        starts_at = self._as_storage(payload["starts_at"])
        ends_at = self._as_storage(payload["ends_at"])
        if ends_at <= starts_at:
            raise ValidationError("课程周期的结束时间必须晚于开始时间")
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            course = repository.course_by_code(payload["course_code"])
            if course is None:
                raise NotFoundError("课程不存在")
            if repository.class_by_code(payload["code"]):
                raise ConflictError("班级编码已存在")
            class_row = repository.create_class(course_id=course["id"], code=payload["code"], name=payload["name"], quota=payload["quota"], starts_at=starts_at, ends_at=ends_at, now=now)
            repository.add_event(reservation_id=None, class_id=class_row["id"], course_id=course["id"], student_id="", action="class_create", actor=actor, reason="", before={}, after=class_row, now=now)
            return class_row

    def list_classes(self, course_code: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_classes(course_code)

    def get_class(self, class_id: int) -> dict[str, Any]:
        class_row = self.repository.class_by_id(class_id)
        if class_row is None:
            raise NotFoundError("班级不存在")
        result = dict(class_row)
        result["waitlist"] = [dict(row) for row in self.repository.waitlisted(class_id)]
        return result

    def adjust_class_quota(self, class_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            class_row = repository.class_by_id(class_id)
            if class_row is None:
                raise NotFoundError("班级不存在")
            occupied = int(class_row["pending_count"]) + int(class_row["confirmed_count"])
            if payload["quota"] < occupied:
                raise ConflictError("新配额低于当前占用，无法缩班", context={"occupied": occupied, "pending": class_row["pending_count"], "confirmed": class_row["confirmed_count"]})
            before = dict(class_row)
            connection.execute("UPDATE enroll_classes SET quota=?,version=version+1,updated_at=? WHERE id=?", (payload["quota"], now, class_id))
            after = dict(repository.class_by_id(class_id))
            repository.add_event(reservation_id=None, class_id=class_id, course_id=class_row["course_id"], student_id="", action="quota_adjust", actor=actor, reason=payload["reason"], before=before, after=after, now=now)
            promotions: list[dict[str, Any]] = []
            if payload["quota"] > int(before["quota"]):
                promotions = self._promote_class(connection, repository, class_id, actor, {"type": "quota_expand", "class_id": class_id, "reason": payload["reason"]}, now_value)
            after["promotions"] = promotions
            return after

    # ---- 报名预留 ----

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        request_digest = digest({
            "class_code": payload["class_code"],
            "student_id": payload["student_id"],
            "confirm_ttl_hours": payload["confirm_ttl_hours"],
        })
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            existing = repository.reservation_by_idempotency(payload["student_id"], payload["idempotency_key"])
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一幂等键对应了不同的报名请求")
                return dict(repository.reservation_by_id(existing["id"]))
            class_row = repository.class_by_code(payload["class_code"])
            if class_row is None or not class_row["course_active"] or class_row["status"] != "open":
                raise NotFoundError("班级不存在或已停止报名")
            self._expire_due(connection, repository, now_value, student_id=payload["student_id"])
            class_row = repository.class_by_id(class_row["id"])
            duplicate = repository.active_reservation(class_row["id"], payload["student_id"])
            if duplicate is not None:
                raise ConflictError("该学员在此班级已有有效预留或候补", context={"reservation_id": duplicate["id"], "status": duplicate["status"]})
            conflicts = repository.student_active_overlaps(payload["student_id"], class_row["starts_at"], class_row["ends_at"])
            if conflicts:
                raise ConflictError("同一学员在重叠时段不得重复占位", context={"conflicts": conflicts})
            class_ok = int(class_row["pending_count"]) + int(class_row["confirmed_count"]) < int(class_row["quota"])
            course_ok = int(class_row["course_pending"]) + int(class_row["course_confirmed"]) < int(class_row["course_capacity"])
            if class_ok and course_ok:
                deadline = to_storage(now_value + timedelta(hours=payload["confirm_ttl_hours"]))
                reservation = repository.create_reservation(
                    class_id=class_row["id"], course_id=class_row["course_id"], student_id=payload["student_id"],
                    status="pending", position=None, idempotency_key=payload["idempotency_key"], request_digest=request_digest,
                    confirm_ttl_hours=payload["confirm_ttl_hours"], confirm_deadline=deadline, confirmed_at=None, source_reservation_id=None, now=now,
                )
                cursor = connection.execute("UPDATE enroll_classes SET pending_count=pending_count+1,version=version+1,updated_at=? WHERE id=? AND pending_count+confirmed_count<quota", (now, class_row["id"]))
                if cursor.rowcount != 1:
                    raise ConflictError("班级配额不足，无法预留")
                cursor = connection.execute("UPDATE enroll_courses SET pending_count=pending_count+1,version=version+1,updated_at=? WHERE id=? AND pending_count+confirmed_count<total_capacity", (now, class_row["course_id"]))
                if cursor.rowcount != 1:
                    raise ConflictError("课程总量不足，无法预留")
                repository.add_event(reservation_id=reservation["id"], class_id=class_row["id"], course_id=class_row["course_id"], student_id=payload["student_id"], action="apply", actor=payload["student_id"], reason="", before={}, after=dict(repository.reservation_by_id(reservation["id"])), now=now)
                return dict(repository.reservation_by_id(reservation["id"]))
            position = int(class_row["waitlist_seq"]) + 1
            connection.execute("UPDATE enroll_classes SET waitlist_seq=waitlist_seq+1,version=version+1,updated_at=? WHERE id=?", (now, class_row["id"]))
            reservation = repository.create_reservation(
                class_id=class_row["id"], course_id=class_row["course_id"], student_id=payload["student_id"],
                status="waitlisted", position=position, idempotency_key=payload["idempotency_key"], request_digest=request_digest,
                confirm_ttl_hours=payload["confirm_ttl_hours"], confirm_deadline=None, confirmed_at=None, source_reservation_id=None, now=now,
            )
            reason = "班级配额已满" if not class_ok else "课程总量已满"
            repository.add_event(
                reservation_id=reservation["id"], class_id=class_row["id"], course_id=class_row["course_id"], student_id=payload["student_id"],
                action="waitlist", actor=payload["student_id"], reason=reason, before={},
                after={"position": position, "class_quota": class_row["quota"], "class_occupied": int(class_row["pending_count"]) + int(class_row["confirmed_count"]), "course_capacity": class_row["course_capacity"], "course_occupied": int(class_row["course_pending"]) + int(class_row["course_confirmed"])},
                now=now,
            )
            return dict(repository.reservation_by_id(reservation["id"]))

    def confirm(self, reservation_id: int, actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        self._expire_reservation_if_due(reservation_id, now_value)
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            reservation = repository.reservation_by_id(reservation_id)
            if reservation is None:
                raise NotFoundError("预留记录不存在")
            if reservation["status"] == "confirmed":
                return dict(reservation)
            if reservation["status"] == "waitlisted":
                raise ConflictError("候补中的预留不能确认，请等待递补")
            if reservation["status"] == "released":
                raise ConflictError("预留已释放，不能确认", context={"release_reason": reservation["release_reason"]})
            cursor = connection.execute(
                "UPDATE enroll_classes SET pending_count=pending_count-1,confirmed_count=confirmed_count+1,version=version+1,updated_at=? WHERE id=? AND pending_count>=1 AND confirmed_count<quota",
                (now, reservation["class_id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("班级配额已调整，当前不足以确认该预留")
            cursor = connection.execute(
                "UPDATE enroll_courses SET pending_count=pending_count-1,confirmed_count=confirmed_count+1,version=version+1,updated_at=? WHERE id=? AND pending_count>=1 AND confirmed_count<total_capacity",
                (now, reservation["course_id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("课程总量已调整，当前不足以确认该预留")
            connection.execute("UPDATE enroll_reservations SET status='confirmed',confirmed_at=?,confirm_deadline=NULL,version=version+1,updated_at=? WHERE id=?", (now, now, reservation_id))
            after = dict(repository.reservation_by_id(reservation_id))
            repository.add_event(reservation_id=reservation_id, class_id=reservation["class_id"], course_id=reservation["course_id"], student_id=reservation["student_id"], action="confirm", actor=actor, reason="", before=dict(reservation), after=after, now=now)
            return after

    def drop(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        now_value = self.clock.now()
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            reservation = repository.reservation_by_id(reservation_id)
            if reservation is None:
                raise NotFoundError("预留记录不存在")
            if reservation["status"] == "released":
                result = dict(reservation)
                result["promotions"] = []
                return result
            _, freed = self._release(connection, repository, reservation, action="drop", release_reason="drop", actor=actor, reason=reason, now_value=now_value)
            promotions: list[dict[str, Any]] = []
            if freed:
                source = {"type": "drop", "reservation_id": reservation["id"], "student_id": reservation["student_id"], "reason": reason}
                promotions = self._promote_class(connection, repository, reservation["class_id"], actor, source, now_value)
            result = dict(repository.reservation_by_id(reservation_id))
            result["promotions"] = promotions
            return result

    def transfer(self, reservation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        request_digest = digest({"reservation_id": reservation_id, "to_class_code": payload["to_class_code"]})
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            source = repository.reservation_by_id(reservation_id)
            if source is None:
                raise NotFoundError("源预留记录不存在")
            existing = repository.reservation_by_idempotency(source["student_id"], payload["idempotency_key"])
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一幂等键对应了不同的转班请求")
                origin = dict(repository.reservation_by_id(existing["source_reservation_id"])) if existing["source_reservation_id"] else None
                return {"source": origin, "target": dict(repository.reservation_by_id(existing["id"])), "promotions": []}
            self._expire_due(connection, repository, now_value, student_id=source["student_id"])
            source = repository.reservation_by_id(reservation_id)
            if source["status"] not in ("pending", "confirmed"):
                raise ConflictError("只有待确认或已确认的预留可以转班", context={"status": source["status"], "release_reason": source["release_reason"]})
            target_class = repository.class_by_code(payload["to_class_code"])
            if target_class is None or not target_class["course_active"] or target_class["status"] != "open":
                raise NotFoundError("目标班级不存在或已停止报名")
            if target_class["id"] == source["class_id"]:
                raise ValidationError("目标班级与源班级相同")
            duplicate = repository.active_reservation(target_class["id"], source["student_id"])
            if duplicate is not None:
                raise ConflictError("该学员在目标班级已有有效预留或候补", context={"reservation_id": duplicate["id"], "status": duplicate["status"]})
            conflicts = repository.student_active_overlaps(source["student_id"], target_class["starts_at"], target_class["ends_at"], exclude_reservation_id=source["id"])
            if conflicts:
                raise ConflictError("同一学员在重叠时段不得重复占位", context={"conflicts": conflicts})
            now = to_storage(now_value)
            _, freed = self._release(connection, repository, source, action="transfer_out", release_reason="transfer_out", actor=payload["actor"], reason=payload["reason"], now_value=now_value)
            promotions: list[dict[str, Any]] = []
            if freed:
                release_source = {"type": "transfer_out", "reservation_id": source["id"], "student_id": source["student_id"], "reason": payload["reason"]}
                promotions = self._promote_class(connection, repository, source["class_id"], payload["actor"], release_source, now_value)
            fresh = repository.class_by_id(target_class["id"])
            class_ok = int(fresh["pending_count"]) + int(fresh["confirmed_count"]) < int(fresh["quota"])
            course_ok = int(fresh["course_pending"]) + int(fresh["course_confirmed"]) < int(fresh["course_capacity"])
            if class_ok and course_ok:
                if source["status"] == "confirmed":
                    status, deadline, confirmed_at, counter = "confirmed", None, now, "confirmed_count"
                else:
                    status = "pending"
                    deadline = to_storage(now_value + timedelta(hours=source["confirm_ttl_hours"]))
                    confirmed_at, counter = None, "pending_count"
                target = repository.create_reservation(
                    class_id=fresh["id"], course_id=fresh["course_id"], student_id=source["student_id"],
                    status=status, position=None, idempotency_key=payload["idempotency_key"], request_digest=request_digest,
                    confirm_ttl_hours=source["confirm_ttl_hours"], confirm_deadline=deadline, confirmed_at=confirmed_at, source_reservation_id=source["id"], now=now,
                )
                cursor = connection.execute(
                    f"UPDATE enroll_classes SET {counter}={counter}+1,version=version+1,updated_at=? WHERE id=? AND pending_count+confirmed_count<quota",
                    (now, fresh["id"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("目标班级配额不足，无法转入")
                cursor = connection.execute(
                    f"UPDATE enroll_courses SET {counter}={counter}+1,version=version+1,updated_at=? WHERE id=? AND pending_count+confirmed_count<total_capacity",
                    (now, fresh["course_id"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("目标课程总量不足，无法转入")
                action, event_reason = "transfer_in", payload["reason"]
            else:
                position = int(fresh["waitlist_seq"]) + 1
                connection.execute("UPDATE enroll_classes SET waitlist_seq=waitlist_seq+1,version=version+1,updated_at=? WHERE id=?", (now, fresh["id"]))
                target = repository.create_reservation(
                    class_id=fresh["id"], course_id=fresh["course_id"], student_id=source["student_id"],
                    status="waitlisted", position=position, idempotency_key=payload["idempotency_key"], request_digest=request_digest,
                    confirm_ttl_hours=source["confirm_ttl_hours"], confirm_deadline=None, confirmed_at=None, source_reservation_id=source["id"], now=now,
                )
                action = "waitlist"
                event_reason = "目标班级配额已满" if not class_ok else "目标课程总量已满"
            repository.add_event(reservation_id=target["id"], class_id=fresh["id"], course_id=fresh["course_id"], student_id=source["student_id"], action=action, actor=payload["actor"], reason=event_reason, before={}, after=dict(repository.reservation_by_id(target["id"])), now=now)
            return {"source": dict(repository.reservation_by_id(source["id"])), "target": dict(repository.reservation_by_id(target["id"])), "promotions": promotions}

    # ---- 过期与查询 ----

    def expire_pending(self, actor: str = EXPIRY_ACTOR) -> dict[str, Any]:
        now_value = self.clock.now()
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            released, promotions = self._expire_due(connection, repository, now_value, actor=actor)
        return {"released": released, "promotions": promotions}

    def get_reservation(self, reservation_id: int) -> dict[str, Any]:
        reservation = self.repository.reservation_by_id(reservation_id)
        if reservation is None:
            raise NotFoundError("预留记录不存在")
        result = dict(reservation)
        result["events"] = self.repository.events_of_reservation(reservation_id)
        return result

    def student_reservations(self, student_id: str) -> list[dict[str, Any]]:
        return self.repository.student_reservations(student_id)

    def get_promotion(self, promotion_id: int) -> dict[str, Any]:
        row = self.repository.promotion_by_id(promotion_id)
        if row is None:
            raise NotFoundError("递补记录不存在")
        return self._promotion_view(row)

    def class_promotions(self, class_id: int) -> list[dict[str, Any]]:
        if self.repository.class_by_id(class_id) is None:
            raise NotFoundError("班级不存在")
        return [self._promotion_view(row) for row in self.repository.promotions_of_class(class_id)]

    def summary(self) -> dict[str, Any]:
        states = {row["status"]: row["amount"] for row in self.connection.execute("SELECT status,COUNT(*) AS amount FROM enroll_reservations GROUP BY status ORDER BY status").fetchall()}
        classes = self.connection.execute("SELECT COUNT(*) FROM enroll_classes").fetchone()[0]
        courses = self.connection.execute("SELECT COUNT(*) FROM enroll_courses").fetchone()[0]
        promotions = self.connection.execute("SELECT COUNT(*) FROM enroll_promotions").fetchone()[0]
        return {"reservations": states, "classes": classes, "courses": courses, "promotions": promotions}

    # ---- 内部：释放、递补、过期 ----

    def _release(self, connection: sqlite3.Connection, repository: EnrollmentRepository, reservation: sqlite3.Row, *, action: str, release_reason: str, actor: str, reason: str, now_value: datetime) -> tuple[dict[str, Any], bool]:
        """释放一条占用；返回（释放来源， 是否释放了名额）。候补不占用名额。"""
        now = to_storage(now_value)
        counter = {"pending": "pending_count", "confirmed": "confirmed_count"}.get(reservation["status"])
        before = dict(reservation)
        if counter is not None:
            cursor = connection.execute(
                f"UPDATE enroll_classes SET {counter}={counter}-1,version=version+1,updated_at=? WHERE id=? AND {counter}>=1",
                (now, reservation["class_id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("班级占用计数异常，无法释放")
            cursor = connection.execute(
                f"UPDATE enroll_courses SET {counter}={counter}-1,version=version+1,updated_at=? WHERE id=? AND {counter}>=1",
                (now, reservation["course_id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("课程占用计数异常，无法释放")
        connection.execute("UPDATE enroll_reservations SET status='released',release_reason=?,confirm_deadline=NULL,version=version+1,updated_at=? WHERE id=?", (release_reason, now, reservation["id"]))
        after = dict(repository.reservation_by_id(reservation["id"]))
        repository.add_event(reservation_id=reservation["id"], class_id=reservation["class_id"], course_id=reservation["course_id"], student_id=reservation["student_id"], action=action, actor=actor, reason=reason, before=before, after=after, now=now)
        release_source = {"type": release_reason, "reservation_id": reservation["id"], "student_id": reservation["student_id"], "reason": reason}
        return release_source, counter is not None

    def _promote_class(self, connection: sqlite3.Connection, repository: EnrollmentRepository, class_id: int, actor: str, release_source: dict[str, Any], now_value: datetime) -> list[dict[str, Any]]:
        """按候补名次升序递补，直到班级配额或课程总量用尽；每次递补记录规则、来源与版本边界。"""
        now = to_storage(now_value)
        promoted: list[dict[str, Any]] = []
        while True:
            class_row = repository.class_by_id(class_id)
            course = repository.course_by_id(class_row["course_id"])
            if int(class_row["pending_count"]) + int(class_row["confirmed_count"]) >= int(class_row["quota"]):
                break
            if int(course["pending_count"]) + int(course["confirmed_count"]) >= int(course["total_capacity"]):
                break
            chosen: sqlite3.Row | None = None
            for candidate in repository.waitlisted(class_id):
                conflicts = repository.student_active_overlaps(candidate["student_id"], class_row["starts_at"], class_row["ends_at"], exclude_reservation_id=candidate["id"])
                if conflicts:
                    repository.add_event(reservation_id=candidate["id"], class_id=class_id, course_id=class_row["course_id"], student_id=candidate["student_id"], action="promote_skip", actor=actor, reason="与已占用时段冲突，保留候补名次", before={"position": candidate["position"]}, after={"conflicts": conflicts}, now=now)
                    continue
                chosen = candidate
                break
            if chosen is None:
                break
            deadline = to_storage(now_value + timedelta(hours=chosen["confirm_ttl_hours"]))
            connection.execute("UPDATE enroll_reservations SET status='pending',confirm_deadline=?,version=version+1,updated_at=? WHERE id=? AND status='waitlisted'", (deadline, now, chosen["id"]))
            connection.execute("UPDATE enroll_classes SET pending_count=pending_count+1,version=version+1,updated_at=? WHERE id=?", (now, class_id))
            connection.execute("UPDATE enroll_courses SET pending_count=pending_count+1,version=version+1,updated_at=? WHERE id=?", (now, class_row["course_id"]))
            after = dict(repository.reservation_by_id(chosen["id"]))
            promotion = repository.add_promotion(
                class_id=class_id, course_id=class_row["course_id"], reservation_id=chosen["id"], student_id=chosen["student_id"],
                position=chosen["position"], rule_code=RULE_FIFO_POSITION, rule_detail=RULE_FIFO_DETAIL, release_source=release_source,
                class_version_before=class_row["version"], class_version_after=int(class_row["version"]) + 1,
                course_version_before=course["version"], course_version_after=int(course["version"]) + 1,
                class_quota=class_row["quota"], class_confirmed=class_row["confirmed_count"],
                course_capacity=course["total_capacity"], course_confirmed=course["confirmed_count"],
                confirm_deadline=deadline, now=now,
            )
            repository.add_event(reservation_id=chosen["id"], class_id=class_id, course_id=class_row["course_id"], student_id=chosen["student_id"], action="promote", actor=actor, reason=f"候补名次 {chosen['position']} 递补成功", before=dict(chosen), after=after, now=now)
            promoted.append(promotion)
        return promoted

    def _promote_course(self, connection: sqlite3.Connection, repository: EnrollmentRepository, course_id: int, actor: str, release_source: dict[str, Any], now_value: datetime) -> list[dict[str, Any]]:
        """课程总量扩容后，按班级主键顺序依次递补，直到课程总量用尽。"""
        promoted: list[dict[str, Any]] = []
        for class_row in repository.classes_of_course(course_id):
            promoted.extend(self._promote_class(connection, repository, class_row["id"], actor, release_source, now_value))
        return promoted

    def _expire_due(self, connection: sqlite3.Connection, repository: EnrollmentRepository, now_value: datetime, *, actor: str = EXPIRY_ACTOR, student_id: str | None = None) -> tuple[list[int], list[dict[str, Any]]]:
        """按主键顺序释放过期未确认的预留并立即递补，保证释放顺序确定。"""
        released: list[int] = []
        promotions: list[dict[str, Any]] = []
        now = to_storage(now_value)
        for reservation in repository.expired_pending(now):
            if student_id is not None and reservation["student_id"] != student_id:
                continue
            source, freed = self._release(connection, repository, reservation, action="expire", release_reason="expired", actor=actor, reason="过期未确认自动释放", now_value=now_value)
            if freed:
                promotions.extend(self._promote_class(connection, repository, reservation["class_id"], actor, source, now_value))
            released.append(int(reservation["id"]))
        return released, promotions

    def _expire_reservation_if_due(self, reservation_id: int, now_value: datetime) -> None:
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = EnrollmentRepository(connection)
            reservation = repository.reservation_by_id(reservation_id)
            if reservation is None or reservation["status"] != "pending":
                return
            if not reservation["confirm_deadline"] or reservation["confirm_deadline"] > now:
                return
            source, freed = self._release(connection, repository, reservation, action="expire", release_reason="expired", actor=EXPIRY_ACTOR, reason="过期未确认自动释放", now_value=now_value)
            if freed:
                self._promote_class(connection, repository, reservation["class_id"], EXPIRY_ACTOR, source, now_value)

    @staticmethod
    def _promotion_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "class_id": row["class_id"],
            "class_code": row["class_code"],
            "course_id": row["course_id"],
            "course_code": row["course_code"],
            "reservation_id": row["reservation_id"],
            "student_id": row["student_id"],
            "position": row["position"],
            "priority_rule": {"code": row["rule_code"], "detail": row["rule_detail"]},
            "release_source": json.loads(row["release_source_json"]),
            "version_boundary": {
                "class_version_before": row["class_version_before"],
                "class_version_after": row["class_version_after"],
                "course_version_before": row["course_version_before"],
                "course_version_after": row["course_version_after"],
                "class_quota": row["class_quota"],
                "class_confirmed": row["class_confirmed"],
                "course_capacity": row["course_capacity"],
                "course_confirmed": row["course_confirmed"],
            },
            "confirm_deadline": row["confirm_deadline"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _as_storage(value: Any) -> str:
        if isinstance(value, datetime):
            return to_storage(value)
        return str(value)
