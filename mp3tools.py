#!/usr/bin/env python3
"""
Interactive menu for MP3 library tools.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from termtext import cell_width

if sys.version_info < (3, 10):
    print(f"Error: Python 3.10 or newer is required (found {sys.version})", file=sys.stderr)
    sys.exit(1)

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
    # \033[2J clears visible screen, \033[3J clears scrollback, \033[H homes cursor
    sys.stdout.write("\033[2J\033[3J\033[H")
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


def select_directory(start: str | None = None) -> str | None:
    cwd = Path(start) if start and Path(start).is_dir() else Path.cwd()
    page = 0

    while True:
        clear_screen()

        try:
            term_w, term_h = os.get_terminal_size()
        except OSError:
            term_w, term_h = 80, 24

        subdirs = sorted(d for d in cwd.iterdir() if d.is_dir() and not d.name.startswith("."))
        num_dirs = len(subdirs)
        num_w = len(str(num_dirs)) if num_dirs else 1

        # 8 header lines + 3 current-dir block + 9 footer lines (incl. prompt)
        avail_rows = max(4, term_h - 20)

        max_name = max((cell_width(d.name) for d in subdirs), default=10)
        col_w = num_w + 3 + max_name + 1 + 2   # "[NNN] name/" + 2-char gap
        num_cols = max(1, (term_w - 2) // col_w)

        per_page = num_cols * avail_rows
        total_pages = max(1, (num_dirs + per_page - 1) // per_page) if num_dirs else 1
        page = max(0, min(page, total_pages - 1))

        print(f"{CYAN}{'=' * 50}{RESET}")
        print(f"{BOLD}{CYAN}  SELECT DIRECTORY{RESET}")
        print(f"{CYAN}{'=' * 50}{RESET}")
        print()
        print(f"  {BOLD}{CYAN}Current location:{RESET} {cwd}")
        print()
        print("-" * 50)
        print()

        print(f"  [{BOLD}{GREEN}.{RESET}] {BOLD}Use current directory{RESET}")
        print(f"      {DIM}{cwd}{RESET}")
        print()

        start = page * per_page
        page_dirs = subdirs[start : start + per_page]

        if page_dirs:
            num_rows = (len(page_dirs) + num_cols - 1) // num_cols
            for r in range(num_rows):
                line = ""
                for c in range(num_cols):
                    i = c * num_rows + r
                    if i >= len(page_dirs):
                        break
                    num = start + i + 1
                    name = page_dirs[i].name
                    plain = f"[{num:{num_w}}] {name}/"
                    colored = f"[{BOLD}{GREEN}{num:{num_w}}{RESET}] {name}/"
                    line += colored + " " * max(0, col_w - cell_width(plain))
                print("  " + line)
        else:
            print(f"  {DIM}(no subdirectories){RESET}")

        print()
        print("-" * 50)
        print()
        if total_pages > 1:
            print(f"  Page {page + 1}/{total_pages}  "
                  f"[{BOLD}{BLUE}>{RESET}] Next  [{BOLD}{BLUE}<{RESET}] Prev")
        print(f"  [{BOLD}{BLUE}p{RESET}] Type absolute path")
        print(f"  [{BOLD}{BLUE}u{RESET}] Go up one level")
        print(f"  [{BOLD}{RED}c{RESET}] Cancel")
        print()

        choice = get_input("Select option: ").strip()

        if choice.lower() == "c":
            return None
        elif choice == ".":
            return str(cwd)
        elif choice.lower() == "p":
            print()
            path_input = get_input("Enter absolute path: ")
            if path_input:
                path = Path(path_input).expanduser().resolve()
                if path.is_dir():
                    return str(path)
                print(f"\n{RED}ERROR: Not a valid directory: {path_input}{RESET}")
                get_input("\nPress Enter to continue...")
        elif choice.lower() == "u":
            parent = cwd.parent
            if parent != cwd:
                cwd = parent
            page = 0
        elif choice == ">" and total_pages > 1:
            page = min(total_pages - 1, page + 1)
        elif choice == "<" and total_pages > 1:
            page = max(0, page - 1)
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(subdirs):
                cwd = subdirs[idx]
                page = 0
            else:
                print(f"\n{RED}Invalid selection{RESET}")
                get_input("\nPress Enter to continue...")
        else:
            print(f"\n{RED}Unknown option: {choice}{RESET}")
            get_input("\nPress Enter to continue...")


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def get_mounted_devices() -> list[dict]:
    skip_fs = {
        "sysfs", "proc", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "pstore", "bpf", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs",
        "fusectl", "configfs", "securityfs", "efivarfs", "overlay", "nsfs",
        "ramfs", "squashfs",
    }
    skip_prefixes = ("/sys", "/proc", "/dev", "/run")

    seen: set[Path] = set()

    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = Path(parts[1])
                fs_type = parts[2]
                if (
                    fs_type not in skip_fs
                    and mount != Path("/")
                    and not any(str(mount).startswith(p) for p in skip_prefixes)
                    and mount.is_dir()
                    and mount not in seen
                ):
                    seen.add(mount)
    except OSError:
        pass

    for base in (Path("/media"), Path("/mnt")):
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            subs = [s for s in item.iterdir() if s.is_dir() and not s.name.startswith(".")]
            if subs:
                for sub in sorted(subs):
                    seen.add(sub)
            else:
                seen.add(item)

    devices = []
    for path in sorted(seen):
        try:
            usage = shutil.disk_usage(path)
            devices.append({"path": path, "free": usage.free, "total": usage.total})
        except OSError:
            devices.append({"path": path, "free": None, "total": None})
    return devices


def select_device() -> str | None:
    while True:
        clear_screen()
        print(f"{CYAN}{'=' * 50}{RESET}")
        print(f"{BOLD}{CYAN}  SELECT DEVICE{RESET}")
        print(f"{CYAN}{'=' * 50}{RESET}")
        print()

        devices = get_mounted_devices()

        if devices:
            print(f"  {BOLD}Mounted devices:{RESET}")
            print()
            for i, dev in enumerate(devices, 1):
                path = dev["path"]
                label = path.name or str(path)
                if dev["free"] is not None:
                    size_info = f"{_fmt_size(dev['free'])} free / {_fmt_size(dev['total'])} total"
                else:
                    size_info = "size unknown"
                print(f"  [{BOLD}{GREEN}{i}{RESET}] {BOLD}{label}{RESET}  {DIM}{path}{RESET}")
                print(f"      {DIM}{size_info}{RESET}")
                print()
        else:
            print(f"  {DIM}No mounted devices found.{RESET}")
            print()

        print("-" * 50)
        print()
        print(f"  [{BOLD}{BLUE}b{RESET}] Browse for a directory")
        print(f"  [{BOLD}{RED}c{RESET}] Cancel")
        print()

        choice = get_input("Select option: ").lower()

        if choice == "c":
            return None
        elif choice == "b":
            return select_directory()
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                return str(devices[idx]["path"])
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
        result = subprocess.run([sys.executable, str(path)] + args, cwd=SCRIPT_DIR)
        if result.returncode != 0:
            print(f"\n{RED}ERROR: {script} exited with code {result.returncode}{RESET}")
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
            selected = select_directory(start=directory)
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
            source = select_directory(start=directory)
            if not source:
                continue
            src_path = Path(source)
            lib_path = Path(directory)
            if source == directory or lib_path in src_path.parents:
                print(f"\n{RED}ERROR: Source cannot be the same as or inside the library{RESET}")
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
            device = select_device()
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

        else:
            print(f"\n{RED}Unknown option: {choice}{RESET}")
            get_input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
