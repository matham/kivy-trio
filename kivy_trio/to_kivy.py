"""Kivy callbacks from Trio
===========================

"""
import trio
import outcome
import math
from functools import wraps, partial
from typing import Optional, Callable
from collections import deque
from asyncio import iscoroutinefunction

from kivy.clock import ClockBase, ClockNotRunningError, ClockEvent

from kivy_trio.context import kivy_clock, kivy_thread, trio_entry, trio_thread

__all__ = (
    'EventLoopStoppedError', 'async_run_in_kivy', 'AsyncKivyEventQueue',
    'AsyncKivyBind')


class EventLoopStoppedError(Exception):
    pass


def _do_nothing(*args):
    pass


def _report_kivy_back_in_trio_thread_fn(task_container, task):
    # This function gets scheduled into the trio run loop to deliver the
    # thread's result.
    # def do_release_then_return_result():
    #     # if canceled, do the cancellation otherwise the result.
    #     if task_container[0] is not None:
    #         task_container[0]()
    #     return task_container[1].unwrap()
    # result = outcome.capture(do_release_then_return_result)

    # currently this is only called when kivy callback was called/not canceled
    trio.lowlevel.reschedule(task, task_container[1])


def async_run_in_kivy(func=None):
    # if it's canceled in the async side, it either succeeds if we cancel on
    # kivy side or waits until kivy calls us back. If Kivy stops early it still
    # processes the callback so it's fine. So it either raises a
    # EventLoopStoppedError immediately or fails
    if func is None:
        return partial(async_run_in_kivy)

    if iscoroutinefunction(func):
        raise ValueError(
            f'run_in_kivy called with async coroutine "{func}", but '
            f'run_in_kivy does not support coroutines (only sync functions)')

    @trio.lowlevel.enable_ki_protection
    @wraps(func)
    async def inner_func(*args, **kwargs):
        """When canceled, executed work is discarded. Thread safe.
        """
        try:
            clock: ClockBase = kivy_clock.get()
            kivy_thread_clock: ClockBase = kivy_thread.get()
        except LookupError as e:
            raise LookupError(
                "Cannot schedule kivy callback because no running kivy "
                "event loop found. Have you forgotten to initialize "
                "kivy_clock or kivy_thread?"
            ) from e
        lock = {}

        if kivy_thread_clock is clock:
            await trio.lowlevel.checkpoint_if_cancelled()
            # behavior should be the same whether it's in kivy's thread
            if clock.has_ended:
                raise EventLoopStoppedError(
                    f'async_run_in_kivy failed to complete <{func}> because '
                    f'clock stopped')
            return func(*args, **kwargs)

        task = trio.lowlevel.current_task()
        token = trio.lowlevel.current_trio_token()
        # items are: cancellation callback, the outcome, whether it was either
        # canceled or callback is already executing. Currently we don't handle
        # cancellation callback because it either succeeds in canceling
        # immediately or we get the kivy result
        task_container = [None, None, False]

        def kivy_thread_callback(*largs):
            # This is the function that runs in the worker thread to do the
            # actual work and then schedule the calls to report back to trio
            # are we handling the callback?
            lock.setdefault(None, 0)
            # it was canceled so we have nothing to do
            if lock[None] is None:
                return

            task_container[1] = outcome.capture(func, *args, **kwargs)

            # this may raise a RunFinishedError, but
            # The entire run finished, so our particular tasks are
            # certainly long gone - this shouldn't have happened because
            # either the task should still be waiting because it wasn't
            # canceled or if it was canceled we should have returned above
            token.run_sync_soon(
                _report_kivy_back_in_trio_thread_fn, task_container, task)

        def kivy_thread_callback_stopped(*largs):
            # This is the function that runs in the worker thread to do the
            # actual work and then schedule the calls to report back to trio
            # are we handling the callback?
            lock.setdefault(None, 0)
            # it was canceled so we have nothing to do
            if lock[None] is None:
                return

            def raise_stopped_error():
                raise EventLoopStoppedError(
                    f'async_run_in_kivy failed to complete <{func}> because '
                    f'clock stopped')

            task_container[1] = outcome.capture(raise_stopped_error)
            token.run_sync_soon(
                _report_kivy_back_in_trio_thread_fn, task_container, task)

        trigger = clock.create_lifecycle_aware_trigger(
            kivy_thread_callback, kivy_thread_callback_stopped,
            release_ref=False)
        try:
            trigger()
        except ClockNotRunningError as e:
            raise EventLoopStoppedError(
                f'async_run_in_kivy failed to complete <{func}>') from e
        # kivy_thread_callback will be called, unless canceled below

        def abort(raise_cancel):
            # task_container[0] = raise_cancel

            # try canceling
            trigger.cancel()
            lock.setdefault(None, None)
            # it is canceled, kivy shouldn't handle it
            if lock[None] is None:
                return trio.lowlevel.Abort.SUCCEEDED
            # it was already started so we can't cancel - wait for result
            return trio.lowlevel.Abort.FAILED

        return await trio.lowlevel.wait_task_rescheduled(abort)

    return inner_func


