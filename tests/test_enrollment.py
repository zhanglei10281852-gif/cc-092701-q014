from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.enrollment.service import PRIORITY_RULE, EnrollmentService


COURSE = {
    "code": "cnc-2026-fall",
    "name": "数控实训",
    "term_label": "2026 秋季",
    "starts_at": "2026-09-01T00:00:00+00:00",
    "ends_at": "2026-12-31T00:00:00+00:00",
    "total_capacity": 3,
    "reservation_seconds": 3600,
}


def make_course(client, **overrides) -> dict:
    payload = {**COURSE, **overrides}
    response = client.post("/api/enrollment/courses?actor=dean", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def make_class(client, code: str, *, capacity: int = 2, starts: str = "2026-09-01T01:00:00+00:00", ends: str = "2026-09-01T03:00:00+00:00", course: str = "cnc-2026-fall", name: str | None = None) -> dict:
    response = client.post(
        f"/api/enrollment/courses/{course}/classes?actor=dean",
        json={"code": code, "name": name or f"班级{code}", "starts_at": starts, "ends_at": ends, "capacity": capacity},
    )
    assert response.status_code == 201, response.text
    return response.json()


def reserve(client, class_code: str, student: str, key: str, *, course: str = "cnc-2026-fall", priority: int = 50):
    return client.post(
        "/api/enrollment/reservations",
        json={"course_code": course, "class_code": class_code, "student_key": student, "priority": priority, "idempotency_key": key},
    )


def test_reserve_and_confirm_atomically_decrements_both_counters(client):
    make_course(client)
    make_class(client, "c1", capacity=2)
    first = reserve(client, "c1", "stu-1", "reserve-000001")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "reserved"
    detail = client.get("/api/enrollment/courses/cnc-2026-fall").json()
    assert detail["reserved_count"] == 1 and detail["confirmed_count"] == 0
    clazz = next(item for item in detail["classes"] if item["code"] == "c1")
    assert clazz["reserved_count"] == 1
    enrollment_id = first.json()["enrollment_id"]
    confirmed = client.post(
        "/api/enrollment/confirmations",
        json={"enrollment_id": enrollment_id, "student_key": "stu-1", "idempotency_key": "confirm-000001"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    detail = client.get("/api/enrollment/courses/cnc-2026-fall").json()
    assert detail["reserved_count"] == 0 and detail["confirmed_count"] == 1
    clazz = next(item for item in detail["classes"] if item["code"] == "c1")
    assert clazz["reserved_count"] == 0 and clazz["confirmed_count"] == 1


def test_class_full_goes_waitlisted_and_cancel_triggers_priority_promotion(client):
    make_course(client)
    make_class(client, "c1", capacity=2)
    reserve(client, "c1", "stu-1", "reserve-000001")
    reserve(client, "c1", "stu-2", "reserve-000002")
    low = reserve(client, "c1", "stu-3", "reserve-000003", priority=10)
    high = reserve(client, "c1", "stu-4", "reserve-000004", priority=90)
    assert low.json()["status"] == "waitlisted" and low.json()["waitlist_seq"] == 1
    assert high.json()["status"] == "waitlisted" and high.json()["waitlist_seq"] == 2

    waiting = client.get("/api/enrollment/courses/cnc-2026-fall/classes/c1/waitlist").json()
    assert [item["student_key"] for item in waiting["items"]] == ["stu-4", "stu-3"]
    assert waiting["priority_rule"] == PRIORITY_RULE

    cancelled = client.post(
        "/api/enrollment/cancellations",
        json={"enrollment_id": 1, "student_key": "stu-1", "reason": "学员主动退课", "idempotency_key": "cancel-000001"},
    )
    assert cancelled.status_code == 200
    promoted = client.get("/api/enrollment/enrollments", params={"student_key": "stu-4"}).json()["items"]
    assert promoted[0]["status"] == "reserved"
    # 第二名候补仍在等待
    still_waiting = client.get("/api/enrollment/enrollments", params={"student_key": "stu-3"}).json()["items"]
    assert still_waiting[0]["status"] == "waitlisted"

    trace = client.get(f"/api/enrollment/enrollments/{promoted[0]['id']}/trace").json()
    promote_events = [event for event in trace["events"] if event["event_type"] == "promote"]
    assert len(promote_events) == 1
    event = promote_events[0]
    assert event["release_source"] == "cancel"
    assert event["priority_rule"] == PRIORITY_RULE
    assert event["class_version_after"] == event["class_version_before"] + 1
    assert event["course_version_after"] == event["course_version_before"] + 1


def test_overlapping_time_window_rejects_duplicate_hold(client):
    make_course(client, total_capacity=10)
    make_class(client, "morning", capacity=5, starts="2026-09-01T01:00:00+00:00", ends="2026-09-01T04:00:00+00:00")
    make_class(client, "overlap", capacity=5, starts="2026-09-01T03:00:00+00:00", ends="2026-09-01T05:00:00+00:00")
    make_class(client, "afternoon", capacity=5, starts="2026-09-01T04:00:00+00:00", ends="2026-09-01T06:00:00+00:00")
    assert reserve(client, "morning", "stu-1", "reserve-000001").status_code == 202
    blocked = reserve(client, "overlap", "stu-1", "reserve-000002")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conflict"
    # 首尾相接不算重叠
    adjacent = reserve(client, "afternoon", "stu-1", "reserve-000003")
    assert adjacent.status_code == 202
    # 同班重复占位也拒绝
    duplicate = reserve(client, "morning", "stu-1", "reserve-000004")
    assert duplicate.status_code == 409


def test_idempotent_repeat_reuses_original_result(client):
    make_course(client)
    make_class(client, "c1", capacity=1)
    first = reserve(client, "c1", "stu-1", "reserve-000001")
    replay = reserve(client, "c1", "stu-1", "reserve-000001")
    assert first.json()["enrollment_id"] == replay.json()["enrollment_id"]
    assert replay.json()["replayed"] is True
    # 同一幂等键携带不同请求被拒绝
    different = client.post(
        "/api/enrollment/reservations",
        json={"course_code": "cnc-2026-fall", "class_code": "c1", "student_key": "stu-1", "priority": 99, "idempotency_key": "reserve-000001"},
    )
    assert different.status_code == 409
    # 满员后第二个学员用新键候补；不会因为前者的幂等键而串号
    other = reserve(client, "c1", "stu-2", "reserve-000002")
    assert other.json()["status"] == "waitlisted"
    other_replay = reserve(client, "c1", "stu-2", "reserve-000002")
    assert other_replay.json()["enrollment_id"] == other.json()["enrollment_id"]


def test_expired_unconfirmed_releases_in_deterministic_order_and_promotes(client):
    clock = FrozenClock(datetime(2026, 9, 1, 1, 0, tzinfo=UTC))
    service = EnrollmentService(get_connection(), clock)
    service.create_course(COURSE, "dean")
    service.create_class(
        {"course_code": "cnc-2026-fall", "code": "c1", "name": "数控一班",
         "starts_at": "2026-09-01T01:00:00+00:00", "ends_at": "2026-09-01T03:00:00+00:00", "capacity": 2},
        "dean",
    )
    service.reserve({"course_code": "cnc-2026-fall", "class_code": "c1", "student_key": "stu-1", "priority": 50, "idempotency_key": "res-1"})
    clock.advance(seconds=600)
    service.reserve({"course_code": "cnc-2026-fall", "class_code": "c1", "student_key": "stu-2", "priority": 50, "idempotency_key": "res-2"})
    service.reserve({"course_code": "cnc-2026-fall", "class_code": "c1", "student_key": "stu-3", "priority": 50, "idempotency_key": "res-3"})

    clock.advance(seconds=3001)  # stu-1 预留到期（3600s），stu-2 未到期
    result = service.expire_unconfirmed()
    assert result["expired_enrollment_ids"] == [1]
    waitlister = service.list_enrollments(student_key="stu-3")[0]
    assert waitlister["status"] == "reserved"
    detail = service.course_detail("cnc-2026-fall")
    assert detail["reserved_count"] == 2  # stu-2 + 递补的 stu-3

    batch = service.batch_trace(result["batch_key"])
    releases = [event for event in batch["events"] if event["event_type"] == "release"]
    promotes = batch["promotions"]
    assert releases[0]["release_source"] == "expire"
    assert promotes[0]["release_source"] == "expire"
    assert promotes[0]["origin_event_id"] == releases[0]["id"]
    assert promotes[0]["student_key"] == "stu-3"


def test_late_confirm_after_expiry_is_rejected_but_replays_same_conclusion(client):
    clock = FrozenClock(datetime(2026, 9, 1, 1, 0, tzinfo=UTC))
    service = EnrollmentService(get_connection(), clock)
    service.create_course(COURSE, "dean")
    service.create_class(
        {"course_code": "cnc-2026-fall", "code": "c1", "name": "数控一班",
         "starts_at": "2026-09-01T01:00:00+00:00", "ends_at": "2026-09-01T03:00:00+00:00", "capacity": 1},
        "dean",
    )
    reserved = service.reserve({"course_code": "cnc-2026-fall", "class_code": "c1", "student_key": "stu-1", "priority": 50, "idempotency_key": "res-1"})
    clock.advance(seconds=3601)
    payload = {"enrollment_id": reserved["enrollment_id"], "student_key": "stu-1", "idempotency_key": "confirm-late"}
    with pytest.raises(Exception) as exc_info:
        service.confirm(payload)
    assert "过期" in exc_info.value.message
    # 重复请求复用原结论
    with pytest.raises(Exception) as replay_info:
        service.confirm(payload)
    assert replay_info.value.context.get("replayed") is True


def test_transfer_with_capacity_releases_source_and_promotes(client):
    make_course(client, total_capacity=10)
    make_class(client, "c1", capacity=2)
    make_class(client, "c2", capacity=2, starts="2026-09-02T01:00:00+00:00", ends="2026-09-02T03:00:00+00:00")
    first = reserve(client, "c1", "stu-1", "reserve-000001")
    reserve(client, "c1", "stu-2", "reserve-000002")
    waiter = reserve(client, "c1", "stu-3", "reserve-000003")
    assert waiter.json()["status"] == "waitlisted"

    moved = client.post(
        "/api/enrollment/transfers",
        json={
            "enrollment_id": first.json()["enrollment_id"],
            "student_key": "stu-1",
            "target_course_code": "cnc-2026-fall",
            "target_class_code": "c2",
            "reason": "时间冲突申请转班",
            "idempotency_key": "transfer-000001",
        },
    )
    assert moved.status_code == 202, moved.text
    body = moved.json()
    assert body["status"] == "reserved" and body["source_enrollment_id"] == first.json()["enrollment_id"]
    # 原班候补递补
    promoted = client.get("/api/enrollment/enrollments", params={"student_key": "stu-3"}).json()["items"][0]
    assert promoted["status"] == "reserved"
    # 原记录标记 transferred
    old = client.get(f"/api/enrollment/enrollments/{first.json()['enrollment_id']}/trace").json()["enrollment"]
    assert old["status"] == "transferred" and old["release_source"] == "transfer"


def test_transfer_to_full_class_keeps_source_and_creates_waitlist(client):
    make_course(client, total_capacity=10)
    make_class(client, "c1", capacity=1, starts="2026-09-01T01:00:00+00:00", ends="2026-09-01T03:00:00+00:00")
    make_class(client, "c2", capacity=1, starts="2026-09-02T01:00:00+00:00", ends="2026-09-02T03:00:00+00:00")
    reserve(client, "c2", "stu-x", "reserve-x")
    hold = reserve(client, "c1", "stu-1", "reserve-000001")
    moved = client.post(
        "/api/enrollment/transfers",
        json={
            "enrollment_id": hold.json()["enrollment_id"],
            "student_key": "stu-1",
            "target_course_code": "cnc-2026-fall",
            "target_class_code": "c2",
            "reason": "希望转班",
            "idempotency_key": "transfer-000001",
        },
    )
    assert moved.status_code == 202
    assert moved.json()["status"] == "waitlisted"
    # 原名额保留
    old = client.get(f"/api/enrollment/enrollments/{hold.json()['enrollment_id']}/trace").json()["enrollment"]
    assert old["status"] == "reserved"


def test_capacity_increase_promotes_waitlist_with_traceable_batch(client):
    make_course(client, total_capacity=1)
    make_class(client, "c1", capacity=1)
    reserve(client, "c1", "stu-1", "reserve-000001", priority=10)
    reserve(client, "c1", "stu-2", "reserve-000002", priority=90)
    adjusted = client.put(
        "/api/enrollment/courses/cnc-2026-fall/capacity?class_code=c1",
        json={"course_capacity": 2, "class_capacity": 2, "actor": "dean", "reason": "临时扩班增加名额"},
    )
    assert adjusted.status_code == 200, adjusted.text
    batch_key = adjusted.json()["batch_key"]
    promoted = client.get("/api/enrollment/enrollments", params={"student_key": "stu-2"}).json()["items"][0]
    assert promoted["status"] == "reserved"
    trace = client.get(f"/api/enrollment/events/batches/{batch_key}").json()
    assert trace["priority_rule"] == PRIORITY_RULE
    promotion = trace["promotions"][0]
    assert promotion["student_key"] == "stu-2"
    assert promotion["release_source"] == "capacity_increase"
    assert promotion["class_version_before"] + 1 == promotion["class_version_after"]


def test_waitlist_rank_and_occupancy_survive_service_restart(client):
    make_course(client)
    make_class(client, "c1", capacity=1)
    reserve(client, "c1", "stu-1", "reserve-000001")
    reserve(client, "c1", "stu-2", "reserve-000002", priority=40)
    reserve(client, "c1", "stu-3", "reserve-000003", priority=80)
    # 模拟服务重启：关闭连接后用全新连接打开服务
    close_connection()
    restarted = EnrollmentService()
    detail = restarted.course_detail("cnc-2026-fall")
    assert detail["reserved_count"] == 1
    waiting = restarted.waitlist("cnc-2026-fall", "c1")
    assert [(item["student_key"], item["waitlist_seq"]) for item in waiting["items"]] == [("stu-3", 2), ("stu-2", 1)]
    close_connection()


def test_batch_trace_explains_priority_source_and_version_boundaries(client):
    make_course(client)
    make_class(client, "c1", capacity=1)
    reserve(client, "c1", "stu-1", "reserve-000001")
    reserve(client, "c1", "stu-2", "reserve-000002", priority=70)
    cancelled = client.post(
        "/api/enrollment/cancellations",
        json={"enrollment_id": 1, "student_key": "stu-1", "reason": "退课", "idempotency_key": "cancel-000001"},
    )
    batch_key = cancelled.json()["batch_key"]
    trace = client.get(f"/api/enrollment/events/batches/{batch_key}").json()
    event_types = [event["event_type"] for event in trace["events"]]
    assert event_types[:2] == ["release", "promote"]
    promotion = trace["promotions"][0]
    assert promotion["priority_rule"] == PRIORITY_RULE
    assert promotion["release_source"] == "cancel"
    assert promotion["class_version_before"] is not None
    assert promotion["class_version_after"] > promotion["class_version_before"]
    assert promotion["course_version_after"] > promotion["course_version_before"]
    # 批次列表与规则元信息
    batches = client.get("/api/enrollment/events/batches").json()
    assert any(item["batch_key"] == batch_key for item in batches["items"])
    meta = client.get("/api/enrollment/meta/rules").json()
    assert "waitlist_priority_rule" in meta and "release_order" in meta and "version_boundary" in meta


def test_course_total_capacity_blocks_promotion_across_classes(client):
    make_course(client, total_capacity=2)
    make_class(client, "c1", capacity=2)
    make_class(client, "c2", capacity=2, starts="2026-09-02T01:00:00+00:00", ends="2026-09-02T03:00:00+00:00")
    reserve(client, "c1", "stu-1", "reserve-000001")
    reserve(client, "c2", "stu-2", "reserve-000002")
    # 课程总量已满，c1 候补无法递补
    waiting = reserve(client, "c1", "stu-3", "reserve-000003")
    assert waiting.json()["status"] == "waitlisted"
    cancelled = client.post(
        "/api/enrollment/cancellations",
        json={"enrollment_id": 2, "student_key": "stu-2", "reason": "退课", "idempotency_key": "cancel-000002"},
    )
    assert cancelled.status_code == 200
    promoted = client.get("/api/enrollment/enrollments", params={"student_key": "stu-3"}).json()["items"][0]
    assert promoted["status"] == "reserved"


def test_concurrent_reservations_never_oversell_class_capacity(client):
    import threading

    make_course(client, total_capacity=20)
    make_class(client, "c1", capacity=3)
    results: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        from app.database import close_connection
        close_connection()  # 每个线程使用独立连接
        service = EnrollmentService()
        barrier.wait()
        try:
            response = service.reserve({
                "course_code": "cnc-2026-fall", "class_code": "c1",
                "student_key": f"stu-{index}", "priority": 50,
                "idempotency_key": f"concurrent-{index:06d}",
            })
            results.append(1 if response["status"] == "reserved" else 0)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            close_connection()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sum(results) == 3
    detail = client.get("/api/enrollment/courses/cnc-2026-fall").json()
    clazz = next(item for item in detail["classes"] if item["code"] == "c1")
    assert clazz["reserved_count"] == 3
    waiting = client.get("/api/enrollment/courses/cnc-2026-fall/classes/c1/waitlist").json()
    assert len(waiting["items"]) == 5


def test_cancel_is_idempotent_and_transfer_rejects_overlap(client):
    make_course(client, total_capacity=10)
    make_class(client, "c1", capacity=2, starts="2026-09-01T01:00:00+00:00", ends="2026-09-01T03:00:00+00:00")
    make_class(client, "c2", capacity=2, starts="2026-09-01T02:00:00+00:00", ends="2026-09-01T04:00:00+00:00")
    make_class(client, "c3", capacity=2, starts="2026-09-02T01:00:00+00:00", ends="2026-09-02T03:00:00+00:00")
    make_class(client, "c4", capacity=2, starts="2026-09-01T02:30:00+00:00", ends="2026-09-01T03:30:00+00:00")
    first = reserve(client, "c1", "stu-1", "reserve-000001")
    other = reserve(client, "c2", "stu-1", "reserve-000002")
    assert other.status_code == 409  # 与 c1 重叠
    cancel_payload = {"enrollment_id": first.json()["enrollment_id"], "student_key": "stu-1", "reason": "退课", "idempotency_key": "cancel-000001"}
    cancelled = client.post("/api/enrollment/cancellations", json=cancel_payload)
    replay = client.post("/api/enrollment/cancellations", json=cancel_payload)
    assert cancelled.json()["enrollment_id"] == replay.json()["enrollment_id"]
    assert replay.json()["replayed"] is True
    # 转班到时段重叠的班级仍被拒绝
    another = reserve(client, "c3", "stu-2", "reserve-000003")
    held = reserve(client, "c4", "stu-2", "reserve-000004")
    assert held.status_code == 202
    overlap_transfer = client.post(
        "/api/enrollment/transfers",
        json={
            "enrollment_id": another.json()["enrollment_id"],
            "student_key": "stu-2",
            "target_course_code": "cnc-2026-fall",
            "target_class_code": "c1",
            "reason": "申请转入",
            "idempotency_key": "transfer-000001",
        },
    )
    assert overlap_transfer.status_code == 409
