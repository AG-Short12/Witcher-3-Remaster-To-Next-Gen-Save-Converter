"""Terminal UI for w3_downgrade: pick a 5.0.x save and an output folder, convert to 4.04."""
import asyncio
import traceback
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DirectoryTree, Footer, Header, Input, Label, RichLog, Static
from textual.worker import get_current_worker

from w3_downgrade import TARGET, convert, read_version


def clean(value):
    """A path as typed or pasted: surrounding blanks and quotes removed ("Copy as path" adds quotes)."""
    return value.strip().strip('"\'')


def start_dir(value):
    """Folder a browser opens in: the closest existing folder of the field's value, else the
    current working directory."""
    if value:
        p = Path(value).expanduser()
        for d in (p, *p.parents):
            if d.is_dir():
                return d
    return Path.cwd()


class PathTree(DirectoryTree):
    """Folders, plus files with the wanted suffix (none: folders only)."""

    def __init__(self, path, suffix, **kwargs):
        super().__init__(path, **kwargs)
        self.suffix = suffix

    def filter_paths(self, paths):
        shown = []
        for p in paths:
            try:
                if p.is_dir() or (self.suffix and p.suffix.lower() == self.suffix):
                    shown.append(p)
            except OSError:  # unreadable entries
                pass
        return shown


class PathPicker(ModalScreen):
    """Browse for a file with the given suffix, or for a folder (suffix None). Dismisses with the
    chosen Path, or None."""

    BINDINGS = [Binding('escape', 'dismiss_none', 'Cancel')]

    def __init__(self, title, start, suffix=None):
        super().__init__()
        self.heading, self.start, self.suffix = title, start, suffix

    def compose(self) -> ComposeResult:
        with Vertical(id='picker'):
            yield Label(self.heading, id='picker-title', markup=False)
            with Horizontal(classes='row'):
                yield Button('Up', id='up')
                yield Input(str(self.start), id='where', tooltip='Type a folder and press Enter to go there')
            yield PathTree(self.start, self.suffix, id='tree')
            with Horizontal(classes='buttons'):
                if self.suffix is None:
                    yield Button('Use this folder', id='choose', variant='primary')
                yield Button('Cancel', id='cancel')

    def on_mount(self):
        self.query_one(PathTree).focus()

    def go(self, folder):
        self.query_one(PathTree).path = folder
        self.query_one('#where', Input).value = str(folder)

    @on(Button.Pressed, '#up')
    def up(self):
        here = Path(self.query_one(PathTree).path)
        if here.parent != here:
            self.go(here.parent)

    @on(Input.Submitted, '#where')
    def typed(self, event):
        p = Path(clean(event.value)).expanduser()
        if p.is_dir():
            self.go(p)
        elif self.suffix and p.is_file() and p.suffix.lower() == self.suffix:
            self.dismiss(p)
        else:
            self.notify(f'No such folder: {p}', severity='error', markup=False)

    @on(DirectoryTree.FileSelected)
    def file_chosen(self, event):
        if self.suffix:
            self.dismiss(event.path)

    @on(Button.Pressed, '#choose')
    def folder_chosen(self):
        tree = self.query_one(PathTree)
        node = tree.cursor_node
        path = node.data.path if node is not None and node.data is not None else Path(tree.path)
        self.dismiss(path if path.is_dir() else path.parent)

    @on(Button.Pressed, '#cancel')
    def action_dismiss_none(self):
        self.dismiss(None)


class Confirm(ModalScreen):
    """Yes/no question; dismisses with True or False."""

    BINDINGS = [Binding('y', 'answer(True)', 'Yes'), Binding('n', 'answer(False)', 'No'),
                Binding('escape', 'answer(False)', 'No', show=False)]

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Static(self.question, markup=False)
            with Horizontal(classes='buttons'):
                yield Button('Yes', id='yes', variant='warning')
                yield Button('No', id='no', variant='primary')

    def on_mount(self):
        self.query_one('#no', Button).focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == 'yes')

    def action_answer(self, yes):
        self.dismiss(yes)


