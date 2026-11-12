"""Canonical single source of truth for supported hardware boards. [SIL-2 / Rule 15 / Rule 37]

Centralizes board identifiers, memory limits, and emulation mappings for:
- Arduino Mega 2560 (ATmega2560, simavr)
- Arduino MKR WiFi 1010 (ATSAMD21G18A, Renode)
- Arduino Nano ESP32 (ESP32-S3 / NORA-W106)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

import typer


@dataclass(frozen=True, slots=True)
class BoardSpec:
    """Specification for a supported microcontroller board."""

    fqbn: str
    slug: str
    label: str
    detailed_name: str
    max_flash: int
    max_ram: int
    emulator: str | None = None


BOARD_MEGA: Final[BoardSpec] = BoardSpec(
    fqbn="arduino:avr:mega",
    slug="arduino-avr-mega",
    label="Arduino Mega 2560",
    detailed_name="Arduino Mega 2560 (ATmega2560)",
    max_flash=253_952,
    max_ram=8_192,
    emulator="simavr",
)

BOARD_MKRWIFI1010: Final[BoardSpec] = BoardSpec(
    fqbn="arduino:samd:mkrwifi1010",
    slug="arduino-samd-mkrwifi1010",
    label="Arduino MKR WiFi 1010",
    detailed_name="Arduino MKR WiFi 1010 (ATSAMD21G18A)",
    max_flash=262_144,
    max_ram=32_768,
    emulator="renode",
)

BOARD_NANO_ESP32: Final[BoardSpec] = BoardSpec(
    fqbn="arduino:esp32:nano_nora",
    slug="arduino-esp32-nano_nora",
    label="Arduino Nano ESP32",
    detailed_name="Arduino Nano ESP32",
    max_flash=3_145_728,
    max_ram=327_680,
    emulator="qemu",
)

OFFICIAL_BOARDS: Final[tuple[BoardSpec, ...]] = (
    BOARD_MEGA,
    BOARD_MKRWIFI1010,
    BOARD_NANO_ESP32,
)

BOARDS_BY_SLUG: Final[dict[str, BoardSpec]] = {b.slug: b for b in OFFICIAL_BOARDS}
BOARDS_BY_FQBN: Final[dict[str, BoardSpec]] = {b.fqbn: b for b in OFFICIAL_BOARDS}

SIMAVR_BOARDS: Final[list[tuple[str, str]]] = [
    (b.fqbn, b.detailed_name) for b in OFFICIAL_BOARDS if b.emulator == "simavr"
]

RENODE_BOARDS: Final[list[tuple[str, str]]] = [
    (b.fqbn, b.detailed_name) for b in OFFICIAL_BOARDS if b.emulator == "renode"
]

QEMU_BOARDS: Final[list[tuple[str, str]]] = [(b.fqbn, b.detailed_name) for b in OFFICIAL_BOARDS if b.emulator == "qemu"]

FLASH_SECTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".text",
        ".data",
        ".rodata",
        ".flash.text",
        ".flash.rodata",
        ".flash.appdesc",
        ".iram0.text",
        ".iram0.vectors",
        ".iram0.data",
        ".dram0.data",
        ".dram0.rodata",
    }
)

RAM_SECTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".data",
        ".bss",
        ".dram0.data",
        ".dram0.bss",
        ".dram0.rodata",
    }
)


def get_board_by_slug(slug: str) -> BoardSpec | None:
    """Return board spec matching slug or None."""
    return BOARDS_BY_SLUG.get(slug)


def get_board_by_fqbn(fqbn: str) -> BoardSpec | None:
    """Return board spec matching FQBN or None."""
    return BOARDS_BY_FQBN.get(fqbn)


def get_official_fqbns() -> tuple[str, ...]:
    """Return tuple of all official FQBNs."""
    return tuple(b.fqbn for b in OFFICIAL_BOARDS)


cli = typer.Typer(help="Hardware board specification metadata CLI")


@cli.command("fqbns")
def cli_fqbns(
    delimiter: Annotated[str, typer.Option("--delimiter", "-d", help="Delimiter between FQBNs")] = " ",
) -> None:
    """Print official FQBNs separated by delimiter."""
    typer.echo(delimiter.join(get_official_fqbns()))


@cli.command("slugs")
def cli_slugs(
    delimiter: Annotated[str, typer.Option("--delimiter", "-d", help="Delimiter between slugs")] = " ",
) -> None:
    """Print official board slugs separated by delimiter."""
    typer.echo(delimiter.join(b.slug for b in OFFICIAL_BOARDS))


@cli.command("list")
def cli_list() -> None:
    """List all supported boards with specifications."""
    for b in OFFICIAL_BOARDS:
        typer.echo(
            f"{b.label} | FQBN: {b.fqbn} | Slug: {b.slug} | Flash: {b.max_flash:,} B | "
            f"RAM: {b.max_ram:,} B | Emulator: {b.emulator or 'None'}"
        )


if __name__ == "__main__":
    cli()
