#!/usr/bin/env python3
"""Daemon for the Arduino Yun Bridge v2.

This daemon bridges communication between the Arduino Yun's microcontroller and
the Linux-based OpenWRT system, using MQTT as the primary protocol for
external communication.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import ssl  # Importado para el tipado de TLS
import struct
import traceback # Añadido para logging detallado de errores
import aio_mqtt
# La importación 'from aio_mqtt import Message' se eliminó porque causa ImportError
# from aio_mqtt import Message

# Importar excepciones específicas si existen, basado en la documentación
from aio_mqtt.exceptions import AccessRefusedError, ConnectionLostError, ConnectionCloseForcedError


import serial
import serial_asyncio
from yunrpc import cobs
from yunrpc.frame import Frame
from yunrpc.protocol import Command, Status
from yunrpc.utils import get_uci_config

# CORRECCIÓN: Volver a importar 'cast'
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple, cast

# --- Logger ---
logger = logging.getLogger("yunbridge")

# --- Configuration Constants ---
# Ruta para el archivo de estado (PID, colas, etc.)
STATUS_FILE_PATH: str = "/tmp/yunbridge_status.json"
# Intervalo (segundos) para reintentar conexiones (Serial y MQTT)
RECONNECT_DELAY_S: int = 5
# Intervalo (segundos) para escribir el archivo de estado
STATUS_INTERVAL_S: int = 5


# --- Topic Constants ---
TOPIC_BRIDGE: str = "br" # Base topic configurable desde UCI? Podría ser útil.
TOPIC_DIGITAL: str = "d"
TOPIC_ANALOG: str = "a"
TOPIC_CONSOLE: str = "console"
TOPIC_SH: str = "sh"
TOPIC_MAILBOX: str = "mailbox"
TOPIC_DATASTORE: str = "datastore"
TOPIC_FILE: str = "file"


class State:
    """A class to hold the shared state of the bridge daemon."""

    def __init__(self) -> None:
        """Initialize the state."""
        self.serial_writer: Optional[asyncio.StreamWriter] = None
        self.mqtt_publish_queue: asyncio.Queue[aio_mqtt.PublishableMessage] = asyncio.Queue()
        self.datastore: Dict[str, str] = {}
        # CORRECCIÓN DE TIPO: Deque debe especificar el tipo de contenido
        self.mailbox_queue: Deque[bytes] = collections.deque()
        self.mcu_is_paused: bool = False
        # CORRECCIÓN DE TIPO: Deque debe especificar el tipo de contenido
        self.console_to_mcu_queue: Deque[bytes] = collections.deque()
        self.running_processes: Dict[int, asyncio.subprocess.Process] = {}
        self.process_lock: asyncio.Lock = asyncio.Lock()
        self.next_pid: int = 1
        # CORRECCIÓN DE TIPO: Usar List en lugar de list
        self.allowed_commands: List[str] = []
        self.process_timeout: int = 10
        self.file_system_root: str = "/root/yun_files"


async def send_frame(state: State, command_id: int, payload: bytes = b"") -> bool:
    """Build and send a frame to the MCU asynchronously."""
    if not state.serial_writer:
        logger.error("Serial writer not available for sending.")
        return False
    try:
        raw_frame = Frame.build(command_id, payload)
        encoded_frame = cobs.encode(raw_frame)
        packet_to_send = encoded_frame + b"\x00"
        state.serial_writer.write(packet_to_send)
        await state.serial_writer.drain()
        try:
            log_name: str = Command(command_id).name
        except ValueError:
            try:
                log_name = Status(command_id).name
            except ValueError:
                log_name = f"UNKNOWN_CMD_ID(0x{command_id:02X})" # Mejor log
        logger.debug("LINUX > %s PAYLOAD: %s", log_name, payload.hex())
        return True
    except ConnectionResetError:
         logger.error("Serial connection reset while sending. Disconnecting.")
         # Intentar cerrar el writer podría fallar si ya está roto
         try:
             if state.serial_writer and not state.serial_writer.is_closing():
                 state.serial_writer.close()
                 await state.serial_writer.wait_closed()
         except Exception:
             logger.exception("Error closing serial writer after reset.")
         finally:
            state.serial_writer = None # Marcar como desconectado
         return False
    except Exception:
        logger.exception("An unexpected error occurred during send_frame")
        # Considerar cerrar la conexión serial si el error es grave
        # if state.serial_writer: state.serial_writer.close()
        # state.serial_writer = None
        return False


# --- MCU Command Handlers ---

MCUCommandHandler = Callable[[State, bytes], Awaitable[None]]


async def _handle_digital_read_resp(state: State, payload: bytes) -> None:
    if len(payload) < 3:
        logger.warning("Malformed DIGITAL_READ_RESP payload: %s", payload.hex())
        return
    pin: int = payload[0]
    # CORRECCIÓN ENDIANNESS: El protocolo y C++ usan Little Endian
    value: int = int.from_bytes(payload[1:3], "little")
    topic: str = f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/{str(pin)}/value"
    message = aio_mqtt.PublishableMessage(
        topic_name=topic, payload=str(value).encode("utf-8")
    )
    await state.mqtt_publish_queue.put(message)


async def _handle_analog_read_resp(state: State, payload: bytes) -> None:
    if len(payload) < 3:
        logger.warning("Malformed ANALOG_READ_RESP payload: %s", payload.hex())
        return
    pin: int = payload[0]
    # CORRECCIÓN ENDIANNESS: El protocolo y C++ usan Little Endian
    value: int = int.from_bytes(payload[1:3], "little")
    topic: str = f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/{str(pin)}/value"
    message = aio_mqtt.PublishableMessage(
        topic_name=topic, payload=str(value).encode("utf-8")
    )
    await state.mqtt_publish_queue.put(message)


async def _handle_ack(state: State, _: bytes) -> None:
    logger.debug("MCU > ACK received, command confirmed.") # Cambiado a debug


async def _handle_xoff(state: State, payload: bytes) -> None:
    logger.warning("MCU > XOFF received, pausing console output.")
    state.mcu_is_paused = True


async def _handle_xon(state: State, payload: bytes) -> None:
    logger.info("MCU > XON received, resuming console output.")
    state.mcu_is_paused = False
    # Vaciar cola de consola pendiente
    while state.console_to_mcu_queue and not state.mcu_is_paused:
        data: bytes = state.console_to_mcu_queue.popleft()
        await send_frame(state, Command.CMD_CONSOLE_WRITE.value, data)


async def _handle_console_write(state: State, payload: bytes) -> None:
    # Publica los datos recibidos de la consola del MCU
    message = aio_mqtt.PublishableMessage(
        topic_name=f"{TOPIC_BRIDGE}/{TOPIC_CONSOLE}/out", payload=payload
    )
    await state.mqtt_publish_queue.put(message)


async def _handle_datastore_get_resp(state: State, payload: bytes) -> None:
    try:
        key: bytes
        value: bytes
        key, value = payload.split(b"\0", 1)
        key_str: str = key.decode("utf-8")
        value_str: str = value.decode("utf-8")
        message = aio_mqtt.PublishableMessage(
            topic_name=f"{TOPIC_BRIDGE}/datastore/get/{key_str}",
            payload=value_str.encode("utf-8")
        )
        await state.mqtt_publish_queue.put(message)
    except (ValueError, UnicodeDecodeError):
        logger.warning("Malformed DATASTORE_GET_RESP payload: %s", payload.hex())


async def _handle_mailbox_processed(state: State, payload: bytes) -> None:
    message = aio_mqtt.PublishableMessage(
        topic_name=f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/processed", payload=payload
    )
    await state.mqtt_publish_queue.put(message)


async def _handle_mailbox_available(state: State, _: bytes) -> None:
    """Handle mailbox available requests from the MCU (Arduino asks Linux)."""
    # Arduino pide saber cuántos mensajes hay en la cola de Linux
    count: bytes = str(len(state.mailbox_queue)).encode("utf-8")
    await send_frame(state, Command.CMD_MAILBOX_AVAILABLE_RESP.value, count)


async def _handle_mailbox_read(state: State, _: bytes) -> None:
    """Handle mailbox read requests from the MCU (Arduino asks Linux)."""
    # Arduino pide leer el siguiente mensaje de la cola de Linux
    message_payload: bytes = b""
    if state.mailbox_queue:
        message_payload = state.mailbox_queue.popleft()
    await send_frame(state, Command.CMD_MAILBOX_READ_RESP.value, message_payload)
    # Publicar nuevo tamaño de cola (útil para clientes MQTT)
    count_msg = aio_mqtt.PublishableMessage(
        topic_name=f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/available",
        payload=str(len(state.mailbox_queue)).encode("utf-8")
    )
    await state.mqtt_publish_queue.put(count_msg)


async def _handle_process_run_async(state: State, payload: bytes) -> None:
    cmd_str: str = payload.decode("utf-8", errors="ignore")
    if state.allowed_commands and cmd_str.split()[0] not in state.allowed_commands:
        logger.warning("Async command not allowed: %s", cmd_str)
        await send_frame(state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"-1")
        return
    logger.info("Received async PROCESS_RUN: '%s'", cmd_str)
    # Obtener PID de forma segura
    async with state.process_lock:
        pid: int = state.next_pid
        state.next_pid += 1
    try:
        proc: asyncio.subprocess.Process = await asyncio.create_subprocess_shell(
            cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async with state.process_lock:
            state.running_processes[pid] = proc
        await send_frame(
            state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, str(pid).encode("utf-8")
        )
        logger.info("Started async process '%s' with PID %d", cmd_str, pid)
    except OSError as e:
        logger.error("Error starting async process: %s", e)
        await send_frame(state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"-1")
    except Exception:
        logger.exception("Unexpected error starting async process")
        await send_frame(state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"-1")


async def _handle_process_poll(state: State, payload: bytes) -> None:
    try:
        pid: int = int(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Invalid PID received for PROCESS_POLL: %s", payload.hex())
        await send_frame(state, Command.CMD_PROCESS_POLL_RESP.value, b"Error: Invalid PID")
        return

    output: bytes = b""
    process_finished : bool = False

    async with state.process_lock:
        if pid not in state.running_processes:
            logger.warning("Polling non-existent PID: %d", pid)
            await send_frame(
                state, Command.CMD_PROCESS_POLL_RESP.value, b"Error: No such process"
            )
            return

        proc: asyncio.subprocess.Process = state.running_processes[pid]

        # Leer salida disponible sin bloquear
        try:
            if proc.stdout:
                stdout_data = await proc.stdout.read(1024) # Leer hasta 1KB
                output += stdout_data
            if proc.stderr:
                 stderr_data = await proc.stderr.read(1024) # Leer hasta 1KB
                 if stderr_data:
                     output += stderr_data # Podríamos diferenciar stdout/stderr si es necesario
        except (OSError, BrokenPipeError, AttributeError, ValueError):
             logger.debug("Error reading non-blocking stdout/stderr for PID %d", pid, exc_info=True)
             # El proceso podría haber terminado justo ahora

        # Verificar si el proceso ha terminado
        if proc.returncode is not None:
            process_finished = True
            # Intentar leer cualquier salida restante después de que termine
            try:
                stdout_rem, stderr_rem = await asyncio.wait_for(proc.communicate(), timeout=0.1)
                output += (stdout_rem or b"") + (stderr_rem or b"")
            except (asyncio.TimeoutError, OSError, BrokenPipeError, ValueError):
                 logger.debug("Error reading final output for PID %d", pid, exc_info=True)

            del state.running_processes[pid]
            logger.info(
                "Async process %d finished with code %d and was cleaned up.",
                pid,
                proc.returncode,
            )

    await send_frame(state, Command.CMD_PROCESS_POLL_RESP.value, output)
    if process_finished:
        logger.debug("Sent final output for finished process PID %d", pid)


async def _handle_process_kill(state: State, payload: bytes) -> None:
    try:
        pid: int = int(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Invalid PID received for PROCESS_KILL: %s", payload.hex())
        return # No hay respuesta definida para kill fallido

    async with state.process_lock:
        if pid in state.running_processes:
            proc_to_kill = state.running_processes[pid]
            try:
                proc_to_kill.kill()
                # Esperar un poco a que el proceso termine
                await asyncio.wait_for(proc_to_kill.wait(), timeout=0.5)
                logger.info("Killed process with PID %d", pid)
            except asyncio.TimeoutError:
                 logger.warning("Process PID %d did not terminate after kill signal.", pid)
            except ProcessLookupError:
                 logger.info("Process PID %d already exited before kill.", pid)
            except Exception:
                logger.exception("Error killing process PID %d", pid)
            finally:
                # Asegurarse de eliminarlo del diccionario
                if pid in state.running_processes:
                    del state.running_processes[pid]
        else:
             logger.warning("Attempted to kill non-existent PID: %d", pid)


# --- File Operations ---

async def _perform_file_operation(
    state: State, operation: str, filename: str, data: Optional[bytes] = None
) -> Optional[bytes]:
    """Performs file operations (read, write, remove) safely within the root directory."""
    safe_path: Optional[str] = _get_safe_path(state, filename)
    if not safe_path:
        logger.warning("File operation blocked for unsafe path: %s", filename)
        # Para 'read', devolver vacío; para otros, devolver None (o un error?)
        return b"" if operation == "read" else None

    try:
        if operation == "write":
            if data is None:
                raise ValueError("Data cannot be None for write operation")
            # Ejecutar la escritura en un hilo para no bloquear asyncio
            await asyncio.to_thread(_write_file_sync, safe_path, data)
            logger.info("Wrote %d bytes to %s", len(data), safe_path)
            return None # Write no devuelve contenido

        # Verificar existencia para read y remove
        exists = await asyncio.to_thread(os.path.exists, safe_path)
        if not exists:
            logger.warning("File operation on non-existent file: %s", safe_path)
            return b"" if operation == "read" else None

        if operation == "read":
            # Ejecutar la lectura en un hilo
            content = await asyncio.to_thread(_read_file_sync, safe_path)
            logger.info("Read %d bytes from %s", len(content), safe_path)
            return content

        if operation == "remove":
            # Ejecutar el borrado en un hilo
            await asyncio.to_thread(os.remove, safe_path)
            logger.info("Removed file %s", safe_path)
            return None # Remove no devuelve contenido

    except (ValueError, OSError):
        logger.exception("File operation failed for %s (%s)", safe_path, operation)

    # Fallback por si algo falla
    return b"" if operation == "read" else None


def _write_file_sync(path: str, data: bytes) -> None:
    """Synchronous file write (to be run in a thread)."""
    # Asegurarse de que el directorio existe
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _read_file_sync(path: str) -> bytes:
    """Synchronous file read (to be run in a thread)."""
    with open(path, "rb") as f:
        return f.read()


def _get_safe_path(state: State, filename: str) -> Optional[str]:
    """Get a safe, absolute path within the configured file system root."""
    base_dir = os.path.abspath(state.file_system_root)
    # Crear directorio base si no existe
    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError:
        logger.exception("Failed to create base directory for files: %s", base_dir)
        return None

    # Limpiar filename para evitar caracteres problemáticos o traversals iniciales
    # Esto es una medida básica, se podría mejorar
    cleaned_filename = filename.lstrip('./\\').replace('../', '')

    # Unir de forma segura
    safe_path = os.path.abspath(os.path.join(base_dir, cleaned_filename))

    # Verificar que el path resultante sigue estando dentro del directorio base
    if os.path.commonpath([safe_path, base_dir]) != base_dir:
        logger.warning(
            "Path traversal attempt blocked: filename='%s', resolved to '%s', base='%s'",
            filename, safe_path, base_dir
        )
        return None
    return safe_path


async def _handle_file_write(state: State, payload: bytes) -> None:
    try:
        filename_bytes: bytes
        data: bytes
        filename_bytes, data = payload.split(b"\0", 1)
        filename: str = filename_bytes.decode("utf-8", errors="ignore")
        await _perform_file_operation(state, "write", filename, data)
        # File write no tiene respuesta definida hacia el MCU
    except ValueError:
        logger.warning("Invalid file write payload format from MCU.")
    except Exception:
        logger.exception("Error handling file write command")


async def _handle_file_read(state: State, payload: bytes) -> None:
    filename: str = payload.decode("utf-8", errors="ignore")
    content: Optional[bytes] = await _perform_file_operation(state, "read", filename)
    await send_frame(state, Command.CMD_FILE_READ_RESP.value, content or b"")


async def _handle_file_remove(state: State, payload: bytes) -> None:
    filename: str = payload.decode("utf-8", errors="ignore")
    await _perform_file_operation(state, "remove", filename)
    # File remove no tiene respuesta definida hacia el MCU


# --- Command Dispatcher ---

MCU_COMMAND_HANDLERS: Dict[int, MCUCommandHandler] = {
    # Responses from MCU
    Command.CMD_DIGITAL_READ_RESP.value: _handle_digital_read_resp,
    Command.CMD_ANALOG_READ_RESP.value: _handle_analog_read_resp,
    Status.ACK.value: _handle_ack,
    Command.CMD_XOFF.value: _handle_xoff,
    Command.CMD_XON.value: _handle_xon,
    Command.CMD_CONSOLE_WRITE.value: _handle_console_write, # MCU writing to Linux console
    Command.CMD_DATASTORE_GET_RESP.value: _handle_datastore_get_resp,
    Command.CMD_MAILBOX_PROCESSED.value: _handle_mailbox_processed,
    Command.CMD_PROCESS_RUN_ASYNC_RESP.value: _handle_process_run_async, # Async process PID response
    Command.CMD_PROCESS_POLL_RESP.value: _handle_process_poll, # Async process poll response
    Command.CMD_FILE_READ_RESP.value: _handle_file_read, # File read content response

    # Requests from MCU that Linux needs to handle
    Command.CMD_MAILBOX_AVAILABLE.value: _handle_mailbox_available, # MCU asks how many msgs Linux has
    Command.CMD_MAILBOX_READ.value: _handle_mailbox_read,           # MCU asks to read next msg from Linux
    Command.CMD_FILE_WRITE.value: _handle_file_write,             # MCU requests to write a file
    Command.CMD_FILE_REMOVE.value: _handle_file_remove,           # MCU requests to remove a file
    Command.CMD_PROCESS_KILL.value: _handle_process_kill,           # MCU requests to kill a process
}


async def handle_mcu_frame(state: State, command_id: int, payload: bytes) -> None:
    """Handle a command frame received from the MCU."""
    command_name: str
    try:
        # Intentar resolver como Comando o Estado para logging
        try:
            command_name = Command(command_id).name
            logger.debug("MCU > CMD: %s PAYLOAD: %s", command_name, payload.hex())
        except ValueError:
            try:
                command_name = Status(command_id).name
                # Cambiado a debug, ACK es frecuente
                logger.debug("MCU > STATUS: %s PAYLOAD: %s", command_name, payload.hex())
            except ValueError:
                command_name = f"UNKNOWN_CMD_ID(0x{command_id:02X})"
                logger.warning(
                    "MCU > Unknown CMD/STATUS ID: %s PAYLOAD: %s",
                    hex(command_id),
                    payload.hex(),
                )
                # Enviar CMD_UNKNOWN solo si es un comando inesperado (no una respuesta)
                if command_id < 0x80:
                     await send_frame(state, Status.CMD_UNKNOWN.value, b"")
                return # Ignorar respuestas desconocidas

        # Encontrar y ejecutar el manejador
        handler: Optional[MCUCommandHandler] = MCU_COMMAND_HANDLERS.get(command_id)
        if handler:
            await handler(state, payload)
        elif command_id < 0x80: # Si es un comando (no respuesta) sin handler específico
             logger.warning("No specific handler for MCU command %s", command_name)
             # Opcionalmente, podríamos enviar NOT_IMPLEMENTED si es un comando válido pero sin handler
             # await send_frame(state, Status.NOT_IMPLEMENTED.value, b"")

    except Exception:
         logger.exception(
             "Error handling MCU frame: CMD=0x%02X, Payload=%s",
             command_id, payload.hex()
         )


# --- Serial Task ---

async def serial_reader_task(
    serial_port: str, serial_baud: int, state: State
) -> None:
    """Connect to, read from, and handle the serial port."""
    while True:
        reader = None
        writer = None
        try:
            logger.info("Attempting to connect to serial port %s at %d baud...", serial_port, serial_baud)
            # CORRECCIÓN: 'cast' es necesario aquí porque Pylance no puede inferir
            # el tipo de 'open_serial_connection' correctamente en todos los casos.
            _reader, _writer = await serial_asyncio.open_serial_connection(
                url=serial_port, baudrate=serial_baud
            )
            reader = cast(asyncio.StreamReader, _reader)
            writer = cast(asyncio.StreamWriter, _writer)

            state.serial_writer = writer
            logger.info("Serial port connected successfully.")
            buffer = bytearray()
            while True:
                # Leer bytes uno a uno o en chunks pequeños para buscar el terminador 0x00
                byte = await reader.read(1)
                if not byte:
                    # End of stream, conexión cerrada
                    logger.warning("Serial read returned empty, connection likely closed.")
                    break
                
                if byte == b'\x00':
                    # Fin de paquete COBS
                    if buffer:
                        encoded_packet = bytes(buffer)
                        buffer.clear() # Limpiar para el siguiente paquete
                        try:
                            raw_frame: bytes = cobs.decode(encoded_packet)
                            command_id: int
                            payload: bytes
                            command_id, payload = Frame.parse(raw_frame)
                            await handle_mcu_frame(state, command_id, payload)
                        except ValueError as e:
                            logger.warning(
                                "Frame processing error: %s. COBS Packet: %s",
                                e,
                                encoded_packet.hex(),
                            )
                        except Exception:
                            logger.exception("Unexpected error parsing frame")
                    # Ignorar bytes 0x00 si el buffer está vacío (ruido o paquetes vacíos)
                else:
                    buffer.append(byte[0])
                    # Opcional: Limitar tamaño del buffer para evitar consumo de memoria
                    # MAX_COBS_PACKET_SIZE = 512 # Ejemplo
                    # if len(buffer) > MAX_COBS_PACKET_SIZE:
                    #     logger.error("COBS buffer overflow (%d bytes), discarding data.", len(buffer))
                    #     buffer.clear()

        except (serial.SerialException, asyncio.IncompleteReadError) as e:
            logger.error("Serial communication error: %s", e)
        except ConnectionResetError:
            logger.error("Serial connection reset.")
        except asyncio.CancelledError:
             logger.info("Serial reader task cancelled.")
             raise # Propagar cancelación
        except Exception:
            logger.critical("Unhandled exception in serial_reader_task", exc_info=True)
        finally:
            if writer and not writer.is_closing():
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    logger.exception("Error closing serial writer.")
            state.serial_writer = None # Marcar como desconectado
            logger.warning(
                "Serial port disconnected. Retrying in %d seconds...",
                RECONNECT_DELAY_S,
            )
            await asyncio.sleep(RECONNECT_DELAY_S)


# --- MQTT Task ---

async def mqtt_task(
    mqtt_host: str,
    mqtt_port: int,
    state: State,
    tls_context: Optional[ssl.SSLContext] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """Handle all MQTT communication (in and out)."""
    while True:
        # Inicializar client dentro del bucle para reconexión limpia
        client = aio_mqtt.Client(loop=asyncio.get_running_loop())
        publisher_task: Optional[asyncio.Task[None]] = None
        subscriber_task: Optional[asyncio.Task[None]] = None
        connect_future: Optional[asyncio.Future[Exception | None]] = None # Para disconnect_reason

        try:
            logger.info("Connecting to MQTT Broker at %s:%s...", mqtt_host, mqtt_port)
            # CORRECCIÓN: Renombrado 'hostname' a 'host' para aio_mqtt.Client.connect
            connect_result = await client.connect(
                host=mqtt_host, # Argumento es 'host', no 'hostname'
                port=mqtt_port,
                username=username,
                password=password,
                ssl=tls_context,
            )
            connect_future = cast(asyncio.Future[Exception | None], connect_result.disconnect_reason) # Guardar el Future
            logger.info("Connected to MQTT Broker.")

            # Suscripciones
            subscriptions: List[Tuple[str, aio_mqtt.QOSLevel]] = [
                (f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/+/mode", aio_mqtt.QOSLevel.QOS_0), # pin mode set
                (f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/+/read", aio_mqtt.QOSLevel.QOS_0), # pin digital read trigger
                (f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/+/read", aio_mqtt.QOSLevel.QOS_0),  # pin analog read trigger
                (f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/+", aio_mqtt.QOSLevel.QOS_0),      # pin digital write (base topic)
                (f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/+", aio_mqtt.QOSLevel.QOS_0),       # pin analog write (base topic)
                (f"{TOPIC_BRIDGE}/{TOPIC_CONSOLE}/in", aio_mqtt.QOSLevel.QOS_0),
                (f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/put/#", aio_mqtt.QOSLevel.QOS_0), # Necesita '#' para capturar keys
                (f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/get/#", aio_mqtt.QOSLevel.QOS_0),  # Necesita '#' para capturar keys
                (f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/write", aio_mqtt.QOSLevel.QOS_0),
                (f"{TOPIC_BRIDGE}/{TOPIC_SH}/run", aio_mqtt.QOSLevel.QOS_0),
                (f"{TOPIC_BRIDGE}/{TOPIC_FILE}/write/#", aio_mqtt.QOSLevel.QOS_0), # Necesita '#' para capturar filenames
                (f"{TOPIC_BRIDGE}/{TOPIC_FILE}/read/#", aio_mqtt.QOSLevel.QOS_0),   # Necesita '#' para capturar filenames
                (f"{TOPIC_BRIDGE}/{TOPIC_FILE}/remove/#", aio_mqtt.QOSLevel.QOS_0),# Necesita '#' para capturar filenames
            ]

            logger.info("Subscribing to %d topics...", len(subscriptions))
            await client.subscribe(*subscriptions)
            logger.info("All topics subscribed successfully.")

            # Lanzar tareas de publicación y suscripción
            publisher_task = asyncio.create_task(
                _mqtt_publisher_loop(client, state)
            )
            subscriber_task = asyncio.create_task(
                _mqtt_subscriber_loop(client, state)
            )

            # Esperar a que las tareas terminen o la conexión se pierda
            if connect_future:
                disconnect_reason: Exception | None = await connect_future
                logger.warning("MQTT disconnected: %s. Attempting reconnect.", disconnect_reason)
            else:
                 logger.error("Could not get disconnect_reason future. Reconnecting.")
                 # Si no tenemos el future, esperar a que alguna tarea falle
                 # CORRECCIÓN: Asegurarse que las tareas existen antes de esperar
                 tasks_to_wait = [t for t in (publisher_task, subscriber_task) if t is not None]
                 if tasks_to_wait:
                     done, pending = await asyncio.wait(
                         tasks_to_wait, return_when=asyncio.FIRST_COMPLETED
                     )
                     for task in pending: task.cancel() # Cancelar la otra tarea
                     # Revisar si la tarea completada tuvo una excepción
                     for task in done:
                         exc = task.exception()
                         if exc: logger.error("MQTT task failed: %s", exc)


        # CORRECCIÓN: Usar excepciones específicas o Exception
        except AccessRefusedError:
            logger.critical("MQTT access refused. Check credentials. Retrying...")
        except ConnectionLostError:
            logger.error("MQTT connection lost. Retrying...")
        except ConnectionCloseForcedError:
             logger.warning("MQTT connection closed by broker/network. Retrying...")
        # CORRECCIÓN: Quitar aio_mqtt.MqttError y usar Exception más genérica al final
        except (OSError, asyncio.TimeoutError) as e:
            # Capturar errores de conexión genéricos
            logger.error("MQTT connection OS/Timeout error: %s. Retrying...", e)
        except asyncio.CancelledError:
             logger.info("MQTT task cancelled.")
             raise # Propagar cancelación
        except Exception: # Captura genérica para otros errores inesperados (incluyendo ValueError de subscribe)
            logger.critical("Unhandled exception in mqtt_task", exc_info=True)
            logger.error(traceback.format_exc()) # Log completo del traceback
        finally:
            logger.debug("MQTT task cleanup.")
            # Crear lista de tareas válidas para gather
            tasks_to_gather: List[asyncio.Task[None]] = []
            if publisher_task:
                if not publisher_task.done(): publisher_task.cancel()
                tasks_to_gather.append(publisher_task)
            if subscriber_task:
                if not subscriber_task.done(): subscriber_task.cancel()
                tasks_to_gather.append(subscriber_task)

            # Esperar a que las tareas se cancelen
            if tasks_to_gather:
                try:
                    await asyncio.gather(*tasks_to_gather, return_exceptions=True)
                except asyncio.CancelledError:
                    pass # Esperado si la tarea principal fue cancelada
                except Exception:
                     logger.exception("Error during MQTT task cleanup gather")


            # Desconectar si aún está conectado
            # El estado is_connected() puede no ser fiable después de errores
            # Usar un try-except para la desconexión
            try:
                # No podemos verificar client.is_connected() fiable aquí si hubo error
                # await client.disconnect() # Podría fallar si ya está desconectado
                # Simplemente logueamos y esperamos antes de reintentar
                logger.info("Attempting MQTT disconnect (if connected)...")
                # Forzar la desconexión si es posible puede ser necesario en algunos casos
                # if client._transport: client._transport.close()
            except Exception:
                # Ignorar errores durante la desconexión forzada o normal en el finally
                logger.debug("Ignoring error during final MQTT disconnect attempt.")

            logger.warning("Waiting %d seconds before MQTT reconnect...", RECONNECT_DELAY_S)
            await asyncio.sleep(RECONNECT_DELAY_S)


async def _mqtt_publisher_loop(client: aio_mqtt.Client, state: State) -> None:
    """Wait for messages on the publish queue and send them to MQTT."""
    while True:
        message_to_publish: Optional[aio_mqtt.PublishableMessage] = None # Para logging en except
        try:
            # Esperar por un mensaje para publicar
            message_to_publish = await state.mqtt_publish_queue.get()

            # Loggear el mensaje que se va a publicar
            topic = message_to_publish.topic_name
            payload_bytes = message_to_publish.payload if isinstance(message_to_publish.payload, bytes) else str(message_to_publish.payload).encode('utf-8')
            payload_str = payload_bytes.decode("utf-8", errors="ignore")
            logger.info("MQTT > %s %s", topic, payload_str)

            # Publicar el objeto PublishableMessage directamente
            await client.publish(message_to_publish)

            # Marcar la tarea como completada en la cola
            state.mqtt_publish_queue.task_done()
        except asyncio.CancelledError:
             logger.info("MQTT publisher loop cancelled.")
             break # Salir del bucle
        except (ConnectionLostError, ConnectionCloseForcedError): # Usar excepciones correctas
            logger.warning("MQTT not connected, cannot publish message for %s.", getattr(message_to_publish, 'topic_name', 'unknown'))
            # Volver a poner el mensaje en la cola podría ser peligroso si la conexión no vuelve
            # Mejor descartarlo y marcar como hecho para evitar bloqueo
            state.mqtt_publish_queue.task_done() # Marcar como hecho
            await asyncio.sleep(1) # Esperar un poco antes de reintentar get()
        except Exception:
            topic_err = getattr(message_to_publish, 'topic_name', 'unknown') # Safely get topic
            logger.exception("Failed to publish MQTT message for topic %s", topic_err)
            state.mqtt_publish_queue.task_done() # Asegurarse de marcar como hecho incluso en error


async def _mqtt_subscriber_loop(client: aio_mqtt.Client, state: State) -> None:
    """Wait for messages from MQTT and process them."""
    try:
        # Iterar sobre los mensajes recibidos
        async for message in client.delivered_messages():
            topic: Optional[str] = None # Para logging en except
            try:
                if not message.topic_name:
                    continue

                topic = message.topic_name
                payload: bytes = message.payload or b'' # Asegurarse que payload sea bytes
                payload_str = payload.decode("utf-8", errors="ignore")
                logger.info("MQTT < %s %s", topic, payload_str)

                parts: List[str] = topic.split("/")

                # Asegurarse que el tópico base sea correcto
                if not parts or parts[0] != TOPIC_BRIDGE:
                    logger.debug("Ignoring MQTT message with invalid base topic: %s", topic)
                    continue

                # --- Manejo de Tópicos Específicos ---

                if len(parts) >= 3:
                    topic_type: str = parts[1]
                    identifier: str = parts[2] # Pin, key, action, etc.

                    # --- File Operations ---
                    # Formato: br/file/write|read|remove/<filename>
                    if topic_type == TOPIC_FILE and len(parts) >= 4:
                        action: str = identifier
                        # Reconstruir filename si contiene '/'
                        filename: str = "/".join(parts[3:])

                        # CORRECCIÓN BUCLE MQTT: IGNORAR RESPUESTAS DE LECTURA RECIBIDAS
                        if action == "read" and identifier == "response": # Chequeo más simple
                             logger.debug("Ignoring received file read response: %s", topic)
                             continue # Ignorar este mensaje

                        if action == "write":
                            await _perform_file_operation(state, "write", filename, payload)
                        elif action == "read":
                            content: Optional[bytes] = await _perform_file_operation(state, "read", filename)
                            response_topic: str = f"{TOPIC_BRIDGE}/{TOPIC_FILE}/read/response/{filename}"
                            resp_msg = aio_mqtt.PublishableMessage(topic_name=response_topic, payload=content or b"")
                            await state.mqtt_publish_queue.put(resp_msg)
                        elif action == "remove":
                            await _perform_file_operation(state, "remove", filename)

                    # --- Console Input ---
                    # Formato: br/console/in
                    elif topic_type == TOPIC_CONSOLE and identifier == "in":
                        if state.mcu_is_paused:
                            logger.warning("MCU is paused, queueing console message.")
                            state.console_to_mcu_queue.append(payload)
                        else:
                            await send_frame(state, Command.CMD_CONSOLE_WRITE.value, payload)

                    # --- DataStore Operations ---
                    # Formato: br/datastore/put/<key> | br/datastore/get/<key>
                    elif topic_type == TOPIC_DATASTORE and len(parts) >= 3:
                        action: str = identifier
                        key: str = "/".join(parts[3:]) # Reconstruir key
                        if not key: continue # Ignorar si no hay key

                        if action == "put":
                            datastore_value: str = payload_str
                            # Enviar a MCU
                            rpc_payload: bytes = f"{key}\0{datastore_value}".encode("utf-8")
                            await send_frame(state, Command.CMD_DATASTORE_PUT.value, rpc_payload)
                            # Actualizar estado local y publicar para sincronización
                            state.datastore[key] = datastore_value
                            resp_msg = aio_mqtt.PublishableMessage(
                                topic_name=f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/get/{key}",
                                payload=datastore_value.encode("utf-8")
                            )
                            await state.mqtt_publish_queue.put(resp_msg)
                        elif action == "get":
                            # Enviar solicitud a MCU
                            await send_frame(state, Command.CMD_DATASTORE_GET.value, key.encode("utf-8"))
                            # Publicar valor local si existe (respuesta rápida)
                            if key in state.datastore:
                                resp_msg = aio_mqtt.PublishableMessage(
                                    topic_name=f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/get/{key}",
                                    payload=state.datastore[key].encode("utf-8")
                                )
                                await state.mqtt_publish_queue.put(resp_msg)


                    # --- Mailbox Write ---
                    # Formato: br/mailbox/write
                    elif topic_type == TOPIC_MAILBOX and identifier == "write":
                        state.mailbox_queue.append(payload)
                        logger.info("Added message to mailbox queue. Size: %d", len(state.mailbox_queue))
                        # Publicar nuevo tamaño
                        count_msg = aio_mqtt.PublishableMessage(
                            topic_name=f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/available",
                            payload=str(len(state.mailbox_queue)).encode("utf-8")
                        )
                        await state.mqtt_publish_queue.put(count_msg)

                    # --- Shell Command Execution ---
                    # Formato: br/sh/run
                    elif topic_type == TOPIC_SH and identifier == "run":
                        cmd_str: str = payload_str
                        if not cmd_str: continue # Ignorar comando vacío
                        logger.info("Executing shell command from MQTT: '%s'", cmd_str)
                        response: str = ""
                        proc: Optional[asyncio.subprocess.Process] = None
                        try:
                            # Comprobar si el comando está permitido
                            # Usar shlex.split para manejar comillas si estuviera disponible
                            cmd_parts = cmd_str.split()
                            cmd_base = cmd_parts[0] if cmd_parts else ""
                            if state.allowed_commands and cmd_base not in state.allowed_commands:
                                raise PermissionError(f"Command '{cmd_base}' not allowed")

                            proc = await asyncio.create_subprocess_shell(
                                cmd_str,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            comm_result: tuple[bytes | None, bytes | None]
                            comm_result = await asyncio.wait_for(
                                proc.communicate(), timeout=state.process_timeout
                            )
                            stdout, stderr = comm_result
                            response = f"""Exit Code: {proc.returncode}
