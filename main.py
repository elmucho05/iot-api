from typing import Optional, List
from typing import Annotated
from datetime import datetime, time, timedelta, date, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from contextlib import asynccontextmanager

from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from random import choice, randint
from fastapi.responses import JSONResponse

import requests

from datetime import time
from fastapi import FastAPI, HTTPException, Depends, Query, Body
from sqlmodel import SQLModel, Field, Session, create_engine, select, delete
from typing import List, Dict
from pydantic import BaseModel
import json
from dateutil import parser
import httpx
import configparser
from zoneinfo import ZoneInfo

import threading


# Database Configuration
sqlite_file_name = "medicine_db.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


# Model Definition
class CompartmentBase(SQLModel):
    compartment_number: int = Field(index=True)  # 1, 2, or 3

    medicine_name: str = Field(index=True)
    number_of_medicines: int = Field(default=0)
    to_be_repeated: bool = Field(default=False)

    # Time fields using datetime.time
    morning_time: Optional[time] = None
    afternoon_time: Optional[time] = None
    evening_time: Optional[time] = None

    # If NOT repeated, this field is required
    time_if_not_repeated: Optional[time] = None

    taken : bool= Field(default=False)
    taken_at : Optional[datetime] = None
    low_stock: bool = Field(default=False)

class Compartment(CompartmentBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class CompartmentCreate(CompartmentBase):
    pass


class CompartmentUpdate(SQLModel):
    medicine_name: Optional[str] = None
    number_of_medicines: Optional[int] = None
    to_be_repeated: Optional[bool] = None
    morning_time: Optional[time] = None
    afternoon_time: Optional[time] = None
    evening_time: Optional[time] = None
    time_if_not_repeated: Optional[time] = None
    
    taken: Optional[bool] = None



class CompartmentPublic(CompartmentBase):
    id: int

class AdafruitData(BaseModel):
    value: str
    feed_name: str
    feed_key: str
    created_at: str
    updated_at: str
    expiration: int

class RefillRequest(BaseModel):
    amount: int

class MedicineLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    compartment_number: int
    medicine_name: str
    taken_at: Optional[datetime] = None  # This can be time of action (taken/refill/manual)
    action: str = Field(default="taken")  # "taken", "refill", "manual" "scheduled"
    remaining_pills: Optional[int] = None
    low_stock: Optional[bool] = None
    scheduled_time: Optional[time] = None  # when it was supposed to be taken
    is_late: Optional[bool] = None
    scheduled_date: Optional[date] = None

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI()
origins = [
    "http://localhost",           # per test locali
    "http://localhost:4200",      # se usi Angular local
    "https://myapi.smartmeds.it"  # opzionale se vuoi permettere a te stesso richieste interne
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],             # oppure usa ["*"] per test ma NON in produzione
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = configparser.ConfigParser()
config.read('config.ini')
if not config.has_section("HTTPAIO"):
    raise RuntimeError("❌ config.ini missing [HTTPAIO] section")

url = config.get("HTTPAIO", "Url")
aio_key = config.get("HTTPAIO", "X-AIO-Key")



def post_to_adafruit(compartment_index: int, value: int):
    if compartment_index > 2:
        return

    print("Executing adafruit post ")

    feed_key = f"Feed{compartment_index + 1}"  # Feed1, Feed2, Feed3
    #url = config.get("HTTPAIO", "Url")
    feed = config.get("HTTPAIO", feed_key)

    full_url = f"{url}{feed}/data"
    headers = {"X-AIO-Key": aio_key}
    payload = {"value": value}

    print(f"> 📤 POST to {full_url} (value={value})")

    try:
        res = requests.post(full_url, headers=headers, data=payload)
        res.raise_for_status()
        print(f"✅ POST success: {res.status_code}")
    except Exception as e:
        print(f"❌ POST error: {e}")

def async_post_to_adafruit(compartment_index: int, value: int):
    thread = threading.Thread(target=post_to_adafruit, args=(compartment_index, value))
    thread.start()

def check_scheduled_logs():
    with Session(engine) as session:
        now = datetime.now(ZoneInfo("Europe/Rome"))  # 🇮🇹 orario italiano corretto
        logs = session.exec(
            select(MedicineLog).where(
                MedicineLog.action == "scheduled",
                MedicineLog.scheduled_time != None,
                MedicineLog.scheduled_date == now.date()
            )
        ).all()
        print("[⏱] Esecuzione cron avviata")
        updated = 0

        for log in logs:
            sched_dt = datetime.combine(log.scheduled_date, log.scheduled_time).replace(tzinfo=ZoneInfo("Europe/Rome"))
            print("tempo estratto dai logs", sched_dt)
            print("tempo ora", now)
            seconds_since_sched = (now - sched_dt).total_seconds()
            print("tempo passato da quando è stato schedulato : ", seconds_since_sched)
            seconds_to_sched = (sched_dt - now).total_seconds()

            # ✅ Controlla se è da marcare come missed (ritardo > 90 minuti)
            if seconds_since_sched > 5400 and not log.taken_at:
                log.action = "missed"
                log.is_late = True
                session.add(log)
                updated += 1
                print(f"❌ Log {log.id} marcato come missed (ritardo > 90 min)")
            print("printing the seconds up to a medicine", seconds_to_sched)
            # ✅ Trigger Adafruit se è previsto entro 10 minuti
            if 0 <= seconds_to_sched <= 650:
                print(f"🔔 Medicinale previsto entro 10 minuti (log {log.id})")

                comp = session.exec(
                    select(Compartment).where(Compartment.compartment_number == log.compartment_number)
                ).first()

                if comp:
                    comp.taken = False
                    session.add(comp)

                    # 🔁 Chiamata in thread separato
                    async_post_to_adafruit(comp.compartment_number - 1, 0)
                    print(f"📤 Comando (threaded) inviato ad Adafruit per compartimento {comp.compartment_number}")

        session.commit()

        if updated:
            print(f"[✔] Logs aggiornati: {updated} medicine marcate come missed")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    # scheduler = BackgroundScheduler()
    # scheduler.add_job(check_scheduled_logs, IntervalTrigger(minutes=1))
    # scheduler.start()


# API Endpoints

@app.post("/compartments/createcompartment", response_model=CompartmentPublic)
def create_compartment(compartment: CompartmentCreate, session: Session = Depends(get_session)):
    """
    Creates a new medicine in a compartment and automatically adds 'scheduled' logs for today.
    """
    if compartment.compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )
    
    if not compartment.to_be_repeated and not compartment.time_if_not_repeated:
        raise HTTPException(
            status_code=400,
            detail="time_if_not_repeated is required if the medicine is not repeated."
        )

    if compartment.to_be_repeated and compartment.time_if_not_repeated:
        raise HTTPException(
            status_code=400,
            detail="time_if_not_repeated should be None if the medicine is repeated."
        )

    db_compartment = Compartment.model_validate(compartment)
    db_compartment.low_stock = db_compartment.number_of_medicines < 4  # auto-calculate stock status
    session.add(db_compartment)
    session.commit()
    session.refresh(db_compartment)

    # 🔁 CREAZIONE AUTOMATICA LOG SCHEDULED
    today = datetime.utcnow().date()

    def create_log(sched_time: time):
        return MedicineLog(
            compartment_number=db_compartment.compartment_number,
            medicine_name=db_compartment.medicine_name,
            action="scheduled",
            taken_at=None,
            remaining_pills=db_compartment.number_of_medicines,
            low_stock=db_compartment.low_stock,
            scheduled_time=sched_time,
            scheduled_date=today,
            is_late=False
        )

    if db_compartment.to_be_repeated:
        times = [db_compartment.morning_time, db_compartment.afternoon_time, db_compartment.evening_time]
        for sched_time in times:
            if sched_time:
                session.add(create_log(sched_time))
    else:
        session.add(create_log(db_compartment.time_if_not_repeated))

    session.commit()
    return db_compartment



