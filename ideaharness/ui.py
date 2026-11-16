"""Terminal output helpers."""

from __future__ import annotations

import os
import sys

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dim(t: str) -> str: return paint(t, "2")
def bold(t: str) -> str: return paint(t, "1")
def red(t: str) -> str: return paint(t, "31")
def green(t: str) -> str: return paint(t, "32")
def yellow(t: str) -> str: return paint(t, "33")
def cyan(t: str) -> str: return paint(t, "36")
def magenta(t: str) -> str: return paint(t, "35")


def clip(text, n: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: str) -> str:
    try:
        return input(question).strip()
    except EOFError:
        return ""


def confirm(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{question} {hint} ").lower()
    if not answer:
        return default
    return answer in ("y", "yes")
