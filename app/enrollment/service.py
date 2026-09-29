from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.enrollment.repository import ACTIVE_STATES, EnrollmentRepository

# 候补递补的确定优先规则，同时写入每条 promote 事件，管理接口可直接复述。
PRIORITY_RULE = "priority 数值大者优先；相同优先级按 waitlist_seq 先到先得；仍相同按 enrollment id 升序"

# 递补循环的硬上限，每次成功递补都会使候补队列缩短，正常情况下远小于该值。
MAX_PROMOTION_STEPS = 10000


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def parse_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间") from exc
    return to_storage(parsed)


class EnrollmentService:
    """课程周期、班级配额与学员预留/候补的可追溯事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = EnrollmentRepository(self.connection)

    # ----- 查询 -----

    def list_courses(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM training_courses ORDER BY starts_at,code").fetchall()
        return [dict(row) for row in rows]

    def course_detail(self, code: str) -> dict[str, Any]:
        with transaction() as connection:
            repo = EnrollmentRepository(connection)
            course = repo.course_by_code(code)
            if course is None:
                raise NotFoundError("实训课程不存在")
            result = dict(course)
            result["classes"] = [dict(row) for row in repo.classes_of_course(course["id"])]
            return result

    def list_enrollments(self, *, class_id: int | None = None, student_key: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if class_id is not None:
            clauses.append("class_id=?")
            values.append(class_id)
        if student_key:
            clauses.append("student_key=?")
            values.append(student_key)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT * FROM training_enrollments" + where + " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def waitlist(self, course_code: str, class_code: str) -> dict[str, Any]:
        with transaction() as connection:
            repo = EnrollmentRepository(connection)
            course, clazz = self._load_class(repo, course_code, class_code)
            rows = repo.waitlist_candidates(clazz["id"])
            return {
                "course_code": course["code"],
                "class_code": clazz["code"],
                "priority_rule": PRIORITY_RULE,
                "items": [
                    {
                        "enrollment_id": row["id"],
                        "student_key": row["student_key"],
                        "priority": row["priority"],
                        "waitlist_seq": row["waitlist_seq"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            }

    def enrollment_trace(self, enrollment_id: int) -> dict[str, Any]:
        with transaction() as connection:
            repo = EnrollmentRepository(connection)
            row = repo.enrollment_by_id(enrollment_id)
            if row is None:
                raise NotFoundError("报名记录不存在")
            return {"enrollment": dict(row), "events": repo.events_by_enrollment(enrollment_id)}

    def list_batches(self, limit: int = 50) -> dict[str, Any]:
        return {"priority_rule": PRIORITY_RULE, "items": self.repository.list_recent_batches(max(1, min(limit, 500)))}

    def batch_trace(self, batch_key: str) -> dict[str, Any]:
        events = self.repository.events_by_batch(batch_key)
        if not events:
            raise NotFoundError("事件批次不存在")
        promotions = [event for event in events if event["event_type"] == "promote"]
        return {
            "batch_key": batch_key,
            "priority_rule": PRIORITY_RULE,
            "events": events,
            "promotions": [
                {
                    "event_id": event["id"],
                    "enrollment_id": event["enrollment_id"],
                    "student_key": event["student_key"],
                    "class_id": event["class_id"],
                    "release_source": event["release_source"],
                    "priority_rule": event["priority_rule"],
                    "origin_event_id": event["origin_event_id"],
                    "class_version_before": event["class_version_before"],
                    "class_version_after": event["class_version_after"],
                    "course_version_before": event["course_version_before"],
                    "course_version_after": event["course_version_after"],
                    "detail": event["detail"],
                    "created_at": event["created_at"],
                }
                for event in promotions
            ],
        }

    # ----- 课程/班级与配额 -----

    def create_course(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        starts_at = parse_timestamp(payload["starts_at"], "starts_at")
        ends_at = parse_timestamp(payload["ends_at"], "ends_at")
        if ends_at <= starts_at:
            raise ValidationError("课程结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            if repo.course_by_code(payload["code"]):
                raise ConflictError("实训课程编码已存在")
            return repo.create_course(
                code=payload["code"], name=payload["name"], term_label=payload["term_label"],
                starts_at=starts_at, ends_at=ends_at, total_capacity=payload["total_capacity"],
                reservation_seconds=payload["reservation_seconds"], created_by=actor, now=now,
            )

    def create_class(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        starts_at = parse_timestamp(payload["starts_at"], "starts_at")
        ends_at = parse_timestamp(payload["ends_at"], "ends_at")
        if ends_at <= starts_at:
            raise ValidationError("班级结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            course = repo.course_by_code(payload["course_code"])
            if course is None:
                raise NotFoundError("实训课程不存在")
            if repo.class_by_code(course["id"], payload["code"]):
                raise ConflictError("课程下班级编码已存在")
            if starts_at < course["starts_at"] or ends_at > course["ends_at"]:
                raise ValidationError("班级时段必须落在课程周期之内")
            return repo.create_class(
                course_id=course["id"], code=payload["code"], name=payload["name"],
                starts_at=starts_at, ends_at=ends_at, capacity=payload["capacity"],
                created_by=actor, now=now,
            )

    def adjust_capacity(self, course_code: str, class_code: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        batch_key = uuid.uuid4().hex
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            course = repo.course_by_code(course_code)
            if course is None:
                raise NotFoundError("实训课程不存在")
            clazz = None
            if class_code is not None:
                clazz = repo.class_by_code(course["id"], class_code)
                if clazz is None:
                    raise NotFoundError("实训班级不存在")
            drain_queue: list[tuple[int, int | None]] = []
            course_adjust_event_id: int | None = None
            if payload["course_capacity"] is not None and int(payload["course_capacity"]) != int(course["total_capacity"]):
                new_capacity = int(payload["course_capacity"])
                occupied = int(course["reserved_count"]) + int(course["confirmed_count"])
                if new_capacity < occupied:
                    raise ConflictError("课程总量不得小于当前已占用名额", context={"occupied": occupied})
                increased = new_capacity > int(course["total_capacity"])
                version_before = int(course["version"])
                self._execute(
                    connection,
                    "UPDATE training_courses SET total_capacity=?,updated_at=?,version=version+1 WHERE id=?",
                    (new_capacity, now, course["id"]),
                )
                course = repo.course_by_id(course["id"])
                course_adjust_event_id = repo.add_event(
                    batch_key=batch_key, event_type="capacity_adjust", course_id=course["id"], class_id=None,
                    enrollment_id=None, origin_event_id=None, actor=payload["actor"],
                    release_source="capacity_increase" if increased else "",
                    priority_rule="", student_key="",
                    class_version_before=None, class_version_after=None,
                    course_version_before=version_before, course_version_after=int(course["version"]),
                    detail={"total_capacity": new_capacity, "reason": payload["reason"]}, now=now,
                )
                if increased:
                    # 课程总量扩容可能解开该课程下所有班级的 course_full 阻塞。
                    drain_queue.extend((int(row["id"]), course_adjust_event_id) for row in repo.classes_of_course(course["id"]))
            class_adjust_event_id: int | None = None
            if clazz is not None and payload["class_capacity"] is not None and int(payload["class_capacity"]) != int(clazz["capacity"]):
                new_capacity = int(payload["class_capacity"])
                occupied = int(clazz["reserved_count"]) + int(clazz["confirmed_count"])
                if new_capacity < occupied:
                    raise ConflictError("班级配额不得小于当前已占用名额", context={"occupied": occupied})
                increased = new_capacity > int(clazz["capacity"])
                version_before = int(clazz["version"])
                self._execute(
                    connection,
                    "UPDATE training_classes SET capacity=?,updated_at=?,version=version+1 WHERE id=?",
                    (new_capacity, now, clazz["id"]),
                )
                clazz = repo.class_by_id(clazz["id"])
                class_adjust_event_id = repo.add_event(
                    batch_key=batch_key, event_type="capacity_adjust", course_id=course["id"], class_id=clazz["id"],
                    enrollment_id=None, origin_event_id=None, actor=payload["actor"],
                    release_source="capacity_increase" if increased else "",
                    priority_rule="", student_key="",
                    class_version_before=version_before, class_version_after=int(clazz["version"]),
                    course_version_before=None, course_version_after=None,
                    detail={"capacity": new_capacity, "reason": payload["reason"]}, now=now,
                )
                if increased:
                    drain_queue = [item for item in drain_queue if item[0] != int(clazz["id"])]
                    drain_queue.append((int(clazz["id"]), class_adjust_event_id))
            self._drain_promotions(connection, repo, drain_queue, batch_key, payload["actor"], now, trigger_source="capacity_increase")
            return {
                "batch_key": batch_key,
                "course": dict(repo.course_by_id(course["id"])),
                "class": dict(repo.class_by_id(clazz["id"])) if clazz else None,
            }

    # ----- 报名预留 / 确认 / 退课 / 转班 -----

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        request_digest = digest({k: payload[k] for k in sorted(payload) if k != "idempotency_key"})
        batch_key = uuid.uuid4().hex
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            course, clazz = self._load_class(repo, payload["course_code"], payload["class_code"])
            existing = repo.idempotency_record(payload["student_key"], payload["idempotency_key"])
            if existing is not None:
                return self._replay(existing, request_digest, "reserve")
            if course["status"] != "open" or clazz["status"] != "open":
                raise ConflictError("课程或班级已关闭报名")
            if repo.find_open_in_class(payload["student_key"], clazz["id"]):
                raise ConflictError("学员已在该班级保有有效名额或候补资格，请勿重复占位")
            course_room = int(course["total_capacity"]) - int(course["reserved_count"]) - int(course["confirmed_count"])
            class_room = int(clazz["capacity"]) - int(clazz["reserved_count"]) - int(clazz["confirmed_count"])
            course_before, class_before = int(course["version"]), int(clazz["version"])
            if course_room > 0 and class_room > 0:
                # 只有真正占用名额时才阻止重叠占位；满员时进入候补，递补前还会再次校验。
                conflict = repo.find_conflict(payload["student_key"], clazz["starts_at"], clazz["ends_at"])
                if conflict is not None:
                    raise ConflictError(
                        "学员在重叠时段已占用其他班级名额",
                        context={"conflict_enrollment_id": conflict["id"], "conflict_class_code": conflict["class_code"]},
                    )
                status = "reserved"
                waitlist_seq: int | None = None
                waitlist_class_id: int | None = None
                expires_at = to_storage(now_value + timedelta(seconds=int(course["reservation_seconds"])))
            else:
                status = "waitlisted"
                waitlist_seq = repo.next_waitlist_seq(clazz["id"])
                waitlist_class_id = clazz["id"]
                expires_at = ""
            enrollment_id = repo.insert_enrollment(
                course_id=course["id"], class_id=clazz["id"], student_key=payload["student_key"],
                status=status, priority=int(payload["priority"]), waitlist_seq=waitlist_seq,
                waitlist_class_id=waitlist_class_id, reserved_expires_at=expires_at,
                successor_enrollment_id=None, now=now,
            )
            if status == "reserved":
                self._occupy(connection, course["id"], clazz["id"], "reserved_count", +1, now)
            course_after = repo.course_by_id(course["id"])
            class_after = repo.class_by_id(clazz["id"])
            repo.add_event(
                batch_key=batch_key, event_type="reserve", course_id=course["id"], class_id=clazz["id"],
                enrollment_id=enrollment_id, origin_event_id=None, actor=payload["student_key"],
                release_source="", priority_rule="", student_key=payload["student_key"],
                class_version_before=class_before, class_version_after=int(class_after["version"]),
                course_version_before=course_before, course_version_after=int(course_after["version"]),
                detail={"result": status, "waitlist_seq": waitlist_seq, "reason_full": "course_full" if course_room <= 0 else "class_full"},
                now=now,
            )
            response = self._reservation_view(repo.enrollment_by_id(enrollment_id), course_after, class_after, batch_key)
            repo.save_idempotency(
                student_key=payload["student_key"], key=payload["idempotency_key"], operation="reserve",
                enrollment_id=enrollment_id, request_digest=request_digest, response=response, now=now,
            )
            return response

    def confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        request_digest = digest({k: payload[k] for k in sorted(payload) if k != "idempotency_key"})
        batch_key = uuid.uuid4().hex
        expired_context: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            existing = repo.idempotency_record(payload["student_key"], payload["idempotency_key"])
            if existing is not None:
                return self._replay(existing, request_digest, "confirm")
            enrollment = self._require_owned(repo, payload["enrollment_id"], payload["student_key"])
            course = repo.course_by_id(enrollment["course_id"])
            clazz = repo.class_by_id(enrollment["class_id"])
            if enrollment["status"] == "confirmed":
                raise ConflictError("名额已经确认，请勿重复确认")
            if enrollment["status"] == "waitlisted":
                raise ConflictError("学员仍在候补队列中，暂不能确认")
            if enrollment["status"] != "reserved":
                raise ConflictError("预留名额已释放，请重新报名")
            if enrollment["reserved_expires_at"] <= now:
                # 过期释放与后续递补在同一事务内按确定顺序完成；事务提交后再向调用方报告过期。
                queue = self._release(
                    connection, repo, enrollment, source="expire", actor=payload["student_key"],
                    reason="确认时发现预留已过期", batch_key=batch_key, now=now,
                )
                self._drain_promotions(connection, repo, queue, batch_key, payload["student_key"], now, trigger_source="expire")
                expired_response = {
                    "outcome": "expired",
                    "enrollment_id": enrollment["id"],
                    "status": "expired",
                    "release_source": "expire",
                    "batch_key": batch_key,
                }
                repo.save_idempotency(
                    student_key=payload["student_key"], key=payload["idempotency_key"], operation="confirm",
                    enrollment_id=enrollment["id"], request_digest=request_digest,
                    response=expired_response, now=now,
                )
                expired_context = {"enrollment_id": enrollment["id"], "batch_key": batch_key}
            else:
                conflict = repo.find_conflict(payload["student_key"], clazz["starts_at"], clazz["ends_at"], exclude_enrollment_id=enrollment["id"])
                if conflict is not None:
                    raise ConflictError(
                        "学员在重叠时段已占用其他班级名额，无法确认",
                        context={"conflict_enrollment_id": conflict["id"], "conflict_class_code": conflict["class_code"]},
                    )
                course_before, class_before = int(course["version"]), int(clazz["version"])
                self._execute(
                    connection,
                    "UPDATE training_enrollments SET status='confirmed',reserved_expires_at='',confirmed_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (now, now, enrollment["id"]),
                )
                # 原子地把预留学生效为确认：reserved-1 与 confirmed+1 在同一 UPDATE 内完成。
                self._execute(
                    connection,
                    "UPDATE training_courses SET reserved_count=reserved_count-1,confirmed_count=confirmed_count+1,updated_at=?,version=version+1 WHERE id=?",
                    (now, course["id"]),
                )
                self._execute(
                    connection,
                    "UPDATE training_classes SET reserved_count=reserved_count-1,confirmed_count=confirmed_count+1,updated_at=?,version=version+1 WHERE id=?",
                    (now, clazz["id"]),
                )
                course_after = repo.course_by_id(course["id"])
                class_after = repo.class_by_id(clazz["id"])
                repo.add_event(
                    batch_key=batch_key, event_type="confirm", course_id=course["id"], class_id=clazz["id"],
                    enrollment_id=enrollment["id"], origin_event_id=None, actor=payload["student_key"],
                    release_source="", priority_rule="", student_key=payload["student_key"],
                    class_version_before=class_before, class_version_after=int(class_after["version"]),
                    course_version_before=course_before, course_version_after=int(course_after["version"]),
                    detail={}, now=now,
                )
                response = self._reservation_view(repo.enrollment_by_id(enrollment["id"]), course_after, class_after, batch_key)
                repo.save_idempotency(
                    student_key=payload["student_key"], key=payload["idempotency_key"], operation="confirm",
                    enrollment_id=enrollment["id"], request_digest=request_digest, response=response, now=now,
                )
                return response
        raise ConflictError("预留已过期未确认，名额已释放并递补", context=expired_context)

    def cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        request_digest = digest({k: payload[k] for k in sorted(payload) if k != "idempotency_key"})
        batch_key = uuid.uuid4().hex
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            existing = repo.idempotency_record(payload["student_key"], payload["idempotency_key"])
            if existing is not None:
                return self._replay(existing, request_digest, "cancel")
            enrollment = self._require_owned(repo, payload["enrollment_id"], payload["student_key"])
            if enrollment["status"] not in {"reserved", "confirmed", "waitlisted"}:
                raise ConflictError("当前报名状态不允许退课")
            if enrollment["status"] == "waitlisted":
                self._execute(
                    connection,
                    "UPDATE training_enrollments SET status='cancelled',released_at=?,release_source='cancel',updated_at=?,version=version+1 WHERE id=?",
                    (now, now, enrollment["id"]),
                )
                repo.add_event(
                    batch_key=batch_key, event_type="release", course_id=enrollment["course_id"],
                    class_id=enrollment["waitlist_class_id"], enrollment_id=enrollment["id"],
                    origin_event_id=None, actor=payload["student_key"], release_source="cancel",
                    priority_rule="", student_key=enrollment["student_key"],
                    class_version_before=None, class_version_after=None,
                    course_version_before=None, course_version_after=None,
                    detail={"from_status": "waitlisted", "reason": payload["reason"]}, now=now,
                )
                queue: list[tuple[int, int | None]] = []
            else:
                queue = self._release(
                    connection, repo, enrollment, source="cancel", actor=payload["student_key"],
                    reason=payload["reason"], batch_key=batch_key, now=now,
                )
            self._drain_promotions(connection, repo, queue, batch_key, payload["student_key"], now, trigger_source="cancel")
            response = self._terminal_view(repo.enrollment_by_id(enrollment["id"]), batch_key)
            repo.save_idempotency(
                student_key=payload["student_key"], key=payload["idempotency_key"], operation="cancel",
                enrollment_id=enrollment["id"], request_digest=request_digest, response=response, now=now,
            )
            return response

    def transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        request_digest = digest({k: payload[k] for k in sorted(payload) if k != "idempotency_key"})
        batch_key = uuid.uuid4().hex
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            existing = repo.idempotency_record(payload["student_key"], payload["idempotency_key"])
            if existing is not None:
                return self._replay(existing, request_digest, "transfer")
            source = self._require_owned(repo, payload["enrollment_id"], payload["student_key"])
            if source["status"] not in {"reserved", "confirmed"}:
                raise ConflictError("只有已预留或已确认的名额可以转班")
            target_course, target_class = self._load_class(repo, payload["target_course_code"], payload["target_class_code"])
            if int(source["class_id"]) == int(target_class["id"]):
                raise ValidationError("目标班级与原班级相同，无需转班")
            if target_course["status"] != "open" or target_class["status"] != "open":
                raise ConflictError("目标课程或班级已关闭报名")
            conflict = repo.find_conflict(
                payload["student_key"], target_class["starts_at"], target_class["ends_at"],
                exclude_enrollment_id=source["id"],
            )
            if conflict is not None:
                raise ConflictError(
                    "目标班级与学员已占用的其他班级时段重叠",
                    context={"conflict_enrollment_id": conflict["id"], "conflict_class_code": conflict["class_code"]},
                )
            same_course = int(source["course_id"]) == int(target_course["id"])
            course_room = int(target_course["total_capacity"]) - int(target_course["reserved_count"]) - int(target_course["confirmed_count"])
            class_room = int(target_class["capacity"]) - int(target_class["reserved_count"]) - int(target_class["confirmed_count"])
            can_take = class_room > 0 and (same_course or course_room > 0)
            if not can_take:
                # 目标班无空位：保留原名额，生成针对目标班的候补，不释放任何占用。
                seq = repo.next_waitlist_seq(target_class["id"])
                new_id = repo.insert_enrollment(
                    course_id=target_course["id"], class_id=target_class["id"],
                    student_key=payload["student_key"], status="waitlisted", priority=source["priority"],
                    waitlist_seq=seq, waitlist_class_id=target_class["id"], reserved_expires_at="",
                    successor_enrollment_id=None, now=now,
                )
                self._execute(
                    connection,
                    "UPDATE training_enrollments SET successor_enrollment_id=?,updated_at=? WHERE id=?",
                    (new_id, now, source["id"]),
                )
                repo.add_event(
                    batch_key=batch_key, event_type="transfer", course_id=target_course["id"],
                    class_id=target_class["id"], enrollment_id=new_id, origin_event_id=None,
                    actor=payload["student_key"], release_source="", priority_rule=PRIORITY_RULE,
                    student_key=payload["student_key"],
                    class_version_before=int(target_class["version"]), class_version_after=int(target_class["version"]),
                    course_version_before=int(target_course["version"]), course_version_after=int(target_course["version"]),
                    detail={"result": "waitlisted", "waitlist_seq": seq, "source_enrollment_id": source["id"], "reason": payload["reason"]},
                    now=now,
                )
                response = self._reservation_view(repo.enrollment_by_id(new_id), target_course, target_class, batch_key)
                response["source_enrollment_id"] = source["id"]
                repo.save_idempotency(
                    student_key=payload["student_key"], key=payload["idempotency_key"], operation="transfer",
                    enrollment_id=new_id, request_digest=request_digest, response=response, now=now,
                )
                return response
            # 目标班有空位：先释放原名额（transfer），再占用目标名额，顺序固定以满足配额 CHECK。
            queue = self._release(
                connection, repo, source, source="transfer", actor=payload["student_key"],
                reason=payload["reason"], batch_key=batch_key, now=now,
            )
            release_events = [event for event in repo.events_by_batch(batch_key) if event["event_type"] == "release"]
            origin_event_id = release_events[-1]["id"] if release_events else None
            expires_at = to_storage(now_value + timedelta(seconds=int(target_course["reservation_seconds"])))
            new_id = repo.insert_enrollment(
                course_id=target_course["id"], class_id=target_class["id"],
                student_key=payload["student_key"], status="reserved", priority=source["priority"],
                waitlist_seq=None, waitlist_class_id=None, reserved_expires_at=expires_at,
                successor_enrollment_id=None, now=now,
            )
            self._execute(
                connection,
                "UPDATE training_enrollments SET successor_enrollment_id=?,updated_at=? WHERE id=?",
                (new_id, now, source["id"]),
            )
            course_before, class_before = int(target_course["version"]), int(target_class["version"])
            self._occupy(connection, target_course["id"], target_class["id"], "reserved_count", +1, now)
            target_course = repo.course_by_id(target_course["id"])
            target_class = repo.class_by_id(target_class["id"])
            repo.add_event(
                batch_key=batch_key, event_type="transfer", course_id=target_course["id"],
                class_id=target_class["id"], enrollment_id=new_id, origin_event_id=origin_event_id,
                actor=payload["student_key"], release_source="transfer", priority_rule="",
                student_key=payload["student_key"],
                class_version_before=class_before, class_version_after=int(target_class["version"]),
                course_version_before=course_before, course_version_after=int(target_course["version"]),
                detail={"result": "reserved", "source_enrollment_id": source["id"], "reason": payload["reason"]},
                now=now,
            )
            # 原班级空出名额，按确定顺序递补原班候补。
            self._drain_promotions(connection, repo, queue, batch_key, payload["student_key"], now, trigger_source="transfer")
            response = self._reservation_view(repo.enrollment_by_id(new_id), target_course, target_class, batch_key)
            response["source_enrollment_id"] = source["id"]
            repo.save_idempotency(
                student_key=payload["student_key"], key=payload["idempotency_key"], operation="transfer",
                enrollment_id=new_id, request_digest=request_digest, response=response, now=now,
            )
            return response

    def expire_unconfirmed(self, actor: str = "expiry-worker") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        batch_key = uuid.uuid4().hex
        with transaction(immediate=True) as connection:
            repo = EnrollmentRepository(connection)
            expired_rows = repo.expired_reservations(now)
            queue: list[tuple[int, int | None]] = []
            if expired_rows:
                repo.add_event(
                    batch_key=batch_key, event_type="expire_scan", course_id=None, class_id=None,
                    enrollment_id=None, origin_event_id=None, actor=actor, release_source="expire",
                    priority_rule="", student_key="",
                    class_version_before=None, class_version_after=None,
                    course_version_before=None, course_version_after=None,
                    detail={"scanned": len(expired_rows), "order": "reserved_expires_at ASC, enrollment id ASC", "now": now},
                    now=now,
                )
            # 固定释放顺序：过期时刻升序，其次报名记录 id；全部释放后再统一递补。
            for row in expired_rows:
                queue.extend(self._release(
                    connection, repo, row, source="expire", actor=actor,
                    reason="超过预留确认期限未确认", batch_key=batch_key, now=now,
                ))
            self._drain_promotions(connection, repo, queue, batch_key, actor, now, trigger_source="expire")
        return {
            "batch_key": batch_key,
            "expired_enrollment_ids": [int(row["id"]) for row in expired_rows],
            "events": self.repository.events_by_batch(batch_key),
        }

    # ----- 递补机制 -----

    def _drain_promotions(self, connection: sqlite3.Connection, repo: EnrollmentRepository, initial_queue: list[tuple[int, int | None]], batch_key: str, actor: str, now: str, *, trigger_source: str) -> None:
        """按班级入队顺序循环递补，直到没有任何班级还能继续递补。

        队列元素为 (班级 id, 触发事件 id)；trigger_source 标注本次递补的释放来源
        （cancel/expire/transfer/capacity_increase），会原样写入每条 promote 事件。

        - 每次成功递补都会让一条候补离开候补队列，因此总步数有上界；
        - 递补中转班候补会再释放一个原班级名额，该班级追加到队尾；
        - 因课程总量已满暂时被阻塞的班级，在出现新释放后重新入队重试；
        - 没有新释放时阻塞班级不会再入队，队列必然耗尽。
        """
        def dedupe(items: list[tuple[int, int | None]]) -> list[tuple[int, int | None]]:
            seen_ids: set[int] = set()
            result: list[tuple[int, int | None]] = []
            for class_id, origin_id in items:
                if class_id not in seen_ids:
                    seen_ids.add(class_id)
                    result.append((class_id, origin_id))
            return result

        queue = dedupe(initial_queue)
        blocked: list[tuple[int, int | None]] = []
        noted_skips: set[tuple[int, int, str]] = set()
        steps = 0
        while queue:
            steps += 1
            if steps > MAX_PROMOTION_STEPS:  # pragma: no cover - 防御性边界
                raise ConflictError("候补递补超出最大步数，事务已回滚")
            class_id, trigger_origin = queue.pop(0)
            extra, _promoted, still_blocked = self._promote_class(
                connection, repo, class_id, batch_key, actor, now,
                noted_skips=noted_skips, trigger_source=trigger_source, trigger_origin=trigger_origin,
            )
            if still_blocked and not any(item[0] == class_id for item in blocked):
                blocked.append((class_id, trigger_origin))
            if extra:
                queue = dedupe(queue + extra)
                # 新释放可能解开课程总量约束，阻塞班级按原阻塞顺序重试。
                queue = dedupe(queue + blocked)
                blocked.clear()

    def _promote_class(self, connection: sqlite3.Connection, repo: EnrollmentRepository, class_id: int, batch_key: str, actor: str, now: str, *, noted_skips: set[tuple[int, int, str]], trigger_source: str, trigger_origin: int | None) -> tuple[list[tuple[int, int | None]], int, bool]:
        affected: list[tuple[int, int | None]] = []
        promoted_count = 0
        while True:
            clazz = repo.class_by_id(class_id)
            if clazz is None or clazz["status"] != "open":
                return affected, promoted_count, False
            course = repo.course_by_id(clazz["course_id"])
            class_room = int(clazz["capacity"]) - int(clazz["reserved_count"]) - int(clazz["confirmed_count"])
            if class_room <= 0:
                return affected, promoted_count, False
            candidates = repo.waitlist_candidates(class_id)
            if not candidates:
                return affected, promoted_count, False
            skipped: list[dict[str, Any]] = []
            chosen: sqlite3.Row | None = None
            capacity_blocked = False
            for candidate in candidates:
                candidate_source = self._transfer_source(repo, candidate)
                # 转班候补的原名额即将被释放，冲突检查必须排除它自己的原班级占用。
                conflict = repo.find_conflict(
                    candidate["student_key"], clazz["starts_at"], clazz["ends_at"],
                    exclude_enrollment_id=None if candidate_source is None else candidate_source["id"],
                )
                if conflict is not None:
                    skipped.append({"enrollment_id": int(candidate["id"]), "reason": "overlap_conflict", "conflict_enrollment_id": int(conflict["id"])})
                    continue
                source = candidate_source
                course_room = int(course["total_capacity"]) - int(course["reserved_count"]) - int(course["confirmed_count"])
                if source is not None and int(source["course_id"]) == int(course["id"]):
                    # 同课程转班递补：占用新班前先释放原班，课程总量净额不变，视为多出一个额度。
                    course_room += 1
                if course_room <= 0:
                    skipped.append({"enrollment_id": int(candidate["id"]), "reason": "course_full"})
                    capacity_blocked = True
                    continue
                chosen = candidate
                break
            for item in skipped:
                key = (class_id, item["enrollment_id"], item["reason"])
                if key in noted_skips:
                    continue
                noted_skips.add(key)
                repo.add_event(
                    batch_key=batch_key, event_type="promote_skip", course_id=course["id"], class_id=class_id,
                    enrollment_id=item["enrollment_id"], origin_event_id=None, actor=actor,
                    release_source="", priority_rule=PRIORITY_RULE, student_key="",
                    class_version_before=int(clazz["version"]), class_version_after=int(clazz["version"]),
                    course_version_before=int(course["version"]), course_version_after=int(course["version"]),
                    detail={k: v for k, v in item.items()}, now=now,
                )
            if chosen is None:
                return affected, promoted_count, capacity_blocked
            course_before, class_before = int(course["version"]), int(clazz["version"])
            source = self._transfer_source(repo, chosen)
            internal_release_id: int | None = None
            if source is not None:
                # 候补来自转班请求：递补成功时先按 transfer 来源释放其原名额。
                released = self._release(
                    connection, repo, source, source="transfer", actor=actor,
                    reason="转班候补递补成功，释放原班级名额", batch_key=batch_key, now=now,
                )
                for sibling_class_id, _origin in released:
                    release_rows = [
                        event for event in repo.events_by_batch(batch_key)
                        if event["event_type"] == "release" and event["class_id"] == sibling_class_id
                    ]
                    origin = int(release_rows[-1]["id"]) if release_rows else None
                    affected.append((sibling_class_id, origin))
                    if int(source["class_id"]) == sibling_class_id:
                        internal_release_id = origin
            expires_at = to_storage(datetime.fromisoformat(now) + timedelta(seconds=int(course["reservation_seconds"])))
            self._execute(
                connection,
                "UPDATE training_enrollments SET status='reserved',class_id=?,course_id=?,waitlist_class_id=NULL,waitlist_seq=NULL,reserved_expires_at=?,updated_at=?,version=version+1 WHERE id=?",
                (class_id, course["id"], expires_at, now, chosen["id"]),
            )
            self._occupy(connection, course["id"], clazz["id"], "reserved_count", +1, now)
            course_after = repo.course_by_id(course["id"])
            class_after = repo.class_by_id(clazz["id"])
            repo.add_event(
                batch_key=batch_key, event_type="promote", course_id=course["id"], class_id=clazz["id"],
                enrollment_id=chosen["id"], origin_event_id=trigger_origin, actor=actor,
                release_source=trigger_source,
                priority_rule=PRIORITY_RULE, student_key=chosen["student_key"],
                class_version_before=class_before, class_version_after=int(class_after["version"]),
                course_version_before=course_before, course_version_after=int(course_after["version"]),
                detail={
                    "student_priority": chosen["priority"],
                    "waitlist_seq": chosen["waitlist_seq"],
                    "reserved_expires_at": expires_at,
                    "from_transfer_source_enrollment_id": None if source is None else int(source["id"]),
                    "internal_release_event_id": internal_release_id,
                },
                now=now,
            )
            promoted_count += 1

    @staticmethod
    def _transfer_source(repo: EnrollmentRepository, waitlisted: sqlite3.Row) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        return repo.connection.execute(
            f"SELECT * FROM training_enrollments WHERE successor_enrollment_id=? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (waitlisted["id"], *ACTIVE_STATES),
        ).fetchone()

    # ----- 释放与占用原语 -----

    def _release(self, connection: sqlite3.Connection, repo: EnrollmentRepository, enrollment: sqlite3.Row, *, source: str, actor: str, reason: str, batch_key: str, now: str) -> list[tuple[int, int | None]]:
        status = str(enrollment["status"])
        if status not in {"reserved", "confirmed"}:
            return []
        new_status = {"expire": "expired", "transfer": "transferred", "cancel": "cancelled"}[source]
        column = "reserved_count" if status == "reserved" else "confirmed_count"
        course = repo.course_by_id(enrollment["course_id"])
        clazz = repo.class_by_id(enrollment["class_id"])
        course_before, class_before = int(course["version"]), int(clazz["version"])
        self._execute(
            connection,
            "UPDATE training_enrollments SET status=?,reserved_expires_at='',released_at=?,release_source=?,updated_at=?,version=version+1 WHERE id=?",
            (new_status, now, source, now, enrollment["id"]),
        )
        self._execute(
            connection,
            f"UPDATE training_courses SET {column}={column}-1,updated_at=?,version=version+1 WHERE id=?",
            (now, course["id"]),
        )
        self._execute(
            connection,
            f"UPDATE training_classes SET {column}={column}-1,updated_at=?,version=version+1 WHERE id=?",
            (now, clazz["id"]),
        )
        course_after = repo.course_by_id(course["id"])
        class_after = repo.class_by_id(clazz["id"])
        release_event_id = repo.add_event(
            batch_key=batch_key, event_type="release", course_id=course["id"], class_id=clazz["id"],
            enrollment_id=enrollment["id"], origin_event_id=None, actor=actor,
            release_source=source, priority_rule="", student_key=enrollment["student_key"],
            class_version_before=class_before, class_version_after=int(class_after["version"]),
            course_version_before=course_before, course_version_after=int(course_after["version"]),
            detail={"from_status": status, "reason": reason}, now=now,
        )
        # 释放不仅空出本班名额，也空出课程总量；同课程其他班级可能正被 course_full 阻塞。
        result: list[tuple[int, int | None]] = [(int(clazz["id"]), release_event_id)]
        result.extend(
            (int(row["id"]), release_event_id)
            for row in repo.classes_of_course(course["id"])
            if int(row["id"]) != int(clazz["id"]) and row["status"] == "open"
        )
        return result

    @staticmethod
    def _occupy(connection: sqlite3.Connection, course_id: int, class_id: int, column: str, delta: int, now: str) -> None:
        sign = "+" if delta >= 0 else "-"
        connection.execute(
            f"UPDATE training_courses SET {column}={column}{sign}?,updated_at=?,version=version+1 WHERE id=?",
            (abs(delta), now, course_id),
        )
        connection.execute(
            f"UPDATE training_classes SET {column}={column}{sign}?,updated_at=?,version=version+1 WHERE id=?",
            (abs(delta), now, class_id),
        )

    @staticmethod
    def _execute(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> None:
        cursor = connection.execute(sql, params)
        if cursor.rowcount != 1:
            raise ConflictError("配额更新未命中唯一记录，操作已回滚")

    # ----- 辅助 -----

    @staticmethod
    def _load_class(repo: EnrollmentRepository, course_code: str, class_code: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        course = repo.course_by_code(course_code)
        if course is None:
            raise NotFoundError("实训课程不存在")
        clazz = repo.class_by_code(course["id"], class_code)
        if clazz is None:
            raise NotFoundError("实训班级不存在")
        return course, clazz

    @staticmethod
    def _require_owned(repo: EnrollmentRepository, enrollment_id: int, student_key: str) -> sqlite3.Row:
        enrollment = repo.enrollment_by_id(enrollment_id)
        if enrollment is None:
            raise NotFoundError("报名记录不存在")
        if enrollment["student_key"] != student_key:
            raise ConflictError("报名记录与学员不匹配")
        return enrollment

    @staticmethod
    def _replay(existing: sqlite3.Row, request_digest: str | None, operation: str) -> dict[str, Any]:
        if existing["operation"] != operation:
            raise ConflictError("同一幂等键已经用于其他报名操作", context={"original_operation": existing["operation"]})
        if request_digest is not None and existing["request_digest"] != request_digest:
            raise ConflictError("同一幂等键对应了不同的报名请求")
        result = json.loads(existing["response_json"])
        if result.get("outcome") == "expired":
            # 重放过期确认：保持与首次请求一致的拒绝结论。
            raise ConflictError(
                "预留已过期未确认，名额已释放并递补",
                context={"enrollment_id": result.get("enrollment_id"), "batch_key": result.get("batch_key"), "replayed": True},
            )
        result["replayed"] = True
        return result

    @staticmethod
    def _reservation_view(enrollment: sqlite3.Row, course: sqlite3.Row, clazz: sqlite3.Row, batch_key: str) -> dict[str, Any]:
        return {
            "enrollment_id": int(enrollment["id"]),
            "course_id": int(course["id"]),
            "class_id": int(clazz["id"]),
            "student_key": enrollment["student_key"],
            "status": enrollment["status"],
            "priority": int(enrollment["priority"]),
            "waitlist_seq": enrollment["waitlist_seq"],
            "reserved_expires_at": enrollment["reserved_expires_at"] or None,
            "enrollment_version": int(enrollment["version"]),
            "course_version": int(course["version"]),
            "class_version": int(clazz["version"]),
            "batch_key": batch_key,
        }

    @staticmethod
    def _terminal_view(enrollment: sqlite3.Row, batch_key: str) -> dict[str, Any]:
        return {
            "enrollment_id": int(enrollment["id"]),
            "student_key": enrollment["student_key"],
            "status": enrollment["status"],
            "release_source": enrollment["release_source"],
            "enrollment_version": int(enrollment["version"]),
            "batch_key": batch_key,
        }