class AsyncKivyEventQueue:
    """A class for asynchronously iterating values in a queue and waiting
    for the queue to be updated with new values through a callback function.

    An instance is an async iterator which for every iteration waits for
    callbacks to add values to the queue and then returns it.

    :meth:`stop` is called automatically if kivy's event loop exits while
    it's in the with block.

    :Parameters:

        `filter`: callable or None
            A callable that is called with :meth:`callback`'s positional
            arguments. When provided, if it returns false, this call is dropped.
        `convert`: callable or None
            A callable that is called with :meth:`callback`'s positional
            arguments. It is called immediately as opposed to async.
            If provided, the return value of convert is returned by
            the iterator rather than the original value. Helpful
            for callback values that need to be processed immediately.
        `max_len`: int or None
            If None, the callback queue may grow to an arbitrary length.
            Otherwise, it is bounded to maxlen. Once it's full, when new items
            are added a corresponding number of oldest items are discarded.
        `thread_fn`: callable or None
            If reading from the queue is done with a different thread than
            writing it, this is the callback that schedules in the read thread.
    """

    _quit: bool = False

    send_channel: Optional[trio.MemorySendChannel] = None

    receive_channel: [trio.MemoryReceiveChannel] = None

    queue: Optional[deque] = None

    filter: Optional[Callable] = None

    convert: Optional[Callable] = None

    _max_len = None

    _eof_event: Optional[ClockEvent] = None

    _eof_trio_event: Optional[trio.Event] = None

    def __init__(
            self, filter_fn: Optional[Callable] = None,
            convert: Optional[Callable] = None, max_len: Optional[int] = None,
            **kwargs):
        super().__init__(**kwargs)
        self.filter = filter_fn
        self.convert = convert
        self._max_len = max_len

    async def __aenter__(self):
        if self.queue is not None:
            raise TypeError('Cannot re-enter because it was not properly '
                            'cleaned up on the last exit')

        self.queue = deque(maxlen=self._max_len)
        self.send_channel, self.receive_channel = trio.open_memory_channel(1)
        self._eof_trio_event = trio.Event()
        self._quit = False

        try:
            await async_run_in_kivy(self.start_data_stream)()
        except BaseException:
            self._eof_trio_event = None
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._quit = True
        self.send_channel = self.receive_channel = None
        try:
            await async_run_in_kivy(self.stop_data_stream)()
        except EventLoopStoppedError:
            # if kivy ended, then it must have called _clock_ended_callback,
            # which would have called stop_data_stream already or in the future
            # so we just need to wait for it to finish
            if self._eof_trio_event is not None:
                await self._eof_trio_event.wait()
        self.queue = None  # this lets us detect if stop raised an error
        self._eof_trio_event = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.send_channel is None:
            raise TypeError('Can only iterate when context was entered')

        while not self.queue and not self._quit:
            await self.receive_channel.receive()

        if self.queue:
            return self.queue.popleft()
        raise StopAsyncIteration

    def stop(self, *args):
        """This function may be executed from another thread. Ignores if the
        queue is not in the with block.

        May raise an exception if restarted while it's here or if not
        initialized.

        May be called multiple times.
        """
        if self._quit:
            return
        self._quit = True

        try:
            entry_token = trio_entry.get()
            thread_token = trio_thread.get()
        except LookupError as e:
            if self.queue is None:
                return
            # if there is a queue, meaning it's in the with block, but we can't
            # find the token, then the user didn't set it
            raise LookupError(
                "Cannot stop because no running trio event loop found. "
                "Have you forgotten to initialize "
                "trio_entry or trio_thread?") from e

        if thread_token is entry_token:
            # same thread
            self._send_on_channel()
        else:
            try:
                entry_token.run_sync_soon(self._send_on_channel)
            except trio.RunFinishedError as e:
                if self.queue is None:
                    return
                # should never be able to be finished if the queue is not None
                # except if the user entered again while we're doing this
                raise

    def add_item(self, *args):
        """This function may be executed from another thread
        because the callback may be bound to code executing from an external
        thread. This is meant to be driven by the trio side, so if the
        trio side is not awaiting, this simply returns silently.
        """
        f = self.filter
        if self._quit or f is not None and not f(*args):
            return

        convert = self.convert
        if convert is not None:
            args = convert(*args)

        queue = self.queue
        if queue is None:
            return
        queue.append(args)

        try:
            entry_token = trio_entry.get()
            thread_token = trio_thread.get()
        except LookupError as e:
            # we had a queue, which implies trio was present a moment ago, but
            # now we can't find a token. So if the queue is gone, move on. But
            # if the queue is still there the token was never there so we
            # should raise an error
            if self.queue is None:
                return
            raise LookupError(
                "Cannot add item because no running trio event loop found. "
                "Have you forgotten to initialize trio_entry or trio_thread?"
            ) from e

        if thread_token is entry_token:
            # same thread
            self._send_on_channel()
        else:
            try:
                entry_token.run_sync_soon(self._send_on_channel)
            except trio.RunFinishedError as e:
                if self.queue is None:
                    return
                # should never be able to be finished if the queue is not None
                # except if the user entered again while we're doing this
                raise

    def _send_on_channel(self):
        send_channel = self.send_channel
        if send_channel is not None:
            try:
                send_channel.send_nowait(None)
            except trio.WouldBlock:
                pass

    def _clock_ended_callback(self, *args):
        # if the clock ended, we have to stop
        self.stop()
        # we also have to call stop_data_stream because the trio thread won't be
        # able to call it because it'll raise a EventLoopStoppedError
        self.stop_data_stream()

        event = self._eof_trio_event
        if event is None:
            return

        try:
            entry_token = trio_entry.get()
            thread_token = trio_thread.get()
        except LookupError as e:
            raise LookupError(
                "Cannot stop because no running trio event loop found. "
                "Have you forgotten to initialize "
                "trio_entry or trio_thread?") from e

        if thread_token is entry_token:
            # same thread
            event.set()
        else:
            try:
                entry_token.run_sync_soon(event.set)
            except trio.RunFinishedError:
                # nothing to signal - it's done
                pass

    def start_data_stream(self):
        try:
            clock: ClockBase = kivy_clock.get()
        except LookupError as e:
            raise LookupError(
                "Cannot schedule kivy callback because no running kivy "
                "event loop found. Have you forgotten to initialize "
                "kivy_clock or kivy_thread?"
            ) from e

        event = self._eof_event = clock.create_lifecycle_aware_trigger(
            _do_nothing, self._clock_ended_callback, timeout=math.inf,
            release_ref=False)
        event()

    def stop_data_stream(self):
        """May be called multiple times.
        """
        if self._eof_event is not None:
            self._eof_event.cancel()
            self._eof_event = None


