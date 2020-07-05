import trio
import time

from kivy.app import App
from kivy.lang import Builder
from kivy.properties import NumericProperty, StringProperty

from kivy_trio.to_trio import kivy_run_in_async, mark, KivyEventCancelled
from kivy_trio.context import kivy_trio_context_manager

kv = '''
BoxLayout:
    spacing: '5dp'
    orientation: 'vertical'
    BoxLayout:
        spacing: '5dp'
        Button:
            on_release: app.wait_async(float(delay.text or 0))
            text: 'Press to wait {} seconds in trio'.format(delay.text)
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
            text: 'Trigger error in trio in 2 seconds:'
        Label:
            text: 'Error message: {}'.format(app.error_msg)
'''


class DemoApp(App):

    delay = NumericProperty(0)

    delay_msg = StringProperty('')

    error_msg = StringProperty('')

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

    async def run_app(self):
        with kivy_trio_context_manager():
            await self.async_run('trio')


if __name__ == '__main__':
    trio.run(DemoApp().run_app)
