import asyncio
import uuid
from time import time
from typing import Callable, Optional, List, Literal, TypeVar, Generic, Awaitable

from pydantic import BaseModel, Field
import aioreactive as rx
from aioreactive import AsyncDisposable, AsyncObservable, AsyncSubject, AsyncIteratorObserver, subscription
import aioreactive as rx
from expression.core import pipe

# --- Constants ---

T = TypeVar("T", bound=BaseModel)
NULL_GUID = "00000000-0000-0000-0000-000000000000"

# --- Models ---

class Event(BaseModel, Generic[T]):
    guid: str                  # Emitter GUID (source of the event)
    data: T            # Typed payload
    timestamp: float = Field(default_factory=time)

    def render(self) -> str:
        return self.data.model_dump_json()

class SimpleMessage(BaseModel):
    message: str
    
system_message_type = Literal[
        "BusCreated",
        "BusRemoved",
        "SubscriptionCreated",
        "SubscriptionClosed"
    ]

class SystemMessage(BaseModel):
    type: system_message_type
    bus: str
    name: str


# --- Filters ---

def system_event(e: Event):
    return e.guid == NULL_GUID


async def subscribe(
    observable: AsyncObservable,
    on_next: Callable[[Event], Awaitable[None]],
    on_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
    on_completed: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncDisposable:
    observer = rx.AsyncAnonymousObserver(asend=on_next, athrow=on_error, aclose=on_completed)
    return await observable.subscribe_async(observer)


# --- EventBus ---

class EventBus:
    def __init__(
        self,
        parent: Optional["EventBus"] = None,
        guid: Optional[str] = None,
        name: Optional[str] = None,
        subject: Optional[AsyncSubject] = None,
    ):
        self.guid = guid or str(uuid.uuid4())
        self.name = name or self.guid
        self.parent = parent
        self._subject: AsyncSubject = subject or AsyncSubject()
        self._children: List["EventBus"] = []
        self._subs: List[Awaitable[AsyncDisposable]] = []

        self._destroyed: AsyncSubject[bool] = AsyncSubject()
        asyncio.ensure_future(self._emit_system("BusCreated"))

    @property
    def path(self):
        if self.parent:
            return f"{self.parent.path}.{self.name}"
        else:
            return self.name

    def child(self, guid=None, name=None, subject=None) -> "EventBus":
        child = EventBus(parent=self, guid=guid, name=name, subject=subject)
        self._children.append(child)
        return child

    async def destroy(self):
        await self._emit_system("BusRemoved")
        await self._destroyed.asend(True)
        for child in self._children:
            await child.destroy()
        self._children.clear()
        self.parent = None

    async def emit(self, model: BaseModel, guid: Optional[str] = None):
        event = Event(
            guid=guid or self.guid,
            data=model
        )
        await self._emit_upwards(event)

    async def _emit_upwards(self, event: Event):
        await self._subject.asend(event)
        if self.parent:
            await self.parent._emit_upwards(event)

    async def _emit_system(self, type_: system_message_type):
        await self.emit(SystemMessage(type=type_, bus=self.guid, name=self.name), guid=NULL_GUID)

    def observe(self, *ops: Callable) -> rx.AsyncSubject:
        """
        Convenience 
        """
        return pipe(self._subject, *ops) if ops else self._subject

    async def subscribe(
        self,
        on_next: Callable[[Event], Awaitable[None]]):
        return await subscribe( self.observe(), on_next )


    def _get_root(self) -> "EventBus":
        return self if not self.parent else self.parent._get_root()

    def __repr__(self):
        return f"<EventBus name={self.name} guid={self.guid}>"

