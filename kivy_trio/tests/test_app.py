import trio


async def test_context(nursery):
    from kivy_trio.context import get_thread_context_managers
    kivy_context_manager, trio_context_manager = get_thread_context_managers()

    with trio_context_manager():
        with kivy_context_manager():
            async with trio.open_nursery():
                pass
