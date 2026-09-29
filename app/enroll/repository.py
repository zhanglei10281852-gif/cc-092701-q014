from __future__ import annotations

import json
import sqlite3
from typing import Any

ACTIVE_STATUSES = ("pending", "confirmed", "waitlisted")


class EnrollmentRepository:
    """封装报名预留领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 课程与班级 ----

    def course_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM enroll_courses WHERE code=?", (code,)).fetchone()

    def course_by_id(self, course_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM enroll_courses WHERE id=?", (course_id,)).fetchone()

    def list_courses(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM enroll_courses ORDER BY code,id").fetchall()
        return [dict(row) for row in rows]

    def create_course(self, *, code: str, name: str, total_capacity: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO enroll_courses(code,name,total_capacity,created_at,updated_at) VALUES(?,?,?,?,?)",
            (code, name, total_capacity, now, now),
        )
        return dict(self.course_by_id(cursor.lastrowid))

    def class_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT c.*,co.code AS course_code,co.name AS course_name,co.total_capacity AS course_capacity,"
            "co.pending_count AS course_pending,co.confirmed_count AS course_confirmed,co.active AS course_active "
            "FROM enroll_classes c JOIN enroll_courses co ON co.id=c.course_id WHERE c.code=?",
            (code,),
        ).fetchone()

    def class_by_id(self, class_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT c.*,co.code AS course_code,co.name AS course_name,co.total_capacity AS course_capacity,"
            "co.pending_count AS course_pending,co.confirmed_count AS course_confirmed,co.active AS course_active "
            "FROM enroll_classes c JOIN enroll_courses co ON co.id=c.course_id WHERE c.id=?",
            (class_id,),
        ).fetchone()

    def list_classes(self, course_code: str | None = None) -> list[dict[str, Any]]:
        if course_code:
            rows = self.connection.execute(
                "SELECT c.*,co.code AS course_code FROM enroll_classes c JOIN enroll_courses co ON co.id=c.course_id WHERE co.code=? ORDER BY c.code,c.id",
                (course_code,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT c.*,co.code AS course_code FROM enroll_classes c JOIN enroll_courses co ON co.id=c.course_id ORDER BY c.code,c.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def classes_of_course(self, course_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM enroll_classes WHERE course_id=? ORDER BY id", (course_id,)).fetchall()

    def create_class(self, *, course_id: int, code: str, name: str, quota: int, starts_at: str, ends_at: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO enroll_classes(course_id,code,name,quota,starts_at,ends_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (course_id, code, name, quota, starts_at, ends_at, now, now),
        )
        return dict(self.class_by_id(cursor.lastrowid))

    # ---- 预留 ----

    def reservation_by_id(self, reservation_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT r.*,c.code AS class_code,c.name AS class_name,c.starts_at,c.ends_at,co.code AS course_code "
            "FROM enroll_reservations r JOIN enroll_classes c ON c.id=r.class_id JOIN enroll_courses co ON co.id=r.course_id "
            "WHERE r.id=?",
            (reservation_id,),
        ).fetchone()

    def reservation_by_idempotency(self, student_id: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM enroll_reservations WHERE student_id=? AND idempotency_key=?", (student_id, key)
        ).fetchone()

    def active_reservation(self, class_id: int, student_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM enroll_reservations WHERE class_id=? AND student_id=? AND status IN ('pending','confirmed','waitlisted')",
            (class_id, student_id),
        ).fetchone()

    def student_active_overlaps(self, student_id: str, starts_at: str, ends_at: str, *, exclude_reservation_id: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [student_id, ends_at, starts_at]
        condition = ""
        if exclude_reservation_id is not None:
            condition = " AND r.id<>?"
            params.append(exclude_reservation_id)
        rows = self.connection.execute(
            "SELECT r.id AS reservation_id,r.status,c.code AS class_code,c.name AS class_name,c.starts_at,c.ends_at "
            "FROM enroll_reservations r JOIN enroll_classes c ON c.id=r.class_id "
            "WHERE r.student_id=? AND r.status IN ('pending','confirmed') AND c.starts_at<? AND c.ends_at>?" + condition + " "
            "ORDER BY c.starts_at,c.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def create_reservation(self, *, class_id: int, course_id: int, student_id: str, status: str, position: int | None, idempotency_key: str, request_digest: str, confirm_ttl_hours: int, confirm_deadline: str | None, confirmed_at: str | None, source_reservation_id: int | None, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO enroll_reservations(class_id,course_id,student_id,status,position,idempotency_key,request_digest,confirm_ttl_hours,confirm_deadline,confirmed_at,source_reservation_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, course_id, student_id, status, position, idempotency_key, request_digest, confirm_ttl_hours, confirm_deadline, confirmed_at, source_reservation_id, now, now),
        )
        return dict(self.reservation_by_id(cursor.lastrowid))

    def waitlisted(self, class_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM enroll_reservations WHERE class_id=? AND status='waitlisted' ORDER BY position ASC,id ASC",
            (class_id,),
        ).fetchall()

    def expired_pending(self, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM enroll_reservations WHERE status='pending' AND confirm_deadline<>'' AND confirm_deadline<=? ORDER BY id ASC",
            (now,),
        ).fetchall()

    def student_reservations(self, student_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT r.*,c.code AS class_code,c.starts_at,c.ends_at,co.code AS course_code "
            "FROM enroll_reservations r JOIN enroll_classes c ON c.id=r.class_id JOIN enroll_courses co ON co.id=r.course_id "
            "WHERE r.student_id=? ORDER BY r.id DESC",
            (student_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 事件与递补 ----

    def add_event(self, *, reservation_id: int | None, class_id: int, course_id: int, student_id: str, action: str, actor: str, reason: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO enroll_events(reservation_id,class_id,course_id,student_id,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (reservation_id, class_id, course_id, student_id, action, actor, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def events_of_reservation(self, reservation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM enroll_events WHERE reservation_id=? ORDER BY id", (reservation_id,)).fetchall()
        return [dict(row) for row in rows]

    def events_of_class(self, class_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM enroll_events WHERE class_id=? ORDER BY id DESC LIMIT ?", (class_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def add_promotion(self, *, class_id: int, course_id: int, reservation_id: int, student_id: str, position: int, rule_code: str, rule_detail: str, release_source: dict[str, Any], class_version_before: int, class_version_after: int, course_version_before: int, course_version_after: int, class_quota: int, class_confirmed: int, course_capacity: int, course_confirmed: int, confirm_deadline: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO enroll_promotions(class_id,course_id,reservation_id,student_id,position,rule_code,rule_detail,release_source_json,"
            "class_version_before,class_version_after,course_version_before,course_version_after,class_quota,class_confirmed,course_capacity,course_confirmed,confirm_deadline,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, course_id, reservation_id, student_id, position, rule_code, rule_detail, json.dumps(release_source, ensure_ascii=False, sort_keys=True),
             class_version_before, class_version_after, course_version_before, course_version_after, class_quota, class_confirmed, course_capacity, course_confirmed, confirm_deadline, now),
        )
        return dict(self.promotion_by_id(cursor.lastrowid))

    def promotion_by_id(self, promotion_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT p.*,c.code AS class_code,co.code AS course_code FROM enroll_promotions p "
            "JOIN enroll_classes c ON c.id=p.class_id JOIN enroll_courses co ON co.id=p.course_id WHERE p.id=?",
            (promotion_id,),
        ).fetchone()

    def promotions_of_class(self, class_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT p.*,c.code AS class_code,co.code AS course_code FROM enroll_promotions p "
            "JOIN enroll_classes c ON c.id=p.class_id JOIN enroll_courses co ON co.id=p.course_id WHERE p.class_id=? ORDER BY p.id",
            (class_id,),
        ).fetchall()
        return [dict(row) for row in rows]
