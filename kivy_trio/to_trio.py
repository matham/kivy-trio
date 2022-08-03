"""Calling into Trio
====================

Executing async coroutines in Trio from Kivy
--------------------------------------------

Kivy is a GUI framework that runs an event loop that calls functions
synchronously upon interactions with the GUI. Some applications also run a
Trio eventloop executing async code in the Kivy thread (when kivy is run
asynchronously) or in a separate thread. This package enables Kivy to execute
an async coroutine in the trio context.

E.g. after the trio and kivy :mod:`kivy_trio.context` is initialized, the
following async function that sends a message to a device:

.. code-block:: python

    @kivy_run_in_async_quiet
    async def send_device_message(delay, device, message):
        await trio.sleep(delay)
        await device.send(message)

can be scheduled to run in Trio from within a kivy context simply by calling it:

.. code-block:: python

    dev = MyDevice()
    send_device_message(3, dev, 'hello')

This uses the :func:`kivy_run_in_async_quiet` to automatically schedule the
async function to run in the trio context by wrapping it in a synchronous
decorator. Then, when ``send_device_message`` is called by Kivy, Kivy doesn't
block waiting for the function to finish.

:func:`kivy_run_in_async_quiet` can only be used for async functions or methods
that cannot raise an exception and when we don't need a return value. Using it
on functions that can raise an exception is unsafe as it will crash the program.

Instead, :func:`kivy_run_in_async` can be used for general functions and methods
as in the following modified async function example:

.. code-block:: python

    async def send_device_message(delay, device, message):
        await trio.sleep(delay)
        return await device.send(message)

Now, we also need to define a synchronous wrapper function that handles the
return value and potential exceptions as follows:

.. code-block:: python

    @kivy_run_in_async
    def kivy_send_message(delay, device, message):
        try:
            response = yield mark(
                send_device_message, delay, device, message=message)
            print(f'Device responded with {response}')
        except ValueError as e:
            print(f'Device failed with error {e}')

This is similarly scheduled to run in a trio context from within a kivy context
by calling it:

.. code-block:: python

    dev = MyDevice()
    kivy_send_message(3, dev, 'hello')

:func:`kivy_run_in_async` performs the following actions when called as
``event = kivy_send_message(...)`` in three phases.

1. First :func:`kivy_run_in_async` instantiates the underlying
   ``kivy_send_message`` generator to get the yielded value. This advances
   ``kivy_send_message`` internally to the ``yield`` point. The value the
   wrapper function must yield to the decorator is a :func:`mark`-ed up async
   function and the positional/keyword arguments that will be passed to it.
2. Next, the :func:`kivy_run_in_async` decorator immediately schedules the
   marked async function to be called in Trio, with the wrapper suspended at
   the ``yield`` point.
3. Finally, when the async function has finished or raised an exception in Trio,
   using the kivy clock the generator is resumed from the ``yield`` point
   by the decorator. It then finishes executing the wrapper function by yielding
   the return value of the wrapped async function or re-raises its exception.

Consequently, the marked async function is executed in Trio, but the return
value is then passed back or any exception the async function has raised is
similarly re-raised in the kivy context when the generator resumes.

Exceptions
----------

If the async coroutine raises an exception, the exception will be caught and
sent to resume the wrapper generator at the ``yield`` statement so the wrapper
will raise that exception and be given an opportunity to handle it. So e.g.:

.. code-block:: python

    async def raise_error():
        raise ValueError('Goodbye')

    @kivy_run_in_async
    def kivy_send_message():
        try:
            response = yield mark(raise_error)
        except ValueError as e:
            print(f'Error caught {e}')

when ``kivy_send_message`` is called, it'll catch the exception raised in trio
and print ``'Error caught Goodbye'``. If the exception is not caught, then it'll
crash Kivy because it's the Kivy Clock that is actually executing it.

If :func:`kivy_run_in_async_quiet` was used instead to decorate ``raise_error``
directly, the Kivy event loop will crash, so it is only to be used for
functions that can't raise exceptions.

Lifecycle and Cancellation
--------------------------

A :func:`kivy_run_in_async` and :func:`kivy_run_in_async_quiet` decorated
function or method may only be called while the Kivy event loop and trio event
loop are running. Otherwise, an exception may be raised when the function is
called.

If the kivy event loop ends while the coroutine is executing in trio, such as
when the Kivy GUI exits, the event will be canceled and a
:class:`KivyEventCancelled` exception will be injected
into the wrapper generator. The coroutine will still finish executing in trio
and there's currently no way to cancel that once it started, but the result
will be discarded when it's done.

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
the same thread, initialization is not stricly nessecary, but it does increase
performance considerably). The coroutine will be executed in the trio context
that it was initialized to, which can be the same or another thread.

See the :mod:`kivy_trio.context` for details.
"""
import trio
from trio.lowlevel import current_trio_token
import outcome
import math
from functools import wraps, partial
from typing import Optional, Generator, Callable, Awaitable, Any, Tuple, Dict, \
    Union, Iterator
