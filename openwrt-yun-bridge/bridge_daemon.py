#!/usr/bin/env python3
"""Daemon for the Arduino Yun Bridge v2.

This daemon bridges communication between the Arduino Yun's microcontroller and
the Linux-based OpenWRT system, using MQTT as the primary protocol for
external communication.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import ssl
import struct
from typing import Any

import aiomqtt
import serial
import serial_asyncio
from yunrpc import cobs
from yunrpc.frame import Frame
from yunrpc.protocol import Command, Status
from yunrpc.utils import get_uci_config

# --- Logger ---
logger = logging.getLogger("yunbridge")

# --- Topic Constants ---
TOPIC_BRIDGE: str = "br"
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
        self.serial_writer: asyncio.StreamWriter | None = None
        self.mqtt_publish_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self.datastore: dict[str, str] = {}
        self.mailbox_queue: collections.deque[bytes] = collections.deque()
        self.mcu_is_paused: bool = False
        self.console_to_mcu_queue: collections.deque[bytes] = collections.deque()
        self.running_processes: dict[int, asyncio.subprocess.Process] = {}
        self.process_lock: asyncio.Lock = asyncio.Lock()
        self.next_pid: int = 1
        self.allowed_commands: list[str] = []


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
            log_name = Command(command_id).name
        except ValueError:
            log_name = Status(command_id).name
        logger.debug("LINUX > %s PAYLOAD: %s", log_name, payload.hex())
        return True
    except Exception:
        logger.exception("An unexpected error occurred during send")
        return False


# --- MCU Command Handlers ---


async def _handle_digital_read_resp(state: State, payload: bytes) -> None:
    pin = payload[0]
    value = int.from_bytes(payload[1:], "little")
    topic = f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/{pin}/value"
    await state.mqtt_publish_queue.put((topic, str(value).encode("utf-8")))


async def _handle_analog_read_resp(state: State, payload: bytes) -> None:
    pin = payload[0]
    value = int.from_bytes(payload[1:], "little")
    topic = f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/{pin}/value"
    await state.mqtt_publish_queue.put((topic, str(value).encode("utf-8")))


async def _handle_ack(state: State, _: bytes) -> None:
    logger.info("MCU > ACK received, command confirmed.")


async def _handle_xoff(state: State, payload: bytes) -> None:
    logger.warning("MCU > XOFF received, pausing console output.")
    state.mcu_is_paused = True


async def _handle_xon(state: State, payload: bytes) -> None:
    logger.info("MCU > XON received, resuming console output.")
    state.mcu_is_paused = False
    while state.console_to_mcu_queue and not state.mcu_is_paused:
        data = state.console_to_mcu_queue.popleft()
        await send_frame(state, Command.CMD_CONSOLE_WRITE.value, data)


async def _handle_console_write(state: State, payload: bytes) -> None:
    await state.mqtt_publish_queue.put((f"{TOPIC_BRIDGE}/{TOPIC_CONSOLE}/out", payload))


async def _handle_datastore_get_resp(state: State, payload: bytes) -> None:
    key, value = payload.split(b"\0", 1)
    key_str = key.decode("utf-8")
    value_str = value.decode("utf-8")
    await state.mqtt_publish_queue.put(
        (f"{TOPIC_BRIDGE}/datastore/get/{key_str}", value_str.encode("utf-8"))
    )


async def _handle_mailbox_processed(state: State, payload: bytes) -> None:
    await state.mqtt_publish_queue.put(
        (f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/processed", payload)
    )


async def _handle_process_run(state: State, payload: bytes) -> None:
    cmd_str = payload.decode("utf-8")
    if cmd_str not in state.allowed_commands:
        logger.warning("Command not allowed: %s", cmd_str)
        await send_frame(state, Command.CMD_PROCESS_RUN_RESP.value, b"Error: Command not allowed")
        return
    logger.info("Received sync PROCESS_RUN: '%s'", cmd_str)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        await send_frame(state, Command.CMD_PROCESS_RUN_RESP.value, stdout)
    except asyncio.TimeoutError:
        await send_frame(state, Command.CMD_PROCESS_RUN_RESP.value, b"Error: Timeout")
    except OSError as e:
        await send_frame(state, Command.CMD_PROCESS_RUN_RESP.value, str(e).encode("utf-8"))


async def _handle_process_run_async(state: State, payload: bytes) -> None:
    cmd_str = payload.decode("utf-8")
    if cmd_str not in state.allowed_commands:
        logger.warning("Command not allowed: %s", cmd_str)
        await send_frame(state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"-1")
        return
    logger.info("Received async PROCESS_RUN: '%s'", cmd_str)
    async with state.process_lock:
        pid = state.next_pid
        state.next_pid += 1
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        async with state.process_lock:
            state.running_processes[pid] = proc
        await send_frame(
            state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, str(pid).encode("utf-8")
        )
    except OSError:
        await send_frame(state, Command.CMD_PROCESS_RUN_ASYNC_RESP.value, b"-1")


async def _handle_process_poll(state: State, payload: bytes) -> None:
    pid = int(payload.decode("utf-8"))
    output = b""
    finished = False

    async with state.process_lock:
        if pid not in state.running_processes:
            await send_frame(state, Command.CMD_PROCESS_POLL_RESP.value, b"Error: No such process")
            return

        proc = state.running_processes[pid]

        # 1. Perform a non-blocking read for immediate output
        try:
            stdout = await proc.stdout.read(1024)
            stderr = await proc.stderr.read(1024)
            output += stdout + stderr
        except (OSError, BrokenPipeError):
            # This can happen if the process ends between the check and the read
            pass

        # 2. Check if the process has terminated
        if proc.returncode is not None:
            # 3. Drain any remaining data from the pipes
            try:
                stdout_rem, stderr_rem = await proc.communicate()
                output += stdout_rem + stderr_rem
            except (OSError, BrokenPipeError, ValueError):
                # ValueError can be raised if transport is closed
                pass  # Ignore errors on final communication
            # 4. Remove the process from the dictionary
            del state.running_processes[pid]
            logger.info("Process %d finished with code %d and was cleaned up.", pid, proc.returncode)

    await send_frame(state, Command.CMD_PROCESS_POLL_RESP.value, output)


async def _handle_process_kill(state: State, payload: bytes) -> None:
    pid = int(payload.decode("utf-8"))
    async with state.process_lock:
        if pid in state.running_processes:
            try:
                state.running_processes[pid].kill()
                del state.running_processes[pid]
                logger.info("Killed process with PID %d", pid)
            except ProcessLookupError:
                # Process already finished
                del state.running_processes[pid]


# --- File I/O Helpers (Async Wrappers) ---

def _write_file_sync(path: str, data: bytes) -> None:
    """Synchronous file write."""
    with open(path, "wb") as f:
        f.write(data)

def _read_file_sync(path: str) -> bytes:
    """Synchronous file read."""
    with open(path, "rb") as f:
        return f.read()

def _get_safe_path(state: State, filename: str) -> str | None:
    """Get a safe path, ensuring it's within the configured root directory."""
    base_dir = state.file_system_root
    os.makedirs(base_dir, exist_ok=True)

    safe_path = os.path.abspath(os.path.join(base_dir, filename))
    if os.path.commonpath([safe_path, base_dir]) != base_dir:
        logger.warning("Path traversal attempt blocked: %s", filename)
        return None
    return safe_path

