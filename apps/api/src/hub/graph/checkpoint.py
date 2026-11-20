"""Where the workflow keeps its checkpoints (ADR-0008, data model "LangGraph checkpoint tables").

The app and the evaluation runner use Postgres (`langgraph-checkpoint-postgres`, psycopg 3):
the tables are created by migration 0009 as the migrator, so the app role only reads and writes
them. Unit tests use the in-memory saver.

The graph is compiled once per store, on first use (ai-pipeline §1: compiled once at start-up
with the checkpointer). It cannot happen when the container is built: the Postgres saver needs
a running event loop, and the container is built before the loop starts.

**Deserialisation allowlist.** The saver reads back Python objects. Only the state's own types
(`STATE_TYPES`) and LangGraph's built-in safe types are rebuilt; anything else written into the
checkpoint tables by someone with write access is returned as plain data, never constructed
(langgraph-checkpoint `JsonPlusSerializer`, `allowed_msgpack_modules`).
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from hub.graph.state import STATE_TYPES
from hub.observability.readiness import libpq_url

# Small: a run holds one connection only while a checkpoint is written.
POOL_MAX_SIZE = 5


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


@dataclass
class CheckpointStore:
    """`url` None: in memory (unit tests). Otherwise the Postgres database at `url`."""

    url: str | None = None
    _pool: AsyncConnectionPool[Any] | None = field(default=None, init=False, repr=False)
    _graph: Any = field(default=None, init=False, repr=False)
    _checkpointer: BaseCheckpointSaver[Any] | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @classmethod
    def memory(cls) -> "CheckpointStore":
        return cls(url=None)

    @classmethod
    def postgres(cls, url: str) -> "CheckpointStore":
        return cls(url=url)

    async def graph[T](self, compile_graph: Callable[[BaseCheckpointSaver[Any]], T]) -> T:
        """The compiled graph of this store, compiled on the first call."""
        async with self._lock:
            if self._graph is None:
                self._graph = compile_graph(await self._saved())
            graph: T = self._graph
            return graph

    async def delete_threads(self, thread_ids: Iterable[str]) -> int:
        """Delete every checkpoint of these threads (ticket ids) through the library
        (`adelete_thread`), never by hand (data model). For erasure and retention (P8-05).
        Returns how many threads were asked for; a thread without checkpoints is no error."""
        async with self._lock:
            saver = await self._saved()
        count = 0
        for thread_id in thread_ids:
            await saver.adelete_thread(thread_id)
            count += 1
        return count

    async def _saved(self) -> BaseCheckpointSaver[Any]:
        """One saver per store: the graph and `delete_threads` share it. Under `_lock`."""
        if self._checkpointer is None:
            self._checkpointer = await self._saver()
        return self._checkpointer

    async def _saver(self) -> BaseCheckpointSaver[Any]:
        if self.url is None:
            return InMemorySaver(serde=serializer())
        # autocommit and prepare_threshold=0: what the saver expects of its connections
        # (`AsyncPostgresSaver.from_conn_string`); no prepared statements also keeps it working
        # through the Neon pooler (operations §6).
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            libpq_url(self.url),
            max_size=POOL_MAX_SIZE,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        self._pool = pool
        return AsyncPostgresSaver(pool, serde=serializer())

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._graph = None
        self._checkpointer = None