from asyncio import iscoroutinefunction

from kivy.clock import ClockBase, ClockNotRunningError, ClockEvent

from kivy_trio.context import trio_entry, trio_thread

__all__ = (
    'KivyEventCancelled', 'mark', 'kivy_run_in_async',
    'kivy_run_in_async_quiet', 'KivyCallbackEvent')


AsyncFunc = Callable[..., Awaitable]
MarkedFunc = Tuple[AsyncFunc, Tuple[Any], Dict[str, Any]]


class KivyEventCancelled(BaseException):
    """The exception raised in the waiting :func:`kivy_run_in_async`
    decorated generator if the event is canceled or if one of the event
    loops exit before doing the callback.

    E.g.:

    .. code-block:: python

        @kivy_run_in_async
        def trigger_async_error(self):
            try:
                yield mark(some_func)
            except KivyEventCancelled:
                # event was canceled while some_func was waiting to be called
                pass
    """
    pass


def mark(
        __async_func: AsyncFunc, *args: Any, **kwargs: Any) -> MarkedFunc:
    """Collects the function and its parameters in a :func:`kivy_run_in_async`
    decorated generator. The function is subsequently called and awaited by
    trio, passing in its positional and keyword arguments.

    E.g. in the code below, :func:`mark` collects the function, positional, and
    keyword arguments and yields it to be called and awaited by trio:

    .. code-block:: python

        async def call_server(name, ip, port):
            if name == "Unknown":
                raise ValueError

        @kivy_run_in_async
        def on_button_press(self, name, ip=0.0.0.0, port=6768):
            try:
                yield mark(call_server, name, ip=ip, port=port)
            except ValueError:
                # async call_server function failed with ValueError
                pass

    :param __async_func: The async function or method to call.
    :param args: Any positional arguments to be passed to the function.
    :param kwargs: Any keyword arguments to be passed to the function.
    :return: A collection of the functions and its args to be used internally.
        The exact form is internal and is not part of the public API.
    """
    return __async_func, args, kwargs


def _callback_raise_exception(*args):
    raise TypeError('Should never have timed out infinite wait')


def _do_nothing(*args):
    pass


