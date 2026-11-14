"""FastAPI dependencies shared by the routers: container, database session, customer session."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.errors import ProblemError
from hub.auth.customer_session import COOKIE_NAME
from hub.db.models import Customer
from hub.db.repositories import CustomerRepository
from hub.deps import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def db_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    """One session per request. Handlers commit explicitly; anything not committed is rolled
    back when the session closes."""
    if container.database is None:
        raise RuntimeError("the app container has no database")
    async with container.database.sessions() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session)]


def _unauthorized() -> ProblemError:
    return ProblemError(401, "unauthorized", "A customer session is required.")


def session_customer_id(
    container: ContainerDep,
    hub_customer: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> uuid.UUID:
    """The customer id of a valid signed cookie. Runs before the database session is opened,
    so a request without a session costs no connection."""
    signer = container.session_signer
    customer_id = None if hub_customer is None or signer is None else signer.verify(hub_customer)
    if customer_id is None:
        raise _unauthorized()
    return customer_id


async def current_customer(
    customer_id: Annotated[uuid.UUID, Depends(session_customer_id)],
    session: SessionDep,
) -> Customer:
    """The customer of the signed session cookie. The identity never comes from the request
    body (AGENTS.md, never-do 3). An erased (anonymised) customer has no session."""
    customer = await CustomerRepository(session).get(customer_id)
    if customer is None or customer.anonymised_at is not None:
        raise _unauthorized()
    return customer


CustomerDep = Annotated[Customer, Depends(current_customer)]


def client_ip(request: Request) -> str:
    # Behind Cloud Run the proxy address is replaced by uvicorn's --proxy-headers (deploy task).
    return request.client.host if request.client else "unknown"