@app.get("/compartments/", response_model=List[CompartmentPublic])
def get_compartments(
    session: Session = Depends(get_session),
    offset: int = 0,
    limit: int = Query(100, le=100)
):
    compartments = session.exec(select(Compartment).offset(offset).limit(limit)).all()
    return compartments


@app.get("/compartments/{compartment_number}", response_model=List[CompartmentPublic])
def get_compartments_by_number(compartment_number: int, session: Session = Depends(get_session)):
    """
    Get all medicines stored in a specific compartment (1, 2, or 3).
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    compartments = session.exec(select(Compartment).where(Compartment.compartment_number == compartment_number)).all()
    return compartments

@app.post("/compartments/bulk-create", response_model=List[CompartmentPublic])
def create_multiple_compartments(
    compartments: List[CompartmentCreate] = Body(...),
    session: Session = Depends(get_session)
):
    created_compartments = []

    for compartment in compartments:
        if compartment.compartment_number not in [1, 2, 3]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid compartment_number: {compartment.compartment_number}. Must be 1, 2, or 3."
            )
        
        if not compartment.to_be_repeated and not compartment.time_if_not_repeated:
            raise HTTPException(
                status_code=400,
                detail=f"time_if_not_repeated is required for non-repeated medicine in compartment {compartment.compartment_number}."
            )

        if compartment.to_be_repeated and compartment.time_if_not_repeated:
            raise HTTPException(
                status_code=400,
                detail=f"time_if_not_repeated must be None for repeated medicine in compartment {compartment.compartment_number}."
            )

        db_compartment = Compartment.model_validate(compartment)
        session.add(db_compartment)
        created_compartments.append(db_compartment)

    session.commit()

    for comp in created_compartments:
        session.refresh(comp)

    return created_compartments
@app.patch("/compartments/updatecompartment/{compartment_id}", response_model=CompartmentPublic)
def update_compartment(compartment_id: int, compartment_update: CompartmentUpdate, session: Session = Depends(get_session)):
    compartment = session.get(Compartment, compartment_id)
    if not compartment:
        raise HTTPException(status_code=404, detail="Compartment not found")

    original_name = compartment.medicine_name
    update_data = compartment_update.model_dump(exclude_unset=True)

    # Validate compartment_number if it's being updated
    if "compartment_number" in update_data and update_data["compartment_number"] not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    for key, value in update_data.items():
        setattr(compartment, key, value)

    session.add(compartment)
    session.commit()
    session.refresh(compartment)

    # ✅ Aggiorna i log associati
    related_logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.compartment_number == compartment.compartment_number,
            MedicineLog.medicine_name == original_name,
            MedicineLog.scheduled_date == datetime.utcnow().date()
        )
    ).all()

    now = datetime.utcnow()

    for log in related_logs:
        log.medicine_name = update_data.get("medicine_name", log.medicine_name)

        if log.action in ["scheduled", "missed", "taken"]:
            if log.scheduled_time:
                # Aggiorna l'orario del log se cambiano i time slot
                if not compartment.to_be_repeated:
                    new_time = update_data.get("time_if_not_repeated")
                    if new_time:
                        log.scheduled_time = new_time
                else:
                    hour = log.scheduled_time.hour
                    if 5 <= hour < 12:
                        new_time = update_data.get("morning_time")
                    elif 12 <= hour < 17:
                        new_time = update_data.get("afternoon_time")
                    else:
                        new_time = update_data.get("evening_time")
                    if new_time:
                        log.scheduled_time = new_time

            # Ripristina il log "missed" a "scheduled" se il nuovo orario è ancora valido
            if log.action == "missed" and log.scheduled_time:
                sched_dt = datetime.combine(log.scheduled_date, log.scheduled_time)
                if (now - sched_dt).total_seconds() <= 5400:
                    log.action = "scheduled"
                    log.is_late = False
                    print(f"✅ Log {log.id} ripristinato da 'missed' a 'scheduled'")

            if log.action == "scheduled":
                log.remaining_pills = update_data.get("number_of_medicines", log.remaining_pills)

                # ✅ Invia comando Adafruit se entro 10 minuti dal nuovo orario
                if log.scheduled_time:
                    sched_dt = datetime.combine(log.scheduled_date, log.scheduled_time)
                    seconds_to_sched = (sched_dt - now).total_seconds()
                    if 0 <= seconds_to_sched <= 600:
                        post_to_adafruit(log.compartment_number, 0)
                        print(f"📤 Inviato comando a Adafruit per compartimento {log.compartment_number}")

        session.add(log)

    session.commit()
    return compartment




@app.delete("/compartments/{compartment_number}")
def delete_medicine_from_compartment(
    compartment_number: int,
    session: Session = Depends(get_session)
):
    """
    Deletes all entries of a specific medicine from a given compartment.
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    # Check existence first
    exists = session.exec(
        select(Compartment).where(
            (Compartment.compartment_number == compartment_number))
    ).first()

    if not exists:
        raise HTTPException(
            status_code=404,
            detail=f"No medicine found in compartment {compartment_number}."
        )

    # Efficient bulk delete
    session.exec(
        delete(Compartment).where(
            (Compartment.compartment_number == compartment_number)
        )
    )
    session.commit()

    return {
        "message": f"All entries of '{compartment_number}' have been removed."
    }

