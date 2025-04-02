import asyncio
import uuid
from time import time
from typing import Any, Callable, Optional, List, Literal, Type, TypeVar, Generic, Awaitable

from pydantic import BaseModel, Field
import aioreactive as rx
from aioreactive import AsyncDisposable, AsyncObservable, AsyncSubject, AsyncIteratorObserver, subscription
from expression.core import pipe
from aioreactive import AsyncIteratorObserver, AsyncObservable
from typing import AsyncIterator, TypeVar, Generic

from .rxutils import LazyIterSubscription, subscribe

# --- Constants ---

T = TypeVar("T", bound=BaseModel)
NULL_GUID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# --- Models ---

class Event(BaseModel, Generic[T]):
    timestamp: float = Field(default_factory=time)
    name: str
    path: str           # Path in bus
    evtype: str
    data: T            # Typed payload
    guid: uuid.UUID                  # Emitter GUID (source of the event)

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
    bus: uuid.UUID
    name: str

# --- Utility Observers ----
async def printer(e:Event):
    print(e.model_dump_json())

# --- Filters ---
# Call these to get rx pipeline entries
# i.e. pipe( types_filter(), guid_filter() etc.. )


def types_filter( event_types: list[type[BaseModel]] ):
    """
    Filter events by payload type
    """
    def _type_filter( e:Event ):
        return isinstance( e.data, tuple(event_types) )
    return rx.filter( _type_filter )

def type_filter( event_type: type[BaseModel] ):
    return types_filter( [event_type] )

def guid_filter( guid: uuid.UUID ):
    def _guid_filter( e:Event ):
        return e.guid == guid
    return rx.filter( _guid_filter )

def system_event():
    return guid_filter(NULL_GUID)

# --- Maps ----
def event_to_json():
    def _event_to_json( ev: Event ) -> str:
        return ev.model_dump_json()
    return rx.map( _event_to_json )

FT = TypeVar("FT")
def filter_type(typ: Type[FT], predicate: Callable[[FT], bool]):
    """
    Allow filtering on a particular type - all other types pass
    """
    def _filter( ev: Event ):
        if isinstance(ev.data, typ):
            return predicate(ev.data)
        else:
            return True
    return rx.filter( _filter )

# --- EventBus ---

class EventBus:
    def __init__(
        self,
        name: Optional[str] = None,
        parent: Optional["EventBus"] = None,
        guid: Optional[uuid.UUID] = None,
        subject: Optional[AsyncSubject] = None,
    ):
        self.guid = guid or uuid.uuid4()
        self.name = name or str(self.guid)
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

    async def emit(self, model: BaseModel, guid: Optional[uuid.UUID] = None):
        event = Event(
            guid=guid or self.guid,
            name=self.name,
            path=self.path,
            evtype=type(model).__name__,
            data=model

        )
        await self._subject.asend(event)

    async def _emit_system(self, type_: system_message_type):
        await self.emit(SystemMessage(type=type_, bus=self.guid, name=self.name), guid=NULL_GUID)

    def observe_destroyed(self) -> rx.AsyncObservable:
        """
        Return this which will only ever emit when the bus is finished - allows lifcycle
        management outside of the bus.
        """
        return pipe( self._destroyed, rx.take(1) )

    def observe(self, *ops: Callable) -> rx.AsyncObservable:
        """
        The base observer pipe which includes the cleanup mechanism.
        """
        takeu = rx.take_until( self._destroyed )
        return pipe(self._subject, takeu, *ops)

    def observe_types(self, event_types: list[type], *ops: Callable):
        """
        Filter on the payload type
        """
        return self.observe(types_filter(event_types), *ops)

    def observe_guid(self, guid: uuid.UUID, *ops: Callable):
        """
        Filter on guid
        """
        return self.observe(guid_filter(guid), *ops)

    def observe_type(self, event_type: type, *ops: Callable):
        return self.observe_types([event_type], *ops)

    def observe_self(self):
        """
        Get only messages generated in this bus
        """
        return self.observe(guid_filter(self.guid))

    # The following are just convenience wrappers really around the observeable objects
    # created above.

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

    async def printer(self):
        """
        Setup a simple printer
        """
        return await subscribe( self.observe(), printer )

    def __repr__(self):
        return f"<EventBus name={self.name} guid={self.guid}>"

