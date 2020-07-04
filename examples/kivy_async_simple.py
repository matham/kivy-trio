import trio
import time
from threading import Thread

from kivy.app import App
from kivy.lang import Builder
from kivy.properties import NumericProperty, StringProperty, BooleanProperty

from kivy_trio.to_kivy import async_run_in_kivy, EventLoopStoppedError, \
    AsyncKivyBind
from kivy_trio.to_trio import kivy_run_in_async, mark, KivyEventCancelled
from kivy_trio.context import get_thread_context_managers

kv = '''
BoxLayout:
    spacing: '5dp'
    orientation: 'vertical'
    BoxLayout:
        spacing: '5dp'
        Button:
            on_release: app.wait_async(float(delay.text or 0))
            text: 'Press to wait'
        TextInput:
            id: delay
            text: '1.5'
            input_filter: 'float'
            hint_text: 'delay'
        Label:
            text: 'measured delay: {}\\n{}'.format(app.delay, app.delay_msg)
    BoxLayout:
        spacing: '5dp'
        Button:
            on_release: app.trigger_async_error()
            text: 'Trigger error:'
        Label:
            text: 'Error message: {}'.format(app.error_msg)
    Label:
        text: 'trio sent: {}'.format(app.trio_msg)
    BoxLayout:
        spacing: '5dp'
        Button:
            on_kv_post: app.press_btn = self
            id: press_btn
            text: 'Press me'
        Label:
            text: 'Pressing: {}'.format(app.pressing_button)
'''


class DemoApp(App):

    delay = NumericProperty(0)

    delay_msg = StringProperty('')

    error_msg = StringProperty('')

    trio_msg = StringProperty('')

    pressing_button = BooleanProperty(False)

    press_btn = None

    count = 0

    def build(self):
        return Builder.load_string(kv)

    async def sleep_for(self, delay):
        await trio.sleep(delay)
        self.count += 1
        return f'Thanks for nap {self.count}!!'

    @kivy_run_in_async
    def wait_async(self, delay):
        self.delay = 0
        self.delay_msg = ''
        ts = time.perf_counter()
        try:
            self.delay_msg = yield mark(self.sleep_for, delay)
        except KivyEventCancelled:
            print('cancelled wait_async while it was waiting')
            return
        self.delay = time.perf_counter() - ts

    async def raise_error(self):
        await trio.sleep(2)
        raise ValueError('Who has woken me at this hour???')

    @kivy_run_in_async
    def trigger_async_error(self):
        self.error_msg = ''
        try:
            yield mark(self.raise_error)
        except ValueError as e:
            self.error_msg = str(e)
        except KivyEventCancelled:
            print('cancelled trigger_async_error while it was waiting')

    @async_run_in_kivy
    def send_kivy_message(self, packet):
        self.trio_msg = f'beetle juice {packet} times'

    async def send_msg_to_kivy_from_trio(self):
        i = 0
        while True:
            try:
                await self.send_kivy_message(i)
            except EventLoopStoppedError:
                # kivy stopped so nothing more to do
                return
            i += 1
            await trio.sleep(1.3)

    @async_run_in_kivy
    def set_button_state(self, state):
        self.pressing_button = state

    async def track_press_button(self):
        while self.press_btn is None:
            # wait for app to be set up
            await trio.sleep(.1)

        async with AsyncKivyBind(
                bound_obj=self.press_btn, bound_name='state') as queue:
            async for value in queue:
                await self.set_button_state(value[1] == 'down')

    def _trio_thread_target(self, trio_context_manager):
        async def runner():
            with trio_context_manager():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self.send_msg_to_kivy_from_trio)
                    nursery.start_soon(self.track_press_button)

        trio.run(runner)

    def run_threading(self):
        kivy_context_manager, trio_context_manager = \
            get_thread_context_managers()

        thread = Thread(
            target=self._trio_thread_target, args=(trio_context_manager, ))
        thread.start()

        with kivy_context_manager():
            self.run()

        thread.join()

    async def run_app(self):
        kivy_context_manager, trio_context_manager = \
            get_thread_context_managers()

        with trio_context_manager():
            with kivy_context_manager():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self.async_run, 'trio')
                    nursery.start_soon(self.send_msg_to_kivy_from_trio)
                    nursery.start_soon(self.track_press_button)


if __name__ == '__main__':
    trio.run(DemoApp().run_app)
    # DemoApp().run_threading()