@app.delete("/compartments/")
def delete_all_compartments(session: Session = Depends(get_session)):
    """
    Deletes all compartments from the database.
    """
    session.exec(delete(Compartment))
    session.commit()
    return {"message": "All compartments have been deleted"}




###########################################################
###################### Take medicine ######################
###########################################################
@app.patch("/compartments/{compartment_number}/mark-taken", response_model=CompartmentPublic)
def mark_medicine_taken(compartment_number: int, session: Session = Depends(get_session)):
    """
    Marks the medicine in the given `compartment_number` as taken.
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    # Find the only medicine in the compartment
    compartment = session.exec(
        select(Compartment).where(Compartment.compartment_number == compartment_number)
    ).first()

    if not compartment:
        raise HTTPException(
            status_code=404,
            detail=f"No medicine found in compartment {compartment_number}."
        )

    # Mark it as taken
    compartment.taken = True
    session.add(compartment)
    session.commit()
    session.refresh(compartment)

    return compartment


@app.patch("/compartments/{compartment_number}/unmark-taken", response_model=CompartmentPublic)
def unmark_medicine_taken(compartment_number: int, session: Session = Depends(get_session)):
    """
    Unmarks the medicine in the given compartment (set taken = False).
    """
    compartment = session.exec(
        select(Compartment).where(Compartment.compartment_number == compartment_number)
    ).first()
    
    if not compartment :
        raise HTTPException(status_code=404, detail="No medicine found in this compartment")

    compartment.taken = False
    compartment.taken_at = None
    session.add(compartment)
    session.commit()
    session.refresh(compartment)
    return compartment


@app.get("/compartments/{compartment_number}/taken", response_model=List[CompartmentPublic])
def get_taken_medicines(compartment_number: int, session: Session = Depends(get_session)):
    """
    Retrieves all medicines in the given compartment that have been taken (taken=True).
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    medicines = session.exec(
        select(Compartment).where(
            (Compartment.compartment_number == compartment_number) &
            (Compartment.taken == True)
        )
    ).all()

    return medicines