class DowngradeApp(App):
    TITLE = 'Witcher 3 save downgrade'
    SUB_TITLE = '5.0 -> 4.04'
    CSS = """
    #form { height: auto; padding: 1 2 0 2; }
    .row { height: auto; }
    .row Input { width: 1fr; }
    .row Button { min-width: 10; margin-left: 1; }
    .caption { width: 16; height: 3; content-align: left middle; }
    #save-info { color: $text-muted; padding-left: 17; }
    #actions { padding-left: 16; }
    #convert { margin-left: 2; }
    #log { height: 1fr; min-height: 5; max-height: 10; margin: 0 2 1 2; border: round $primary; }
    PathPicker, Confirm { align: center middle; }
    #picker { width: 90%; height: 85%; padding: 1 2; border: thick $primary; background: $surface; }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #tree { height: 1fr; margin-top: 1; }
    #dialog { width: 70; max-width: 95%; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    .buttons { height: auto; margin-top: 1; align-horizontal: right; }
    .buttons Button { margin-left: 1; }
    """
    BINDINGS = [
        Binding('f2', 'pick_save', 'Pick save'),
        Binding('f3', 'pick_out', 'Pick output'),
        Binding('f4', 'pick_template', 'Pick template'),
        Binding('ctrl+r', 'convert', 'Convert'),
        Binding('ctrl+q', 'quit', 'Quit'),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id='form'):
            with Horizontal(classes='row'):
                yield Label('5.0 save', classes='caption')
                yield Input(placeholder='path to a .sav file', id='save')
                yield Button('Browse', id='browse-save')
            yield Static('Pick a save to convert.', id='save-info', markup=False)
            with Horizontal(classes='row'):
                yield Label('Output folder', classes='caption')
                yield Input(placeholder='folder for the converted save', id='out')
                yield Button('Browse', id='browse-out')
            with Horizontal(classes='row'):
                yield Label('Skill template', classes='caption')
                yield Input(placeholder='optional: built-in 4.04 skill tree', id='template')
                yield Button('Browse', id='browse-template')
            with Horizontal(classes='row', id='actions'):
                keep = Checkbox('Keep 5.0-only data', id='keep')
                keep.tooltip = ('Fallback if the normal conversion will not load: keeps the build ID, mod list, '
                                'map-pin ShowAlways fields and HasScar. Versions, inventories and skills are '
                                'still converted.')
                yield keep
                yield Button('Convert to 4.04', id='convert', variant='primary')
        log = RichLog(id='log', wrap=True, min_width=20)  # wraps long lines instead of scrolling sideways
        log.border_title = 'Log'
        yield log
        yield Footer()

    def field(self, name):
        return clean(self.query_one(f'#{name}', Input).value)

    # ---- picking paths

    def pick(self, name, title, suffix):
        def chosen(path):
            if path is not None:
                self.query_one(f'#{name}', Input).value = str(path)
        self.push_screen(PathPicker(title, start_dir(self.field(name)), suffix), chosen)

    def action_pick_save(self):
        self.pick('save', 'Select the 5.0 save to convert (.sav)', '.sav')

    def action_pick_out(self):
        self.pick('out', 'Select the output folder', None)

    def action_pick_template(self):
        self.pick('template', 'Select a 4.04 skill template (.json)', '.json')

    @on(Button.Pressed, '#browse-save')
    def browse_save(self):
        self.action_pick_save()

    @on(Button.Pressed, '#browse-out')
    def browse_out(self):
        self.action_pick_out()

    @on(Button.Pressed, '#browse-template')
    def browse_template(self):
        self.action_pick_template()

    # ---- save version

    @on(Input.Changed, '#save')
    def save_changed(self):
        p = Path(self.field('save')).expanduser()
        if self.field('save') and p.is_file():
            self.show_version(p)
        else:
            self.query_one('#save-info', Static).update('Pick a save to convert.')

    @work(thread=True, exclusive=True, group='info')
    def show_version(self, path):
        try:
            v = read_version(path)
            text = (f'saveVersion {v[0]}, gameVersion {v[1]}, build {v[2]}'
                    + ('' if v[0] > TARGET[0] else '  -  already 4.04 format or older, nothing to convert'))
        except Exception as e:
            text = f'Not a readable Witcher 3 save: {e}'
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self.query_one('#save-info', Static).update, text)

    # ---- converting

    def action_convert(self):
        self.run_conversion()

    @on(Button.Pressed, '#convert')
    def convert_pressed(self):
        self.run_conversion()

    def fail(self, message):
        self.notify(message, severity='error', markup=False)

    @work(exclusive=True, group='convert')
    async def run_conversion(self):
        save, out, template = self.field('save'), self.field('out'), self.field('template')
        if not save or not Path(save).expanduser().is_file():
            return self.fail('Select a .sav file to convert.')
        if not out:
            return self.fail('Select an output folder.')
        if template and not Path(template).expanduser().is_file():
            return self.fail(f'Skill template not found: {template}')
        save, out = Path(save).expanduser(), Path(out).expanduser()
        if out.resolve() == save.parent.resolve():
            return self.fail("The output folder must differ from the save's folder, "
                             'or the original would be overwritten.')
        if out.name.lower() == 'gamesaves' and not await self.push_screen_wait(Confirm(
                "This looks like the game's save folder. If your current install is 5.0, Steam Cloud "
                'syncs it. Write the 4.04 save here anyway?')):
            return
        target = out / save.name
        if target.exists() and not await self.push_screen_wait(Confirm(f'{target} already exists. Overwrite it?')):
            return

        button, log = self.query_one('#convert', Button), self.query_one(RichLog)
        button.disabled = True
        log.clear()

        def write(line):
            self.call_from_thread(log.write, line)
        try:
            written = await asyncio.to_thread(convert, save, out, Path(template).expanduser() if template else None,
                                              self.query_one('#keep', Checkbox).value, write)
            self.notify(f'Converted save written to {written}', title='Done', markup=False, timeout=8)
        except ValueError as e:  # the converter's own checks
            log.write(f'error: {e}')
            self.fail(f'Conversion failed: {e}')
        except Exception as e:
            log.write(traceback.format_exc().rstrip())
            self.fail(f'Conversion failed: {e}')
        finally:
            button.disabled = False
            button.focus()  # disabling it moved focus to the checkbox, where Space would tick it


if __name__ == '__main__':
    DowngradeApp().run()
