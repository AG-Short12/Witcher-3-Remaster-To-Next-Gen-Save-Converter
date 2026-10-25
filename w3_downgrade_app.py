"""w3-downgrade: convert Witcher 3 next-gen (5.0.x) saves to the 4.04 format.

Without arguments it opens the terminal UI; with arguments it is the command-line converter
(see --help). This is the entry point of the standalone executable built by build.py.
"""
import sys


def main():
    if len(sys.argv) > 1:
        from w3_downgrade import main as cli
        cli(epilog='Run without arguments to open the terminal UI.')
    else:
        from w3_downgrade_tui import DowngradeApp
        DowngradeApp().run()


if __name__ == '__main__':
    main()