@app.get("/compartments/{compartment_number}/pending", response_model=List[CompartmentPublic])
def get_pending_medicines(
    compartment_number: int,
    session: Session = Depends(get_session)
):
    """
    Ritorna le medicine nel compartimento specificato che non sono ancora state prese (taken=False).
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(status_code=400, detail="compartment_number must be 1, 2, or 3.")

    pending = session.exec(
        select(Compartment)
        .where(
            (Compartment.compartment_number == compartment_number) &
            (Compartment.taken == False)
        )
    ).all()

    return pending




#################################################################
###################### Adafruit stuff ###########################
#################################################################
# ✅ Update webhook to UPDATE the existing scheduled log instead of creating new one
@app.post("/adafruit-taken-webhook/")
def pill_taken_webhook(data: List[AdafruitData], session: Session = Depends(get_session)):
    for entry in data:
        feed = entry.feed_name.lower()
        feed_map = {
            "comp1-taken": 1,
            "comp2-taken": 2,
            "comp3-taken": 31
        }
        comp_num = feed_map.get(feed)
        if not comp_num:
            continue

        comp = session.exec(
            select(Compartment).where(Compartment.compartment_number == comp_num)
        ).first()
        if not comp:
            continue

        if entry.value.strip() == "1":
            taken_time = parser.isoparse(entry.created_at)
            comp.taken = True
            comp.taken_at = taken_time
            comp.number_of_medicines = max(comp.number_of_medicines - 1, 0)
            comp.low_stock = comp.number_of_medicines < 4

            # ✅ Cerca il primo log programmato con scheduled_date corrispondente e time vicino
            scheduled_logs = session.exec(
                select(MedicineLog).where(
                    MedicineLog.compartment_number == comp.compartment_number,
                    MedicineLog.medicine_name == comp.medicine_name,
                    MedicineLog.scheduled_date == taken_time.date(),
                    MedicineLog.action == "scheduled"
                )
            ).all()

            matched_log = None
            for log in scheduled_logs:
                if log.scheduled_time:
                    sched_dt = datetime.combine(log.scheduled_date, log.scheduled_time, tzinfo=taken_time.tzinfo)
                    diff = abs((taken_time - sched_dt).total_seconds())
                    if diff <= 1200:  # entro 20 minuti → OK e non in ritardo
                        matched_log = log
                        matched_log.is_late = False
                        break
                    elif diff <= 3600:  # entro 1 ora → OK ma in ritardo
                        matched_log = log
                        matched_log.is_late = True
                        break

            if matched_log:
                matched_log.action = "taken"
                matched_log.taken_at = taken_time
                matched_log.remaining_pills = comp.number_of_medicines
                matched_log.low_stock = comp.low_stock
                session.add(matched_log)
            else:
                # Se nessun log trovato nel range → fallback: il più vicino, segnato come missed
                if scheduled_logs:
                    fallback = min(
                        scheduled_logs,
                        key=lambda log: abs((taken_time - datetime.combine(log.scheduled_date, log.scheduled_time, tzinfo=taken_time.tzinfo)).total_seconds())
                    )
                    fallback.action = "missed"
                    fallback.taken_at = None
                    fallback.remaining_pills = comp.number_of_medicines
                    fallback.low_stock = comp.low_stock
                    fallback.is_late = True
                    session.add(fallback)

            session.add(comp)
            session.commit()
            session.refresh(comp)

            return {
                "message": f"Compartment {comp_num} updated",
                "new_count": comp.number_of_medicines,
                "low_stock": comp.low_stock
            }

    return {"message": "No valid update processed"}

@app.post("/compartments/{compartment_number}/refill")
def refill_medicine(compartment_number: int, refill: RefillRequest, session: Session = Depends(get_session)):
    comp = session.exec(
        select(Compartment).where(Compartment.compartment_number == compartment_number)
    ).first()

    if compartment_number not in [1, 2, 3]:
        raise HTTPException(status_code=400, detail="Invalid compartment number. Only 1, 2, or 3 are allowed.")

    if not comp:
        raise HTTPException(status_code=404, detail="Compartment not found")

    comp.number_of_medicines += refill.amount
    comp.taken = False
    comp.low_stock = comp.number_of_medicines < 4

    log = MedicineLog(
        compartment_number=compartment_number,
        medicine_name=comp.medicine_name,
        ##taken_at=datetime.utcnow(),
        action="refill",
        remaining_pills=comp.number_of_medicines,
        low_stock=comp.low_stock
    )

    session.add(log)
    session.add(comp)
    session.commit()
    session.refresh(comp)

    return {
        "message": f"Refilled compartment {compartment_number} with {refill.amount} units.",
        "current_total": comp.number_of_medicines
    }


# @app.post("/adafruit-webhook/")
# def receive_adafruit_data(data: List[AdafruitData], session: Session = Depends(get_session)):
#     """
#     Receives data from Adafruit IO, determines which compartment is activated,
#     and marks the medicine in that compartment as taken.
#     """
#     for entry in data:
#         feed_name = entry.feed_name.lower()  # Convert to lowercase to avoid case issues

