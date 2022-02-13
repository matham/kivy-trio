"""Async coroutines in Trio from Kivy
=====================================

:func:`kivy_run_in_async` is a kivy decorator that allows executing an async
coroutine in the trio context. E.g. after the trio and kivy
:mod:`kivy_trio.context` is initialized, the following async function:

.. code-block:: python

    async def send_device_message(delay, device, message):
        await trio.sleep(delay)
        return await device.send(message)

can be scheduled to run from within a kivy context as follows:

.. code-block:: python

    @kivy_run_in_async
    def kivy_send_message(message):
        dev = MyDevice()
        response = yield mark(send_device_message, delay, dev, message=message)
        print(f'Device responded with {response}')

Subsequently, from Kivy we can call ``event = kivy_send_message('hello')``
and this will suspend the function at the ``yield`` statement and schedule
``send_device_message`` to be called and waited on in
trio with the given parameters. When the coroutine returns in trio, the result
of the coroutine will automatically be sent to resume the generator, the
``yield`` statement will return the result and the ``kivy_send_message``
function will finish executing.

Exceptions
----------

If the async coroutine raises an exception, the exception will be caught and
sent to resume the generator at the yield statement so the generator will
raise that exception. So e.g.:

.. code-block:: python

    async def raise_error():
        raise ValueError()

    @kivy_run_in_async
    def kivy_send_message():
        try:
            response = yield mark(raise_error)
        except ValueError:
            print('Error caught')

when ``kivy_send_message`` is called, it'll catch the exception raised in trio
and print ``'Error caught'``.

Lifecycle and Cancellation
--------------------------

A :func:`kivy_run_in_async` decorated function or method may only be called
while the Kivy event loop and trio event loop are running. Otherwise, an
exception will be raised when the function is called.

If the kivy event loop ends while the coroutine is executing in trio, the event
will be canceled and a :class:`KivyEventCancelled` exception will be raised
into the generator. The coroutine will still finish executing in trio, but the
result will be discarded when it's done.

An waiting event may be explicitly canceled with
:meth:`KivyCallbackEvent.cancel`. As above a :class:`KivyEventCancelled`
exception will be raised into the generator and the coroutine will still finish
executing in trio, but its result will be discarded.

Threading
---------

A :func:`kivy_run_in_async` decorated function is only safe to be called from
the kivy thread, and only if the :mod:`kivy_trio.context` was properly
initialized. The coroutine will be executed in the trio context that it was
initialized to.

But the context can be initialized to a trio event loop running in the
same or in a different thread than kivy with identical behavior for
:func:`kivy_run_in_async`.
"""
import trio
from trio.lowlevel import current_trio_token
import outcome
import math
from functools import wraps, partial
from typing import Optional, Generator, Callable
from asyncio import iscoroutinefunction

from kivy.clock import ClockBase, ClockNotRunningError, ClockEvent

from kivy_trio.context import trio_entry, trio_thread

__all__ = (
    'KivyEventCancelled', 'mark', 'kivy_run_in_async',
    'kivy_run_in_async_quiet', 'KivyCallbackEvent')


class KivyEventCancelled(BaseException):
    """The exception injected into the waiting :func:`kivy_run_in_async`
    decorated generator when the event is canceled.
    """
    pass


def mark(__func, *args, **kwargs):
    """Collects the function and its parameters to be used by
    :func:`kivy_run_in_async` as needed.

    :param __func: The function or method object.
    :param args: Any positional arguments.
    :param kwargs: Any keyword arguments.
    """
    return __func, args, kwargs


def _callback_raise_exception(*args):
    raise TypeError('Should never have timed out infinite wait')


def _do_nothing(*args):
    pass


