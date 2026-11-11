"""Installed examples and user-owned output locations."""

from pathlib import Path


def examples():
    return Path(__file__).with_name("examples")


def workspace():
    return Path.cwd() / ".parity"
