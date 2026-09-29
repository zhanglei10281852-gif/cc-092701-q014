from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, init_db
from app.enroll.service import EnrollmentService

WEEK_A = ("2026-10-12T09:00:00+00:00", "2026-10-16T17:00:00+00:00")
WEEK_A_PM = ("2026-10-14T13:00:00+00:00", "2026-10-18T17:00:00+00:00")
WEEK_B = ("2026-10-19T09:00:00+00:00", "2026-10-23T17:00:00+00:00")


def make_course(client, code: str = "cnc", capacity: int = 10) -> dict:
    response = client.post("/api/enroll/courses?actor=administrator", json={"code": code, "name": "数控实训", "total_capacity": capacity})
    assert response.status_code == 201, response.text
    return response.json()


def make_class(client, course: str = "cnc", code: str = "cnc-1", quota: int = 2, window=WEEK_A) -> dict:
    response = client.post(
        "/api/enroll/classes?actor=administrator",
        json={"course_code": course, "code": code, "name": f"班级{code}", "quota": quota, "starts_at": window[0], "ends_at": window[1]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def apply(client, student: str, class_code: str = "cnc-1", key: str | None = None, ttl: int = 48) -> dict:
    response = client.post(
        "/api/enroll/reservations",
        json={"class_code": class_code, "student_id": student, "idempotency_key": key or f"apply-{student}-{class_code}", "confirm_ttl_hours": ttl},
    )
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client, reservation_id: int, actor: str = "clerk-1") -> dict:
    response = client.post(f"/api/enroll/reservations/{reservation_id}/confirm", json={"actor": actor})
    assert response.status_code == 200, response.text
    return response.json()


def test_apply_confirm_dual_decrement_and_idempotent_replay(client):
    make_course(client, capacity=1)
    class_row = make_class(client, quota=1)
    reservation = apply(client, "s-01", key="apply-key-0001")
    assert reservation["status"] == "pending"
    assert reservation["confirm_deadline"]
    replay = apply(client, "s-01", key="apply-key-0001")
    assert replay["id"] == reservation["id"]
    detail = client.get(f"/api/enroll/classes/{class_row['id']}").json()
    assert detail["pending_count"] == 1 and detail["confirmed_count"] == 0
    course = client.get("/api/enroll/courses").json()["items"][0]
    assert course["pending_count"] == 1 and course["confirmed_count"] == 0
    confirmed = confirm(client, reservation["id"])
    assert confirmed["status"] == "confirmed"
    again = confirm(client, reservation["id"])
    assert again["id"] == confirmed["id"] and again["confirmed_at"] == confirmed["confirmed_at"]
    detail = client.get(f"/api/enroll/classes/{class_row['id']}").json()
    assert detail["pending_count"] == 0 and detail["confirmed_count"] == 1
    course = client.get("/api/enroll/courses").json()["items"][0]
    assert course["pending_count"] == 0 and course["confirmed_count"] == 1
    waitlisted = apply(client, "s-02", key="apply-key-0002")
    assert waitlisted["status"] == "waitlisted" and waitlisted["position"] == 1


def test_idempotency_key_with_different_payload_conflicts(client):
    make_course(client)
    make_class(client, code="cnc-1")
    make_class(client, code="cnc-2", window=WEEK_B)
    apply(client, "s-01", class_code="cnc-1", key="shared-key-01")
    conflict = client.post(
        "/api/enroll/reservations",
        json={"class_code": "cnc-2", "student_id": "s-01", "idempotency_key": "shared-key-01", "confirm_ttl_hours": 48},
    )
    assert conflict.status_code == 409
    duplicate = client.post(
        "/api/enroll/reservations",
        json={"class_code": "cnc-1", "student_id": "s-01", "idempotency_key": "another-key-01", "confirm_ttl_hours": 48},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["message"] == "该学员在此班级已有有效预留或候补"


def test_overlapping_window_cannot_double_hold(client):
    make_course(client)
    make_class(client, code="cnc-1", window=WEEK_A)
    make_class(client, code="cnc-2", window=WEEK_A_PM)
    make_class(client, code="cnc-3", window=WEEK_B)
    first = apply(client, "s-01", class_code="cnc-1", key="overlap-key-1")
    conflict = client.post(
        "/api/enroll/reservations",
        json={"class_code": "cnc-2", "student_id": "s-01", "idempotency_key": "overlap-key-2", "confirm_ttl_hours": 48},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["conflicts"][0]["class_code"] == "cnc-1"
    ok = apply(client, "s-01", class_code="cnc-3", key="overlap-key-3")
    assert ok["status"] == "pending"
    dropped = client.post(f"/api/enroll/reservations/{first['id']}/drop", json={"actor": "clerk-1", "reason": "学员退课"})
    assert dropped.status_code == 200
    retry = apply(client, "s-01", class_code="cnc-2", key="overlap-key-2")
    assert retry["status"] == "pending"


def test_drop_promotes_waitlist_and_explains_promotion(client):
    make_course(client, capacity=5)
    class_row = make_class(client, quota=1)
    first = confirm(client, apply(client, "s-01", key="drop-key-1")["id"])
    second = apply(client, "s-02", key="drop-key-2")
    third = apply(client, "s-03", key="drop-key-3")
    assert second["status"] == "waitlisted" and second["position"] == 1
    assert third["status"] == "waitlisted" and third["position"] == 2
    dropped = client.post(f"/api/enroll/reservations/{first['id']}/drop", json={"actor": "clerk-1", "reason": "学员退课"})
    assert dropped.status_code == 200
    promotions = dropped.json()["promotions"]
    assert len(promotions) == 1 and promotions[0]["reservation_id"] == second["id"]
    explanation = client.get(f"/api/enroll/promotions/{promotions[0]['id']}").json()
    assert explanation["priority_rule"]["code"] == "fifo_position"
    assert explanation["release_source"]["type"] == "drop"
    assert explanation["release_source"]["reservation_id"] == first["id"]
    boundary = explanation["version_boundary"]
    assert boundary["class_version_after"] == boundary["class_version_before"] + 1
    assert boundary["course_version_after"] == boundary["course_version_before"] + 1
    assert boundary["class_quota"] == 1 and boundary["class_confirmed"] == 0
    promoted = client.get(f"/api/enroll/reservations/{second['id']}").json()
    assert promoted["status"] == "pending" and promoted["confirm_deadline"]
    assert [event["action"] for event in promoted["events"]] == ["waitlist", "promote"]
    confirm(client, second["id"])
    waitlist = client.get(f"/api/enroll/classes/{class_row['id']}").json()["waitlist"]
    assert [row["student_id"] for row in waitlist] == ["s-03"]
    assert waitlist[0]["position"] == 2


def test_transfer_releases_source_promotes_then_occupies_target(client):
    make_course(client, capacity=2)
    make_class(client, code="cnc-1", quota=1, window=WEEK_A)
    target = make_class(client, code="cnc-2", quota=1, window=WEEK_A_PM)
    first = confirm(client, apply(client, "s-01", class_code="cnc-1", key="xfer-key-1")["id"])
    waiting = apply(client, "s-02", class_code="cnc-1", key="xfer-key-2")
    assert waiting["status"] == "waitlisted"
    moved = client.post(
        f"/api/enroll/reservations/{first['id']}/transfer",
        json={"to_class_code": "cnc-2", "actor": "clerk-1", "reason": "学员申请转班", "idempotency_key": "xfer-req-0001"},
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["source"]["status"] == "released" and body["source"]["release_reason"] == "transfer_out"
    assert body["target"]["status"] == "confirmed" and body["target"]["class_code"] == "cnc-2"
    assert body["target"]["source_reservation_id"] == first["id"]
    assert [item["reservation_id"] for item in body["promotions"]] == [waiting["id"]]
    replay = client.post(
        f"/api/enroll/reservations/{first['id']}/transfer",
        json={"to_class_code": "cnc-2", "actor": "clerk-1", "reason": "学员申请转班", "idempotency_key": "xfer-req-0001"},
    )
    assert replay.status_code == 200
    assert replay.json()["target"]["id"] == body["target"]["id"]
    conflict = client.post(
        f"/api/enroll/reservations/{first['id']}/transfer",
        json={"to_class_code": "cnc-1", "actor": "clerk-1", "reason": "换目标", "idempotency_key": "xfer-req-0001"},
    )
    assert conflict.status_code == 409
    source_class = client.get("/api/enroll/classes").json()["items"]
    by_code = {row["code"]: row for row in source_class}
    assert by_code["cnc-1"]["pending_count"] == 1 and by_code["cnc-1"]["confirmed_count"] == 0
    assert by_code["cnc-2"]["confirmed_count"] == 1
    promoted = client.get(f"/api/enroll/reservations/{waiting['id']}").json()
    assert promoted["status"] == "pending"
    explanation = client.get(f"/api/enroll/classes/{by_code['cnc-1']['id']}/promotions").json()["items"][0]
    assert explanation["release_source"]["type"] == "transfer_out"
    assert target["code"] == "cnc-2"


def test_course_capacity_bounds_classes_and_expansion_promotes(client):
    course = make_course(client, capacity=1)
    make_class(client, code="cnc-1", quota=1)
    make_class(client, code="cnc-2", quota=1, window=WEEK_B)
    confirm(client, apply(client, "s-01", class_code="cnc-1", key="cap-key-1")["id"])
    waiting = apply(client, "s-02", class_code="cnc-2", key="cap-key-2")
    assert waiting["status"] == "waitlisted"
    shrink = client.put(f"/api/enroll/courses/{course['id']}/capacity?actor=administrator", json={"capacity": 0, "reason": "临时缩容"})
    assert shrink.status_code == 409
    expanded = client.put(f"/api/enroll/courses/{course['id']}/capacity?actor=administrator", json={"capacity": 2, "reason": "增开设备到位"})
    assert expanded.status_code == 200
    promotions = expanded.json()["promotions"]
    assert len(promotions) == 1 and promotions[0]["reservation_id"] == waiting["id"]
    explanation = client.get(f"/api/enroll/promotions/{promotions[0]['id']}").json()
    assert explanation["release_source"]["type"] == "capacity_expand"
    assert explanation["version_boundary"]["course_capacity"] == 2


def test_class_quota_adjust_expand_and_shrink_guard(client):
    make_course(client, capacity=5)
    class_row = make_class(client, quota=1)
    confirm(client, apply(client, "s-01", key="quota-key-1")["id"])
    waiting = apply(client, "s-02", key="quota-key-2")
    shrink = client.put(f"/api/enroll/classes/{class_row['id']}/quota?actor=administrator", json={"quota": 0, "reason": "压缩名额"})
    assert shrink.status_code == 409
    expanded = client.put(f"/api/enroll/classes/{class_row['id']}/quota?actor=administrator", json={"quota": 2, "reason": "扩班"})
    assert expanded.status_code == 200
    assert [item["reservation_id"] for item in expanded.json()["promotions"]] == [waiting["id"]]
    explanation = client.get(f"/api/enroll/classes/{class_row['id']}/promotions").json()["items"][0]
    assert explanation["release_source"]["type"] == "quota_expand"


def test_expired_pending_sweep_and_confirm_after_expiry(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service = EnrollmentService(get_connection(), clock)
    service.create_course({"code": "ecom", "name": "电商实训", "total_capacity": 5}, "administrator")
    service.create_class({"course_code": "ecom", "code": "ecom-1", "name": "电商一班", "quota": 1, "starts_at": WEEK_A[0], "ends_at": WEEK_A[1]}, "administrator")
    first = service.apply({"class_code": "ecom-1", "student_id": "s-01", "idempotency_key": "sweep-key-1", "confirm_ttl_hours": 1})
    second = service.apply({"class_code": "ecom-1", "student_id": "s-02", "idempotency_key": "sweep-key-2", "confirm_ttl_hours": 48})
    assert first["status"] == "pending" and second["status"] == "waitlisted"
    clock.advance(hours=2)
    swept = service.expire_pending()
    assert swept["released"] == [first["id"]]
    assert [item["reservation_id"] for item in swept["promotions"]] == [second["id"]]
    expired = service.get_reservation(first["id"])
    assert expired["status"] == "released" and expired["release_reason"] == "expired"
    assert [event["action"] for event in expired["events"]] == ["apply", "expire"]
    promoted = service.get_reservation(second["id"])
    assert promoted["status"] == "pending"
    clock.advance(hours=49)
    try:
        service.confirm(second["id"], "clerk-1")
        raise AssertionError("确认过期预留应当失败")
    except Exception as exc:
        assert getattr(exc, "code", "") == "conflict"
    assert service.get_reservation(second["id"])["release_reason"] == "expired"


def test_waitlist_positions_and_occupancy_survive_restart(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service = EnrollmentService(get_connection(), clock)
    service.create_course({"code": "cnc", "name": "数控实训", "total_capacity": 5}, "administrator")
    class_row = service.create_class({"course_code": "cnc", "code": "cnc-1", "name": "数控一班", "quota": 1, "starts_at": WEEK_A[0], "ends_at": WEEK_A[1]}, "administrator")
    first = service.apply({"class_code": "cnc-1", "student_id": "s-01", "idempotency_key": "boot-key-1", "confirm_ttl_hours": 48})
    service.confirm(first["id"], "clerk-1")
    second = service.apply({"class_code": "cnc-1", "student_id": "s-02", "idempotency_key": "boot-key-2", "confirm_ttl_hours": 48})
    third = service.apply({"class_code": "cnc-1", "student_id": "s-03", "idempotency_key": "boot-key-3", "confirm_ttl_hours": 48})
    close_connection()
    restarted = EnrollmentService(get_connection(), clock)
    detail = restarted.get_class(class_row["id"])
    assert detail["confirmed_count"] == 1 and detail["pending_count"] == 0
    assert [(row["student_id"], row["position"]) for row in detail["waitlist"]] == [("s-02", 1), ("s-03", 2)]
    dropped = restarted.drop(first["id"], "clerk-1", "学员退课")
    assert [item["reservation_id"] for item in dropped["promotions"]] == [second["id"]]
    detail = restarted.get_class(class_row["id"])
    assert [(row["student_id"], row["position"]) for row in detail["waitlist"]] == [("s-03", 2)]
    assert restarted.get_reservation(third["id"])["status"] == "waitlisted"
