import os

from sqlmodel import Session, SQLModel, create_engine

# Overridable so the pre-flight check can run the whole demo against a scratch
# database instead of the one you are about to record with.
DATABASE_URL = os.environ.get("MANDATE_DB_URL", "sqlite:///./mandate_compiler.db")

engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
