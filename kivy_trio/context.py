"""Context Manager
==================

"""

from trio.lowlevel import TrioToken, current_trio_token
from contextvars import ContextVar, Token
from contextlib import contextmanager
from typing import Optional, Any

from kivy.clock import ClockBase

__all__ = (
    'trio_context_manager', 'kivy_context_manager',
    'initialize_kivy_from_trio', 'kivy_trio_context_manager',
    'ContextVarContextManagerBase',
    'ContextVarContextManager', 'TrioTokenContextManager',
    'KivyClockContextManager', 'initialize_kivy_thread',
    'initialize_trio_thread', 'kivy_clock', 'kivy_thread', 'trio_entry',
    'trio_thread'
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


async def initialize_kivy_from_trio(clock: ClockBase = None):
    from kivy_trio.to_kivy import async_run_in_kivy
    if clock is None:
        from kivy.clock import Clock
        clock = Clock

    token = current_trio_token()

    await async_run_in_kivy(initialize_kivy_thread)(token, clock)


def initialize_trio_thread(clock: ClockBase, token: TrioToken = None):
    if token is None:
        token = current_trio_token()
    kivy_clock.set(clock)
    trio_thread.set(token)
    trio_entry.set(token)


def initialize_kivy_thread(token: TrioToken, clock: ClockBase = None):
    if clock is None:
        from kivy.clock import Clock
        clock = Clock

    trio_entry.set(token)
    kivy_thread.set(clock)
    kivy_clock.set(clock)


@contextmanager
def trio_context_manager(clock: ClockBase = None):
    if clock is None:
        from kivy.clock import Clock
        clock = Clock

    token = current_trio_token()
    with KivyClockContextManager(clock):
        with TrioTokenContextManager(token):
            with ContextVarContextManager(trio_thread, token):
                yield


@contextmanager
def kivy_context_manager(token: TrioToken):
    from kivy.clock import Clock
    with TrioTokenContextManager(token):
        with KivyClockContextManager(Clock):
            with ContextVarContextManager(kivy_thread, Clock):
                yield


@contextmanager
def kivy_trio_context_manager():
    from kivy.clock import Clock
    token = current_trio_token()

    with KivyClockContextManager(Clock):
        with ContextVarContextManager(trio_thread, token):
            with TrioTokenContextManager(token):
                with ContextVarContextManager(kivy_thread, Clock):
                    yield
