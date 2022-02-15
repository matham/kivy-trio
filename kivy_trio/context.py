"""Context Manager
==================

"""

from trio.lowlevel import TrioToken, current_trio_token
from contextvars import ContextVar, Token
from contextlib import contextmanager
from typing import Optional, Any

from kivy.clock import ClockBase

__all__ = (
    'shared_thread_context', 'trio_thread_context', 'kivy_thread_context',
    'initialize_from_trio', 'initialize_shared_thread',
    'initialize_trio_thread', 'initialize_kivy_thread',
    'ContextVarContextManager', 'kivy_clock', 'kivy_thread', 'trio_entry',
    'trio_thread'
)


class ContextVarContextManager:
    """Sets the context variable :attr:`context_var` to :attr:`value` when
    this object is entered in a ``with`` block.

    The context variable :attr:`context_var` is restored to its previous value
    when the block is exited.

    E.g.:

    .. code-block:: python

        >>> from contextvars import ContextVar
        >>> my_context = ContextVar('my_context')
        >>> my_context.get(12)
        12
        >>> with ContextVarContextManager(my_context, 42):
        ...     print(my_context.get())
        42
    """

    context_var: Optional[ContextVar] = None
    """A ``ContextVar`` instance that will be set to :attr:`value`, when we
    enter the ``with`` block.
    """

    value: Any = None
    """The value to which the :attr:`context_var` will be set to.
    """

    _token: Optional[Token] = None
    """Token that reset the context variable.
    """

    def __init__(self, context_var: ContextVar = None, value: Any = None):
        self.context_var = context_var
        self.value = value

    def __enter__(self):
        self._token = self.context_var.set(self.value)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.context_var.reset(self._token)
        self._token = None


kivy_clock: ContextVar[ClockBase] = ContextVar('kivy_clock')
"""A :attr:`~contextvars.ContextVar` that contains the :attr:`~kivy.clock.Clock`
:mod:`kivy_trio` uses to call into kivy to execute callbacks.

It will typically default to :attr:`~kivy.clock.Clock` in functions that need a
kivy clock when it has not been initialized.
"""
kivy_thread: ContextVar[Optional[ClockBase]] = ContextVar(
    'kivy_thread', default=None)
"""A :attr:`~contextvars.ContextVar` that contains the
:attr:`~kivy.clock.Clock`, or None (the default) :mod:`kivy_trio` uses to
determine whether the current thread is the kivy thread.

We only set its value to the current kivy :attr:`~kivy.clock.Clock` in the main
kivy thread. Then, from any thread we compare its value to :data:`kivy_clock`.
If they are the same, we know this thread is the main Kivy thread. Otherwise,
it's a different thread and we use callbacks to schedule kivy code to execute in
the main kivy thread.
"""
trio_entry: ContextVar[TrioToken] = ContextVar('trio_entry')
"""A :attr:`~contextvars.ContextVar` that contains the
:attr:`~trio.lowlevel.TrioToken` :mod:`kivy_trio` uses to call into trio to
execute async code.
"""
trio_thread: ContextVar[Optional[TrioToken]] = ContextVar(
    'trio_thread', default=None)
"""A :attr:`~contextvars.ContextVar` that contains the
:attr:`~trio.lowlevel.TrioToken`, or None (the default) :mod:`kivy_trio` uses to
determine whether the current thread is the trio thread that runs
:data:`trio_entry`.

We only set its value to the current :attr:`~trio.lowlevel.TrioToken` in a trio
thread. Then, from any thread we compare its value to :data:`trio_entry`.
If they are the same, we know this thread is the trio thread that generated
:data:`trio_entry`. Otherwise, it's a different thread and we use safe callbacks
to schedule trio code to execute in that thread.
"""