class KivyCallbackEvent:
    """The event class returned when a :func:`kivy_run_in_async` or
    :func:`kivy_run_in_async_quiet` decorated function is called.
    It represents the async code scheduled to be called by trio.

    This class should not be instantiated manually in user code.

    E.g. the following code prints the event as it's scheduled:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async_quiet, \
kivy_run_in_async, mark
        from kivy_trio.context import initialize_shared_thread

        kv = \"""
        BoxLayout:
            Button:
                text: "Simple async"
                on_release: print(f'Event: {app.do_something_simple_async()}')
            Button:
                text: "async with error checking"
                on_release: print(f'Event: {app.call_async()}')
        \"""

        class DemoApp(App):

            def build(self):
                return Builder.load_string(kv)

            @kivy_run_in_async_quiet
            async def do_something_simple_async(self):
                await trio.sleep(.1)
                print('Slept')

            async def do_async(self):
                await trio.sleep(1)
                print('Slept')
                raise ValueError('Woke up')

            @kivy_run_in_async
            def call_async(self):
                try:
                    yield mark(self.do_async)
                except ValueError as e:
                    print(f'Got error "{e}"')

            def on_start(self):
                initialize_shared_thread()

        trio.run(DemoApp().async_run, 'trio')
    """

    __slots__ = (
        '_gen', '_clock', '_orig_func', '_clock_event', '_marked_func_name',
        '__weakref__')

    _gen: Optional[Generator[MarkedFunc, None, Any]]

    _clock: ClockBase

    _orig_func: Union[
        AsyncFunc, Callable[..., Generator[MarkedFunc, None, Any]]]

    _clock_event: Optional[ClockEvent]

    _marked_func_name: str

    def __init__(
            self, clock: ClockBase,
            func: Union[
                AsyncFunc, Callable[..., Generator[MarkedFunc, None, Any]]],
            gen: Generator[MarkedFunc, None, Any],
            ret_func: MarkedFunc
    ):
        super().__init__()

        self._clock = clock
        self._orig_func = func
        self._marked_func_name = str(ret_func[0])

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

    def __str__(self):
        marked = self._marked_func_name
        orig = str(self._orig_func)
        if marked == orig:
            return f'<{self.__class__.__name__} func={orig}>'
        return f'<{self.__class__.__name__} func={marked}, generator={orig}>'

    def cancel(self, *args) -> None:
        """Cancels the waiting event from reporting back to the kivy thread.

        The async code itself is immediately scheduled to be executed from the
        async context so that can't be canceled once started and it will finish
        executing unless that is manually canceled using trio's mechanisms.
        However, :meth:`cancel` allows canceling the generator if
        :func:`kivy_run_in_async` was used. Meaning, canceling will inject a
        :class:`KivyEventCancelled` exception into the waiting generator.
        If the async code hasn't started executing then it will also be
        canceled. However, we don't provide visibility to the user whether the
        async code has started.

        The injected :class:`KivyEventCancelled` will be caught by cancel so
        that the exception will not be propagated beyond :meth:`cancel` and it
        won't cause an app crash.

        .. warning::

            This is only safe to be called from the kivy thread.

        E.g. in the following code we schedule a async sleep event for 5s and
        then cancel it after 0.5s. In one button we only cancel the generator
        while the other uses a trio mechanism to also manually cancel the
        waiting trio code:

        .. code-block:: python

            import trio
            from kivy.app import App
            from kivy.lang import Builder
            from kivy.clock import Clock
            from kivy_trio.to_trio import kivy_run_in_async, mark, \
KivyEventCancelled
            from kivy_trio.context import initialize_shared_thread

            kv = \"""
            BoxLayout:
                Button:
                    text: "async without full cancel"
                    on_release: app.start_and_cancel_async(False)
                Button:
                    text: "async with full cancel"
                    on_release: app.start_and_cancel_async(True)
            \"""

            class DemoApp(App):

                # scope we use to manually cancel async code from trio
                _cancel_scope = None

                def build(self):
                    return Builder.load_string(kv)

                async def do_async(self, allow_cancel):
                    print('starting to async sleep')
                    if allow_cancel:
                        # if we want to be able to cancel trio's code, we need
                        # to create a scope that can be canceled
                        with trio.CancelScope() as self._cancel_scope:
                            try:
                                await trio.sleep(5)
                            except trio.Cancelled:
                                print(\
'Async sleeping code was canceled by trio')
                        self._cancel_scope = None
                    else:
                        await trio.sleep(5)
                        print('Finished async sleeping')

                @kivy_run_in_async
                def call_async(self, allow_cancel):
                    try:
                        yield mark(self.do_async, allow_cancel=allow_cancel)
                    except KivyEventCancelled:
                        # catch the cancellation event
                        print(\
'Generator waiting for async to finish was canceled')
                        if self._cancel_scope is not None:
                            # we can call trio's cancel because it's running \
in kivy thread
                            self._cancel_scope.cancel()
                    print('Generator finished')

                def start_and_cancel_async(self, allow_cancel):
                    event = self.call_async(allow_cancel)
                    Clock.schedule_once(event.cancel, .5)

                def on_start(self):
                    initialize_shared_thread()

            trio.run(DemoApp().async_run, 'trio')
        """
        self._cancel(suppress_cancel=True)

    def _cancel(self, *args, e=None, suppress_cancel=False):
        if self._gen is None:
            return

        if e is None:
            e = KivyEventCancelled("Event was canceled")

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
            try:
                self._gen.close()
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
            try:
                self._gen.close()
            finally:
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


