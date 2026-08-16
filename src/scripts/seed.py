"""Seed a demo dataset so every screen in the frontend renders real data.

Idempotent: re-running skips anything that already exists.

    python -m src.scripts.seed
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from src.core.security import hash_password
from src.db.session import AsyncSessionLocal
from src.models.clinical import (
    LabReport,
    MedicalRecord,
    Medicine,
    Prescription,
    PrescriptionMedicine,
)
from src.models.enums import Gender, TestStatus, UserRole
from src.models.organizations import DiagnosticCenter, Hospital, Pharmacy
from src.models.people import Doctor, Nurse, Patient
from src.models.user import User

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

DEMO_PASSWORD = "MediLink@123"

# Clustered around central Bengaluru so the cross-hospital queue has a realistic
# spread of travel times (roughly 2-12 km apart).
HOSPITALS = [
    ("MediLink City Hospital", "MG Road, Bengaluru", 12.9750, 77.6060, "HOSP-KA-0001"),
    ("Sunrise Multispecialty", "Indiranagar, Bengaluru", 12.9784, 77.6408, "HOSP-KA-0002"),
    ("Green Valley Medical Center", "Koramangala, Bengaluru", 12.9352, 77.6245, "HOSP-KA-0003"),
    ("Northside General Hospital", "Hebbal, Bengaluru", 13.0358, 77.5970, "HOSP-KA-0004"),
    ("Westend Care Hospital", "Rajajinagar, Bengaluru", 12.9916, 77.5530, "HOSP-KA-0005"),
]

# (full_name, specialization, qualification, years, fee, avg_minutes, hospital_index)
DOCTORS = [
    ("Dr. Ananya Rao", "General Medicine", "MBBS, MD", 12, 500, 10, 0),
    ("Dr. Vikram Shetty", "Cardiology", "MBBS, MD, DM", 18, 1200, 20, 0),
    ("Dr. Priya Nair", "Dermatology", "MBBS, MD", 8, 700, 12, 1),
    ("Dr. Rahul Menon", "Orthopedics", "MBBS, MS", 15, 900, 18, 1),
    ("Dr. Sneha Iyer", "Pediatrics", "MBBS, MD", 10, 600, 14, 2),
    ("Dr. Arjun Desai", "General Medicine", "MBBS", 5, 400, 9, 2),
    ("Dr. Kavya Reddy", "Neurology", "MBBS, MD, DM", 20, 1500, 25, 3),
    ("Dr. Imran Khan", "ENT", "MBBS, MS", 9, 650, 11, 3),
    ("Dr. Meera Joshi", "Gynecology", "MBBS, MS", 14, 800, 16, 4),
    ("Dr. Sanjay Gupta", "Gastroenterology", "MBBS, MD, DM", 16, 1100, 18, 4),
    ("Dr. Neha Bhat", "Pulmonology", "MBBS, MD", 11, 850, 15, 0),
    ("Dr. Rohit Verma", "Psychiatry", "MBBS, MD", 13, 1000, 30, 2),
]

# (name, generic, manufacturer, strength, form, price)
MEDICINES = [
    ("Paracetamol", "Acetaminophen", "Cipla", "500mg", "tablet", 2.0),
    ("Dolo 650", "Paracetamol", "Micro Labs", "650mg", "tablet", 2.5),
    ("Amoxicillin", "Amoxicillin", "Sun Pharma", "500mg", "capsule", 8.0),
    ("Azithromycin", "Azithromycin", "Alkem", "500mg", "tablet", 22.0),
    ("Cetirizine", "Cetirizine HCl", "Dr Reddy's", "10mg", "tablet", 1.5),
    ("Pantoprazole", "Pantoprazole", "Zydus", "40mg", "tablet", 5.0),
    ("Omeprazole", "Omeprazole", "Cipla", "20mg", "capsule", 4.5),
    ("Metformin", "Metformin HCl", "USV", "500mg", "tablet", 3.0),
    ("Amlodipine", "Amlodipine Besylate", "Torrent", "5mg", "tablet", 3.5),
    ("Atorvastatin", "Atorvastatin", "Sun Pharma", "10mg", "tablet", 6.0),
    ("Telmisartan", "Telmisartan", "Glenmark", "40mg", "tablet", 7.5),
    ("Ibuprofen", "Ibuprofen", "Abbott", "400mg", "tablet", 3.0),
    ("Diclofenac", "Diclofenac Sodium", "Novartis", "50mg", "tablet", 4.0),
    ("Montelukast", "Montelukast Sodium", "Cipla", "10mg", "tablet", 12.0),
    ("Salbutamol", "Albuterol", "Cipla", "100mcg", "inhaler", 150.0),
    ("Prednisolone", "Prednisolone", "Wyeth", "10mg", "tablet", 4.0),
    ("Levothyroxine", "Levothyroxine Sodium", "Abbott", "50mcg", "tablet", 3.5),
    ("Ranitidine", "Ranitidine", "GSK", "150mg", "tablet", 3.0),
    ("Ondansetron", "Ondansetron", "Cipla", "4mg", "tablet", 8.0),
    ("Domperidone", "Domperidone", "Torrent", "10mg", "tablet", 4.5),
    ("Ciprofloxacin", "Ciprofloxacin", "Ranbaxy", "500mg", "tablet", 12.0),
    ("Doxycycline", "Doxycycline", "Cipla", "100mg", "capsule", 9.0),
    ("Vitamin D3", "Cholecalciferol", "USV", "60000IU", "sachet", 35.0),
    ("Vitamin B12", "Methylcobalamin", "Zydus", "1500mcg", "tablet", 6.0),
    ("Iron + Folic Acid", "Ferrous Sulphate", "Emcure", "100mg", "tablet", 4.0),
    ("Calcium Carbonate", "Calcium", "Shelcal", "500mg", "tablet", 7.0),
    ("Aspirin", "Acetylsalicylic Acid", "Bayer", "75mg", "tablet", 1.5),
    ("Clopidogrel", "Clopidogrel", "Sanofi", "75mg", "tablet", 14.0),
    ("Losartan", "Losartan Potassium", "Torrent", "50mg", "tablet", 5.5),
    ("Metoprolol", "Metoprolol Succinate", "AstraZeneca", "25mg", "tablet", 6.5),
    ("Insulin Glargine", "Insulin Glargine", "Sanofi", "100IU/ml", "injection", 850.0),
    ("Gliclazide", "Gliclazide", "Serdia", "80mg", "tablet", 8.0),
    ("Fluconazole", "Fluconazole", "Cipla", "150mg", "tablet", 18.0),
    ("Acyclovir", "Acyclovir", "Cipla", "400mg", "tablet", 11.0),
    ("Hydroxyzine", "Hydroxyzine HCl", "UCB", "25mg", "tablet", 5.0),
    ("Sertraline", "Sertraline", "Lupin", "50mg", "tablet", 9.0),
    ("Escitalopram", "Escitalopram", "Sun Pharma", "10mg", "tablet", 10.0),
    ("Clonazepam", "Clonazepam", "Roche", "0.5mg", "tablet", 4.0),
    ("Tramadol", "Tramadol HCl", "Cipla", "50mg", "capsule", 7.0),
    ("ORS Sachet", "Oral Rehydration Salts", "FDC", "21.8g", "sachet", 20.0),
]

# (first, last, dob, gender, blood, phone, uhid)
PATIENTS = [
    ("Harsh", "Kumar", date(1998, 4, 12), Gender.MALE, "O+", "+919876543210", "ML2500000001"),
    ("Aisha", "Fernandes", date(1991, 11, 3), Gender.FEMALE, "A+", "+919876543211", "ML2500000002"),
    ("Ravi", "Subramanian", date(1965, 7, 22), Gender.MALE, "B+", "+919876543212", "ML2500000003"),
]


async def _get_or_create_user(db, email, username, full_name, role) -> tuple[User, bool]:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user:
        return user, False
    user = User(
        email=email,
        username=username,
        full_name=full_name,
        hashed_password=hash_password(DEMO_PASSWORD),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user, True


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        # -- Admin ------------------------------------------------------------
        admin, created = await _get_or_create_user(
            db, "admin@medilink.health", "admin", "MediLink Administrator", UserRole.ADMIN
        )
        logger.info("%s admin: admin@medilink.health", "created" if created else "exists")

        # -- Hospitals --------------------------------------------------------
        hospitals: list[Hospital] = []
        for name, address, lat, lng, license_no in HOSPITALS:
            result = await db.execute(select(Hospital).where(Hospital.license_number == license_no))
            hospital = result.scalar_one_or_none()
            if hospital is None:
                hospital = Hospital(
                    name=name,
                    address=address,
                    phone="+918000000000",
                    email=f"contact@{license_no.lower()}.medilink.health",
                    license_number=license_no,
                    latitude=lat,
                    longitude=lng,
                )
                db.add(hospital)
                await db.flush()
            hospitals.append(hospital)
        logger.info("hospitals: %d", len(hospitals))

        # -- Doctors ----------------------------------------------------------
        doctors: list[Doctor] = []
        for idx, (full_name, spec, qual, years, fee, avg_min, hosp_idx) in enumerate(DOCTORS, start=1):
            license_no = f"DOC-KA-{idx:04d}"
            result = await db.execute(select(Doctor).where(Doctor.license_number == license_no))
            doctor = result.scalar_one_or_none()
            if doctor is None:
                email = full_name.lower().replace("dr. ", "").replace(" ", ".") + "@medilink.health"
                user, _ = await _get_or_create_user(
                    db, email, email.split("@")[0], full_name, UserRole.DOCTOR
                )
                doctor = Doctor(
                    user_id=user.id,
                    hospital_id=hospitals[hosp_idx].id,
                    license_number=license_no,
                    specialization=spec,
                    years_of_experience=years,
                    qualification=qual,
                    consultation_fee=float(fee),
                    avg_consultation_minutes=float(avg_min),
                )
                db.add(doctor)
                await db.flush()
            doctors.append(doctor)
        logger.info("doctors: %d", len(doctors))

        # -- Nurse ------------------------------------------------------------
        result = await db.execute(select(Nurse).where(Nurse.license_number == "NUR-KA-0001"))
        if result.scalar_one_or_none() is None:
            user, _ = await _get_or_create_user(
                db, "nurse.lakshmi@medilink.health", "nurse.lakshmi", "Lakshmi Devi", UserRole.NURSE
            )
            db.add(
                Nurse(
                    user_id=user.id,
                    hospital_id=hospitals[0].id,
                    license_number="NUR-KA-0001",
                    department="Emergency",
                    shift="Morning",
                    years_of_experience=7,
                )
            )
            await db.flush()

        # -- Pharmacy ---------------------------------------------------------
        result = await db.execute(select(Pharmacy).where(Pharmacy.license_number == "PHM-KA-0001"))
        if result.scalar_one_or_none() is None:
            user, _ = await _get_or_create_user(
                db, "pharmacy@medilink.health", "pharmacy", "MediLink Pharmacy", UserRole.PHARMACY
            )
            db.add(
                Pharmacy(
                    user_id=user.id,
                    name="MediLink Pharmacy - MG Road",
                    license_number="PHM-KA-0001",
                    address="MG Road, Bengaluru",
                    phone="+918000000001",
                    email="pharmacy@medilink.health",
                    latitude=12.9752,
                    longitude=77.6062,
                )
            )
            await db.flush()

        # -- Diagnostic center ------------------------------------------------
        result = await db.execute(
            select(DiagnosticCenter).where(DiagnosticCenter.license_number == "DGC-KA-0001")
        )
        center = result.scalar_one_or_none()
        if center is None:
            user, _ = await _get_or_create_user(
                db, "lab@medilink.health", "lab", "MediLink Diagnostics", UserRole.DIAGNOSTIC_CENTER
            )
            center = DiagnosticCenter(
                user_id=user.id,
                name="MediLink Diagnostics",
                license_number="DGC-KA-0001",
                address="Indiranagar, Bengaluru",
                phone="+918000000002",
                email="lab@medilink.health",
                services_offered="Pathology, Radiology, ECG, Ultrasound",
                latitude=12.9780,
                longitude=77.6400,
            )
            db.add(center)
            await db.flush()

        # -- Medicines --------------------------------------------------------
        medicine_count = 0
        for name, generic, mfr, strength, form, price in MEDICINES:
            result = await db.execute(
                select(Medicine).where(Medicine.name == name, Medicine.strength == strength)
            )
            if result.scalar_one_or_none() is None:
                db.add(
                    Medicine(
                        name=name,
                        generic_name=generic,
                        manufacturer=mfr,
                        strength=strength,
                        form=form,
                        price_per_unit=price,
                    )
                )
                medicine_count += 1
        await db.flush()
        logger.info("medicines added: %d", medicine_count)

        # -- Patients ---------------------------------------------------------
        patients: list[Patient] = []
        for first, last, dob, gender, blood, phone, uhid in PATIENTS:
            result = await db.execute(select(Patient).where(Patient.uhid == uhid))
            patient = result.scalar_one_or_none()
            if patient is None:
                email = f"{first.lower()}.{last.lower()}@example.com"
                user, _ = await _get_or_create_user(
                    db, email, f"{first.lower()}.{last.lower()}", f"{first} {last}", UserRole.PATIENT
                )
                patient = Patient(
                    user_id=user.id,
                    uhid=uhid,
                    first_name=first,
                    last_name=last,
                    date_of_birth=dob,
                    gender=gender,
                    blood_group=blood,
                    phone=phone,
                    address="Bengaluru, Karnataka",
                    emergency_contact_name="Emergency Contact",
                    emergency_contact_phone="+919000000000",
                    medical_allergies="Penicillin" if first == "Harsh" else None,
                    chronic_conditions="Type 2 Diabetes, Hypertension" if first == "Ravi" else None,
                )
                db.add(patient)
                await db.flush()
            patients.append(patient)
        logger.info("patients: %d", len(patients))

        # -- A little clinical history so the doctor/patient views aren't empty --
        result = await db.execute(select(MedicalRecord).limit(1))
        if result.scalar_one_or_none() is None:
            db.add(
                MedicalRecord(
                    patient_id=patients[2].id,
                    doctor_id=doctors[0].id,
                    chief_complaint="Routine diabetes follow-up",
                    diagnosis="Type 2 Diabetes Mellitus - controlled",
                    treatment_plan="Continue Metformin 500mg twice daily. Review in 3 months.",
                    vitals="BP 130/85, Pulse 78, Weight 74kg, HbA1c 6.9%",
                    visit_date=datetime.now(UTC) - timedelta(days=45),
                )
            )

            metformin = (await db.execute(select(Medicine).where(Medicine.name == "Metformin"))).scalar_one()
            prescription = Prescription(
                patient_id=patients[2].id,
                doctor_id=doctors[0].id,
                notes="Take after meals. Monitor blood sugar weekly.",
                follow_up_date=date.today() + timedelta(days=45),
                prescription_date=datetime.now(UTC) - timedelta(days=45),
            )
            db.add(prescription)
            await db.flush()
            db.add(
                PrescriptionMedicine(
                    prescription_id=prescription.id,
                    medicine_id=metformin.id,
                    dosage="1-0-1",
                    frequency="Twice daily",
                    duration="90 days",
                    instructions="After meals",
                    quantity_prescribed=180,
                )
            )

            db.add(
                LabReport(
                    patient_id=patients[2].id,
                    diagnostic_center_id=center.id,
                    requested_by_doctor_id=doctors[0].id,
                    test_name="HbA1c",
                    test_type="Pathology",
                    results="6.9%",
                    reference_ranges="4.0 - 5.6% (normal), 5.7 - 6.4% (prediabetes)",
                    interpretation="Diabetes, reasonably controlled.",
                    status=TestStatus.COMPLETED,
                    requested_date=datetime.now(UTC) - timedelta(days=50),
                    completed_date=datetime.now(UTC) - timedelta(days=48),
                )
            )

        await db.commit()

    logger.info("")
    logger.info("Seed complete. All demo accounts use the password: %s", DEMO_PASSWORD)
    logger.info("  admin      admin@medilink.health")
    logger.info("  doctor     ananya.rao@medilink.health")
    logger.info("  patient    harsh.kumar@example.com  (UHID ML2500000001)")
    logger.info("  patient    ravi.subramanian@example.com  (UHID ML2500000003, has history)")
    logger.info("  pharmacy   pharmacy@medilink.health")
    logger.info("  lab        lab@medilink.health")


if __name__ == "__main__":
    asyncio.run(seed())