async def initialize_from_trio(clock: ClockBase = None) -> None:
    """Call this in a trio context to initialize Kivy and trio so kivy can
    execute async functions in this trio thread context e.g. when
    :func:`~kivy_trio.to_trio.kivy_run_in_async` is used. Similar so that trio
    can call kivy. It must be called **after** kivy is running and its
    :attr:`~kivy.clock.Clock` is active.

    Initializing Kivy/trio is only needed if trio is running in a separate
    thread than kivy or when there are multiple trio event loops running in
    different threads. However, initializing when they are in the same thread
    significantly improves performance (see :func:`initialize_shared_thread`).

    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g. the following example shows how kivy can run async code in trio
    when trio is running in a second thread. Notice trio calling this once kivy
    is running.

    When run, pressing the button will show that the async function is running
    in the second trio thread:

    .. code-block:: python

        import trio
        from threading import Thread, get_ident
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async_quiet
        from kivy_trio.context import initialize_from_trio

        class DemoApp(App):

            def build(self):
                return Builder.load_string(
                    "Button:\\n    on_release: app.do_something_async()")

            @kivy_run_in_async_quiet
            async def do_something_async(self):
                await trio.sleep(.1)
                print('trio thread ID:', get_ident())

            def _trio_thread_target(self):
                async def runner():
                    await initialize_from_trio()
                    # now that kivy thread is initialized with trio token,
                    # do_something_async can be safely called
                    while self.get_running_app() is not None:
                        await trio.sleep(.1)

                trio.run(runner)

            def on_start(self):
                print('kivy thread ID:', get_ident())
                # start trio once the kivy Clock is running
                thread = Thread(target=self._trio_thread_target)
                thread.start()

        DemoApp().run()
    """
    from kivy_trio.to_kivy import async_run_in_kivy
    if clock is None:
        from kivy.clock import Clock
        clock = Clock

    token = current_trio_token()

    # set the variables to use in the trio thread - both kivy clock and trio
    # token. Also set trio_thread because this is the trio thread
    trio_entry.set(token)
    trio_thread.set(token)
    kivy_clock.set(clock)

    await async_run_in_kivy(initialize_kivy_thread)(token, clock)


def initialize_kivy_thread(
        token: Optional[TrioToken], clock: ClockBase = None) -> None:
    """Initializes only the Kivy thread with the trio token similar to
    :func:`initialize_from_trio`, except that you manually call this in the
    kivy thread passing the trio token acquired seperately and that you should
    also call :func:`initialize_trio_thread` in the trio thread.

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None if trio and kivy are running in the same thread and if
        called from a trio context, then we automatically get the token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g.:

    .. code-block:: python

        >>> # get the trio token for your trio event loop from the trio thread
        >>> from trio.lowlevel import current_trio_token
        >>> trio_token = current_trio_token()
        >>> initialize_trio_thread()
        >>> ...
        >>> # later, in the kivy thread use the token to init the kivy thread
        >>> initialize_kivy_thread(trio_token)
        >>> # now you can use kivy_run_in_async(_quiet) to run async code
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please provide the trio token") from e

        # if we get the current token in kivy thread, then kivy and trio share
        # the same thread, so set it
        trio_thread.set(token)

    # set the variables to use in the kivy thread - both kivy clock and trio
    # entry. Also set kivy_thread because this is the kivy thread
    trio_entry.set(token)
    kivy_thread.set(clock)
    kivy_clock.set(clock)


def initialize_trio_thread(
        token: Optional[TrioToken] = None, clock: ClockBase = None) -> None:
    """Initializes only the trio thread with the trio token and clock similar to
    :func:`initialize_from_trio`, except that you manually call this in the
    trio thread passing the trio token (optionally) and that you must
    also call :func:`initialize_kivy_thread` in the kivy thread with the token.

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None, and then we automatically get the currently active token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g.:

    .. code-block:: python

        >>> # get the trio token for your trio event loop from the trio thread
        >>> from trio.lowlevel import current_trio_token
        >>> trio_token = current_trio_token()
        >>> initialize_trio_thread()
        >>> ...
        >>> # later, in the kivy thread use the token to init the kivy thread
        >>> initialize_kivy_thread(trio_token)
        >>> # now you can use kivy_run_in_async(_quiet) to run async code
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please run from a trio context") from e

    # set the variables to use in the trio thread - both kivy clock and trio
    # entry. Also set trio_thread because this is the trio thread
    trio_entry.set(token)
    trio_thread.set(token)
    kivy_clock.set(clock)


