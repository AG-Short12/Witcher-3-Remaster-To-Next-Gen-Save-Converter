"""Small window around w3_downgrade: pick a 5.0.x save, pick an output folder, convert to 4.04."""
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from w3_downgrade import Save, TARGET, convert, read_container

GAMESAVES = Path.home() / 'Documents' / 'The Witcher 3' / 'gamesaves'
DEFAULT_OUT = Path(__file__).parent / 'output'


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Witcher 3 save downgrade (5.0 -> 4.04)')
        self.minsize(640, 360)
        self.save = tk.StringVar()
        self.out = tk.StringVar(value=str(DEFAULT_OUT))
        self.keep = tk.BooleanVar(value=False)
        self.info = tk.StringVar(value='Pick a save to convert.')

        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text='Save to convert:').grid(row=0, column=0, sticky='w')
        ttk.Entry(f, textvariable=self.save).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(f, text='Browse...', command=self.pick_save).grid(row=0, column=2)

        ttk.Label(f, textvariable=self.info, foreground='#555').grid(
            row=1, column=1, columnspan=2, sticky='w', padx=6, pady=(2, 8))

        ttk.Label(f, text='Output folder:').grid(row=2, column=0, sticky='w')
        ttk.Entry(f, textvariable=self.out).grid(row=2, column=1, sticky='ew', padx=6)
        ttk.Button(f, text='Browse...', command=self.pick_out).grid(row=2, column=2)

        ttk.Checkbutton(f, variable=self.keep,
                        text='Keep 5.0-only blocks and values (fallback if the normal conversion will not load)'
                        ).grid(row=3, column=1, columnspan=2, sticky='w', padx=6, pady=8)

        self.go = ttk.Button(f, text='Convert', command=self.run)
        self.go.grid(row=4, column=1, sticky='w', padx=6)

        self.log = tk.Text(f, height=10, state='disabled', wrap='word')
        self.log.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=(10, 0))
        f.rowconfigure(5, weight=1)

    def pick_save(self):
        path = filedialog.askopenfilename(
            title='Select the 5.0.x save to convert',
            initialdir=GAMESAVES if GAMESAVES.exists() else Path.home(),
            filetypes=[('Witcher 3 saves', '*.sav'), ('All files', '*.*')])
        if not path:
            return
        self.save.set(path)
        try:
            v = Save(read_container(path)).version
            ok = v[0] > TARGET[0]
            self.info.set(f'saveVersion {v[0]}, gameVersion {v[1]}, build {v[2]}'
                          + ('' if ok else '  -  already 4.04 format or older, nothing to convert'))
        except Exception as e:
            self.info.set(f'Not a readable Witcher 3 save: {e}')

    def pick_out(self):
        path = filedialog.askdirectory(title='Select the output folder', initialdir=self.out.get() or Path.home())
        if path:
            self.out.set(path)

    def write(self, msg):
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def run(self):
        save, out = Path(self.save.get()), Path(self.out.get())
        if not save.is_file():
            return messagebox.showerror('No save', 'Select a .sav file to convert.')
        if not self.out.get():
            return messagebox.showerror('No output folder', 'Select an output folder.')
        if out.resolve() == save.parent.resolve():
            return messagebox.showerror('Same folder', 'The output folder must differ from the save\'s folder, '
                                        'or the original would be overwritten.')
        if GAMESAVES.exists() and out.resolve() == GAMESAVES.resolve() and not messagebox.askyesno(
                'Current game folder', 'This is the save folder of your current (5.0) install, which Steam Cloud '
                'syncs. Write the 4.04 save here anyway?'):
            return
        target = out / save.name
        if target.exists() and not messagebox.askyesno('Overwrite?', f'{target} already exists. Overwrite it?'):
            return

        # conversion takes about a second, so it runs on the UI thread
        self.go.configure(state='disabled')
        self.config(cursor='watch')

        def log(m):
            self.write(m)
            self.update_idletasks()

        try:
            t = convert(save, out, self.keep.get(), log)
            messagebox.showinfo('Done', f'Converted save written to:\n{t}')
        except Exception as e:
            log(traceback.format_exc())
            messagebox.showerror('Conversion failed', str(e))
        finally:
            self.config(cursor='')
            self.go.configure(state='normal')


if __name__ == '__main__':
    App().mainloop()
