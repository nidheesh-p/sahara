"""PyInstaller entry point for the Sahara console command."""

from sahara.cli import main

if __name__ == "__main__":
    main(prog_name="sahara")
