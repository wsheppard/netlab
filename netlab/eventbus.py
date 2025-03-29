import asyncio
import uuid
from time import time
from typing import Any, Callable, Optional, List, Literal, TypeVar, Generic, Awaitable

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
    name: str
    path: str           # Path in bus
    data: T            # Typed payload
    evtype: str
    timestamp: float = Field(default_factory=time)

    def render(self) -> str:
        return self.data.model_dump_json()

class SimpleMessage(BaseModel):
    message: str
    data: Any|None = None
    
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

from aioreactive import AsyncIteratorObserver, AsyncObservable
from typing import AsyncIterator, TypeVar, Generic

_T = TypeVar("_T")

class LazyIterSubscription(Generic[_T]):
    def __init__(self, source: AsyncObservable[_T]):
        self._observer = AsyncIteratorObserver(source)

    async def __aenter__(self) -> AsyncIterator[_T]:
        # triggers lazy subscription on first iteration
        return self._observer

    async def __aexit__(self, *args):
        await self._observer.dispose_async()

    def __aiter__(self) -> AsyncIterator[_T]:
        return self._observer


# --- EventBus ---

class EventBus:
    def __init__(
        self,
        name: Optional[str] = None,
        parent: Optional["EventBus"] = None,
        guid: Optional[str] = None,
        subject: Optional[AsyncSubject] = None,
    ):
        self.guid = guid or str(uuid.uuid4())
        self.name = name or self.guid
        self.parent = parent
        self._subject: AsyncSubject = subject or AsyncSubject()
        self._children: List["EventBus"] = []

        self._destroyed: AsyncSubject[bool] = AsyncSubject()
        asyncio.ensure_future(self._emit_system("BusCreated"))

        if parent:
            psub = self.subscribe( self._push_to_parent ) 
            asyncio.ensure_future(psub)
        else:
            self._parent_subs = None

    async def _push_to_parent(self, e:Event):
        if self.parent:
            await self.parent._subject.asend(e)

    @property
    def path(self):
        if self.parent:
            return f"{self.parent.path}.{self.name}"
        else:
            return self.name

    async def simple(self, msg:str, data:Any|None=None):
        await self.emit(SimpleMessage(message=msg,data=data))

    def child(self, name=None, guid=None, subject=None) -> "EventBus":
        child = EventBus(parent=self, guid=guid, name=name)
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
            name=self.name,
            path=self.path,
            evtype=type(model).__name__,
            data=model

        )
        await self._subject.asend(event)

    async def _emit_upwards(self, event: Event):
        await self._subject.asend(event)
        if self.parent:
            await self.parent._emit_upwards(event)

    async def _emit_system(self, type_: system_message_type):
        await self.emit(SystemMessage(type=type_, bus=self.guid, name=self.name), guid=NULL_GUID)

    def observe(self, *ops: Callable) -> rx.AsyncObservable:
        """
        Convenience 
        """
        takeu = rx.take_until( self._destroyed )
        return pipe(self._subject, takeu, *ops)

    def observe_types(self, event_types: list[type], *ops: Callable):
        def _type_filter( e:Event ):
            return isinstance( e.data, tuple(event_types) )
        filt = rx.filter( _type_filter )
        return self.observe(filt, *ops)

    def observe_type(self, event_type: type, *ops: Callable):
        return self.observe_types([event_type], *ops)

    def subscribe(
        self,
        on_next: Callable[[Event], Awaitable[None]]):
        return subscribe( self.observe(), on_next )

    def subscribe_types(
        self,
        event_types: list[type],
        on_next: Callable[[Event], Awaitable[None]]):
        return subscribe( self.observe_types(event_types), on_next )

    def subscribe_type(
        self,
        event_type: type,
        on_next: Callable[[Event], Awaitable[None]]):
        return self.subscribe_types([event_type], on_next)

    def iter(self, *ops) -> LazyIterSubscription:
        return LazyIterSubscription(self.observe(*ops))

    def iter_type(self, event_type: type, *ops: Callable) -> LazyIterSubscription:
        return LazyIterSubscription(self.observe_type(event_type, *ops))

    def iter_types(self, event_types: list[type], *ops: Callable) -> LazyIterSubscription:
        return LazyIterSubscription(self.observe_types(event_types, *ops))

    def _get_root(self) -> "EventBus":
        return self if not self.parent else self.parent._get_root()

    def __repr__(self):
        return f"<EventBus name={self.name} guid={self.guid}>"

