"""Context Manager
==================

"""

from trio.lowlevel import TrioToken, current_trio_token
from functools import wraps, partial
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Optional
import time
from queue import Queue, Empty

from kivy.clock import ClockBase

__all__ = (
    'get_thread_context_managers', 'trio_context_manager',
    'kivy_context_manager', 'ContextVarContextManager',
    'TrioTokenContextManager', 'KivyClockContextManager', 'kivy_clock',
    'kivy_thread', 'trio_entry', 'trio_thread'
)


class ContextVarContextManager:

    context_var = None

    value = None

    token = None

    def __init__(self, context_var, value=None):
        self.context_var = context_var
        self.value = value

    def __enter__(self):
        self.token = self.context_var.set(self.value)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.context_var.reset(self.token)
        self.token = None


class TrioTokenContextManager(ContextVarContextManager):

    def __init__(self, token: TrioToken):
        super(TrioTokenContextManager, self).__init__(trio_entry)
        self.value = token


class KivyClockContextManager(ContextVarContextManager):

    def __init__(self, clock: ClockBase):
        super(KivyClockContextManager, self).__init__(kivy_clock)
        self.value = clock


kivy_clock = ContextVar('kivy_clock')
kivy_thread: ContextVar[Optional[ClockBase]] = ContextVar(
    'kivy_thread', default=None)
trio_entry: ContextVar[TrioToken] = ContextVar('trio_entry')
trio_thread: ContextVar[Optional[TrioToken]] = ContextVar(
    'trio_thread', default=None)


@contextmanager
def trio_context_manager(clock, queue):
    token = current_trio_token()
    queue.put(token)

    with KivyClockContextManager(clock):
        with ContextVarContextManager(trio_thread, token):
            yield


@contextmanager
def kivy_context_manager(clock, queue):
    ts = time.perf_counter()
    res = None
    while time.perf_counter() - ts < 1:
        try:
            res = queue.get(block=True, timeout=.1)
        except Empty:
            pass
        else:
            break

    if res is None:
        raise TimeoutError(
            "Timed out waiting for trio thread to initialize. If there's only "
            "on thread, did you enter kivy_context_manager before "
            "trio_context_manager? With multiple threads, the trio thread "
            "should be started before entering kivy_context_manager to "
            "prevent deadlock of the kivy thread")

    with TrioTokenContextManager(res):
        with ContextVarContextManager(kivy_thread, clock):
            yield


def get_thread_context_managers():
    """Gets the context managers required to run kivy and trio threads.

    :return: tuple of kivy_context_manager, trio_context_manager
    """
    from kivy.clock import Clock
    queue = Queue(maxsize=1)

    return partial(kivy_context_manager, Clock, queue), \
        partial(trio_context_manager, Clock, queue)
