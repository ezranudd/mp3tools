#!/usr/bin/env python3
"""
Interactive menu for MP3 library tools.
"""

import subprocess
import sys
from pathlib import Path

BOLD    = "\033[1m"
RESET   = "\033[0m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
CYAN    = "\033[96m"
DIM     = "\033[2m"

SCRIPT_DIR = Path(__file__).parent.resolve()


def clear_screen():
    sys.stdout.write("\033c")
    sys.stdout.flush()


def get_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def print_menu(directory: str, dry_run: bool):
    print(f"{CYAN}{'=' * 50}{RESET}")
    print(f"{BOLD}{CYAN}  MP3 TOOLS v1.0{RESET}")
    print(f"{CYAN}{'=' * 50}{RESET}")
    print()

    dir_display = directory or f"{DIM}(not set){RESET}"
    print(f"  {BOLD}{CYAN}Library:{RESET}   {BOLD}{dir_display}{RESET}")

    if dry_run:
        mode_display = f"{BOLD}{YELLOW}DRY RUN{RESET} {DIM}(preview only){RESET}"
    else:
        mode_display = f"{BOLD}{RED}LIVE{RESET} {RED}(will modify files){RESET}"
    print(f"  {BOLD}{CYAN}Mode:{RESET}      {mode_display}")

    print()
    print("-" * 50)
    print()
    print(f"  [{BOLD}{GREEN}1{RESET}] Audit")
    print(f"      {DIM}Scan and report all compliance issues (read-only){RESET}")
    print()
    print(f"  [{BOLD}{GREEN}2{RESET}] Browse")
    print(f"      {DIM}Browse library in an interactive terminal tree{RESET}")
    print()
    print(f"  [{BOLD}{GREEN}3{RESET}] Standardize")
    print(f"      {DIM}Run all fixes in order; prompts for missing tags{RESET}")
    print()
    print(f"  [{BOLD}{GREEN}4{RESET}] Import")
    print(f"      {DIM}Copy and standardize tracks from another directory into the library{RESET}")
    print()
    print(f"  [{BOLD}{GREEN}5{RESET}] Sync")
    print(f"      {DIM}Sync selected artists to a device such as an SD card{RESET}")
    print()
    print("-" * 50)
    print()
    print(f"  [{BOLD}{BLUE}d{RESET}] Change directory")
    print(f"  [{BOLD}{BLUE}m{RESET}] Toggle dry-run mode")
    print(f"  [{BOLD}{RED}q{RESET}] Quit")
    print()


def select_directory() -> str | None:
    cwd = Path.cwd()

    while True:
        clear_screen()
        print(f"{CYAN}{'=' * 50}{RESET}")
        print(f"{BOLD}{CYAN}  SELECT DIRECTORY{RESET}")
        print(f"{CYAN}{'=' * 50}{RESET}")
        print()
        print(f"  {BOLD}{CYAN}Current location:{RESET} {cwd}")
        print()
        print("-" * 50)
        print()

        subdirs = sorted([d for d in cwd.iterdir() if d.is_dir() and not d.name.startswith(".")])

        print(f"  [{BOLD}{GREEN}.{RESET}] {BOLD}Use current directory{RESET}")
        print(f"      {DIM}{cwd}{RESET}")
        print()

        for i, subdir in enumerate(subdirs, 1):
            print(f"  [{BOLD}{GREEN}{i}{RESET}] {subdir.name}/")

        print()
        print("-" * 50)
        print()
        print(f"  [{BOLD}{BLUE}p{RESET}] Type absolute path")
        print(f"  [{BOLD}{BLUE}u{RESET}] Go up one level")
        print(f"  [{BOLD}{RED}c{RESET}] Cancel")
        print()

        choice = get_input("Select option: ").lower()

        if choice == "c":
            return None
        elif choice == ".":
            return str(cwd)
        elif choice == "p":
            print()
            path_input = get_input("Enter absolute path: ")
            if path_input:
                path = Path(path_input).expanduser().resolve()
                if path.is_dir():
                    return str(path)
                print(f"\n{RED}ERROR: Not a valid directory: {path_input}{RESET}")
                get_input("\nPress Enter to continue...")
        elif choice == "u":
            parent = cwd.parent
            if parent != cwd:
                cwd = parent
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(subdirs):
                cwd = subdirs[idx]
            else:
                print(f"\n{RED}Invalid selection{RESET}")
                get_input("\nPress Enter to continue...")
        else:
            print(f"\n{RED}Unknown option: {choice}{RESET}")
            get_input("\nPress Enter to continue...")