-- STDOUT --
{(stdout or b"").decode(errors='ignore')}
-- STDERR --
{(stderr or b"").decode(errors='ignore')}"""
                        except asyncio.TimeoutError:
                            response = f"Error: Command timed out after {state.process_timeout} seconds."
                            if proc and proc.returncode is None: # Solo matar si no ha terminado
                                 try: proc.kill()
                                 except ProcessLookupError: pass
                        except PermissionError as e:
                             response = f"Error: {e}"
                        except OSError as e:
                            response = f"Error: Failed to execute command: {e}"
                        except Exception:
                             logger.exception("Unexpected error executing shell command")
                             response = "Error: Unexpected server error"

                        resp_msg = aio_mqtt.PublishableMessage(
                            topic_name=f"{TOPIC_BRIDGE}/{TOPIC_SH}/response",
                            payload=response.encode("utf-8")
                        )
                        await state.mqtt_publish_queue.put(resp_msg)

                    # --- Pin Control ---
                    # Formato: br/d|a/<pin>/mode | br/d|a/<pin>/read | br/d|a/<pin>
                    elif topic_type in (TOPIC_DIGITAL, TOPIC_ANALOG):
                        try:
                            pin_str: str = identifier
                            pin: int = -1
                            # Manejar pines analógicos A0, A1, etc.
                            # La librería C++ espera números. Mapear aquí si es necesario.
                            if pin_str.upper().startswith('A') and pin_str[1:].isdigit():
                                # Ejemplo: Mapear A0-A5 a pines digitales 14-19 si aplica a la placa
                                # analog_pin_map = {'A0': 14, 'A1': 15, ...}
                                # pin = analog_pin_map.get(pin_str.upper(), -1)
                                pin = int(pin_str[1:]) # Asumir número directo por ahora
                            elif pin_str.isdigit():
                                pin = int(pin_str)

                            if pin < 0: raise ValueError(f"Invalid pin identifier: {pin_str}")

                            # Subtópico (mode, read, o valor directo)
                            if len(parts) == 4:
                                subtopic: str = parts[3]
                                if subtopic == "mode" and topic_type == TOPIC_DIGITAL:
                                    mode: int = int(payload_str)
                                    if mode not in [0, 1, 2]: raise ValueError(f"Invalid mode: {mode}")
                                    # <BB = pin (byte), mode (byte), Little Endian
                                    await send_frame(state, Command.CMD_SET_PIN_MODE.value, struct.pack("<BB", pin, mode))
                                elif subtopic == "read":
                                    command: Command = (
                                        Command.CMD_DIGITAL_READ if topic_type == TOPIC_DIGITAL
                                        else Command.CMD_ANALOG_READ
                                    )
                                    # <B = pin (byte), Little Endian
                                    await send_frame(state, command.value, struct.pack("<B", pin))
                                else:
                                     logger.warning("Ignoring MQTT message with unknown pin subtopic: %s", topic)

                            # Escritura directa de valor (digital o analógico)
                            elif len(parts) == 3:
                                value_str = payload_str
                                pin_value: int = int(value_str) if value_str else 0 # Default a 0 si payload vacío?

                                command: Command
                                if topic_type == TOPIC_DIGITAL:
                                    command = Command.CMD_DIGITAL_WRITE
                                    if pin_value not in [0, 1]: raise ValueError(f"Invalid digital value: {pin_value}")
                                    # <BB = pin (byte), value (byte), Little Endian
                                    await send_frame(state, command.value, struct.pack("<BB", pin, pin_value))
                                else: # Analog
                                    command = Command.CMD_ANALOG_WRITE
                                    if not (0 <= pin_value <= 255): raise ValueError(f"Invalid analog value: {pin_value}")
                                    # <BB = pin (byte), value (byte), Little Endian
                                    await send_frame(state, command.value, struct.pack("<BB", pin, pin_value))
                        except (ValueError, IndexError):
                             logger.warning("Invalid pin control message format: Topic=%s Payload=%s", topic, payload_str, exc_info=True)

                # --- Fin del manejo ---
            except Exception: # Capturar error procesando un mensaje específico
                 logger.exception("Error processing MQTT message: %s", topic)

    except asyncio.CancelledError:
        logger.info("MQTT subscriber loop cancelled.")
        # No re-lanzar aquí para permitir limpieza
    except (ConnectionLostError, ConnectionCloseForcedError):
        logger.warning("MQTT connection lost/closed during subscription loop.")
        # La tarea principal (mqtt_task) gestionará la reconexión
    except Exception: # Otros errores inesperados en el bucle principal
        logger.critical("Unhandled exception in subscriber loop.", exc_info=True)
        # Re-lanzar para que mqtt_task lo capture y gestione la reconexión
        raise
    finally:
        logger.debug("MQTT subscriber loop finished.")


# --- Status Writer Task ---

async def status_writer_task(state: State, interval: int = STATUS_INTERVAL_S) -> None:
    """Periodically writes the daemon's status to a temporary file."""
    while True:
        try:
            status_data: Dict[str, Any] = {
                "serial_connected": state.serial_writer is not None and not state.serial_writer.is_closing(),
                "mqtt_queue_size": state.mqtt_publish_queue.qsize(),
                "datastore_keys": list(state.datastore.keys()),
                "mailbox_size": len(state.mailbox_queue),
                "mcu_paused": state.mcu_is_paused,
                "console_queue_size": len(state.console_to_mcu_queue),
                "running_processes": list(state.running_processes.keys()),
            }
            # Usar to_thread para la escritura síncrona
            await asyncio.to_thread(
                _write_status_file_sync, STATUS_FILE_PATH, status_data
            )
        except asyncio.CancelledError:
             logger.info("Status writer task cancelled.")
             break
        except Exception:
            logger.exception("Failed to write status file.")
        # Asegurarse de esperar incluso si hubo error de escritura
        await asyncio.sleep(interval)