#         # Determine which compartment is triggered
#         if feed_name == "comp1":
#             compartment_number = 1
#         elif feed_name == "comp2":
#             compartment_number = 2
#         elif feed_name == "comp3":
#             compartment_number = 3
#         else:
#             continue  # Ignore feeds that don't match compartment names

#         # Find the medicine in the correct compartment
#         compartment = session.exec(
#             select(Compartment).where(Compartment.compartment_number == compartment_number)
#         ).first()

#         if not compartment:
#             return {"message": f"No medicine found in compartment {compartment_number}."}

#         try :
#             taken_time = parser.isoparse(entry.created_at)
#         except Exception:
#             taken_time = datetime.utcnow() #fallback

#         try:
#             remaining_pills = int(entry.value)
#         except ValueError:
#             return {"error": f"Invalid pill count in feed: {entry.value}"}

#         # Mark the medicine as taken
#         compartment.taken = True
#         compartment.taken_at = taken_time
#         compartment.number_of_medicines = remaining_pills
        
#         compartment.low_stock = remaining_pills < 4


#         session.add(compartment)
#         session.commit()
#         session.refresh(compartment)

#         response = {
#             "message": f"Medicine in compartment {compartment_number} marked as taken.",
#             "compartment": compartment_number,
#             "remaining_pills": remaining_pills,
#             "low_stock": compartment.low_stock,
#             "taken_at": taken_time.isoformat(),
#             "medicine_name": compartment.medicine_name
#         }
#         return response

#     return {"message": "No valid compartment found in the received data."}


@app.post("/populate-test-data/")
def populate_test_data(session: Session = Depends(get_session)):
    """
    Populates the database with:
    - Compartment 1: A repeated medicine with normal stock
    - Compartment 2: A repeated medicine with low stock, already taken
    - Compartment 3: A one-time medicine, not taken yet
    """

    # Clear any existing data
    session.exec(delete(Compartment))
    session.commit()

    def str_to_time(t: str) -> Optional[time]:
        return datetime.strptime(t, "%H:%M:%S").time() if t else None

    now = datetime.utcnow()

    test_data = [
        Compartment(
            compartment_number=1,
            medicine_name="Paracetamol",
            number_of_medicines=10,
            to_be_repeated=True,
            taken=False,
            taken_at=None,
            low_stock=False,
            morning_time=str_to_time("08:00:00"),
            afternoon_time=str_to_time("14:00:00"),
            evening_time=str_to_time("20:00:00"),
            time_if_not_repeated=None
        ),
        Compartment(
            compartment_number=2,
            medicine_name="Ibuprofen",
            number_of_medicines=2,  # Low stock
            to_be_repeated=True,
            taken=True,
            taken_at=now,
            low_stock=True,
            morning_time=str_to_time("09:00:00"),
            afternoon_time=str_to_time("15:00:00"),
            evening_time=str_to_time("21:00:00"),
            time_if_not_repeated=None
        ),
        Compartment(
            compartment_number=3,
            medicine_name="Antibiotic",
            number_of_medicines=5,
            to_be_repeated=False,
            taken=False,
            taken_at=None,
            low_stock=False,
            morning_time=None,
            afternoon_time=None,
            evening_time=None,
            time_if_not_repeated=str_to_time("12:00:00")
        )
    ]

    session.add_all(test_data)
    session.commit()

    return {"message": "Test data added successfully!"}


