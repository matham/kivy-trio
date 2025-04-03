"""Calling into Kivy
====================

Executing sync functions in Kivy from Trio
------------------------------------------

Kivy is a GUI framework that runs an event loop that calls functions
synchronously upon interactions with the GUI. Some applications also run a
Trio eventloop executing async code in the Kivy thread (when kivy is run
asynchronously) or in a separate thread. This package enables asynchronously
executing synchronous code in the Kivy thread from Trio such that Trio is not
blocked waiting for the Kivy clock to execute the code.

E.g. after the trio and kivy :mod:`kivy_trio.context` is initialized, the
following function that updates a Kivy button's state:

.. code-block:: python

    @async_run_in_kivy
    def set_button(kivy_app, state):
        kivy_app.warn_button.state = state

can be scheduled to run in Kivy from within a Trio context, even when Trio is
running in a differernt thread - simply by calling it:

.. code-block:: python

    app = App.get_running_app()
    await set_button(app, 'down')

This uses the :func:`async_run_in_kivy` to automatically schedule the
synchronous function with Kivy's clock to run in the Kivy context by wrapping
it in an asynchronous decorator and waiting until it's done.

:func:`async_run_in_kivy` returns the function's return value and catches and
re-raises any exceptions in the waiting Trio context as shown in this example
that sets a label to a number, raising an exception if it's a negative
value:

.. code-block:: python

    import trio
    from kivy.app import App
    from kivy.uix.label import Label
    from random import random
    from kivy_trio.to_kivy import async_run_in_kivy
    from kivy_trio.context import initialize_shared_thread

    class DemoApp(App):

        start_event = None

        def build(self):
            return Label(text='Empty')

        @async_run_in_kivy
        def update_text(self):
            val = random() - .5
            if val < 0:
                raise ValueError(f'Cannot set it to "{val}"')

            self.root.text = str(val)
            return val

        def on_start(self):
            # notify the waiting async trio that kivy started so it can proceed
            initialize_shared_thread()
            self.start_event.set()

    async def run_app():
        app = DemoApp()
        app.start_event = trio.Event()

        async with trio.open_nursery() as nursery:
            # start app and wait until the app is started
            nursery.start_soon(app.async_run, 'trio')
            await app.start_event.wait()

            # now that the app is started, change the text, wait, and exit
            for _ in range(5):
                try:
                    print('set label value to', await app.update_text())
                except ValueError as e:
                    print(f'got exception "{e}"')
                await trio.sleep(2)

            app.stop()

    trio.run(run_app)

When run, this printed e.g.::

    got exception "Cannot set it to "-0.41975669370612656""
    set label value to 0.2312564066758095
    set label value to 0.34180029860423355
    set label value to 0.054374588655983325
    set label value to 0.08700667397013406

:func:`async_run_in_kivy` does the following when called as e.g.
``await app.update_text()`` in three phases:

1. First it wraps and schedules the underlying ``update_text`` method to be
   called by Kivy in the Kivy thread (if they share a thread and properly
   initialized it just executes it directly skipping the remaining steps).
2. Next, it waits for Kivy to execute the method, either saving its return
   value or catching the exception.
3. Finally, when the function has finished or raised an exception, the waiting
   async line is woken up returning the return value or re-raising the
   exception.

Consequently, the marked synchronous function is executed in Kivy, but the
return value is then passed back or any exception the function has raised is
similarly re-raised in the Trio context when the line resumes.

Lifecycle and Cancellation
--------------------------

A :func:`kivy_run_in_async` and :func:`kivy_run_in_async_quiet` decorated
function or method may only be called while the Kivy event loop and trio event
loop are running. Otherwise, an exception may be raised when the function is
called.

If the kivy event loop ends while the coroutine is executing in trio, such as
when the Kivy GUI exits, the event will be canceled and a
:class:`KivyEventCancelled` exception will be injected
into the generator. The coroutine will still finish executing in trio, but the
result will be discarded when it's done.

A waiting event may be explicitly canceled with
:meth:`KivyCallbackEvent.cancel`. As above a :class:`KivyEventCancelled`
exception will be injected into the generator and the coroutine will still
finish executing in trio, but its result will be discarded.

E.g. given the following functions:

.. code-block:: python

    async def send_device_message(delay, device, message):
        await trio.sleep(delay)
        result = await device.send(message)
        return result

    @kivy_run_in_async
    def kivy_send_message(delay, device, message):
        try:
            response = yield mark(
                send_device_message, delay, device, message=message)
            print(f'Device responded with {response}')
        except KivyEventCancelled:
            print('Event canceled')

then if we do:

.. code-block:: python

    >>> dev = MyDevice()
    >>> event = kivy_send_message(3, dev, 'hello')
    >>> # a little later in kivy
    >>> event.cancel()

this will print ``Event canceled``.

Threading
---------

A :func:`kivy_run_in_async` and :func:`kivy_run_in_async_quiet` decorated
function is only safe to be called from the kivy thread, and generally only if
the :mod:`kivy_trio.context` was properly initialized (if kivy and trio share
the thread initialization is not stricly nessecary). The coroutine will be
executed in the trio context that it was initialized to, which can be the same
or another thread.

See the :mod:`kivy_trio.context` for details.
"""
import trio
from trio.lowlevel import current_trio_token, TrioToken
import outcome
import math
from functools import wraps, partial
from typing import Optional, Callable, Awaitable, TypeVar, overload, Coroutine
from collections import deque
from asyncio import iscoroutinefunction