def _write_status_file_sync(path: str, data: Dict[str, Any]) -> None:
    """Synchronously writes status data to a file in JSON format."""
    try:
        # Escribir en un archivo temporal y luego renombrar para atomicidad
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, path) # Renombrado atómico
    except (OSError, TypeError):
         logger.exception("Error writing status file sync.")
    except Exception: # Captura genérica por si acaso
         logger.exception("Unexpected error writing status file sync.")


# --- Main ---

async def main_async(config: Dict[str, str]) -> None:
    """Run the main asynchronous application."""

    state: State = State()
    # Procesar comandos permitidos
    allowed_cmds_str = config.get("allowed_commands", "")
    state.allowed_commands = [cmd for cmd in allowed_cmds_str.split() if cmd] # Filtrar vacíos
    state.file_system_root = config.get("file_system_root", "/root/yun_files")
    try:
        state.process_timeout = int(config.get("process_timeout", "10"))
    except ValueError:
        logger.warning("Invalid process_timeout value, using default 10s.")
        state.process_timeout = 10

    # Configuración Serial y MQTT
    serial_port: str = config.get("serial_port", "/dev/ttyATH0")
    try:
        serial_baud: int = int(config.get("serial_baud", "115200"))
    except ValueError:
         logger.warning("Invalid serial_baud value, using default 115200.")
         serial_baud = 115200
    mqtt_host: str = config.get("mqtt_host", "127.0.0.1")
    try:
        mqtt_port: int = int(config.get("mqtt_port", "1883"))
    except ValueError:
        logger.warning("Invalid mqtt_port value, using default 1883.")
        mqtt_port = 1883
    mqtt_user: Optional[str] = config.get("mqtt_user") or None # Asegurar None si está vacío
    mqtt_pass: Optional[str] = config.get("mqtt_pass") or None # Asegurar None si está vacío

    # Configuración TLS
    tls_context: Optional[ssl.SSLContext] = None
    if config.get("mqtt_tls", "0") == "1":
        logger.info("TLS for MQTT is enabled.")
        ca_file: Optional[str] = config.get("mqtt_cafile")
        cert_file: Optional[str] = config.get("mqtt_certfile")
        key_file: Optional[str] = config.get("mqtt_keyfile")

        if ca_file and os.path.exists(ca_file):
            try:
                tls_context = ssl.create_default_context(
                    ssl.Purpose.SERVER_AUTH, cafile=ca_file
                )
                if cert_file and key_file and os.path.exists(cert_file) and os.path.exists(key_file):
                    tls_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
                    logger.info("Using TLS with CA, client cert, and key.")
                else:
                     logger.info("Using TLS with CA: '%s'. Client cert/key not provided or not found.", ca_file)
            except (ssl.SSLError, FileNotFoundError) as e:
                logger.error(
                    "Failed to create TLS context: %s. Check certificate paths/permissions. Proceeding without TLS.", e
                )
                tls_context = None # Fallback a no TLS si hay error
        else:
            logger.warning(
                "TLS is enabled, but CA file '%s' not specified or not found. Proceeding without TLS.", ca_file
            )

    logger.info("Starting async yun-bridge daemon. Serial: %s@%d. MQTT: %s:%d",
                serial_port, serial_baud, mqtt_host, mqtt_port)

    # Crear y lanzar las tareas principales
    tasks : List[asyncio.Task[None]] = []
    try:
        s_task: asyncio.Task[None] = asyncio.create_task(
            serial_reader_task(serial_port, serial_baud, state)
        )
        tasks.append(s_task)

        status_task: asyncio.Task[None] = asyncio.create_task(status_writer_task(state))
        tasks.append(status_task)

        m_task: asyncio.Task[None] = asyncio.create_task(
            mqtt_task(
                mqtt_host,
                mqtt_port,
                state,
                tls_context=tls_context,
                username=mqtt_user,
                password=mqtt_pass,
            )
        )
        tasks.append(m_task)

        # Esperar a que todas las tareas principales terminen (normalmente no debería pasar)
        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        logger.info("Main task cancelled, shutting down.")
    finally:
        logger.info("Shutting down yun-bridge daemon...")
        # Cancelar tareas restantes
        running_tasks = [t for t in tasks if t and not t.done()]
        for task in running_tasks:
            task.cancel()
        # Esperar a que las tareas se cancelen limpiamente
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        logger.info("Yun-bridge daemon stopped.")
        # Limpiar archivo de estado al salir limpiamente?
        try:
            if os.path.exists(STATUS_FILE_PATH):
                os.remove(STATUS_FILE_PATH)
        except OSError:
            pass # Ignorar errores al borrar el archivo de estado


