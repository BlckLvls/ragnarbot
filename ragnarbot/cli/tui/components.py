"""Reusable TUI widgets for onboarding screens."""

from rich.console import Console
from rich.text import Text

from ragnarbot import __logo__
from ragnarbot.cli.tui.keys import Key, get_key_reader


class QuitOnboarding(Exception):
    """Raised when user presses Q to quit."""
    pass


def clear_screen(console: Console) -> None:
    """Clear terminal screen."""
    console.print("\033[2J\033[H", end="")


def draw_header(console: Console, title: str, subtitle: str = "") -> None:
    """Draw screen header with logo and title."""
    clear_screen(console)
    console.print()
    console.print(f"  {__logo__} [bold]ragnarbot setup[/bold]", highlight=False)
    console.print(f"  [dim]{'─' * 40}[/dim]")
    console.print(f"  [bold cyan]{title}[/bold cyan]")
    if subtitle:
        console.print(f"  [dim]{subtitle}[/dim]")
    console.print()


def draw_footer(console: Console, hints: str) -> None:
    """Draw navigation hints at the bottom."""
    console.print()
    console.print(f"  [dim]{hints}[/dim]")


def select_menu(
    console: Console,
    title: str,
    options: list[tuple[str, str]],
    selected: int = 0,
    subtitle: str = "",
    back_label: str = "Back",
) -> int | None:
    """Arrow-key menu selection.

    Args:
        console: Rich console instance
        title: Screen title
        options: List of (label, description) tuples
        selected: Initially selected index
        subtitle: Optional subtitle
        back_label: Label for back hint

    Returns:
        Selected index, or None if user pressed Esc (back)

    Raises:
        QuitOnboarding: If user pressed Q to quit
    """
    read = get_key_reader()

    while True:
        draw_header(console, title, subtitle)

        for i, (label, desc) in enumerate(options):
            if i == selected:
                console.print(f"  [bold cyan]▸ {label}[/bold cyan]  [dim]{desc}[/dim]")
            else:
                console.print(f"    {label}  [dim]{desc}[/dim]")

        draw_footer(console, f"↑/↓ Navigate  Enter Select  Esc {back_label}  Q Quit")

        key, char = read()

        if key == Key.UP:
            selected = (selected - 1) % len(options)
        elif key == Key.DOWN:
            selected = (selected + 1) % len(options)
        elif key == Key.ENTER:
            return selected
        elif key == Key.ESC:
            return None
        elif key == Key.CHAR and char.lower() == "q":
            raise QuitOnboarding()


def text_input(
    console: Console,
    title: str,
    prompt: str,
    hint: str = "",
    secret: bool = False,
    allow_empty: bool = False,
    subtitle: str = "",
) -> str | None:
    """Character-by-character text input.

    Args:
        console: Rich console instance
        title: Screen title
        prompt: Input prompt label
        hint: Help text shown above input
        secret: Mask input (show last 4 chars)
        allow_empty: Allow Enter on empty buffer (returns "")
        subtitle: Optional subtitle

    Returns:
        Input string, "" if skipped (allow_empty), or None if Esc (back)

    Raises:
        QuitOnboarding: If user pressed Q to quit (only when buffer empty)
    """
    read = get_key_reader()
    buffer = ""
    error = ""

    while True:
        draw_header(console, title, subtitle)

        if hint:
            console.print(f"  [dim]{hint}[/dim]")
            console.print()

        # Display the input
        if secret and buffer:
            visible = "•" * max(0, len(buffer) - 4) + buffer[-4:]
        else:
            visible = buffer

        console.print(f"  {prompt}: {visible}[blink]_[/blink]")

        if error:
            console.print(f"\n  [red]{error}[/red]")
            error = ""

        skip_hint = "  Enter Skip" if allow_empty else ""
        draw_footer(console, f"Type to enter  Esc Back  Q Quit{skip_hint}")

        key, char = read()

        if key == Key.BACKSPACE:
            buffer = buffer[:-1]
        elif key == Key.ENTER:
            if buffer:
                return buffer
            elif allow_empty:
                return ""
            else:
                error = "Input cannot be empty"
        elif key == Key.ESC:
            return None
        elif key == Key.CHAR:
            if char.lower() == "q" and not buffer:
                raise QuitOnboarding()
            else:
                buffer += char


def info_screen(
    console: Console,
    title: str,
    lines: list[str],
    subtitle: str = "",
) -> bool:
    """Display info and wait for Enter.

    Returns True on Enter, False on Esc.

    Raises:
        QuitOnboarding: If user pressed Q to quit
    """
    read = get_key_reader()

    draw_header(console, title, subtitle)

    for line in lines:
        console.print(f"  {line}")

    draw_footer(console, "Enter Continue  Esc Back  Q Quit")

    while True:
        key, char = read()
        if key == Key.ENTER:
            return True
        elif key == Key.ESC:
            return False
        elif key == Key.CHAR and char.lower() == "q":
            raise QuitOnboarding()