class KivyCallbackEvent:
    """The event class returned when a :func:`kivy_run_in_async` decorated
    generator is called.

    This class should not be instantiated manually in user code.
    """

    __slots__ = '_gen', '_clock', '_orig_func', '_clock_event'

    _gen: Optional[Generator]

    _clock: ClockBase

    _orig_func: Callable

    _clock_event: Optional[ClockEvent]

    def __init__(self, clock: ClockBase, func, gen, ret_func):
        super().__init__()
        self._clock = clock
        self._orig_func = func

        # if kivy stops before we finished processing gen, cancel it
        self._gen = gen
        event = self._clock_event = clock.create_lifecycle_aware_trigger(
            _callback_raise_exception, self._cancel, timeout=math.inf,
            release_ref=False)

        try:
            event()

            try:
                entry_token = trio_entry.get(None)
                # trio_thread defaults to None
                thread_token = trio_thread.get()
                if entry_token is None:
                    entry_token = current_trio_token()
            except RuntimeError as e:
                raise LookupError(
                    "Cannot enter because no running trio event loop found. "
                    "Have you forgotten to initialize trio_entry with your "
                    "event loop token?") from e

            if thread_token is entry_token:
                trio.lowlevel.spawn_system_task(
                    self._async_callback, *ret_func)
            else:
                entry_token.run_sync_soon(self._spawn_task, ret_func)
        except BaseException as e:
            self._cancel(e=e)

    def cancel(self, *args):
        """Cancels the waiting event.

        Raises a :class:`KivyEventCancelled` exception into the generator.
        The coroutine will still finish executing in trio if it's not finished,
        but its result will be discarded.

        .. warning::

            Only safe to be called from the kivy thread.
        """
        self._cancel(suppress_cancel=True)

    def _cancel(self, *args, e=None, suppress_cancel=False):
        if self._gen is None:
            return

        if e is None:
            e = KivyEventCancelled

        self._clock_event.cancel()
        self._clock_event = None

        try:
            self._gen.throw(e)
        except StopIteration:
            # generator is done
            pass
        except KivyEventCancelled:
            # it is canceled
            if not suppress_cancel:
                raise
        finally:
            # can't cancel again
            self._gen = None

    def _spawn_task(self, ret_func):
        try:
            trio.lowlevel.spawn_system_task(self._async_callback, *ret_func)
        except BaseException as e:
            event = self._clock.create_lifecycle_aware_trigger(
                partial(self._cancel, e=e), _do_nothing, release_ref=False)
            try:
                event()
            except ClockNotRunningError:
                pass

    def _kivy_callback(self, result, *args):
        # check if canceled
        if self._gen is None:
            return

        try:
            result.send(self._gen)
        except StopIteration:
            pass
        else:
            raise RuntimeError(
                f'{self._orig_func} does not return after the first yield. '
                f'Does it maybe have more than one yield? Only one yield '
                f'statement is supported')
        finally:
            self._clock_event.cancel()
            self._clock_event = None
            self._gen = None

    async def _async_callback(self, ret_func, ret_args=(), ret_kwargs=None):
        # check if canceled
        if self._gen is None:
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
        if self._gen is None:
            return

        event = self._clock.create_lifecycle_aware_trigger(
            partial(self._kivy_callback, result), _do_nothing,
            release_ref=False)
        try:
            event()
        except ClockNotRunningError:
            pass


def kivy_run_in_async(gen):
    """Decorator that takes a generator that yields an async coroutine,
    schedules it in trio, and sends the result or exception as the return value
    of the ``yield`` statement in the generator.

    See :mod:`kivy_trio.to_trio` for details.
    """
    @wraps(gen)
    def run_to_yield(*args, **kwargs):
        from kivy.clock import Clock

        gen_inst = gen(*args, **kwargs)
        try:
            ret_func = next(gen_inst)
        except StopIteration:
            return None

        return KivyCallbackEvent(Clock, gen, gen_inst, ret_func)

    return run_to_yield


def kivy_run_in_async_quiet(async_func):
    """Decorator that takes a generator that yields an async coroutine,
    schedules it in trio, and sends the result or exception as the return value
    of the ``yield`` statement in the generator.

    See :mod:`kivy_trio.to_trio` for details.
    """
    @wraps(async_func)
    def run_to_yield(*args, **kwargs):
        from kivy.clock import Clock

        def gen():
            yield mark(async_func, *args, **kwargs)

        gen_inst = gen()
        ret_func = next(gen_inst)

        return KivyCallbackEvent(Clock, async_func, gen_inst, ret_func)

    return run_to_yield
