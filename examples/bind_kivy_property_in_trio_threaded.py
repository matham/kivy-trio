import trio
from threading import Thread

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.properties import BooleanProperty

from kivy_trio.to_kivy import async_run_in_kivy, AsyncKivyBind
from kivy_trio.context import trio_context_manager, \
    initialize_kivy_from_trio

kv = '''
BoxLayout:
    spacing: '5dp'
    Button:
        on_kv_post: app.press_btn = self
        id: press_btn
        text: 'Press me'
    Label:
        text: 'Trio says: button is {}'.format(app.pressing_button)
'''


class DemoApp(App):

    pressing_button = BooleanProperty(False)

    press_btn = None

    def build(self):
        return Builder.load_string(kv)

    @async_run_in_kivy
    def set_button_state(self, state):
        self.pressing_button = state

    async def track_press_button(self):
        async with AsyncKivyBind(obj=self.press_btn, name='state') as queue:
            # for loop automatically exits when kivy clock stops
            async for value in queue:
                await self.set_button_state(value[1])

    def _trio_thread_target(self):
        async def runner():
            with trio_context_manager():
                await initialize_kivy_from_trio()
                await self.track_press_button()

        trio.run(runner)

    def run_threading(self):
        thread = Thread(target=self._trio_thread_target)
        # start the trio thread once kivy's widgets are set up and ready
        Clock.schedule_once(lambda x: thread.start())

        self.run()
        # wait until trio thread is done
        thread.join()


if __name__ == '__main__':
    DemoApp().run_threading()