async def _handle_file_write(state: State, payload: bytes) -> None:
    """Handle file write requests from the MCU asynchronously."""
    try:
        filename, data = payload.split(b"\0", 1)
        safe_path = _get_safe_path(state, filename.decode("utf-8"))
        if not safe_path:
            return
        await asyncio.to_thread(_write_file_sync, safe_path, data)
        logger.info("Wrote %d bytes to %s from MCU", len(data), safe_path)
    except (ValueError, OSError) as e:
        logger.exception("File write error from MCU: %s", e)


async def _handle_file_read(state: State, payload: bytes) -> None:
    """Handle file read requests from the MCU asynchronously."""
    content = b""
    try:
        filename = payload.decode("utf-8")
        safe_path = _get_safe_path(state, filename)
        if safe_path and await asyncio.to_thread(os.path.exists, safe_path):
            content = await asyncio.to_thread(_read_file_sync, safe_path)
        else:
            logger.warning("File read from MCU: file not found %s", safe_path)
    except (ValueError, OSError) as e:
        logger.exception("File read error from MCU: %s", e)
    finally:
        await send_frame(state, Command.CMD_FILE_READ_RESP.value, content)


async def _handle_file_remove(state: State, payload: bytes) -> None:
    """Handle file remove requests from the MCU asynchronously."""
    try:
        filename = payload.decode("utf-8")
        safe_path = _get_safe_path(state, filename)
        if safe_path and await asyncio.to_thread(os.path.exists, safe_path):
            await asyncio.to_thread(os.remove, safe_path)
            logger.info("Removed file %s from MCU", safe_path)
        else:
            logger.warning("File remove from MCU: file not found %s", safe_path)
    except (ValueError, OSError) as e:
        logger.exception("File remove error from MCU: %s", e)


