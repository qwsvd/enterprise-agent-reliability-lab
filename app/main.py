import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401
from app.api import get_session, router
from app.database import Database
from app.seed import seed_demo_data


def create_app(database_url: str | None = None) -> FastAPI:
    database = Database(database_url or os.getenv("DATABASE_URL", "sqlite:///./after_sales.db"))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database.create_schema()
        with database.session_factory() as session:
            seed_demo_data(session)
        yield
        database.dispose()

    application = FastAPI(
        title="Enterprise After-Sales Service", version="0.1.0", lifespan=lifespan
    )
    application.dependency_overrides[get_session] = database.sessions
    application.include_router(router)
    application.state.database = database
    return application


app = create_app()

