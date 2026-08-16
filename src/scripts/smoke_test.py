"""End-to-end smoke test against a running API.

Walks the phase-1 story the way the frontend does:
  patient login -> read own chart -> issue a prescription QR ->
  pharmacy redeems it -> dispenses -> prescription is burnt ->
  admin creates a hospital + doctor -> doctor writes a record and an Rx.

    python -m src.scripts.smoke_test [base_url]
"""

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PASSWORD = "MediLink@123"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def login(client: httpx.Client, email: str, password: str = PASSWORD) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=20.0) as client:
        print("\n[health]")
        health = client.get("/api/v1/health").json()
        check("health reports the database is reachable", health["status"] == "healthy")

        print("\n[patient]")
        ph = login(client, "ravi.subramanian@example.com")
        me = client.get("/api/auth/me", headers=ph).json()
        check("auth/me returns the patient role", me["role"] == "patient")

        profile = client.get("/api/patients/me", headers=ph).json()
        check("profile carries a UHID", profile["uhid"].startswith("ML"), profile["uhid"])
        patient_id = profile["id"]

        records = client.get("/api/patients/me/medical-records", headers=ph).json()
        check("medical records are returned", len(records) >= 1)
        check(
            "doctor_name is flattened onto the record",
            bool(records and records[0].get("doctor_name")),
            records[0].get("doctor_name") if records else "",
        )

        labs = client.get("/api/patients/me/lab-reports", headers=ph).json()
        check("lab reports are returned", len(labs) >= 1)
        check(
            "lab status uses the openapi vocabulary",
            bool(labs) and labs[0]["status"] in {"requested", "in_progress", "completed", "cancelled"},
            labs[0]["status"] if labs else "",
        )

        rx = client.get("/api/patients/me/prescriptions", headers=ph).json()
        check("prescriptions are returned", len(rx) >= 1)
        check(
            "medicine_name is flattened for the Rx list",
            bool(rx) and bool(rx[0]["medicines"][0].get("medicine_name")),
        )
        # Mint a fresh prescription to dispense below. Reusing the seeded one would
        # make this script pass only on the first run against a given database.
        dh_early = login(client, "ananya.rao@medilink.health")
        fresh = client.post(
            "/api/doctor/prescriptions",
            json={
                "patient_id": patient_id,
                "notes": "Smoke test prescription",
                "medicines": [
                    {
                        "medicine_id": 1,
                        "dosage": "1-0-1",
                        "frequency": "Twice daily",
                        "duration": "5 days",
                        "quantity_prescribed": 10,
                    }
                ],
            },
            headers=dh_early,
        )
        check("doctor writes a prescription", fresh.status_code == 201, str(fresh.status_code))
        prescription_id = fresh.json()["id"]

        print("\n[qr]")
        qr = client.post(
            "/api/patients/me/qr-access",
            json={"access_type": "prescription_only", "duration_hours": 0.5},
            headers=ph,
        ).json()
        import base64

        png = base64.b64decode(qr["qr_code"])
        check("qr_code is a bare base64 PNG", png[:4] == b"\x89PNG" and not qr["qr_code"].startswith("data:"))
        check("fractional duration_hours accepted", True, "0.5h")
        token = qr["access_token"]

        print("\n[pharmacy]")
        fh = login(client, "pharmacy@medilink.health")
        by_qr = client.get(f"/api/pharmacy/prescriptions/qr/{token}", headers=fh)
        check("pharmacy redeems the QR", by_qr.status_code == 200, str(by_qr.status_code))

        info = client.get(f"/api/pharmacy/patient-info/qr/{token}", headers=fh)
        check("pharmacy reads patient info from the QR", info.status_code == 200)

        by_uhid = client.get(f"/api/pharmacy/prescriptions/uhid/{profile['uhid']}", headers=fh)
        check("pharmacy looks up by UHID", by_uhid.status_code == 200)

        disp = client.post(f"/api/pharmacy/dispense/{prescription_id}", headers=fh)
        check("dispense succeeds", disp.status_code == 200, str(disp.status_code))
        if disp.status_code == 200:
            check("prescription is marked dispensed", disp.json()["status"] == "dispensed")

        again = client.post(f"/api/pharmacy/dispense/{prescription_id}", headers=fh)
        check("re-dispensing the same Rx is refused", again.status_code == 409, str(again.status_code))

        dispensed = client.get("/api/pharmacy/dispensed-medicines", headers=fh).json()
        check("dispensed list includes it", any(p["id"] == prescription_id for p in dispensed))

        print("\n[admin]")
        ah = login(client, "admin@medilink.health")
        hospitals = client.get("/api/admin/hospitals", headers=ah).json()
        check("hospitals are listed", len(hospitals) >= 5, f"{len(hospitals)} hospitals")
        check(
            "hospitals carry coordinates for the queue engine",
            all(h["latitude"] and h["longitude"] for h in hospitals),
        )

        doctors = client.get("/api/admin/doctors", headers=ah).json()
        check("doctors are listed", len(doctors) >= 12, f"{len(doctors)} doctors")

        users = client.get("/api/admin/users", headers=ah).json()
        # Seed creates 1 admin + 12 doctors + 1 nurse + 1 pharmacy + 1 lab + 3 patients.
        check("users are listed", len(users) >= 19, f"{len(users)} users")

        print("\n[doctor]")
        dh = login(client, "ananya.rao@medilink.health")
        dme = client.get("/api/doctor/me", headers=dh).json()
        check("doctor/me returns an id for the queue endpoints", "id" in dme, f"id={dme.get('id')}")

        lookup = client.get(f"/api/doctor/patients/{profile['uhid']}", headers=dh)
        check("doctor looks a patient up by UHID", lookup.status_code == 200)

        new_record = client.post(
            "/api/doctor/medical-records",
            json={
                "patient_id": patient_id,
                "chief_complaint": "Smoke test complaint",
                "diagnosis": "Smoke test diagnosis",
                "treatment_plan": "None required",
            },
            headers=dh,
        )
        check("doctor creates a medical record", new_record.status_code == 201, str(new_record.status_code))

        mine = client.get("/api/doctor/my-patients", headers=dh).json()
        check("doctor's patient list includes them", any(p["id"] == patient_id for p in mine))

        print("\n[ai triage]")
        health = client.get("/api/ai/health").json()
        check(
            "triage health reports a provider",
            health["provider"] in {"claude", "rules"},
            f"{health['provider']} ({health['status']})",
        )
        check("rule-engine fallback is always available", health["fallback_available"] is True)

        triage = client.post(
            "/api/ai/analyze-symptoms",
            json={"symptoms_description": "Sore throat and earache for the past three days"},
            headers=ph,
        )
        check("triage returns an analysis", triage.status_code == 200, str(triage.status_code))
        if triage.status_code == 200:
            body = triage.json()
            top = body["recommended_specializations"][0]
            check("routes a sore throat to ENT", top["specialization"] == "ENT", top["specialization"])
            check(
                "priority is 1-based and lower-is-better",
                top["priority"] == 1,
                f"priority={top['priority']}",
            )
            check(
                "urgency uses the frontend vocabulary",
                body["urgency_level"] in {"emergency", "high", "medium", "low"},
                body["urgency_level"],
            )
            check("follow-up questions are returned", len(body["additional_questions"]) >= 2)

        emergency = client.post(
            "/api/ai/analyze-symptoms",
            json={"symptoms_description": "Crushing chest pain spreading to my left arm, sweating"},
            headers=ph,
        ).json()
        check(
            "chest pain is triaged as an emergency",
            emergency["urgency_level"] == "emergency",
            emergency["urgency_level"],
        )
        check("emergency explains its red flags", bool(emergency["red_flags"]))
        check(
            "emergency summary tells the patient not to wait",
            "emergency" in emergency["summary"].lower(),
        )

        short = client.post(
            "/api/ai/analyze-symptoms",
            json={"symptoms_description": "hurts"},
            headers=ph,
        )
        check("too-short descriptions are rejected", short.status_code == 422, str(short.status_code))

        print("\n[authorization]")
        forbidden = client.get("/api/admin/hospitals", headers=ph)
        check("patient cannot read admin endpoints", forbidden.status_code == 403)

        unauth = client.get("/api/patients/me")
        check("unauthenticated request is 401", unauth.status_code == 401)

        bad = client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
        check("garbage token is 401", bad.status_code == 401)

        shape = client.post("/api/auth/login", json={"email": "nope"})
        check(
            "422 uses the detail[] shape the frontend parses",
            shape.status_code == 422 and isinstance(shape.json()["detail"], list),
        )

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
