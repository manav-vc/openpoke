"""Generation context also propagated into asyncio.to_thread tool calls."""
from contextvars import ContextVar
from contextlib import contextmanager

execution_context = ContextVar("openpoke_execution_context", default=None)


def check_execution_generation() -> None:
    current = execution_context.get()
    if current is not None:
        registry, generation = current
        with registry.connect(generation=generation):
            pass


@contextmanager
def generation_write_guard():
    """Serialize an external-store write with a registry reset transaction."""
    current = execution_context.get()
    if current is None:
        yield
    else:
        registry, generation = current
        with registry.connect(write=True, generation=generation):
            yield