def run_script(script: str, args: list[str]):
    path = SCRIPT_DIR / script
    if not path.exists():
        print(f"\n{RED}ERROR: Script not found: {path}{RESET}")
        return
    print(f"\n{'=' * 50}")
    print(f"Running: {script}")
    print("=" * 50)
    print()
    try:
        subprocess.run([sys.executable, str(path)] + args, cwd=SCRIPT_DIR)
    except Exception as e:
        print(f"ERROR: {e}")


def main():
    directory = str(Path.cwd())
    dry_run   = True

    while True:
        clear_screen()
        print_menu(directory, dry_run)

        choice = get_input("Select option: ").lower()

        if choice == "q":
            print("\nGoodbye!")
            break

        elif choice == "d":
            selected = select_directory()
            if selected:
                directory = selected

        elif choice == "m":
            dry_run = not dry_run
            print(f"\nMode: {'DRY RUN' if dry_run else 'LIVE'}")
            get_input("\nPress Enter to continue...")

        elif choice == "1":
            if not directory:
                print(f"\n{RED}ERROR: Please set a directory first (press 'd'){RESET}")
                get_input("\nPress Enter to continue...")
                continue
            run_script("audit.py", [directory])
            get_input("\nPress Enter to continue...")

        elif choice == "2":
            if not directory:
                print(f"\n{RED}ERROR: Please set a directory first (press 'd'){RESET}")
                get_input("\nPress Enter to continue...")
                continue
            # browse.py takes over the terminal — no dry-run concept
            run_script("browse.py", [directory])

        elif choice == "3":
            if not directory:
                print(f"\n{RED}ERROR: Please set a directory first (press 'd'){RESET}")
                get_input("\nPress Enter to continue...")
                continue
            args = [directory]
            if dry_run:
                args.append("--dry-run")
            run_script("standardize.py", args)
            get_input("\nPress Enter to continue...")

        elif choice == "4":
            if not directory:
                print(f"\n{RED}ERROR: Please set a library directory first (press 'd'){RESET}")
                get_input("\nPress Enter to continue...")
                continue
            print(f"\n{CYAN}Select the source directory to import from:{RESET}\n")
            get_input("Press Enter to choose source directory...")
            source = select_directory()
            if not source:
                continue
            if source == directory:
                print(f"\n{RED}ERROR: Source and library cannot be the same directory{RESET}")
                get_input("\nPress Enter to continue...")
                continue
            args = [source, directory]
            if dry_run:
                args.append("--dry-run")
            run_script("import_tracks.py", args)
            get_input("\nPress Enter to continue...")

        elif choice == "5":
            if not directory:
                print(f"\n{RED}ERROR: Please set a library directory first (press 'd'){RESET}")
                get_input("\nPress Enter to continue...")
                continue
            print(f"\n{CYAN}Select the device directory to sync to:{RESET}\n")
            get_input("Press Enter to choose device directory...")
            device = select_directory()
            if not device:
                continue
            if device == directory:
                print(f"\n{RED}ERROR: Device and library cannot be the same directory{RESET}")
                get_input("\nPress Enter to continue...")
                continue
            args = [directory, device]
            if dry_run:
                args.append("--dry-run")
            run_script("sync_library.py", args)
            get_input("\nPress Enter to continue...")

        else:
            print(f"\n{RED}Unknown option: {choice}{RESET}")
            get_input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
