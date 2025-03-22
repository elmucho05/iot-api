from typing import Optional, List
from typing import Annotated
from datetime import datetime, time, timedelta

from fastapi.middleware.cors import CORSMiddleware


from datetime import time
from fastapi import FastAPI, HTTPException, Depends, Query, Body
from sqlmodel import SQLModel, Field, Session, create_engine, select, delete
from typing import List, Dict
from pydantic import BaseModel
import json
from dateutil import parser
import httpx
ADAFRUIT_FEED_URLS = {
    1:  "https://io.adafruit.com/api/v2/webhooks/feed/hDxXDFEQ581nYMaygY2nHRMN9nXm",
    2:  "https://io.adafruit.com/api/v2/webhooks/feed/CfAs3xJMCTNSgTENkrnZgQok8cg2",
    3:  "https://io.adafruit.com/api/v2/webhooks/feed/8ethcgUgXJ6CxZh3Bx5SSYihoNnG" 
}

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

class MedicineLog(SQLModel, table= True):
    id: Optional[int] = Field(default=None, primary_key=True)
    compartment_number: int
    medicine_name: str
    taken_at: datetime
    action: str = Field(default="taken")  # can be "taken", "refill", "manual"



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


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# API Endpoints

@app.post("/compartments/createcompartment", response_model=CompartmentPublic)
def create_compartment(compartment: CompartmentCreate, session: Session = Depends(get_session)):
    """
    Ensure that if `to_be_repeated` is False, `time_if_not_repeated` is required.
    Validate that `compartment_number` is 1, 2, or 3.    """
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
    session.add(db_compartment)
    session.commit()
    session.refresh(db_compartment)
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
    return compartment



@app.delete("/compartments/{compartment_number}/{medicine_name}")
def delete_medicine_from_compartment(compartment_number: int, medicine_name: str, session: Session = Depends(get_session)):
    """
    Deletes all entries of a specific medicine from a given compartment.
    """
    # Validate compartment_number
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    # Find the medicine in the specified compartment
    compartments = session.exec(
        select(Compartment).where(
            (Compartment.compartment_number == compartment_number) &
            (Compartment.medicine_name == medicine_name)
        )
    ).all()

    if not compartments:
        raise HTTPException(
            status_code=404,
            detail=f"No medicine named '{medicine_name}' found in compartment {compartment_number}."
        )

    # Delete all matching records
    for compartment in compartments:
        session.delete(compartment)
    
    session.commit()
    
    return {
        "message": f"All entries of '{medicine_name}' have been removed from compartment {compartment_number}."
    }

@app.delete("/compartments/")
def delete_all_compartments(session: Session = Depends(get_session)):
    """
    Deletes all compartments from the database.
    """
    session.exec(select(Compartment).delete())
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
def get_pending_medicines(compartment_number: int, session: Session = Depends(get_session)):
    """
    Retrieves all medicines in the given compartment that are still pending (taken=False).
    """
    if compartment_number not in [1, 2, 3]:
        raise HTTPException(
            status_code=400,
            detail="compartment_number must be 1, 2, or 3."
        )

    medicines = session.exec(
        select(Compartment).where(
            (Compartment.compartment_number == compartment_number) &
            (Compartment.taken == False)
        )
    ).all()

    return medicines

#################################################################
###################### Adafruit stuff ###########################
#################################################################
@app.post("/adafruit-taken-webhook/")
def pill_taken_webhook(data: List[AdafruitData], session: Session = Depends(get_session)):
    for entry in data:
        feed = entry.feed_name.lower()

        feed_map = {
            "comp1-taken": 1,
            "comp2-taken": 2,
            "comp3-taken": 3
        }
        comp_num = feed_map.get(feed)
        if not comp_num:
            continue  # unknown feed, ignore

        comp = session.exec(
            select(Compartment).where(Compartment.compartment_number == comp_num)
        ).first()

        if not comp:
            continue

        if entry.value.strip() == "1":
            comp.number_of_medicines -= 1
            comp.number_of_medicines = max(comp.number_of_medicines, 0)  # prevent negatives
            comp.taken = True
            comp.taken_at = parser.isoparse(entry.created_at)
            comp.low_stock = comp.number_of_medicines < 4
            
            log = MedicineLog(
                compartment_number=comp.compartment_number,
                medicine_name=comp.medicine_name,
                taken_at=comp.taken_at,
                action="taken"
            )

            session.add(log)
            session.add(comp)
            session.commit()
            session.refresh(comp)

            return {
                "message": f"Compartment {comp_num} updated",
                "new_count": comp.number_of_medicines,
                "low_stock": comp.low_stock
            }

    return {"message": "No valid update processed"}


@app.get("/logs/", response_model=List[MedicineLog])
def get_all_logs(session: Session = Depends(get_session)):
    return session.exec(select(MedicineLog).order_by(MedicineLog.taken_at.desc())).all()

