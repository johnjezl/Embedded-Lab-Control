"""Install hints for optional dependencies ("extras").

A bare ``pip install embedded-lab-control`` has only the core
dependencies; the web UI, MCP server, Kasa plugs, SDWire muxes and USB
relays each need an extra. Code that imports one of those packages
turns the ImportError into a one-line message naming the extra, built
here so every surface (CLI, REST, MCP) says the same thing.
"""

import sys

DIST_NAME = "embedded-lab-control"

# extra -> what it enables. Keep in step with
# [project.optional-dependencies] in pyproject.toml; a test checks the
# names and extra_packages() against it.
EXTRAS: dict[str, str] = {
    "web": "Web dashboard and REST API support",
    "mcp": "MCP support",
    "kasa": "Kasa power control",
    "sdwire": "SDWire support",
    "actuators": "USB relay support",
}

# The extras' requirements, as pyproject.toml declares them, for
# `pipx inject` (which installs packages, not extras).
_PACKAGES: dict[str, tuple[str, ...]] = {
    "web": ("flask>=3.0", "flask-socketio>=5.0"),
    "mcp": ("mcp>=2.2,<3",),
    "sdwire": ("sdwire>=0.3",),
    "actuators": ("pyserial>=3.5",),
}


class MissingExtraError(RuntimeError):
    """An optional dependency is not installed.

    A RuntimeError so existing ``except RuntimeError`` reporting (CLI
    "Error: ...", REST/MCP error results) shows the hint unchanged.
    """

    def __init__(self, extra: str, what: str | None = None):
        self.extra = extra
        super().__init__(missing_extra_message(extra, what))


def extra_packages(extra: str) -> tuple[str, ...]:
    """The extra's requirements for the running interpreter."""
    if extra == "kasa":
        # python-kasa >= 0.8 needs Python 3.11; 3.10 stays on 0.7.x.
        if sys.version_info >= (3, 11):
            return ("python-kasa>=0.12",)
        return ("python-kasa>=0.7.7,<0.8",)
    return _PACKAGES[extra]


def missing_extra_message(extra: str, what: str | None = None) -> str:
    """One line: what is missing and how to install the extra.

    ``what`` overrides the extra's description, for a command that
    needs only part of it (e.g. password hashing from [web]).
    """
    what = what or EXTRAS[extra]
    if extra == "sdwire" and sys.version_info < (3, 12):
        # PyPI sdwire requires Python >= 3.12: on older interpreters the
        # extra installs nothing, so no install command here would work.
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        return (
            f"{what} needs Python 3.12+ (this is Python {running}); the "
            f"[sdwire] extra installs nothing here. Install labctl under "
            f"Python 3.12 or newer to use SDWire"
        )
    pipx = " ".join(f'"{p}"' for p in extra_packages(extra))
    message = (
        f"{what} is not installed. Install the [{extra}] extra: "
        f'pip install "{DIST_NAME}[{extra}]" '
        f"(pipx: pipx inject {DIST_NAME} {pipx})"
    )
    if extra == "sdwire":
        message += "; sdwire needs Python 3.12+"
    return message


def is_missing(error: ImportError, *modules: str) -> bool:
    """True when ``error`` says one of ``modules`` is not installed.

    Lets callers translate only the optional package's absence. Any
    other ImportError, including ``cannot import name`` from an
    installed but incompatible version (a plain ImportError, not
    ModuleNotFoundError), keeps its real cause, as does a missing
    submodule (``kasa.iot``): the package is there, but not a version
    labctl can use.
    """
    if not isinstance(error, ModuleNotFoundError):
        return False
    name = error.name or ""
    return name in modules
