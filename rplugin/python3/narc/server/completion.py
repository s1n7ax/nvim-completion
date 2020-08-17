from asyncio import Queue, gather, wait
from dataclasses import dataclass
from math import inf
from os import linesep
from typing import Awaitable, Callable, Dict, Iterator, List, Optional, Set, Tuple

from pynvim import Nvim

from ..shared.nvim import print
from ..shared.sql import AConnection
from ..shared.types import Comm, Context, Position
from .context import gen_context, goahead
from .fuzzy import fuzzy
from .logging import log
from .nvim import VimCompletion
from .settings import load_factories
from .sql import init, populate_batch, populate_suggestions
from .types import BufferContext, Completion, Settings, SourceFactory, Step


@dataclass(frozen=True)
class GenOptions:
    force: bool = False


@dataclass(frozen=True)
class StepContext:
    batch: int
    timeout: float
    force: bool


StepFunction = Callable[[AConnection, Context, StepContext], Awaitable[None]]


async def manufacture(
    nvim: Nvim, name: str, factory: SourceFactory
) -> Tuple[StepFunction, Queue]:
    chan: Queue = Queue()
    comm = Comm(nvim=nvim, chan=chan)
    src = await factory.manufacture(comm, factory.seed)

    async def source(
        conn: AConnection, context: Context, s_context: StepContext
    ) -> None:
        timeout = s_context.timeout
        acc: List[Completion] = []

        async def cont() -> None:
            async for comp in src(context):
                acc.append(comp)

        done, pending = await wait((cont(),), timeout=timeout)
        for p in pending:
            p.cancel()
        await gather(*done)
        await populate_suggestions(
            conn, batch=s_context.batch, source=1, completions=acc
        )

        if pending:
            timeout_fmt = round(timeout * 1000)
            msg1 = "⚠️  Completion source timed out - "
            msg2 = f"{name}, exceeded {timeout_fmt}ms{linesep}"
            await print(nvim, msg1 + msg2)

    return source, chan


async def osha(
    nvim: Nvim, name: str, factory: SourceFactory, retries: int
) -> Tuple[str, StepFunction, Optional[Queue]]:
    async def nil_steps(_: AConnection, __: Context, ___: StepContext) -> None:
        pass

    try:
        step_fn, chan = await manufacture(nvim, name=name, factory=factory)
    except Exception as e:
        message = f"Error in source {name}:{linesep}{e}"
        log.exception("%s", message)
        return name, nil_steps, None
    else:
        errored = 0

        async def o_step(
            conn: AConnection, context: Context, s_context: StepContext
        ) -> None:
            nonlocal errored
            if errored >= retries:
                return
            else:
                try:
                    await step_fn(conn, context, s_context)
                except Exception as e:
                    errored += 1
                    message = f"Error in source {name}:{linesep}{e}"
                    log.exception("%s", message)
                    return
                else:
                    errored = 0

        return name, o_step, chan


def buffer_opts(
    factories: Dict[str, SourceFactory], buf_context: BufferContext
) -> Tuple[Set[str], Dict[str, float]]:
    def is_enabled(name: str, factory: SourceFactory) -> bool:
        if name in buf_context.sources:
            spec = buf_context.sources.get(name)
            if spec is not None:
                enabled = spec.enabled
                if enabled is not None:
                    return enabled
        return factory.enabled

    enabled: Set[str] = {
        name for name, factory in factories.items() if is_enabled(name, factory=factory)
    }

    limits = {
        **{name: fact.limit for name, fact in factories.items() if name in enabled},
    }

    return enabled, limits


async def merge(
    nvim: Nvim, settings: Settings
) -> Tuple[
    Callable[[GenOptions], Awaitable[Tuple[Position, Iterator[VimCompletion]]]],
    Dict[str, Queue],
]:
    display_opt, match_opt, cache_opt = settings.display, settings.match, settings.cache
    factories = load_factories(settings=settings)
    src_gen = await gather(
        *(
            osha(nvim, name=name, factory=factory, retries=settings.retries)
            for name, factory in factories.items()
        )
    )
    sources: Dict[str, StepFunction] = {name: source for name, source, _ in src_gen}

    conn = AConnection()
    await init(conn)

    async def gen(options: GenOptions) -> Tuple[Position, Iterator[VimCompletion]]:
        timeout = inf if options.force else settings.timeout
        context, buf_context = await gen_context(nvim, options=match_opt)
        position = context.position

        batch = await populate_batch(conn, position=position)
        s_context = StepContext(batch=batch, timeout=timeout, force=options.force)

        enabled, limits = buffer_opts(factories, buf_context=buf_context)

        if options.force or goahead(context):

            source_gen = (
                source(conn, context, s_context)
                for name, source in sources.items()
                if name in enabled
            )
            await gather(*source_gen)

            return (
                position,
                fuzzy(iter(()), display=display_opt, options=match_opt, limits=limits),
            )
        else:
            return position, iter(())

    chans: Dict[str, Queue] = {name: chan for name, _, chan in src_gen if chan}
    return gen, chans