class AsyncKivyBind(AsyncKivyEventQueue):
    """A class for asynchronously observing kivy properties and events.

    Creates an async iterator which for every iteration waits and
    returns the property or event value for every time the property changes
    or the event is dispatched.

    The returned value is identical to the list of values passed to a function
    bound to the event or property with bind. So at minimum it's a one element
    (for events) or two element (for properties, instance and value) list.

    :Parameters:
        `bound_obj`: :class:`EventDispatcher`
            The :class:`EventDispatcher` instance that contains the property
            or event being observed.
        `bound_name`: str
            The property or event name to observe.
        `current`: bool
            Whether the iterator should return the current value on its
            first class (True) or wait for the first event/property dispatch
            before having a value (False). Defaults to True.
            Only if it's a property and not an event.
    E.g.::
        async for x, y in AsyncBindQueue(
            bound_obj=widget, bound_name='size', convert=lambda x: x[1]):
            print(value)
    Or::
        async for touch in AsyncBindQueue(
            bound_obj=widget, bound_name='on_touch_down',
            convert=lambda x: x[0]):
            print(value)
    """

    bound_obj = None

    bound_name = ''

    bound_uid = 0

    current = True

    def __init__(self, bound_obj, bound_name, current=True, **kwargs):
        super().__init__(**kwargs)
        self.bound_name = bound_name
        self.bound_obj = bound_obj
        self.current = current

    def start_data_stream(self):
        super().start_data_stream()

        bound_obj = self.bound_obj
        bound_name = self.bound_name

        uid = self.bound_uid = bound_obj.fbind(bound_name, self.add_item)
        if not uid:
            raise ValueError(
                '{} is not a recognized property or event of {}'
                ''.format(bound_name, bound_obj))

        if self.current and not bound_obj.is_event_type(bound_name):
            self.add_item(bound_obj, getattr(bound_obj, bound_name))

    def stop_data_stream(self):
        super().stop_data_stream()

        if self.bound_uid:
            self.bound_obj.unbind_uid(self.bound_name, self.bound_uid)
            self.bound_uid = 0
            self.bound_obj = None
