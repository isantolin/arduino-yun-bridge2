#!/usr/bin/env python3
"""Interactive Pigweed console for Arduino MCU Bridge 2.

Connects directly to an MCU serial device, PTY (simavr/Renode/QEMU), or TCP
socket endpoint using upstream Pigweed ``pw_hdlc``, ``pw_rpc``,
``pw_tokenizer``, ``pw_metric``, and ``pw_console.PwConsoleEmbed``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import socket
import sys
from types import ModuleType
from typing import Annotated, Any, Final, cast
from urllib.parse import urlparse

from pw_console.embed import PwConsoleEmbed
import pw_hdlc.rpc
from pw_hdlc.decode import Frame
from pw_metric import metric_parser
from pw_metric_proto import metric_service_pb2
from pw_rpc.callback_client.errors import RpcError, RpcTimeout
from pw_rpc.console_tools.console import ClientInfo, flattened_rpc_completions
from pw_rpc.descriptors import Channel as PwRpcChannel
from pw_stream.stream_readers import CancellableReader
from pw_tokenizer import detokenize, elf_reader
from pw_tokenizer.tokens import DatabaseFormatError
from pw_transfer import transfer_pb2
import serial
import typer

from mcubridge.protocol import cloud_gateway_pb2, config_schema_pb2, mcu_wire_pb2, protocol
from mcubridge.transport.serial import (
    DEFAULT_TOKEN_DATABASE_CANDIDATES,
    decode_tokenized_log_payload,
    resolve_serial_url,
)
from tools.emulation.emulation_runner import resolve_firmware_token_database

APP_TITLE: Final[str] = "Arduino MCU Bridge 2 Console"
DEFAULT_CHANNEL_ID: Final[int] = protocol.HDLC_ADDRESS_RPC
DEFAULT_DEVICE: Final[str] = "/dev/ttyATH0"
DEFAULT_METRICS_TIMEOUT_S: Final[float] = 2.0
DEFAULT_READ_SIZE: Final[int] = 4096
DEFAULT_SOCKET_TIMEOUT_S: Final[float] = 5.0
MCU_LOG_LOGGER_NAME: Final[str] = "mcubridge.console.mcu"
MCU_CONSOLE_LOGGER_NAME: Final[str] = "mcubridge.console.stream"
HOST_LOG_LOGGER_NAME: Final[str] = "mcubridge.console.host"

_PYSERIAL_URL_SCHEMES: Final[tuple[str, ...]] = ("loop://", "rfc2217://", "hwgrep://", "spy://", "alt://")

_HOST_LOGGER = logging.getLogger(HOST_LOG_LOGGER_NAME)


def resolve_console_endpoint(device: str) -> str:
    """Resolve a serial path, pyserial URL, or TCP endpoint into a canonical URL."""
    clean = device.strip()
    if not clean:
        return ""
    if clean.startswith(_PYSERIAL_URL_SCHEMES):
        return clean
    return resolve_serial_url(clean)


@dataclass(frozen=True, slots=True)
class BridgeTransport:
    """Active Pigweed stream reader and frame writer bound to a device link."""

    endpoint: str
    resolved_url: str
    reader: CancellableReader
    writer: Callable[[bytes], Any]
    closer: Callable[[], None]

    def close(self) -> None:
        """Deterministically cancel pending reads and close the underlying link."""
        try:
            self.reader.cancel_read()
        except (AttributeError, OSError, RuntimeError) as exc:
            _HOST_LOGGER.debug("Ignoring reader cancel error during teardown: %s", exc)
        try:
            self.closer()
        except (AttributeError, OSError, RuntimeError) as exc:
            _HOST_LOGGER.debug("Ignoring transport close error during teardown: %s", exc)


def load_rpc_protos() -> list[ModuleType]:
    """Return compiled Protobuf modules exposed to the Pigweed RPC client."""
    return [
        mcu_wire_pb2,
        config_schema_pb2,
        cloud_gateway_pb2,
        transfer_pb2,
        metric_service_pb2,
    ]


def list_rpc_service_names(protos: Sequence[ModuleType] | None = None) -> list[str]:
    """Return sorted fully qualified RPC service names from loaded proto modules."""
    modules = list(protos) if protos is not None else load_rpc_protos()
    service_names: set[str] = set()
    for module in modules:
        descriptor = getattr(module, "DESCRIPTOR", None)
        if descriptor is None:
            continue
        for service in descriptor.services_by_name.values():
            service_names.add(str(service.full_name))
    return sorted(service_names)


def resolve_token_database_path(
    token_database: Path | None = None,
    *,
    firmware: Path | None = None,
) -> Path | None:
    """Resolve the active Pigweed token database path with explicit priority.

    Priority:
      1. Explicit ``token_database`` CLI/API argument.
      2. Sibling ``tokens.csv`` or ELF via ``resolve_firmware_token_database``.
      3. ``MCUBRIDGE_TOKEN_DATABASE`` environment variable.
      4. Standard installed OpenWrt paths under ``/etc/mcubridge`` and ``/usr/share/mcubridge``.
    """
    if token_database is not None:
        return token_database
    if firmware is not None:
        resolved = resolve_firmware_token_database(firmware)
        if resolved is not None:
            return resolved
    env_path = os.environ.get("MCUBRIDGE_TOKEN_DATABASE")
    if env_path:
        return Path(env_path)
    for candidate in DEFAULT_TOKEN_DATABASE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def build_detokenizer(
    token_database: Path | None = None,
    *,
    firmware: Path | None = None,
) -> tuple[detokenize.Detokenizer, Path | None]:
    """Load a Pigweed Detokenizer from a CSV, binary, or ELF database."""
    resolved_path = resolve_token_database_path(token_database, firmware=firmware)
    if resolved_path is None:
        return detokenize.Detokenizer(), None

    if not resolved_path.is_file():
        if token_database is not None:
            raise FileNotFoundError(f"Token database not found: {resolved_path}")
        _HOST_LOGGER.warning("Configured Pigweed token database does not exist: %s", resolved_path)
        return detokenize.Detokenizer(), None

    try:
        return detokenize.Detokenizer(resolved_path), resolved_path
    except (OSError, ValueError, TypeError, DatabaseFormatError, elf_reader.FileDecodeError) as exc:
        if token_database is not None:
            raise ValueError(f"Failed to load token database '{resolved_path}': {exc}") from exc
        _HOST_LOGGER.warning("Failed to load Pigweed token database '%s': %s", resolved_path, exc)
        return detokenize.Detokenizer(), None


def decode_hdlc_log_frame(payload: bytes, detokenizer: detokenize.Detokenizer) -> str:
    """Detokenize a Pigweed HDLC log frame payload or preserve printable UTF-8 text."""
    return decode_tokenized_log_payload(payload, detokenizer)


def make_log_frame_handler(
    detokenizer: detokenize.Detokenizer,
    mcu_logger: logging.Logger,
) -> Callable[[bytes], str]:
    """Create an HDLC_ADDRESS_LOG frame callback that emits into the MCU Logs pane."""

    def _handle_log(payload: bytes) -> str:
        decoded = decode_hdlc_log_frame(payload, detokenizer)
        mcu_logger.info("%s", decoded)
        return decoded

    return _handle_log


def make_console_frame_handler(console_logger: logging.Logger) -> Callable[[Frame], str]:
    """Create an HDLC_ADDRESS_CONSOLE frame callback for raw MCU console streams."""

    def _handle_console(frame: Frame) -> str:
        try:
            text = frame.data.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            text = frame.data.hex()
        console_logger.info("%s", text)
        return text

    return _handle_console


def collect_metrics_tree(
    rpcs: Any,
    detokenizer: detokenize.Detokenizer,
    *,
    timeout_s: float = DEFAULT_METRICS_TIMEOUT_S,
) -> dict[str, Any]:
    """Query ``pw.metric.proto.MetricService`` and return detokenized nested metrics."""
    try:
        parsed: object = cast(Any, metric_parser).parse_metrics(rpcs, detokenizer, timeout_s)
    except (RpcError, RpcTimeout) as exc:
        _HOST_LOGGER.warning("MetricService.Get failed: %s", exc)
        return {}
    return dict(cast(dict[str, Any], parsed)) if isinstance(parsed, dict) else {}


def _parse_socket_host_port(socket_url: str) -> tuple[str, int]:
    parsed = urlparse(socket_url)
    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        raise ValueError(f"Invalid socket endpoint URL (expected socket://host:port): {socket_url}")
    return host, port


def open_bridge_transport(
    device: str,
    *,
    baudrate: int = protocol.DEFAULT_BAUDRATE,
    socket_timeout_s: float = DEFAULT_SOCKET_TIMEOUT_S,
) -> BridgeTransport:
    """Open either a serial/PTY device or a TCP socket endpoint for HDLC traffic."""
    resolved_url = resolve_console_endpoint(device)
    if not resolved_url:
        raise ValueError("Device or socket endpoint must not be empty")

    if resolved_url.startswith("socket://"):
        host, port = _parse_socket_host_port(resolved_url)
        sock = socket.create_connection((host, port), timeout=socket_timeout_s)
        sock.settimeout(None)
        reader: CancellableReader = pw_hdlc.rpc.SocketReader(sock, DEFAULT_READ_SIZE)
        return BridgeTransport(
            endpoint=device,
            resolved_url=resolved_url,
            reader=reader,
            writer=sock.sendall,
            closer=sock.close,
        )

    ser = serial.serial_for_url(
        resolved_url,
        baudrate=baudrate,
        timeout=0.1,
        write_timeout=2.0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    serial_reader: CancellableReader = pw_hdlc.rpc.SerialReader(cast(Any, ser), DEFAULT_READ_SIZE)
    return BridgeTransport(
        endpoint=device,
        resolved_url=resolved_url,
        reader=serial_reader,
        writer=ser.write,
        closer=ser.close,
    )


def create_rpc_client(
    reader: CancellableReader,
    writer: Callable[[bytes], Any],
    *,
    detokenizer: detokenize.Detokenizer,
    mcu_logger: logging.Logger,
    console_logger: logging.Logger,
    protos: Sequence[ModuleType] | None = None,
    channel_id: int = DEFAULT_CHANNEL_ID,
) -> pw_hdlc.rpc.HdlcRpcClient:
    """Construct an upstream Pigweed ``HdlcRpcClient`` wired to MCU Bridge HDLC addresses."""
    loaded_protos = list(protos) if protos is not None else load_rpc_protos()
    channels = [
        PwRpcChannel(
            channel_id,
            pw_hdlc.rpc.channel_output(writer, address=protocol.HDLC_ADDRESS_RPC),
        )
    ]
    return pw_hdlc.rpc.HdlcRpcClient(
        reader=reader,
        paths_or_modules=loaded_protos,
        channels=channels,
        output=make_log_frame_handler(detokenizer, mcu_logger),
        rpc_frames_address=protocol.HDLC_ADDRESS_RPC,
        log_frames_address=protocol.HDLC_ADDRESS_LOG,
        extra_frame_handlers={protocol.HDLC_ADDRESS_CONSOLE: make_console_frame_handler(console_logger)},
    )


def build_console_embed(
    device: pw_hdlc.rpc.HdlcRpcClient,
    detokenizer: detokenize.Detokenizer,
    *,
    mcu_logger: logging.Logger | None = None,
    console_logger: logging.Logger | None = None,
    host_logger: logging.Logger | None = None,
    endpoint_label: str = "",
    token_db_path: Path | None = None,
    config_file_path: Path | None = None,
    channel_id: int = DEFAULT_CHANNEL_ID,
    metrics_timeout_s: float = DEFAULT_METRICS_TIMEOUT_S,
) -> PwConsoleEmbed:
    """Configure an upstream ``pw_console.PwConsoleEmbed`` with REPL helpers and log panes."""
    resolved_mcu_logger = mcu_logger or logging.getLogger(MCU_LOG_LOGGER_NAME)
    resolved_console_logger = console_logger or logging.getLogger(MCU_CONSOLE_LOGGER_NAME)
    resolved_host_logger = host_logger or logging.getLogger(HOST_LOG_LOGGER_NAME)
    rpcs = device.rpcs(channel_id)

    def get_metrics(timeout_s: float = metrics_timeout_s) -> dict[str, Any]:
        return collect_metrics_tree(rpcs, detokenizer, timeout_s=timeout_s)

    protos_packages: Any = cast(Any, device.protos).packages
    global_vars: dict[str, Any] = {
        "device": device,
        "rpcs": rpcs,
        "protos": protos_packages,
        "detokenizer": detokenizer,
        "get_metrics": get_metrics,
        "logs": resolved_mcu_logger,
        "console_logs": resolved_console_logger,
        "host_logs": resolved_host_logger,
    }

    loggers: dict[str, list[logging.Logger]] = {
        "MCU Logs": [resolved_mcu_logger],
        "MCU Console": [resolved_console_logger],
        "Host Logs": [
            resolved_host_logger,
            logging.getLogger("pw_rpc"),
            logging.getLogger("pw_hdlc"),
        ],
    }

    title = f"{APP_TITLE} ({endpoint_label})" if endpoint_label else APP_TITLE
    db_line = str(token_db_path) if token_db_path is not None else "none (raw UTF-8 fallback)"
    welcome_message = (
        f"{title}\n"
        f"Token database: {db_line}\n\n"
        "REPL examples:\n"
        "  rpcs.rpc.pb.SystemService.GetVersion()\n"
        "  rpcs.rpc.pb.SystemService.GetCapabilities()\n"
        "  rpcs.rpc.pb.GpioService.DigitalRead(pin=13)\n"
        "  get_metrics()\n"
    )

    embed = PwConsoleEmbed(
        global_vars=global_vars,
        local_vars=None,
        loggers=loggers,
        repl_startup_message=welcome_message,
        help_text=__doc__,
        app_title=title,
        config_file_path=config_file_path,
    )
    completions = flattened_rpc_completions([ClientInfo("rpcs", rpcs, device.client)])
    completions.update(
        {
            "get_metrics()": "Fetch detokenized pw_metric tree via MetricService.Get",
            "device": "HdlcRpcClient connected to the MCU bridge",
            "detokenizer": "Pigweed Detokenizer instance",
            "logs": "Python logger bound to the MCU Logs pane",
        }
    )
    embed.add_sentence_completer(completions)
    return embed


def build_check_summary(
    *,
    device: str,
    baudrate: int,
    token_database: Path | None,
    firmware: Path | None,
    channel_id: int,
) -> dict[str, Any]:
    """Resolve configuration, protos, and token database without opening hardware."""
    detokenizer, loaded_db = build_detokenizer(token_database, firmware=firmware)
    protos = load_rpc_protos()
    return {
        "app_title": APP_TITLE,
        "device": device,
        "resolved_url": resolve_console_endpoint(device),
        "baudrate": baudrate,
        "channel_id": channel_id,
        "hdlc_addresses": {
            "rpc": protocol.HDLC_ADDRESS_RPC,
            "log": protocol.HDLC_ADDRESS_LOG,
            "console": protocol.HDLC_ADDRESS_CONSOLE,
        },
        "token_database": str(loaded_db) if loaded_db is not None else None,
        "token_entries": len(detokenizer.database),
        "services": list_rpc_service_names(protos),
    }


cli = typer.Typer(
    help="Interactive Pigweed console (pw_console + pw_rpc + pw_tokenizer + pw_metric) for Arduino MCU Bridge 2.",
    add_completion=False,
)


@cli.command()
def run_console(
    device: Annotated[
        str,
        typer.Option(
            "--device",
            "-d",
            envvar="MCUBRIDGE_SERIAL_PORT",
            help="Serial port, PTY path (/dev/pts/N), or TCP socket URL (tcp://host:port, socket://host:port).",
        ),
    ] = DEFAULT_DEVICE,
    baudrate: Annotated[
        int,
        typer.Option("--baudrate", "-b", help="UART baud rate for serial/PTY links."),
    ] = protocol.DEFAULT_BAUDRATE,
    token_database: Annotated[
        Path | None,
        typer.Option(
            "--token-database",
            "-t",
            help="Path to Pigweed token database (.csv, .bin, or firmware .elf).",
        ),
    ] = None,
    firmware: Annotated[
        Path | None,
        typer.Option(
            "--firmware",
            "-f",
            help="Optional firmware binary path used to auto-locate sibling tokens.csv or ELF database.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config-file", help="Optional pw_console YAML configuration file."),
    ] = None,
    channel_id: Annotated[
        int,
        typer.Option("--channel-id", help="Pigweed RPC channel ID."),
    ] = DEFAULT_CHANNEL_ID,
    metrics_timeout: Annotated[
        float,
        typer.Option("--metrics-timeout", help="Timeout in seconds for get_metrics() RPC calls."),
    ] = DEFAULT_METRICS_TIMEOUT_S,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Validate protos, endpoint URL, and token database and print JSON summary without opening a TTY.",
        ),
    ] = False,
) -> None:
    """Launch the interactive Pigweed bridge console or validate its configuration."""
    if check:
        summary = build_check_summary(
            device=device,
            baudrate=baudrate,
            token_database=token_database,
            firmware=firmware,
            channel_id=channel_id,
        )
        typer.echo(json.dumps(summary, indent=2, sort_keys=True))
        return

    detokenizer, loaded_db = build_detokenizer(token_database, firmware=firmware)
    mcu_logger = logging.getLogger(MCU_LOG_LOGGER_NAME)
    console_logger = logging.getLogger(MCU_CONSOLE_LOGGER_NAME)
    host_logger = logging.getLogger(HOST_LOG_LOGGER_NAME)

    transport = open_bridge_transport(device, baudrate=baudrate)
    rpc_client = create_rpc_client(
        transport.reader,
        transport.writer,
        detokenizer=detokenizer,
        mcu_logger=mcu_logger,
        console_logger=console_logger,
        channel_id=channel_id,
    )
    try:
        embed = build_console_embed(
            rpc_client,
            detokenizer,
            mcu_logger=mcu_logger,
            console_logger=console_logger,
            host_logger=host_logger,
            endpoint_label=transport.resolved_url,
            token_db_path=loaded_db,
            config_file_path=config_file,
            channel_id=channel_id,
            metrics_timeout_s=metrics_timeout,
        )
        embed.setup_python_logging(loggers_with_no_propagation=[mcu_logger, console_logger])
        cast(Any, embed).embed()
    finally:
        rpc_client.close()
        transport.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point registered as the ``pw bridge-console`` plugin in ``pigweed.json``."""
    args = list(argv) if argv is not None else None
    try:
        cli(args=args, prog_name="pw bridge-console", standalone_mode=True)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
