"""End-to-end walk of the cross-hospital queue against a running API.

Drives the real product story: triage -> pick candidates -> join -> watch the
position -> doctor calls the patient -> consultation completes.

    python -m src.scripts.queue_demo [base_url]
"""

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PASSWORD = "MediLink@123"
# Roughly MG Road, Bengaluru -- next to the seeded City Hospital.
PATIENT_LOCATION = {"latitude": 12.9750, "longitude": 77.6060}

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def login(client: httpx.Client, email: str) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        ph = login(client, "harsh.kumar@example.com")

        # Clear any leftover entry from a previous run so this is re-runnable.
        for q in client.get("/api/queue/my-queues", headers=ph).json()["queues"]:
            if q["status"] in ("waiting", "in_consultation"):
                client.delete(f"/api/queue/cancel/{q['queue_id']}", headers=ph)

        print("\n[1. triage]")
        analysis = client.post(
            "/api/ai/analyze-symptoms",
            json={"symptoms_description": "Chest tightness and palpitations when climbing stairs"},
            headers=ph,
        ).json()
        specializations = [r["specialization"] for r in analysis["recommended_specializations"]]
        check("triage suggests a department", bool(specializations), ", ".join(specializations))

        print("\n[2. find doctors]")
        available = client.get(
            "/api/queue/available-doctors",
            params={
                "queue_type": "cross_hospital",
                "specializations": ",".join(specializations),
                "include_general": True,
                "latitude": PATIENT_LOCATION["latitude"],
                "longitude": PATIENT_LOCATION["longitude"],
            },
            headers=ph,
        ).json()
        check(
            "available doctors are returned",
            available["total_available"] > 0,
            f"{available['total_available']} doctors",
        )
        check(
            "the list is ranked with distance",
            available["doctors"][0].get("distance") is not None,
            f"nearest {available['doctors'][0]['distance']} km",
        )
        doctor_ids = [d["doctor_id"] for d in available["doctors"][:5]]

        print("\n[3. join the queue]")
        joined = client.post(
            "/api/queue/join",
            json={
                "doctor_ids": doctor_ids,
                "symptoms_description": "Chest tightness and palpitations when climbing stairs",
                "queue_type": "cross_hospital",
                "ai_recommendation": analysis,
                "patient_location": PATIENT_LOCATION,
            },
            headers=ph,
        ).json()
        check("join succeeds", joined["success"] is True, joined.get("error") or "")
        if not joined["success"]:
            print(f"\n  {passed} passed, {failed} failed")
            return 1

        queue_id = joined["queue_id"]
        check(
            "server assigned a hospital",
            joined["assigned_hospital_id"] is not None,
            joined["assigned_hospital_name"],
        )
        check("patient got a token number", joined["queue_number"] > 0, f"#{joined['queue_number']}")
        check(
            "runners-up are offered",
            isinstance(joined["alternative_options"], list),
            f"{len(joined['alternative_options'])} alternatives",
        )
        check("a leave-at time is returned", joined["leave_at"] is not None, str(joined["leave_at"]))

        print("\n[4. patient watches their place]")
        status = client.get(f"/api/queue/status/{queue_id}", headers=ph).json()
        check("status is waiting", status["status"] == "waiting")
        check(
            "position is reported", status["current_position"] >= 1, f"position {status['current_position']}"
        )
        check(
            "both hospital field spellings are served",
            status["hospital_name"] == status["assigned_hospital_name"],
        )

        mine = client.get("/api/queue/my-queues", headers=ph).json()
        check("my-queues uses an envelope", "queues" in mine, f"{len(mine['queues'])} entries")

        print("\n[5. live updates]")
        token = client.post("/api/events/token", headers=ph).json()
        check("an SSE token is issued", bool(token["token"]), f"expires in {token['expires_in']}s")
        with client.stream(
            "GET", f"/api/events/queue/{queue_id}", params={"token": token["token"]}
        ) as stream:
            check("the SSE stream opens", stream.status_code == 200, str(stream.status_code))
            check(
                "it is an event stream",
                "text/event-stream" in stream.headers.get("content-type", ""),
                stream.headers.get("content-type", ""),
            )
            first = next(stream.iter_lines(), "")
            check("the stream greets the client", "connected" in first, first[:40])

        print("\n[6. doctor calls the patient]")
        assigned_doctor = joined["doctor_id"]
        doctors = login(client, "admin@medilink.health")
        all_doctors = client.get("/api/admin/doctors", headers=doctors).json()
        email = next(d["email"] for d in all_doctors if d["id"] == assigned_doctor)
        dh = login(client, email)

        queue = client.get(f"/api/queue/doctor/{assigned_doctor}", headers=dh).json()
        check(
            "the patient appears in the doctor's queue",
            queue["total_waiting"] >= 1,
            f"{queue['total_waiting']} waiting",
        )

        started = client.patch(
            f"/api/queue/status/{queue_id}", json={"new_status": "in_consultation"}, headers=dh
        )
        check("doctor starts the consultation", started.status_code == 200, str(started.status_code))

        during = client.get(f"/api/queue/status/{queue_id}", headers=ph).json()
        check("patient sees in_consultation", during["status"] == "in_consultation")
        check("patient was marked notified", during["patient_notified_at"] is not None)

        completed = client.patch(
            f"/api/queue/status/{queue_id}", json={"new_status": "completed"}, headers=dh
        )
        check("doctor completes the consultation", completed.status_code == 200)

        print("\n[7. invariants]")
        illegal = client.patch(f"/api/queue/status/{queue_id}", json={"new_status": "waiting"}, headers=dh)
        check("completed cannot revert to waiting", illegal.status_code == 409, str(illegal.status_code))

        emergency = client.post(
            "/api/queue/join",
            json={
                "doctor_ids": doctor_ids,
                "symptoms_description": "Crushing chest pain radiating to my arm",
                "queue_type": "cross_hospital",
                "priority": "normal",
                "ai_recommendation": {"urgency_level": "emergency", "summary": "Seek care now"},
                "patient_location": PATIENT_LOCATION,
            },
            headers=ph,
        ).json()
        if emergency["success"]:
            detail = client.get(f"/api/queue/status/{emergency['queue_id']}", headers=ph).json()
            check(
                "triage emergency overrides a client-sent priority",
                detail["priority"] == "emergency" and detail["is_emergency"] is True,
                detail["priority"],
            )
            client.delete(f"/api/queue/cancel/{emergency['queue_id']}", headers=ph)

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
