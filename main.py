from typing import Optional, List
from typing import Annotated
from datetime import datetime, time

from datetime import time
from fastapi import FastAPI, HTTPException, Depends, Query
from sqlmodel import SQLModel, Field, Session, create_engine, select, delete
from typing import List, Dict
from pydantic import BaseModel
import json
from dateutil import parser


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

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# API Endpoints

@app.post("/compartments/", response_model=CompartmentPublic)
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

@app.patch("/compartments/{compartment_id}", response_model=CompartmentPublic)
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


####################################################################################################
##### Take medicine #####
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
#### Adafruit stuff ####

@app.post("/adafruit-webhook/")
def receive_adafruit_data(data: List[AdafruitData], session: Session = Depends(get_session)):
    """
    Receives data from Adafruit IO, determines which compartment is activated,
    and marks the medicine in that compartment as taken.
    """
    for entry in data:
        feed_name = entry.feed_name.lower()  # Convert to lowercase to avoid case issues

        # Determine which compartment is triggered
        if feed_name == "comp1":
            compartment_number = 1
        elif feed_name == "comp2":
            compartment_number = 2
        elif feed_name == "comp3":
            compartment_number = 3
        else:
            continue  # Ignore feeds that don't match compartment names

        # Find the medicine in the correct compartment
        compartment = session.exec(
            select(Compartment).where(Compartment.compartment_number == compartment_number)
        ).first()

        if not compartment:
            return {"message": f"No medicine found in compartment {compartment_number}."}

        try :
            taken_time = parser.isoparse(entry.created_at)
        except Exception:
            taken_time = datetime.utcnow() #fallback

        try:
            remaining_pills = int(entry.value)
        except ValueError:
            return {"error": f"Invalid pill count in feed: {entry.value}"}

        # Mark the medicine as taken
        compartment.taken = True
        compartment.taken_at = taken_time
        compartment.number_of_medicines = remaining_pills
        
        compartment.low_stock = remaining_pills < 4


        session.add(compartment)
        session.commit()
        session.refresh(compartment)

        response = {
            "message": f"Medicine in compartment {compartment_number} marked as taken.",
            "compartment": compartment_number,
            "remaining_pills": remaining_pills,
            "low_stock": compartment.low_stock,
            "taken_at": taken_time.isoformat(),
            "medicine_name": compartment.medicine_name
        }
        return response

    return {"message": "No valid compartment found in the received data."}


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
    session.add(compartment)
    session.commit()
    session.refresh(compartment)
    return compartment

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