MCU_COMMAND_HANDLERS = {
    Command.CMD_DIGITAL_READ_RESP: _handle_digital_read_resp,
    Command.CMD_ANALOG_READ_RESP: _handle_analog_read_resp,
    Status.ACK: _handle_ack,
    Status.XOFF: _handle_xoff,
    Status.XON: _handle_xon,
    Command.CMD_CONSOLE_WRITE: _handle_console_write,
    Command.CMD_DATASTORE_GET_RESP: _handle_datastore_get_resp,
    Command.CMD_MAILBOX_PROCESSED: _handle_mailbox_processed,
    Command.CMD_PROCESS_RUN: _handle_process_run,
    Command.CMD_PROCESS_RUN_ASYNC: _handle_process_run_async,
    Command.CMD_PROCESS_POLL: _handle_process_poll,
    Command.CMD_PROCESS_KILL: _handle_process_kill,
    Command.CMD_FILE_WRITE: _handle_file_write,
    Command.CMD_FILE_READ: _handle_file_read,
    Command.CMD_FILE_REMOVE: _handle_file_remove,
}


async def handle_mcu_frame(state: State, command_id: int, payload: bytes) -> None:
    """Handle a command frame received from the MCU."""
    try:
        command = Command(command_id)
        logger.debug("MCU > CMD: %s PAYLOAD: %s", command.name, payload.hex())
    except ValueError:
        try:
            command = Status(command_id)
            logger.debug("MCU > STATUS: %s PAYLOAD: %s", command.name, payload.hex())
        except ValueError:
            logger.warning(
                "MCU > Unknown CMD/STATUS ID: %s PAYLOAD: %s",
                hex(command_id),
                payload.hex(),
            )
            await send_frame(state, Status.CMD_UNKNOWN.value, b"")
            return

    handler = MCU_COMMAND_HANDLERS.get(command)
    if handler:
        await handler(state, payload)


async def serial_reader_task(serial_port: str, serial_baud: int, state: State) -> None:
    """Connect to, read from, and handle the serial port."""
    while True:
        try:
            logger.info("Attempting to connect to serial port %s...", serial_port)
            reader, writer = await serial_asyncio.open_serial_connection(
                url=serial_port, baudrate=serial_baud
            )
            state.serial_writer = writer
            logger.info("Serial port connected successfully.")
            while True:
                encoded_packet = await reader.readuntil(separator=b"\x00")
                if not encoded_packet:
                    continue
                try:
                    raw_frame = cobs.decode(encoded_packet[:-1])
                    command_id, payload = Frame.parse(raw_frame)
                    await handle_mcu_frame(state, command_id, payload)
                except ValueError as e:
                    logger.warning(
                        "Frame processing error: %s. Packet: %s",
                        e,
                        encoded_packet.hex(),
                    )
        except (OSError, serial.SerialException):
            logger.exception("Serial communication error")
        except Exception:
            logger.critical("Unhandled exception in serial_reader_task", exc_info=True)
        finally:
            state.serial_writer = None
            logger.warning("Serial port disconnected. Retrying in 5 seconds...")
            await asyncio.sleep(5)


