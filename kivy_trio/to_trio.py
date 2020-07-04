"""Async Trio callbacks from Kivy
=================================

"""
import trio
import outcome
import math
from functools import wraps, partial
from typing import Optional, Generator, Callable
from asyncio import iscoroutinefunction

from kivy.clock import ClockBase, ClockNotRunningError, ClockEvent

from kivy_trio.context import trio_entry, trio_thread

__all__ = (
    'KivyEventCancelled', 'mark', 'kivy_run_in_async', 'KivyCallbackEvent')


class KivyEventCancelled(BaseException):
    pass


def mark(__func, *args, **kwargs):
    return __func, args, kwargs


def _callback_raise_exception(*args):
    raise TypeError('Should never have timed out infinite wait')


def _do_nothing(*args):
    pass


class KivyCallbackEvent:

    __slots__ = 'gen', 'clock', 'orig_func', 'clock_event'

    gen: Optional[Generator]

    clock: ClockBase

    orig_func: Callable

    clock_event: Optional[ClockEvent]

    def __init__(self, clock: ClockBase, func, gen, ret_func):
        super().__init__()
        self.clock = clock
        self.orig_func = func

        # if kivy stops before we finished processing gen, cancel it
        self.gen = gen
        event = self.clock_event = clock.create_lifecycle_aware_trigger(
            _callback_raise_exception, self._cancel, timeout=math.inf,
            release_ref=False)

        try:
            event()

            try:
                entry_token = trio_entry.get()
                thread_token = trio_thread.get()
            except LookupError as e:
                raise LookupError(
                    "Cannot schedule trio callback because no running trio "
                    "event loop found. Have you forgotten to initialize "
                    "trio_entry or trio_thread?"
                ) from e

            if thread_token is entry_token:
                trio.lowlevel.spawn_system_task(
                    self._async_callback, *ret_func)
            else:
                entry_token.run_sync_soon(self._spawn_task, ret_func)
        except BaseException as e:
            self._cancel(e=e)

    def cancel(self, *args):
        """Only safe from kivy thread.
        """
        self._cancel(suppress_cancel=True)

    def _cancel(self, *args, e=None, suppress_cancel=False):
        if self.gen is None:
            return

        if e is None:
            e = KivyEventCancelled

        self.clock_event.cancel()
        self.clock_event = None

        try:
            self.gen.throw(e)
        except StopIteration:
            # generator is done
            pass
        except KivyEventCancelled:
            # it is canceled
            if not suppress_cancel:
                raise
        finally:
            # can't cancel again
            self.gen = None

    def _spawn_task(self, ret_func):
        try:
            trio.lowlevel.spawn_system_task(self._async_callback, *ret_func)
        except BaseException as e:
            event = self.clock.create_lifecycle_aware_trigger(
                partial(self._cancel, e=e), _do_nothing, release_ref=False)
            try:
                event()
            except ClockNotRunningError:
                pass

    def _kivy_callback(self, result, *args):
        # check if canceled
        if self.gen is None:
            return

        try:
            result.send(self.gen)
        except StopIteration:
            pass
        else:
            raise RuntimeError(
                f'{self.orig_func} does not return after the first yield. '
                f'Does it maybe have more than one yield? Only one yield '
                f'statement is supported')
        finally:
            self.clock_event.cancel()
            self.clock_event = None
            self.gen = None

    async def _async_callback(self, ret_func, ret_args=(), ret_kwargs=None):
        # check if canceled
        if self.gen is None:
            return

        if iscoroutinefunction(ret_func):
            with trio.CancelScope(shield=True):
                # TODO: cancel this when event is cancelled
                result = await outcome.acapture(
                    ret_func, *ret_args, **(ret_kwargs or {}))

            assert not (hasattr(result, 'error') and
                        isinstance(result.error, trio.Cancelled))
        else:
            result = outcome.capture(ret_func, *ret_args, **(ret_kwargs or {}))

        # check if canceled
        if self.gen is None:
            return

        event = self.clock.create_lifecycle_aware_trigger(
            partial(self._kivy_callback, result), _do_nothing,
            release_ref=False)
        try:
            event()
        except ClockNotRunningError:
            pass


def kivy_run_in_async(func):
    """May be raised from other threads.
    """
    @wraps(func)
    def run_to_yield(*args, **kwargs):
        from kivy.clock import Clock

        gen = func(*args, **kwargs)
        try:
            ret_func = next(gen)
        except StopIteration:
            return None

        return KivyCallbackEvent(Clock, func, gen, ret_func)

    return run_to_yield