###################### LOGS ####################
@app.get("/logs/", response_model=List[MedicineLog])
def get_all_logs(session: Session = Depends(get_session)):
    return session.exec(
        select(MedicineLog).order_by(MedicineLog.scheduled_date.desc(), MedicineLog.taken_at.desc())
    ).all()

@app.get("/logs/by-day/{date}", response_model=List[MedicineLog])
def get_logs_by_day(date: str, session: Session = Depends(get_session)):
    try:
        day_start = datetime.fromisoformat(date).date()
    except:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    logs = session.exec(
        select(MedicineLog).where(MedicineLog.scheduled_date == day_start).order_by(MedicineLog.scheduled_time)
    ).all()

    return logs


@app.get("/logs/by-compartment")
def get_log_summary(session: Session = Depends(get_session)):
    summary = []

    for comp_num in [1, 2, 3]:
        logs = session.exec(
            select(MedicineLog).where(MedicineLog.compartment_number == comp_num)
        ).all()

        taken_logs = [log for log in logs if log.action == "taken"]
        refill_logs = [log for log in logs if log.action == "refill"]

        last_taken = max([log.taken_at for log in taken_logs], default=None)
        last_refill = max([log.taken_at for log in refill_logs], default=None)
        total_taken = len(taken_logs)
        total_refill = sum([log.remaining_pills or 0 for log in refill_logs])

        current_stock = session.exec(
            select(Compartment.number_of_medicines).where(Compartment.compartment_number == comp_num)
        ).first()

        summary.append({
            "compartment": comp_num,
            "total_taken": total_taken,
            "total_refilled": total_refill,
            "last_taken": last_taken,
            "last_refill": last_refill,
            "current_stock": current_stock
        })

    return summary

@app.get("/logs/last-actions")
def get_last_actions(session: Session = Depends(get_session)):
    result = []
    for comp_num in [1, 2, 3]:
        last_log = session.exec(
            select(MedicineLog)
            .where(MedicineLog.compartment_number == comp_num)
            .order_by(MedicineLog.taken_at.desc())
        ).first()

        if last_log:
            result.append({
                "compartment": comp_num,
                "last_action": last_log.action,
                "medicine": last_log.medicine_name,
                "timestamp": last_log.taken_at,
                "remaining_pills": last_log.remaining_pills,
                "low_stock": last_log.low_stock
            })
    return result

@app.get("/logs/stats")
def get_log_stats(session: Session = Depends(get_session)):
    logs = session.exec(select(MedicineLog)).all()

    total_taken = sum(1 for log in logs if log.action == "taken")
    total_refills = sum(1 for log in logs if log.action == "refill")
    low_stock_events = sum(1 for log in logs if log.low_stock)

    return {
        "total_taken": total_taken,
        "total_refills": total_refills,
        "low_stock_events": low_stock_events
    }

# @app.get("/logs/missed")
# def get_missed_doses(session: Session = Depends(get_session)):
#     compartments = session.exec(select(Compartment)).all()
#     missed = []

#     now = datetime.utcnow()
#     today = now.date()

#     for comp in compartments:
#         if not comp.to_be_repeated:
#             continue

#         scheduled_times = [
#             ("morning", comp.morning_time),
#             ("afternoon", comp.afternoon_time),
#             ("evening", comp.evening_time)
#         ]

#         for label, sched_time in scheduled_times:
#             if not sched_time:
#                 continue

#             sched_datetime = datetime.combine(today, sched_time)
#             logs = session.exec(
#                 select(MedicineLog).where(
#                     MedicineLog.compartment_number == comp.compartment_number,
#                     MedicineLog.taken_at >= sched_datetime - timedelta(minutes=30),
#                     MedicineLog.taken_at <= sched_datetime + timedelta(minutes=90),
#                     MedicineLog.action == "taken"
#                 )
#             ).all()

#             if not logs and now > sched_datetime:
#                 missed.append({
#                     "compartment": comp.compartment_number,
#                     "medicine": comp.medicine_name,
#                     "missed_time": sched_time.strftime("%H:%M:%S"),
#                     "period": label
#                 })

#     return missed

@app.get("/logs/punteggio-percentuale")
def punteggio_percentuale(session: Session = Depends(get_session)):
    compartments = session.exec(select(Compartment)).all()
    taken_logs = session.exec(
        select(MedicineLog).where(MedicineLog.action == "taken")
    ).all()

    expected = 0
    actual = 0
    today = datetime.utcnow().date()

    for comp in compartments:
        if not comp.to_be_repeated:
            continue

        for t in [comp.morning_time, comp.afternoon_time, comp.evening_time]:
            if t:
                expected += 1
                expected_datetime = datetime.combine(today, t)
                match = any(
                    abs((log.taken_at - expected_datetime).total_seconds()) < 3600 and
                    log.compartment_number == comp.compartment_number
                    for log in taken_logs
                )
                if match:
                    actual += 1

    score = round((actual / expected) * 100, 2) if expected else None
    return {
        "expected_doses_today": expected,
        "taken_doses_today": actual,
        "adherence_score_percent": score
    }