async def mqtt_task(
    mqtt_host: str, mqtt_port: int, state: State, tls_params: dict[str, Any] | None = None
) -> None:
    """Handle all MQTT communication (in and out)."""
    while True:
        try:
            async with aiomqtt.Client(
                hostname=mqtt_host, port=mqtt_port, **(tls_params or {})
            ) as client:
                logger.info("Connected to MQTT Broker at %s:%s", mqtt_host, mqtt_port)

                subscriptions = [
                    f"{TOPIC_BRIDGE}/{TOPIC_DIGITAL}/#",
                    f"{TOPIC_BRIDGE}/{TOPIC_ANALOG}/#",
                    f"{TOPIC_BRIDGE}/{TOPIC_CONSOLE}/in",
                    f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/put/#",
                    f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/get/#",
                    f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/write",
                    f"{TOPIC_BRIDGE}/{TOPIC_SH}/run",
                    f"{TOPIC_BRIDGE}/file/#",
                ]
                for topic in subscriptions:
                    await client.subscribe(topic)
                    logger.info("Subscribed to topic: %s", topic)

                publisher_task = asyncio.create_task(_mqtt_publisher_loop(client, state))
                subscriber_task = asyncio.create_task(_mqtt_subscriber_loop(client, state))
                await asyncio.gather(publisher_task, subscriber_task)

        except aiomqtt.MqttError:
            logger.exception("MQTT Error. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception:
            logger.critical("Unhandled exception in mqtt_task", exc_info=True)
            await asyncio.sleep(5)


async def _mqtt_publisher_loop(client: aiomqtt.Client, state: State) -> None:
    """Wait for messages on the publish queue and send them to MQTT."""
    while True:
        topic, payload = await state.mqtt_publish_queue.get()
        logger.info("MQTT > %s %s", topic, payload.decode("utf-8", errors="ignore"))
        await client.publish(topic, payload)
        state.mqtt_publish_queue.task_done()


async def _mqtt_subscriber_loop(client: aiomqtt.Client, state: State) -> None:
    """Wait for messages from MQTT and process them."""
    async for message in client.messages:
        topic: str = message.topic.value
        payload: bytes = message.payload
        logger.info("MQTT < %s %s", topic, payload.decode("utf-8", errors="ignore"))

        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != TOPIC_BRIDGE:
            continue

        topic_type = parts[1]

        if topic_type == TOPIC_FILE and len(parts) >= 4:
            action = parts[2]
            filename = "/".join(parts[3:])
            safe_path = _get_safe_path(state, filename)

            if not safe_path:
                return

            if action == "write":
                try:
                    await asyncio.to_thread(_write_file_sync, safe_path, payload)
                    logger.info("Wrote %d bytes to %s via MQTT", len(payload), safe_path)
                except OSError as e:
                    logger.exception("MQTT file write error: %s", e)

            elif action == "read":
                content = b""
                try:
                    if await asyncio.to_thread(os.path.exists, safe_path):
                        content = await asyncio.to_thread(_read_file_sync, safe_path)
                    else:
                        logger.warning("MQTT file read: file not found %s", safe_path)
                except OSError as e:
                    logger.exception("MQTT file read error: %s", e)
                finally:
                    response_topic = f"{TOPIC_BRIDGE}/{TOPIC_FILE}/read/response/{filename}"
                    await state.mqtt_publish_queue.put((response_topic, content))

            elif action == "remove":
                try:
                    if await asyncio.to_thread(os.path.exists, safe_path):
                        await asyncio.to_thread(os.remove, safe_path)
                        logger.info("Removed file %s via MQTT", safe_path)
                    else:
                        logger.warning("MQTT file remove: file not found %s", safe_path)
                except OSError as e:
                    logger.exception("MQTT file remove error: %s", e)

        elif topic_type == TOPIC_CONSOLE and parts[2] == "in":
            if state.mcu_is_paused:
                logger.warning("MCU is paused, queueing console message.")
                state.console_to_mcu_queue.append(payload)
            else:
                await send_frame(state, Command.CMD_CONSOLE_WRITE.value, payload)

        elif topic_type == TOPIC_DATASTORE and parts[2] == "put":
            if len(parts) > 3:
                key = "/".join(parts[3:])
                value = payload.decode("utf-8", errors="ignore")
                rpc_payload = f"{key}\0{value}".encode()
                await send_frame(state, Command.CMD_DATASTORE_PUT.value, rpc_payload)
                state.datastore[key] = value
                await state.mqtt_publish_queue.put(
                    (
                        f"{TOPIC_BRIDGE}/{TOPIC_DATASTORE}/get/{key}",
                        value.encode("utf-8"),
                    )
                )

        elif topic_type == TOPIC_DATASTORE and parts[2] == "get":
            if len(parts) > 3:
                key = "/".join(parts[3:])
                await send_frame(state, Command.CMD_DATASTORE_GET.value, key.encode("utf-8"))

        elif topic_type == TOPIC_MAILBOX and parts[2] == "write":
            state.mailbox_queue.append(payload)
            logger.info(
                "Added message to mailbox queue. Size: %d", len(state.mailbox_queue)
            )
            await state.mqtt_publish_queue.put(
                (
                    f"{TOPIC_BRIDGE}/{TOPIC_MAILBOX}/available",
                    str(len(state.mailbox_queue)).encode("utf-8"),
                )
            )

        elif topic_type == TOPIC_SH and parts[2] == "run":
            cmd_str = payload.decode("utf-8", errors="ignore")
            logger.info("Executing shell command from MQTT: '%s'", cmd_str)
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
                response = f"""Exit Code: {proc.returncode}
-- STDOUT --
{stdout.decode()}
-- STDERR --
{stderr.decode()}"""
            except asyncio.TimeoutError:
                response = "Error: Command timed out after 15 seconds."
            except OSError as e:
                response = f"Error: Failed to execute command: {e}"
            await state.mqtt_publish_queue.put(
                (f"{TOPIC_BRIDGE}/{TOPIC_SH}/response", response.encode("utf-8"))
            )

        elif topic_type == TOPIC_DIGITAL and len(parts) == 4 and parts[3] == "mode":
            pin = int(parts[2])
            mode = int(payload.decode("utf-8", errors="ignore"))
            await send_frame(state, Command.CMD_SET_PIN_MODE.value, struct.pack("<BB", pin, mode))

        elif (
            topic_type in (TOPIC_DIGITAL, TOPIC_ANALOG)
            and len(parts) == 4
            and parts[3] == "read"
        ):
            pin = int(parts[2])
            command = (
                Command.CMD_DIGITAL_READ
                if topic_type == TOPIC_DIGITAL
                else Command.CMD_ANALOG_READ
            )
            await send_frame(state, command.value, struct.pack("<B", pin))

        elif topic_type in (TOPIC_DIGITAL, TOPIC_ANALOG) and len(parts) == 3:
            pin = int(parts[2])
            value = int(payload.decode("utf-8", errors="ignore"))
            command = (
                Command.CMD_DIGITAL_WRITE
                if topic_type == TOPIC_DIGITAL
                else Command.CMD_ANALOG_WRITE
            )
            await send_frame(state, command.value, struct.pack("<BB", pin, value))


async def main_async() -> None:
    """Run the main asynchronous application."""
    config = get_uci_config()
    serial_port = config.get("serial_port", "/dev/ttyATH0")
    serial_baud = int(config.get("serial_baud", 115200))
    mqtt_host = config.get("mqtt_host", "127.0.0.1")
    mqtt_port = int(config.get("mqtt_port", 1883))
    log_level = logging.DEBUG if config.get("debug", "0") == "1" else logging.INFO
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    state = State()
    state.allowed_commands = config.get("allowed_commands", "").split()
    state.file_system_root = config.get("file_system_root", "/root/yun_files")

    tls_params: dict[str, Any] | None = None
    if config.get("mqtt_tls", "0") == "1":
        logger.info("TLS for MQTT is enabled.")
        ca_file = config.get("mqtt_cafile")
        cert_file = config.get("mqtt_certfile")
        key_file = config.get("mqtt_keyfile")

        if ca_file and cert_file and key_file:
            tls_context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=ca_file
            )
            tls_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            tls_params = {"tls_context": tls_context, "tls_insecure": False}
            logger.info(
                "Using TLS with: CA='%s', Cert='%s', Key='%s'",
                ca_file,
                cert_file,
                key_file,
            )
        else:
            logger.warning(
                "TLS is enabled, but some certificate files are missing. Proceeding without TLS."
            )

    logger.info("Starting async yun-bridge daemon.")

    try:
        s_task = asyncio.create_task(serial_reader_task(serial_port, serial_baud, state))
        m_task = asyncio.create_task(
            mqtt_task(mqtt_host, mqtt_port, state, tls_params=tls_params)
        )
        await asyncio.gather(s_task, m_task)
    finally:
        logger.info("Shutting down yun-bridge.")


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Daemon shut down by user.")