if __name__ == "__main__":
    # Configurar logging básico inicial
    log_level_initial = logging.INFO
    if os.environ.get("YUNBRIDGE_DEBUG") == "1":
        log_level_initial = logging.DEBUG
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    # Añadir filename y lineno al formato si está en modo DEBUG
    if log_level_initial == logging.DEBUG:
        log_format = "%(asctime)s - %(name)s - %(levelname)s [%(filename)s:%(lineno)d] - %(message)s"

    logging.basicConfig(level=log_level_initial, format=log_format)

    # Cargar configuración UCI
    config: Dict[str, str] = get_uci_config()

    # Reconfigurar logging según UCI (si 'debug' está definido)
    log_level_uci : int = logging.DEBUG if config.get("debug", "0") == "1" else logging.INFO
    # Obtener el logger raíz y establecer el nivel
    root_logger = logging.getLogger()
    # Solo cambiar formato si el nivel UCI es DEBUG y no lo era antes
    should_change_format = log_level_uci == logging.DEBUG and log_level_initial != logging.DEBUG
    root_logger.setLevel(log_level_uci)

    # Actualizar handlers si ya existen
    for handler in root_logger.handlers:
        handler.setLevel(log_level_uci)
        if should_change_format:
             # CORRECCIÓN: Usar formato correcto basado en nivel
             current_format = log_format
             if log_level_uci != logging.DEBUG:
                  # Quitar filename/lineno si NO es debug
                  current_format = current_format.replace(" [%(filename)s:%(lineno)d]", "")
             formatter = logging.Formatter(current_format)
             handler.setFormatter(formatter)


    logger.info("Daemon starting with log level: %s", logging.getLevelName(log_level_uci))

    try:
        # Iniciar el bucle de eventos asyncio
        asyncio.run(main_async(config))
    except KeyboardInterrupt:
        logger.info("Daemon shut down by user (KeyboardInterrupt).")
    except Exception:
         # Loguear el traceback completo aquí antes de salir
         logger.critical("Fatal unhandled error in main execution", exc_info=True)
         logger.error(traceback.format_exc())
         # Salir con código de error
         # sys.exit(1) # Podría ser útil si se ejecuta desde un script
