"""UI utility functions."""

import os


def clear_screen():
    """Print a visible separator instead of clearing the terminal.

    This preserves the full session log so users can scroll back
    through every prompt and output.
    """
    print("\n" + "─" * 60 + "\n")