def initialize_shared_thread(
        token: Optional[TrioToken] = None, clock: ClockBase = None) -> None:
    """Initializes the shared trio and kivy thread with the trio token and kivy
    clock similar to :func:`initialize_from_trio`, except that you manually call
    this in the kivy/trio thread passing the trio token (optionally).

    When kivy and trio are running from the same thread, we don't need to
    initialize either, however, initializing significanly imporves performance.

    This must be called only after both kivy and trio are running and from
    the main shared thread under the trio context (unless you provide the
    token).

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None, and then we automatically get the currently active token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

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
                    "Button:\n    on_release: app.do_something_async()")

            @kivy_run_in_async_quiet
            async def do_something_async(self):
                await trio.sleep(.1)
                print('ran async')

            def on_start(self):
                initialize_shared_thread()
                # now you can run async code

        trio.run(DemoApp().async_run, 'trio')
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please provide the trio token") from e

    trio_entry.set(token)
    trio_thread.set(token)
    kivy_thread.set(clock)
    kivy_clock.set(clock)


@contextmanager
def kivy_thread_context(token: Optional[TrioToken], clock: ClockBase = None):
    """The same as :func:`initialize_kivy_thread`, but in context manager
    form so that the tokens are reset when exiting the context.

    This ensures that any initialization is cleared when exiting so that it can
    be run again if needed with different kivy clock.

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None if trio and kivy are running in the same thread and if
        called from a trio context, then we automatically get the token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g.:

    .. code-block:: python

        >>> # get the trio token for your trio event loop from the trio thread
        >>> import trio
        >>> from trio.lowlevel import current_trio_token
        >>> trio_token = None
        >>> async def my_func():
        ...     global trio_token
        ...     trio_token = current_trio_token()
        ...     with trio_thread_context():
        ...         await trio.sleep(10000)
        >>> trio.run(my_func)
        >>> ...
        >>> # now back in the kivy thread
        >>> with kivy_thread_context(trio_token):
        ...     MyApp().run()
        >>> ...
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please provide the trio token") from e

        # if we get the current token in kivy thread, then kivy and trio share
        # the same thread, so set it
        with ContextVarContextManager(trio_entry, token):
            with ContextVarContextManager(trio_thread, token):
                with ContextVarContextManager(kivy_clock, clock):
                    with ContextVarContextManager(kivy_thread, clock):
                        yield
    else:
        with ContextVarContextManager(trio_entry, token):
            with ContextVarContextManager(kivy_clock, clock):
                with ContextVarContextManager(kivy_thread, clock):
                    yield


@contextmanager
def trio_thread_context(
        token: Optional[TrioToken] = None, clock: ClockBase = None):
    """The same as :func:`initialize_trio_thread`, but in context manager
    form so that the tokens are reset when exiting the context.

    This ensures that any initialization is cleared when exiting so that it can
    be run again if needed with different trio tokens.

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None, and then we automatically get the currently active token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g.:

    .. code-block:: python

        >>> # get the trio token for your trio event loop from the trio thread
        >>> import trio
        >>> from trio.lowlevel import current_trio_token
        >>> trio_token = None
        >>> async def my_func():
        ...     global trio_token
        ...     trio_token = current_trio_token()
        ...     with trio_thread_context():
        ...         await trio.sleep(10000)
        >>> trio.run(my_func)
        >>> ...
        >>> # now back in the kivy thread
        >>> with kivy_thread_context(trio_token):
        ...     MyApp().run()
        >>> ...
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please provide the trio token") from e

    with ContextVarContextManager(trio_entry, token):
        with ContextVarContextManager(trio_thread, token):
            with ContextVarContextManager(kivy_clock, clock):
                yield


@contextmanager
def shared_thread_context(
        token: Optional[TrioToken] = None, clock: ClockBase = None):
    """The same as :func:`initialize_shared_thread`, but in context manager
    form so that the tokens are reset when exiting the context.

    This ensures that any initialization is cleared when exiting so that it can
    be run again if needed with different trio or kivy tokens.

    :param token: The :attr:`~trio.lowlevel.TrioToken` to use to run async code.
        Can be None, and then we automatically get the currently active token.
    :param clock: An optional kivy clock, in case the default kivy clock is
        not used, e.g. during testing. If it's None, :attr:`~kivy.clock.Clock`
        is used.

    E.g.:

    .. code-block:: python

        import trio
        from kivy.app import App
        from kivy.lang import Builder
        from kivy_trio.to_trio import kivy_run_in_async_quiet
        from kivy_trio.context import shared_thread_context

        class DemoApp(App):

            def build(self):
                return Builder.load_string(
                    "Button:\n    on_release: app.do_something_async()")

            @kivy_run_in_async_quiet
            async def do_something_async(self):
                await trio.sleep(.1)
                print('ran async')

            async def async_run(self, async_lib=None):
                with shared_thread_context():
                    await super(DemoApp, self).async_run('trio')

        trio.run(DemoApp().async_run, 'trio')
    """
    if clock is None:
        from kivy.clock import Clock
        clock = Clock
    if token is None:
        try:
            token = current_trio_token()
        except RuntimeError as e:
            raise ValueError(
                "Cannot get token because we're not running in a trio context. "
                "Please provide the trio token") from e

    with ContextVarContextManager(trio_entry, token):
        with ContextVarContextManager(trio_thread, token):
            with ContextVarContextManager(kivy_clock, clock):
                with ContextVarContextManager(kivy_thread, clock):
                    yield
