"""The small PCS-Scanner.exe the player's shortcut runs once the app is installed as a folder (see pcs_install.py):
starts the installed app with the same arguments and exits. Standard library only (a small, fast exe)."""
import sys

import pcs_install


def main() -> int:
    try:
        args = sys.argv[1:]
        return pcs_install.run_app(args, sys.executable, wait="--selftest" in args or "--scan" in args) or 0
    except FileNotFoundError:
        pcs_install.message("PCS Scanner's files are missing on this PC. Download PCS-Scanner.exe again from the "
                            "release page and run it: your settings and recordings are kept.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
