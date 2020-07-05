import trio

from kivy.app import App
from kivy.lang import Builder
from kivy.properties import BooleanProperty

from kivy_trio.to_kivy import async_run_in_kivy, AsyncKivyBind
from kivy_trio.context import kivy_trio_context_manager

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
        while self.press_btn is None:
            # wait for app to be set up
            await trio.sleep(.1)

        async with AsyncKivyBind(obj=self.press_btn, name='state') as queue:
            async for value in queue:
                await self.set_button_state(value[1])

    async def run_app(self):
        with kivy_trio_context_manager():
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.async_run, 'trio')
                nursery.start_soon(self.track_press_button)


if __name__ == '__main__':
    trio.run(DemoApp().run_app)
