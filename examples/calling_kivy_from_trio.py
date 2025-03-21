import trio
import random

from kivy.app import App
from kivy.lang import Builder
from kivy.properties import StringProperty

from kivy_trio.to_kivy import async_run_in_kivy, EventLoopStoppedError
from kivy_trio.context import shared_thread_context

kv = '''
Label:
    text: 'trio sent: {}'.format(app.trio_msg)
'''


class DemoApp(App):

    trio_msg = StringProperty('')

    def build(self):
        return Builder.load_string(kv)

    @async_run_in_kivy
    def send_kivy_message(self, packet):
        ex = '!' * (packet % 3 + 1)
        self.trio_msg = f'beetle juice {packet + 1} times{ex}'

    async def send_msg_to_kivy_from_trio(self):
        i = 0
        while True:
            try:
                await self.send_kivy_message(i)
            except EventLoopStoppedError:
                # kivy stopped so nothing more to do
                return
            i += 1
            await trio.sleep(1 + random.random())

    async def run_app(self):
        with shared_thread_context():
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.async_run, 'trio')
                nursery.start_soon(self.send_msg_to_kivy_from_trio)


if __name__ == '__main__':
    trio.run(DemoApp().run_app)