@app.get("/logs/low-stock-history")
def low_stock_history(session: Session = Depends(get_session)):
    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.low_stock == True
        ).order_by(MedicineLog.taken_at.desc())
    ).all()

    return [
        {
            "compartment": log.compartment_number,
            "medicine": log.medicine_name,
            "timestamp": log.taken_at,
            "remaining": log.remaining_pills
        }
        for log in logs
    ]


@app.post("/populate-logs-test/")
def populate_test_logs(session: Session = Depends(get_session)):
    """
    Crea log di 7 giorni per test:
    - Comparti 1, 2, 3
    - Medicine prese/non prese
    - Orari schedulati (mattina, pomeriggio, sera)
    - Ricariche ogni 3 giorni
    """
    medicine_names = ["Paracetamol", "Ibuprofen", "Antibiotic"]
    scheduled_times = [time(8, 0), time(14, 0), time(20, 0)]
    today = datetime.utcnow().date()

    for days_ago in range(7):
        date = today - timedelta(days=days_ago)
        for comp_num in [1, 2, 3]:
            medicine = medicine_names[comp_num - 1]

            for sched_time in scheduled_times:
                scheduled_dt = datetime.combine(date, sched_time)
                taken = choice([True, False])
                if taken:
                    taken_time = scheduled_dt + timedelta(minutes=randint(-15, 90))
                    log = MedicineLog(
                        compartment_number=comp_num,
                        medicine_name=medicine,
                        taken_at=taken_time,
                        action="taken",
                        remaining_pills=randint(1, 8),
                        low_stock=randint(0, 1) == 1,
                        scheduled_time=sched_time,
                        is_late=taken_time.time() > sched_time
                    )
                    session.add(log)

            if days_ago % 3 == 0:  # refill ogni 3 giorni
                refill_time = datetime.combine(date, time(10, 30))
                log = MedicineLog(
                    compartment_number=comp_num,
                    medicine_name=medicine,
                    taken_at=refill_time,
                    action="refill",
                    remaining_pills=randint(5, 10),
                    low_stock=False
                )
                session.add(log)

    session.commit()
    return {"message": "Dati di test inseriti con successo ✅"}

from random import random

# @app.post("/populate-scheduled-vs-taken-logs")
# def populate_scheduled_vs_taken_logs(session: Session = Depends(get_session)):
#     compartments = session.exec(
#         select(Compartment).where(Compartment.to_be_repeated == True)
#     ).all()

#     days_back = 7
#     logs_created = 0
#     taken_created = 0
#     today = datetime.utcnow().date()

#     for offset in range(days_back):
#         scheduled_date = today - timedelta(days=offset)

#         for comp in compartments:
#             scheduled_times = [
#                 ("morning", comp.morning_time),
#                 ("afternoon", comp.afternoon_time),
#                 ("evening", comp.evening_time)
#             ]

#             for label, sched_time in scheduled_times:
#                 if not sched_time:
#                     continue

#                 # Log scheduled
#                 sched_log = MedicineLog(
#                     compartment_number=comp.compartment_number,
#                     medicine_name=comp.medicine_name,
#                     action="scheduled",
#                     scheduled_time=sched_time,
#                     scheduled_date=scheduled_date,
#                     taken_at=datetime.combine(scheduled_date, sched_time)
#                 )
#                 session.add(sched_log)
#                 logs_created += 1

#                 # 70% chance of being taken
#                 if random() < 0.7:
#                     taken_time = datetime.combine(scheduled_date, sched_time) + timedelta(minutes=randint(-10, 60))
#                     taken_log = MedicineLog(
#                         compartment_number=comp.compartment_number,
#                         medicine_name=comp.medicine_name,
#                         action="taken",
#                         taken_at=taken_time,
#                         remaining_pills=randint(2, 9),
#                         low_stock=randint(0, 1) == 1,
#                         scheduled_time=sched_time,
#                         scheduled_date=scheduled_date,
#                         is_late=taken_time.time() > sched_time
#                     )
#                     session.add(taken_log)
#                     taken_created += 1

#     session.commit()
#     return {
#         "message": f"✅ Created {logs_created} scheduled logs and {taken_created} taken logs for past {days_back} days."
#     }

# @app.get("/logs/scheduled-vs-taken-summary")
# def scheduled_vs_taken_summary(session: Session = Depends(get_session)):
#     today = datetime.utcnow().date()
#     summary = []

