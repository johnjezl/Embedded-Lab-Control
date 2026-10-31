"""Install hints for optional dependencies ("extras").

A bare ``pip install embedded-lab-control`` has only the core
dependencies; the web UI, MCP server, Kasa plugs, SDWire muxes and USB
relays each need an extra. Code that imports one of those packages
turns the ImportError into a one-line message naming the extra, built
here so every surface (CLI, REST, MCP) says the same thing.
"""

import sys

DIST_NAME = "embedded-lab-control"

# extra -> (what it enables, packages for `pipx inject`). Keep in step
# with [project.optional-dependencies] in pyproject.toml (a test checks
# the extra names).
EXTRAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "web": ("Web dashboard and REST API support", ("flask", "flask-socketio")),
    "mcp": ("MCP support", ("mcp>=2.2,<3",)),
    "kasa": ("Kasa power control", ("python-kasa",)),
    "sdwire": ("SDWire support", ("sdwire",)),
    "actuators": ("USB relay support", ("pyserial",)),
}


class MissingExtraError(RuntimeError):
    """An optional dependency is not installed.

    A RuntimeError so existing ``except RuntimeError`` reporting (CLI
    "Error: ...", REST/MCP error results) shows the hint unchanged.
    """

    def __init__(self, extra: str):
        self.extra = extra
        super().__init__(missing_extra_message(extra))


def missing_extra_message(extra: str) -> str:
    """One line: what is missing and how to install the extra."""
    what, packages = EXTRAS[extra]
    pipx = " ".join(f'"{p}"' if any(c in p for c in "<>=,") else p for p in packages)
    message = (
        f"{what} is not installed. Install the [{extra}] extra: "
        f'pip install "{DIST_NAME}[{extra}]" '
        f"(pipx: pipx inject {DIST_NAME} {pipx})"
    )
    if extra == "sdwire":
        # PyPI sdwire requires Python >= 3.12; on older interpreters the
        # extra installs nothing, so say why the install didn't help.
        if sys.version_info < (3, 12):
            running = f"{sys.version_info.major}.{sys.version_info.minor}"
            message += (
                f"; sdwire needs Python 3.12+, and this is Python {running}, "
                "where the extra installs nothing"
            )
        else:
            message += "; sdwire needs Python 3.12+"
    return message


def is_missing(error: ImportError, *modules: str) -> bool:
    """True when ``error`` is about one of ``modules`` (or a submodule).

    Lets callers translate only the optional package's absence and keep
    the traceback for any other missing module, which would be a bug.
    """
    name = getattr(error, "name", None) or ""
    return any(name == m or name.startswith(m + ".") for m in modules)