from kivy.clock import ClockBase, ClockNotRunningError, ClockEvent, Clock
from kivy.event import EventDispatcher

from kivy_trio.context import kivy_clock, kivy_thread, trio_entry, trio_thread

__all__ = (
    'EventLoopStoppedError', 'async_run_in_kivy', 'AsyncKivyEventQueue',
    'AsyncKivyBind')


T = TypeVar("T")


class EventLoopStoppedError(Exception):
    """Exception raised in trio when it is waiting to run something in Kivy,
    but the Kivy app already finished or finished while waiting.
    """
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


@overload
def async_run_in_kivy(
        func: Callable[..., T], clock: Optional[ClockBase] = ...
) -> Callable[..., Awaitable[T]]: ...


@overload
def async_run_in_kivy(
    clock: Optional[ClockBase] = ...
) -> Callable[[Callable[..., T]], Callable[..., Awaitable[T]]]: ...


def async_run_in_kivy(func=None, clock=None):
    """Decorator that runs the given function or method in a Kivy context
    waiting (asynchronously) until it's done.

    It is primarily useful when kivy and trio are running in different threads
    and we need to run some Kivy code that change the GUI, waiting until it's
    done. See :mod:`kivy_trio.context` and the note below for how to initialize
    kivy/trio so it knows the kivy/trio event loop it runs within.

    :param func: The synchronous function to be called in the Kivy event loop.
    :param clock: The kivy :attr:`~kivy.clock.Clock` to use to schedule the
        function, if needed. Defaults to :attr:`~kivy.clock.Clock` if not
        provided and :attr:`kivy_trio.context.kivy_clock` is not set, otherwise
        one of them is used in that order.

    E.g. the in the following app the trio async code will change a label
    and print the result:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.uix.label import Label
        from kivy_trio.to_kivy import async_run_in_kivy
        from kivy_trio.context import initialize_shared_thread


        class DemoApp(App):

            start_event = None

            def build(self):
                return Label(text='Empty')

            @async_run_in_kivy
            def update_text(self, text):
                # set the label of the text
                self.root.text = text
                return text * 2

            def on_start(self):
                # notify the waiting async trio that kivy started so it can
                # proceed
                initialize_shared_thread()
                self.start_event.set()


        async def run_app():
            app = DemoApp()
            app.start_event = trio.Event()

            async with trio.open_nursery() as nursery:
                # start app and wait until the app is started
                nursery.start_soon(app.async_run, 'trio')
                await app.start_event.wait()

                # now that the app is started, change the text, wait, and exit
                print(await app.update_text('App started'))
                await trio.sleep(2)
                print(await app.update_text('App closing'))
                await trio.sleep(2)
                app.stop()

        trio.run(run_app)

    When run, this prints::

        App startedApp started
        App closingApp closing

    and exits the Kivy app.

    Similarly, it will catch any exceptions in the decorated function and
    re-raise it in the waiting async code. E.g. If we change ``update_text`` to

    .. code-block:: python

        @async_run_in_kivy
        def update_text(self, text):
            raise ValueError(f'Cannot set it to "{text}"')

    then when run we'll get something like the following error::

        Traceback (most recent call last):
           File "mod.py", line 41, in <module>
             trio.run(run_app)
           File _run.py", line 1932, in run
             raise runner.main_task_outcome.error
           File "mod_16.py", line 35, in run_app
             print(await app.update_text('App started'))
           ...
           File "mod.py", line 17, in update_text
             raise ValueError(f'Cannot set it to "{text}"')
         ValueError: Cannot set it to "App started"

    .. note::

        If Kivy is running in a different thread (or if we're unsure which
        thread is running currently) it will schedule the function to be called
        using the Kivy Clock in the Kivy thread in the next clock frame,
        otherwise, if we know it's running in the same thread (e.g. because
        :func:`~kivy_trio.context.initialize_shared_thread` was used), it may
        call the function directly.
    """
    # if it's canceled in the async side, it either succeeds if we cancel on
    # kivy side or waits until kivy calls us back. If Kivy stops early it still
    # processes the callback so it's fine. So it either raises a
    # EventLoopStoppedError immediately or fails
    if func is None:
        return partial(async_run_in_kivy, clock=clock)

    if iscoroutinefunction(func):
        raise ValueError(
            f'run_in_kivy called with async coroutine "{func}", but '
            f'run_in_kivy does not support coroutines (only sync functions)')

    @trio.lowlevel.enable_ki_protection
    @wraps(func)
    async def inner_func(*args, **kwargs):
        """When canceled, executed work is discarded. Thread safe.
        """
        nonlocal clock
        if clock is None:
            clock = kivy_clock.get(Clock)
        # kivy_thread defaults to None
        kivy_thread_clock: ClockBase = kivy_thread.get()
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

        if not clock.has_started:
            raise EventLoopStoppedError(
                f'async_run_in_kivy failed to complete <{func}> because the '
                f'Kivy clock has not started to run yet. Consider using a '
                f'trio.Event() to wait until Kivy has started')

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
    for the queue to be updated with new values from a synchronous method
    :meth:`add_item`.

    An instance is an async iterator which for every iteration asynchronously
    waits for :meth:`add_item` to add values to the queue and then yields it.

    :Parameters:

        `filter`: callable or None
            See :attr:`filter`.
        `convert`: callable or None
            See :attr:`convert`.
        `max_len`: int or None
            If None, the queue may grow to an arbitrary length.
            Otherwise, it is bounded to ``maxlen``. Once it's full, when new
            items are added a corresponding number of oldest items are
            discarded.

    .. note::

        :meth:`stop` is called automatically if kivy's event loop exits while
        the queue is in its ``with`` block.

    E.g. try pressing the button in this app:

    .. code-block:: python

        from kivy_trio.to_kivy import AsyncKivyEventQueue
        from kivy.app import App
        from kivy.lang import Builder
        import trio

        class MyApp(App):

            i = 0
            queue: AsyncKivyEventQueue = None

            async def run_app(self):
                # run app and trio queue
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self.run_queue)
                    nursery.start_soon(self.async_run, 'trio')

            async def run_queue(self):
                async with AsyncKivyEventQueue() as queue:
                    # save queue so we can add stuff from button
                    self.queue = queue
                    # queue will finish when stop is called below
                    async for a, b in queue:
                        print(f'got {a}, {b}')

            def button_pressed(self):
                # add items to queue and stop queue/app after 5
                self.queue.add_item(self.i, self.i ** 2)
                self.i += 1
                if self.i == 5:
                    self.queue.stop()
                    self.stop()

            def build(self):
                return Builder.load_string(
                    "Button:\\n"
                    "    text: 'Press me'\\n"
                    "    on_release: app.button_pressed()")

        trio.run(MyApp().run_app)
    """

    _quit: bool = False

    _send_channel: Optional[trio.MemorySendChannel] = None

    _receive_channel: Optional[trio.MemoryReceiveChannel] = None

    _queue: Optional[deque] = None

    filter: Optional[Callable[..., bool]] = None
    """A callable that is internally called with :meth:`add_item` 's
    positional arguments for each :meth:`add_item` call.

    When provided, if the filter function returns false for these arguments,
    :meth:`add_item` won't enqueue the item.
    """

    convert: Optional[Callable] = None
    """A callable that is internally called with :meth:`add_item` 's
    positional arguments for each :meth:`add_item` call.

    When provided, the return value of ``convert`` is enqueued and returned by
    the iterator rather than the original value. It is helpful
    for callback values that need to be processed immediately in the
    synchronous context that adds it.
    """

    _max_len = None

    _eof_event: Optional[ClockEvent] = None

    _eof_trio_event: Optional[trio.Event] = None

    _trio_token: Optional[TrioToken] = None

    def __init__(
            self, filter: Optional[Callable[..., bool]] = None,
            convert: Optional[Callable] = None, max_len: Optional[int] = None,
            **kwargs):
        super().__init__(**kwargs)
        self.filter = filter
        self.convert = convert
        self._max_len = max_len

    async def __aenter__(self):
        if self._queue is not None:
            raise TypeError('Cannot re-enter because it was not properly '
                            'cleaned up on the last exit')

        try:
            entry_token = trio_entry.get(None)
            if entry_token is None:
                entry_token = current_trio_token()
        except RuntimeError as e:
            if self._queue is None:
                return
            # if there is a queue, meaning it's in the with block, but we can't
            # find the token, then the user didn't set it
            raise LookupError(
                "Cannot enter because no running trio event loop found. "
                "Have you forgotten to initialize trio_entry with your event "
                "loop token?") from e

        self._trio_token = entry_token
        self._queue = deque(maxlen=self._max_len)
        self._send_channel, self._receive_channel = trio.open_memory_channel(1)
        self._eof_trio_event = trio.Event()
        self._quit = False

        try:
            await async_run_in_kivy(self._start_data_stream)()
        except BaseException:
            self._eof_trio_event = None
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._quit = True
        self._send_channel = self._receive_channel = None
        try:
            await async_run_in_kivy(self._stop_data_stream)()
        except EventLoopStoppedError:
            # if kivy ended, then it must have called _clock_ended_callback,
            # which would have called stop_data_stream already or in the future
            # so we just need to wait for it to finish
            if self._eof_trio_event is not None:
                await self._eof_trio_event.wait()
        self._queue = None  # this lets us detect if stop raised an error
        self._eof_trio_event = None
        self._trio_token = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._send_channel is None:
            raise TypeError('Can only iterate when context was entered')

        while not self._queue and not self._quit:
            await self._receive_channel.receive()

        if self._queue:
            return self._queue.popleft()
        raise StopAsyncIteration

    def stop(self, *args):
        """Call from the synchronous side to make the async iterator end.

        This method may be executed from another thread. It is ignored though
        if the queue is not in the with block.

        It may raise an exception if the iterator is restarted while it's in the
        method or if not initialized.

        May be called multiple times.
        """
        if self._quit:
            return
        self._quit = True

        entry_token = self._trio_token
        if trio_thread.get() is entry_token:
            # same thread
            self._send_on_channel()
        else:
            try:
                entry_token.run_sync_soon(self._send_on_channel)
            except trio.RunFinishedError as e:
                if self._queue is None:
                    return
                # should never be able to be finished if the queue is not None
                # except if the user entered again while we're doing this
                raise

    def add_item(self, *args):
        """Adds the args to the queue to be returned by the async iterator.

        This method may be executed from another thread that has been
        initialized with the trio context as described in
        :mod:`kivy_trio.context`.

        .. warning::

            If the trio side has not entered the ``with`` block,
            :meth:`add_item` returns silently.

        .. note::

            If :attr:`filter` or :attr:`convert` was provided, these functions
            are called from within :meth:`add_item` before enqueuing.
        """
        f = self.filter
        if self._quit or f is not None and not f(*args):
            return

        convert = self.convert
        if convert is not None:
            args = convert(*args)

        queue = self._queue
        if queue is None:
            return
        queue.append(args)

        entry_token = self._trio_token
        if trio_thread.get() is entry_token:
            # same thread
            self._send_on_channel()
        else:
            try:
                entry_token.run_sync_soon(self._send_on_channel)
            except trio.RunFinishedError as e:
                if self._queue is None:
                    return
                # should never be able to be finished if the queue is not None
                # except if the user entered again while we're doing this
                raise

    def _send_on_channel(self):
        send_channel = self._send_channel
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
        self._stop_data_stream()

        event = self._eof_trio_event
        if event is None:
            return

        entry_token = self._trio_token
        if trio_thread.get() is entry_token:
            # same thread
            event.set()
        else:
            try:
                entry_token.run_sync_soon(event.set)
            except trio.RunFinishedError:
                # nothing to signal - it's done
                pass

    def _start_data_stream(self):
        clock: ClockBase = kivy_clock.get(Clock)

        if not clock.has_started:
            raise EventLoopStoppedError(
                f'async_run_in_kivy failed to complete <{self}> because the '
                f'Kivy clock has not started to run yet. Consider using a '
                f'trio.Event() to wait until Kivy has started')

        event = self._eof_event = clock.create_lifecycle_aware_trigger(
            _do_nothing, self._clock_ended_callback, timeout=math.inf,
            release_ref=False)
        event()

    def _stop_data_stream(self):
        """May be called multiple times.
        """
        if self._eof_event is not None:
            self._eof_event.cancel()
            self._eof_event = None


class AsyncKivyBind(AsyncKivyEventQueue):
    """Asynchronously observe kivy properties and events using this queue.

    It creates an async iterator which for every iteration waits and then
    yields the property or event value, every time the property changes
    or the event is dispatched.

    The yielded value is identical to the list of values passed to a function
    bound to the event or property with ``bind``. So at minimum it's a one
    element (for events) or two element (for properties, instance and value)
    list.

    The interface is the same as :class:`AsyncKivyEventQueue` and it supports
    its :attr:`~AsyncKivyEventQueue.filter`,
    ':attr:`~AsyncKivyEventQueue.convert` functions and its ``max_len``
    argument. Its :meth:`~AsyncKivyEventQueue.add_item` is automatically called
    by the internal binding.

    :Parameters:

        `obj`: :class:`EventDispatcher`
            See :attr:`obj`.
        `name`: str
            See :attr:`name`.
        `current`: bool
            See :attr:`current`.

    E.g. try resizing the window and then pressing the button in this app:

    .. code-block:: python

        from kivy_trio.to_kivy import AsyncKivyBind
        from kivy.app import App
        from kivy.lang import Builder
        import trio

        class MyApp(App):

            queue: AsyncKivyBind = None

            async def run_app(self):
                # run app and trio queue
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self.run_queue)
                    nursery.start_soon(self.async_run, 'trio')

            async def run_queue(self):
                # hack to ensure the app is running before binding
                await trio.sleep(1)
                async with AsyncKivyBind(obj=self.root, name='size') as queue:
                    # save queue so we can add stuff from button
                    self.queue = queue
                    # queue will finish when stop is called below
                    async for obj, value in queue:
                        print(f'got {value}')

            def stop(self, *largs):
                super().stop(*largs)
                self.queue.stop()

            def build(self):
                return Builder.load_string(
                    "Button:\n"
                    "    text: 'Resize me'\n"
                    "    on_release: app.stop()")

        trio.run(MyApp().run_app)
    """

    obj: Optional[EventDispatcher] = None
    """The :class:`EventDispatcher` instance that contains the property or
    event being observed.
    """

    name: str = ''
    """The property or event name to observe.
    """

    _bound_uid = 0

    current: bool = True
    """Whether the iterator should yield the current property value on its
    first iteration (True) or wait for the first dispatch before yielding the
    value (False). Defaults to True.

    .. note::

        This only works for properties and ignored form events.
    """

    def __init__(self, obj, name: str, current: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.name = name
        self.obj = obj
        self.current = current

    def _start_data_stream(self):
        super()._start_data_stream()

        obj = self.obj
        name = self.name

        uid = self._bound_uid = obj.fbind(name, self.add_item)
        if not uid:
            raise ValueError(
                '{} is not a recognized property or event of {}'
                ''.format(name, obj))

        if self.current and not obj.is_event_type(name):
            self.add_item(obj, getattr(obj, name))

    def _stop_data_stream(self):
        super()._stop_data_stream()

        if self._bound_uid:
            self.obj.unbind_uid(self.name, self._bound_uid)
            self._bound_uid = 0
            self.obj = None