#     for day_offset in range(7):
#         day = today - timedelta(days=day_offset)

#         scheduled_logs = session.exec(
#             select(MedicineLog).where(
#                 MedicineLog.action == "scheduled",
#                 MedicineLog.scheduled_date == day
#             )
#         ).all()

#         taken_logs = session.exec(
#             select(MedicineLog).where(
#                 MedicineLog.action == "taken",
#                 MedicineLog.scheduled_date == day
#             )
#         ).all()

#         scheduled_count = len(scheduled_logs)
#         taken_count = len(taken_logs)
#         missed_count = scheduled_count - taken_count
#         adherence = round((taken_count / scheduled_count) * 100, 2) if scheduled_count else None

#         summary.append({
#             "date": day.isoformat(),
#             "scheduled": scheduled_count,
#             "taken": taken_count,
#             "missed": missed_count,
#             "adherence_percent": adherence
#         })

#     return summary


@app.get("/logs/correct")
def get_correctly_taken_logs(session: Session = Depends(get_session)):
    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.action == "taken",
            MedicineLog.is_late == False
        )
    ).all()
    return logs
@app.get("/logs/late")
def get_late_logs(session: Session = Depends(get_session)):
    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.action == "taken",
            MedicineLog.is_late == True
        )
    ).all()
    return logs


@app.get("/logs/missed")
def get_missed_logs(session: Session = Depends(get_session)):
    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.action == "missed"
        )
    ).all()
    return logs


@app.post("/logs/update-missed")
def update_missed_logs(session: Session = Depends(get_session)):
    now = datetime.utcnow()

    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.action == "scheduled",
            MedicineLog.scheduled_time != None,
            MedicineLog.scheduled_date == now.date()
        )
    ).all()

    updated = 0
    for log in logs:
        sched_dt = datetime.combine(log.scheduled_date, log.scheduled_time)
        if (now - sched_dt).total_seconds() > 3600:
            log.action = "missed"
            log.is_late = True
            session.add(log)
            updated += 1

    session.commit()
    return {"updated_missed": updated}


@app.post("/daily-reset")
def reset_medicines_and_schedule(session: Session = Depends(get_session)):
    today = datetime.utcnow().date()

    compartments = session.exec(select(Compartment).where(Compartment.to_be_repeated == True)).all()
    logs_created = 0
    compartments_reset = 0

    for comp in compartments:
        # Reset dello stato "taken" per ogni medicine ripetibile
        comp.taken = False
        comp.taken_at = None
        session.add(comp)
        compartments_reset += 1

        times = [comp.morning_time, comp.afternoon_time, comp.evening_time]
        for sched_time in times:
            if not sched_time:
                continue

            # Evita duplicati
            exists = session.exec(select(MedicineLog).where(
                MedicineLog.compartment_number == comp.compartment_number,
                MedicineLog.scheduled_date == today,
                MedicineLog.scheduled_time == sched_time,
                MedicineLog.action == "scheduled"
            )).first()

            if not exists:
                log = MedicineLog(
                    compartment_number=comp.compartment_number,
                    medicine_name=comp.medicine_name,
                    action="scheduled",
                    scheduled_time=sched_time,
                    scheduled_date=today,
                    remaining_pills=comp.number_of_medicines,
                    low_stock=comp.number_of_medicines < 4
                )
                session.add(log)
                logs_created += 1

    session.commit()

    return {
        "message": "Reset giornaliero completato ✅",
        "compartments_reset": compartments_reset,
        "logs_created": logs_created
    }


@app.get("/logs/daily-status")
def get_daily_status(session: Session = Depends(get_session)):
    today = datetime.utcnow().date()
    compartments = session.exec(select(Compartment)).all()
    status = []

    for comp in compartments:
        logs = session.exec(
            select(MedicineLog).where(
                MedicineLog.compartment_number == comp.compartment_number,
                MedicineLog.scheduled_date == today
            )
        ).all()

        log_map = {log.scheduled_time: log.action + (" (late)" if log.is_late else "") for log in logs}

        if comp.to_be_repeated:
            status.append({
                "compartment": comp.compartment_number,
                "medicine": comp.medicine_name,
                "total_to_take": len([t for t in [comp.morning_time, comp.afternoon_time, comp.evening_time] if t]),
                "morning": log_map.get(comp.morning_time, "not scheduled"),
                "afternoon": log_map.get(comp.afternoon_time, "not scheduled"),
                "evening": log_map.get(comp.evening_time, "not scheduled")
            })
        else:
            status.append({
                "compartment": comp.compartment_number,
                "medicine": comp.medicine_name,
                "total_to_take": 1,
                "scheduled_time": comp.time_if_not_repeated,
                "status": log_map.get(comp.time_if_not_repeated, "not scheduled")
            })

    return status