@app.get("/logs/by-day/{date}", response_model=List[MedicineLog])
def get_logs_by_day(date: str, session: Session = Depends(get_session)):
    try:
        day_start = datetime.fromisoformat(date)
    except:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    day_end = day_start + timedelta(days=1)

    logs = session.exec(
        select(MedicineLog).where(
            MedicineLog.taken_at >= day_start,
            MedicineLog.taken_at < day_end
        ).order_by(MedicineLog.taken_at)
    ).all()

    return logs


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

# @app.post("/compartments/{compartment_number}/refill")
# def refill_medicine(compartment_number : int, refill: RefillRequest, session : Session = Depends(get_session)):
#     compartment = session.exec(select(Compartment).where(Compartment.compartment_number==compartment_number)).first()

#     if compartment_number not in [1, 2, 3]:
#         raise HTTPException(
#             status_code=400,
#             detail="Invalid compartment number. Only 1, 2, or 3 are allowed."
#         )
    
#     if not compartment:
#         raise HTTPException(status_code=404, detail="Compartment not found")
    
#     compartment.number_of_medicines += refill.amount  #if i want just the value, can simply remove the +
#     compartment.taken = False
#     compartment.low_stock = compartment.number_of_medicines < 4
#     session.add(compartment)
#     session.commit()
#     session.refresh(compartment)

#     where_to_send = ADAFRUIT_FEED_URLS.get(compartment_number)
    
#     if where_to_send:
#         try:
#             httpx.post(
#                 where_to_send,
#                 headers={"X-AIO-Key": ""},
#                 json={"value": compartment.number_of_medicines}
#             )
#         except Exception as e:
#             print("Failed to update Adafruit:", e)

#     return {
#         "message": f"Refilled compartment {compartment_number} with {refill.amount} units.",
#         "current_total": compartment.number_of_medicines
#     }

# @app.post("/populate-test-data/")
# def populate_test_data(session: Session = Depends(get_session)):
#     """
#     Populates the database with:
#     - Compartment 1: A repeated medicine with normal stock
#     - Compartment 2: A repeated medicine with low stock, already taken
#     - Compartment 3: A one-time medicine, not taken yet
#     """

#     # Clear any existing data
#     session.exec(delete(Compartment))
#     session.commit()

#     def str_to_time(t: str) -> Optional[time]:
#         return datetime.strptime(t, "%H:%M:%S").time() if t else None

#     now = datetime.utcnow()

#     test_data = [
#         Compartment(
#             compartment_number=1,
#             medicine_name="Paracetamol",
#             number_of_medicines=10,
#             to_be_repeated=True,
#             taken=False,
#             taken_at=None,
#             low_stock=False,
#             morning_time=str_to_time("08:00:00"),
#             afternoon_time=str_to_time("14:00:00"),
#             evening_time=str_to_time("20:00:00"),
#             time_if_not_repeated=None
#         ),
#         Compartment(
#             compartment_number=2,
#             medicine_name="Ibuprofen",
#             number_of_medicines=2,  # Low stock
#             to_be_repeated=True,
#             taken=True,
#             taken_at=now,
#             low_stock=True,
#             morning_time=str_to_time("09:00:00"),
#             afternoon_time=str_to_time("15:00:00"),
#             evening_time=str_to_time("21:00:00"),
#             time_if_not_repeated=None
#         ),
#         Compartment(
#             compartment_number=3,
#             medicine_name="Antibiotic",
#             number_of_medicines=5,
#             to_be_repeated=False,
#             taken=False,
#             taken_at=None,
#             low_stock=False,
#             morning_time=None,
#             afternoon_time=None,
#             evening_time=None,
#             time_if_not_repeated=str_to_time("12:00:00")
#         )
#     ]

#     session.add_all(test_data)
#     session.commit()

#     return {"message": "Test data added successfully!"}


# @app.post("/adafruit-taken-webhook/")
# def pill_taken_from_adafruit(data: List[AdafruitData], session: Session = Depends(get_session)):
#     for entry in data:
#         feed_name = entry.feed_name.lower()

#         # Map feed name to compartment number (taken feeds only)
#         feed_to_compartment = {
#             "comp1-taken": 1,
#             "comp2-taken": 2,
#             "comp3-taken": 3
#         }

#         compartment_number = feed_to_compartment.get(feed_name)
#         if not compartment_number:
#             continue

#         compartment = session.exec(
#             select(Compartment).where(Compartment.compartment_number == compartment_number)
#         ).first()

#         if not compartment:
#             return {"message": f"No medicine found in compartment {compartment_number}."}

#         try:
#             remaining = int(entry.value)
#         except ValueError:
#             return {"error": f"Invalid value: {entry.value}"}

#         try:
#             taken_time = parser.isoparse(entry.created_at)
#         except Exception:
#             taken_time = datetime.utcnow()

#         compartment.taken = True
#         compartment.taken_at = taken_time
#         compartment.number_of_medicines = remaining
#         compartment.low_stock = remaining < 4

#         session.add(compartment)
#         session.commit()
#         session.refresh(compartment)

#         return {
#             "message": f"Marked compartment {compartment_number} as taken.",
#             "new_value": remaining,
#             "taken_at": taken_time.isoformat(),
#             "low_stock": compartment.low_stock
#         }

#     return {"message": "No valid compartment feed found."}
