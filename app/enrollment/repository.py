from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

ACTIVE_STATES = ("reserved", "confirmed")
OPEN_STATES = ("reserved", "confirmed", "waitlisted")


class EnrollmentRepository:
    """封装实训报名领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ----- 课程与班级 -----

    def course_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_courses WHERE code=?", (code,)).fetchone()

    def course_by_id(self, course_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_courses WHERE id=?", (course_id,)).fetchone()

    def create_course(self, *, code: str, name: str, term_label: str, starts_at: str, ends_at: str, total_capacity: int, reservation_seconds: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO training_courses(code,name,term_label,starts_at,ends_at,total_capacity,reservation_seconds,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, name, term_label, starts_at, ends_at, total_capacity, reservation_seconds, created_by, now, now),
        )
        return dict(self.course_by_id(cursor.lastrowid))

    def classes_of_course(self, course_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM training_classes WHERE course_id=? ORDER BY code,id", (course_id,)).fetchall())

    def class_by_code(self, course_id: int, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_classes WHERE course_id=? AND code=?", (course_id, code)).fetchone()

    def class_by_id(self, class_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_classes WHERE id=?", (class_id,)).fetchone()

    def create_class(self, *, course_id: int, code: str, name: str, starts_at: str, ends_at: str, capacity: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO training_classes(course_id,code,name,starts_at,ends_at,capacity,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (course_id, code, name, starts_at, ends_at, capacity, created_by, now, now),
        )
        return dict(self.class_by_id(cursor.lastrowid))

    # ----- 报名记录 -----

    def enrollment_by_id(self, enrollment_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_enrollments WHERE id=?", (enrollment_id,)).fetchone()

    def find_open_in_class(self, student_key: str, class_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT * FROM training_enrollments WHERE student_key=? AND class_id=? AND status IN ({','.join('?' for _ in OPEN_STATES)}) ORDER BY id DESC LIMIT 1",
            (student_key, class_id, *OPEN_STATES),
        ).fetchone()

    def find_active_in_course(self, student_key: str, course_id: int, *, exclude_enrollment_id: int | None = None) -> sqlite3.Row | None:
        sql = f"SELECT * FROM training_enrollments WHERE student_key=? AND course_id=? AND status IN ({','.join('?' for _ in ACTIVE_STATES)})"
        params: list[Any] = [student_key, course_id, *ACTIVE_STATES]
        if exclude_enrollment_id is not None:
            sql += " AND id<>?"
            params.append(exclude_enrollment_id)
        sql += " ORDER BY id DESC LIMIT 1"
        return self.connection.execute(sql, params).fetchone()

    def find_conflict(self, student_key: str, starts_at: str, ends_at: str, *, exclude_enrollment_id: int | None = None) -> sqlite3.Row | None:
        sql = (
            f"SELECT e.*,c.code AS class_code,c.course_id AS owner_course_id,c.starts_at AS class_starts_at,c.ends_at AS class_ends_at "
            "FROM training_enrollments e JOIN training_classes c ON c.id=e.class_id "
            f"WHERE e.student_key=? AND e.status IN ({','.join('?' for _ in ACTIVE_STATES)}) "
            "AND c.starts_at < ? AND c.ends_at > ?"
        )
        params: list[Any] = [student_key, *ACTIVE_STATES, ends_at, starts_at]
        if exclude_enrollment_id is not None:
            sql += " AND e.id<>?"
            params.append(exclude_enrollment_id)
        sql += " ORDER BY c.starts_at,e.id LIMIT 1"
        return self.connection.execute(sql, params).fetchone()

    def next_waitlist_seq(self, class_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(waitlist_seq),0)+1 FROM training_enrollments WHERE waitlist_class_id=?",
            (class_id,),
        ).fetchone()[0])

    def waitlist_candidates(self, class_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM training_enrollments WHERE waitlist_class_id=? AND status='waitlisted' ORDER BY priority DESC,waitlist_seq ASC,id ASC",
            (class_id,),
        ).fetchall())

    def expired_reservations(self, now: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM training_enrollments WHERE status='reserved' AND reserved_expires_at<>'' AND reserved_expires_at<=? ORDER BY reserved_expires_at,id",
            (now,),
        ).fetchall())

    def insert_enrollment(self, *, course_id: int, class_id: int, student_key: str, status: str, priority: int, waitlist_seq: int | None, waitlist_class_id: int | None, reserved_expires_at: str, successor_enrollment_id: int | None, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO training_enrollments(course_id,class_id,student_key,status,priority,waitlist_seq,waitlist_class_id,reserved_expires_at,successor_enrollment_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (course_id, class_id, student_key, status, priority, waitlist_seq, waitlist_class_id, reserved_expires_at, successor_enrollment_id, now, now),
        )
        return int(cursor.lastrowid)

    # ----- 幂等记录 -----

    def idempotency_record(self, student_key: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM training_idempotency WHERE student_key=? AND idempotency_key=?",
            (student_key, key),
        ).fetchone()

    def save_idempotency(self, *, student_key: str, key: str, operation: str, enrollment_id: int, request_digest: str, response: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO training_idempotency(student_key,idempotency_key,operation,enrollment_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (student_key, key, operation, enrollment_id, request_digest, json.dumps(response, ensure_ascii=False, sort_keys=True), now),
        )

    # ----- 事件流 -----

    def add_event(self, *, batch_key: str, event_type: str, course_id: int | None, class_id: int | None, enrollment_id: int | None, origin_event_id: int | None, actor: str, release_source: str, priority_rule: str, student_key: str, class_version_before: int | None, class_version_after: int | None, course_version_before: int | None, course_version_after: int | None, detail: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO training_events(batch_key,event_type,course_id,class_id,enrollment_id,origin_event_id,actor,release_source,priority_rule,student_key,class_version_before,class_version_after,course_version_before,course_version_after,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_key, event_type, course_id, class_id, enrollment_id, origin_event_id, actor, release_source, priority_rule, student_key, class_version_before, class_version_after, course_version_before, course_version_after, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
        return int(cursor.lastrowid)

    def event_by_id(self, event_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM training_events WHERE id=?", (event_id,)).fetchone()

    def events_by_batch(self, batch_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM training_events WHERE batch_key=? ORDER BY id", (batch_key,)).fetchall()
        return [self._event_dict(row) for row in rows]

    def events_by_enrollment(self, enrollment_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM training_events WHERE enrollment_id=? OR id IN (SELECT origin_event_id FROM training_events WHERE enrollment_id=?) ORDER BY id", (enrollment_id, enrollment_id)).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_recent_batches(self, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT batch_key,MIN(created_at) AS started_at,MAX(created_at) AS ended_at,COUNT(*) AS event_count FROM training_events GROUP BY batch_key ORDER BY MAX(id) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        return item