def kivy_run_in_async(
        gen: Callable[..., Generator[MarkedFunc, None, Any]]
) -> Callable[..., KivyCallbackEvent]:
    """Decorator for a generator that yields an async coroutine. When the
    decorated generator is called in synchronous code from Kivy, it schedules
    the async coroutine in trio and returns a :class:`KivyCallbackEvent`
    representing the scheduled code.

    If the async coroutine cannot cause an exception, you don't need its
    return value, and we don't care if it's canceled with
    :meth:`~KivyCallbackEvent.cancel` while we still wait, you can use
    :func:`kivy_run_in_async_quiet` instead to decorate the async code directly.

    E.g. to execute async code in trio with passed parameters and then catch
    the exception it raises:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async, mark
        from kivy_trio.context import initialize_shared_thread

        class DemoApp(App):

            def build(self):
                return Builder.load_string(
                    "Button:\\n"
                    "    text: 'Press me'\\n"
                    "    on_release: app.call_async(5)")

            async def do_async(self, duration):
                print(f'Sleeping for {duration}s')
                await trio.sleep(duration)
                raise ValueError('Woke up')

            @kivy_run_in_async
            def call_async(self, duration):
                try:
                    yield mark(self.do_async, duration=duration)
                except ValueError as e:
                    print(f'Got exception "{e}"')

            def on_start(self):
                initialize_shared_thread()

        trio.run(DemoApp().async_run, 'trio')

    When run and pressing a button and waiting, this prints::

        Sleeping for 5s
        Got exception "Woke up"

    Similarly, by changing ``do_async`` and ``call_async`` to the following:

    .. code-block:: python

        async def do_async(self, duration):
            print(f'Sleeping for {duration}s')
            await trio.sleep(duration)
            return duration

        @kivy_run_in_async
            def call_async(self, duration):
                result = yield mark(self.do_async, duration=duration)
                print(f'Slept for {result}')

    the async code won't raise an exception and instead returns the slept time
    to the generator and when run it prints::

        Sleeping for 5s
        Slept for 5

    The generator can also catch exceptions due to the user canceling the event
    with :meth:`~KivyCallbackEvent.cancel` (see that method for details)
    or if the Kivy event loop closes before the async code is done.

    The following example demonstrates the cancellation that automatically
    happens when Kivy ends while the async code is still waiting. Run the code,
    press the button and then exit the window while it's sleeping:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async, mark, \
KivyEventCancelled
        from kivy_trio.context import initialize_shared_thread

        class DemoApp(App):

            def build(self):
                return Builder.load_string(
                    "Button:\\n"
                    "    text: 'Press me'\\n"
                    "    on_release: app.call_async(5)")

            async def do_async(self, duration):
                print(f'Sleeping for {duration}s')
                await trio.sleep(duration)

            @kivy_run_in_async
            def call_async(self, duration):
                try:
                    yield mark(self.do_async, duration=duration)
                except KivyEventCancelled as e:
                    print(f'Got canceled exception "{e}"')

            def on_start(self):
                initialize_shared_thread()

        trio.run(DemoApp().async_run, 'trio')

    When run like that it should print::

        Sleeping for 5s
        Got canceled exception "Event was canceled"
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


def kivy_run_in_async_quiet(
        async_func: AsyncFunc) -> Callable[..., KivyCallbackEvent]:
    """Decorator for an async coroutine that schedules the async coroutine in
    trio and returns a :class:`KivyCallbackEvent` representing the scheduled
    code.

    It's similar to :func:`kivy_run_in_async`, however unlike that function
    that can catch exceptions and the async coroutine's return value, this will
    only execute the async coroutine without any protection or return value.

    :func:`kivy_run_in_async_quiet` should only be used if the async coroutine
    cannot cause an exception (as it can't be caught and will crash the
    program), you don't need its return value, and we don't care if it's
    canceled with :meth:`~KivyCallbackEvent.cancel` while we still wait
    (because you won't know) and Kivy won't be stopped while we wait (that will
    raise a :class:`KivyEventCancelled` in Kivy as it exits).

    E.g.:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async_quiet
        from kivy_trio.context import initialize_shared_thread

        class DemoApp(App):

            def build(self):
                return Builder.load_string(
                    "Button:\\n"
                    "    text: 'Press me'\\n"
                    "    on_release: app.call_async(5)")

            @kivy_run_in_async_quiet
            async def call_async(self, duration):
                print(f'Sleeping for {duration}s')
                await trio.sleep(duration)

            def on_start(self):
                initialize_shared_thread()

        trio.run(DemoApp().async_run, 'trio')
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
