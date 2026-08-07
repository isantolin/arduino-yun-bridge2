"""SIL-2 compliant service orchestrator for McuBridge."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import structlog
from grpclib.client import Channel

from mcubridge.config.settings import RuntimeConfig, configure_logging, load_runtime_config
from mcubridge.metrics import metrics
from mcubridge.protocol import protocol_pb2 as pb
from mcubridge.protocol.frame import (
    PROTOCOL_VERSION,
    DecodeError,
    FrameError,
    IntegrityError,
    build_frame,
    parse_frame,
)
from mcubridge.protocol.mcubridge_grpc import CloudGatewayStub, LocalBridgeStub
from mcubridge.protocol.structures import PendingPinRequest
from mcubridge.protocol.topic import Topic, topic_path
from mcubridge.state.context import RuntimeState
from mcubridge.state.policy import AllowedCommandPolicy
from mcubridge.state.storage import SqliteDeque
from mcubridge.transport.serial import SerialTransport

logger = structlog.get_logger(__name__)


def _sanitize_path(base_dir: str, rel_path: str) -> Path | None:
    """SIL-2 Path Traversal Defense.

    Validates that rel_path resolves strictly within base_dir.
    Returns the resolved Path if safe, or None if invalid/traversal detected.
    """
    try:
        base = Path(base_dir).resolve()
        target = (base / rel_path).resolve()
        if base in target.parents or target == base:
            return target
        logger.warning("Path traversal attempt blocked", base=str(base), rel_path=rel_path)
        return None
    except (ValueError, OSError) as exc:
        logger.warning("Invalid path sanitization candidate", rel_path=rel_path, error=str(exc))
        return None


class LocalBridgeService(LocalBridgeStub):
    """UNIX domain socket gRPC service handling local MPU/CGI requests."""

    def __init__(self, bridge_service: BridgeService) -> None:
        self.bridge_service = bridge_service

    async def Publish(self, stream: Any) -> None:
        request = await stream.recv_message()
        if request is None:
            return
        await self.bridge_service.handle_request(request)
        await stream.send_message(pb.LocalPublishResponse(success=True))

    async def Subscribe(self, stream: Any) -> None:
        request = await stream.recv_message()
        if request is None:
            return

        queue: asyncio.Queue[pb.LocalEvent] = asyncio.Queue(maxsize=100)
        self.bridge_service.state.subscribe(queue, topic_filter=request.topic)
        try:
            while True:
                event = await queue.get()
                await stream.send_message(event)
        finally:
            self.bridge_service.state.unsubscribe(queue)

    async def GetState(self, stream: Any) -> None:
        await stream.recv_message()
        state = self.bridge_service.state
        response = pb.LocalStateResponse(
            state=state.state,
            connected=state.connected,
            mcu_id=state.mcu_id,
            mcu_version=state.mcu_version,
            pending_pin_requests=len(state.pending_digital_reads) + len(state.pending_analog_reads),
            pending_process_requests=len(state.running_processes),
        )
        await stream.send_message(response)


class BridgeService:
    """Main orchestrator coordinating Serial transport, State, and gRPC Cloud Gateway."""

    def __init__(
        self,
        config: RuntimeConfig,
        state: RuntimeState,
        serial_transport: SerialTransport | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.serial = serial_transport or SerialTransport(config.serial_port, config.baudrate)
        self.policy = AllowedCommandPolicy(
            allow_all=config.allow_all_commands,
            allowed_commands=config.allowed_commands,
        )
        self._cloud_stream: Any = None
        self._cloud_channel: Channel | None = None
        self._cloud_spool: SqliteDeque | None = None

    async def run(self) -> None:
        """Run the main service loops supervised under a task group."""
        logger.info(
            "Starting McuBridge service",
            port=self.config.serial_port,
            baudrate=self.config.baudrate,
        )

        spool_dir = Path(self.config.cloud_spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        self._cloud_spool = SqliteDeque(
            path=str(spool_dir / "spool.db"),
            maxlen=self.config.cloud_spool_max_messages,
        )
        self.state.cloud_spool_pending_messages = await self._cloud_spool.length()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._serial_loop())
            tg.create_task(self._cloud_loop())
            tg.create_task(self._ipc_server_loop())

    async def _serial_loop(self) -> None:
        """Supervised serial read loop processing incoming MCU frames."""
        try:
            await self.serial.open()
            logger.info("Serial transport connected", port=self.config.serial_port)
            self.state.connected = True
            metrics.set_serial_connected(True)

            while True:
                try:
                    frame_bytes = await self.serial.read_frame()
                    if frame_bytes:
                        await self._handle_mcu_frame(frame_bytes)
                except FrameError as exc:
                    logger.warning("Frame parsing error", error=str(exc))
                    metrics.inc_serial_errors()
                except (OSError, RuntimeError) as exc:
                    logger.error("Serial transport error", error=str(exc))
                    metrics.inc_serial_errors()
                    self.state.connected = False
                    metrics.set_serial_connected(False)
                    await asyncio.sleep(1.0)
                    await self.serial.open()
                    self.state.connected = True
                    metrics.set_serial_connected(True)
        finally:
            self.state.connected = False
            metrics.set_serial_connected(False)
            await self.serial.close()

    async def _handle_mcu_frame(self, frame_bytes: bytes) -> None:
        """Process a validated binary frame from the MCU."""
        session_key = self.state.session_key if self.state.is_handshake_complete else None
        parsed = parse_frame(frame_bytes, session_key=session_key)

        metrics.inc_mcu_frame(parsed.command_id)
        logger.debug(
            "Received MCU frame",
            command_id=parsed.command_id,
            seq=parsed.sequence_id,
            len=len(parsed.payload),
        )

        envelope = pb.RpcEnvelope()
        try:
            envelope.ParseFromString(parsed.payload)
        except Exception as exc:
            raise DecodeError(f"Invalid protobuf payload in frame: {exc}") from exc

        if envelope.HasField("handshake_init"):
            await self._handle_handshake_init(envelope.handshake_init, parsed.sequence_id)
            return

        if not self.state.is_handshake_complete:
            logger.warning("Dropping frame received prior to handshake completion", command=parsed.command_id)
            return

        if envelope.HasField("pin_response"):
            await self._handle_pin_response(envelope.pin_response)
        elif envelope.HasField("mailbox_push"):
            await self._handle_mailbox_push(envelope.mailbox_push)
        elif envelope.HasField("mailbox_read"):
            await self._handle_mailbox_read(envelope.mailbox_read)
        elif envelope.HasField("mailbox_available"):
            await self._handle_mailbox_available(envelope.mailbox_available)
        elif envelope.HasField("datastore_put"):
            await self._handle_datastore_put(envelope.datastore_put)
        elif envelope.HasField("datastore_get"):
            await self._handle_datastore_get(envelope.datastore_get)
        elif envelope.HasField("console_write"):
            await self._handle_console_write(envelope.console_write)
        elif envelope.HasField("process_run"):
            await self._handle_process_run(envelope.process_run)
        elif envelope.HasField("process_write"):
            await self._handle_process_write(envelope.process_write)
        elif envelope.HasField("process_close"):
            await self._handle_process_close(envelope.process_close)
        elif envelope.HasField("file_write"):
            await self._handle_file_write(envelope.file_write)
        elif envelope.HasField("file_read"):
            await self._handle_file_read(envelope.file_read)
        elif envelope.HasField("file_remove"):
            await self._handle_file_remove(envelope.file_remove)
        elif envelope.HasField("spi_transfer"):
            await self._handle_spi_transfer(envelope.spi_transfer)
        elif envelope.HasField("xon"):
            self.state.tx_paused = False
            logger.debug("MCU XON received, resuming TX")
        elif envelope.HasField("xoff"):
            self.state.tx_paused = True
            logger.debug("MCU XOFF received, pausing TX")
        else:
            logger.warning("Unhandled envelope payload type", envelope=str(envelope))

    async def _handle_handshake_init(self, init: pb.HandshakeInit, sequence_id: int) -> None:
        """Process MCU HandshakeInit and respond with HandshakeAck."""
        logger.info(
            "MCU HandshakeInit received",
            mcu_id=init.mcu_id,
            mcu_version=init.mcu_version,
            client_nonce=init.client_nonce.hex(),
        )
        self.state.mcu_id = init.mcu_id
        self.state.mcu_version = init.mcu_version

        ack = pb.HandshakeAck(
            server_nonce=os.urandom(16),
            status=pb.HANDSHAKE_STATUS_SUCCESS,
        )
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, handshake_ack=ack)
        frame = build_frame(
            command_id=pb.CMD_HANDSHAKE_ACK,
            sequence_id=sequence_id,
            payload=envelope.SerializeToString(),
        )
        await self.serial.send(frame)
        self.state.state = "synchronized"
        self.state.link_sync_event.set()
        logger.info("Handshake complete, state SYNCHRONIZED", mcu_id=init.mcu_id)

    async def _handle_pin_response(self, resp: pb.PinResponse) -> None:
        """Fulfill a pending digital or analog read request."""
        topic_str = topic_path(self.config.topic_prefix, Topic.PIN_READ, str(resp.pin))
        event = pb.LocalEvent(
            topic=topic_str,
            payload=str(resp.value).encode(),
            timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
        )

        if resp.pin in self.state.pin_modes and self.state.pin_modes[resp.pin] == pb.PIN_MODE_ANALOG:
            if self.state.pending_analog_reads:
                req = self.state.pending_analog_reads.popleft()
                if req.reply_context:
                    await self.state.publish_to_subscriber(req.reply_context, event)
        else:
            if self.state.pending_digital_reads:
                req = self.state.pending_digital_reads.popleft()
                if req.reply_context:
                    await self.state.publish_to_subscriber(req.reply_context, event)

        await self.state.notify_subscribers(event)
        cloud_msg = pb.CloudQueuedPublish(topic=topic_str, payload=str(resp.value).encode())
        await self.enqueue_cloud(cloud_msg)

    async def _handle_mailbox_push(self, push: pb.MailboxPush) -> None:
        """Handle a message pushed from MCU to Linux mailbox."""
        await self.state.mailbox_queue.append(push.msg)
        topic_str = topic_path(self.config.topic_prefix, Topic.MAILBOX_PUSH, "")
        event = pb.LocalEvent(
            topic=topic_str,
            payload=push.msg,
            timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
        )
        await self.state.notify_subscribers(event)
        cloud_msg = pb.CloudQueuedPublish(topic=topic_str, payload=push.msg)
        await self.enqueue_cloud(cloud_msg)

    async def _handle_mailbox_read(self, read_req: pb.MailboxRead) -> None:
        """Respond to MCU mailbox read request with next available message or empty."""
        del read_req
        try:
            item = await self.state.mailbox_incoming_queue.popleft()
            resp = pb.MailboxReadResponse(msg=item, available=True)
        except IndexError:
            resp = pb.MailboxReadResponse(msg=b"", available=False)

        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, mailbox_read_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_MAILBOX_READ_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_mailbox_available(self, avail: pb.MailboxAvailable) -> None:
        """Respond to MCU inquiry regarding incoming mailbox count."""
        del avail
        count = await self.state.mailbox_incoming_queue.length()
        resp = pb.MailboxAvailableResponse(count=count)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, mailbox_avail_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_MAILBOX_AVAIL_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_datastore_put(self, put: pb.DatastorePut) -> None:
        """Store key-value pair in datastore cache."""
        if self.state.datastore_cache:
            await self.state.datastore_cache.set(put.key, put.value)
        topic_str = topic_path(self.config.topic_prefix, Topic.DATASTORE_PUT, put.key)
        event = pb.LocalEvent(
            topic=topic_str,
            payload=put.value,
            timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
        )
        await self.state.notify_subscribers(event)
        cloud_msg = pb.CloudQueuedPublish(topic=topic_str, payload=put.value)
        await self.enqueue_cloud(cloud_msg)

    async def _handle_datastore_get(self, get_req: pb.DatastoreGet) -> None:
        """Retrieve key-value pair from datastore cache for MCU."""
        val = b""
        found = False
        if self.state.datastore_cache:
            res = await self.state.datastore_cache.get(get_req.key)
            if res is not None:
                val = res
                found = True

        resp = pb.DatastoreGetResponse(key=get_req.key, value=val, found=found)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, datastore_get_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_DATASTORE_GET_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_console_write(self, write: pb.ConsoleWrite) -> None:
        """Handle console output from MCU."""
        sys.stdout.buffer.write(write.data)
        sys.stdout.buffer.flush()
        topic_str = topic_path(self.config.topic_prefix, Topic.CONSOLE_READ, "")
        event = pb.LocalEvent(
            topic=topic_str,
            payload=write.data,
            timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
        )
        await self.state.notify_subscribers(event)

    async def _handle_process_run(self, req: pb.ProcessRun) -> None:
        """Execute a Linux process requested by MCU under SIL-2 policy restrictions."""
        if not self.policy.is_command_allowed(req.cmd):
            logger.warning("Process execution denied by policy", cmd=req.cmd)
            resp = pb.ProcessRunResponse(pid=0, exit_code=126, success=False)
            envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, process_run_resp=resp)
            frame = build_frame(
                command_id=pb.CMD_PROCESS_RUN_RESP,
                sequence_id=0,
                payload=envelope.SerializeToString(),
                session_key=self.state.session_key,
            )
            await self.serial.send(frame)
            return

        try:
            proc = await asyncio.create_subprocess_shell(
                req.cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
            pid = proc.pid

            async def _pipe_output(stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
                if not stream:
                    return
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    topic_kind = Topic.PROCESS_ERR if is_stderr else Topic.PROCESS_OUT
                    topic_str = topic_path(self.config.topic_prefix, topic_kind, str(pid))
                    event = pb.LocalEvent(
                        topic=topic_str,
                        payload=line,
                        timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
                    )
                    await self.state.notify_subscribers(event)

            asyncio.create_task(_pipe_output(proc.stdout, False))
            asyncio.create_task(_pipe_output(proc.stderr, True))

            resp = pb.ProcessRunResponse(pid=pid, exit_code=0, success=True)
        except (OSError, RuntimeError) as exc:
            logger.error("Failed to spawn process", cmd=req.cmd, error=str(exc))
            resp = pb.ProcessRunResponse(pid=0, exit_code=1, success=False)

        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, process_run_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_PROCESS_RUN_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_process_write(self, req: pb.ProcessWrite) -> None:
        """Write stdin to running process."""
        del req
        logger.debug("Process stdin write ignored in standard mode")

    async def _handle_process_close(self, req: pb.ProcessClose) -> None:
        """Close/terminate running process."""
        del req
        logger.debug("Process close ignored in standard mode")

    async def _handle_file_write(self, req: pb.FileWrite) -> None:
        """Write file on Linux filesystem requested by MCU with path traversal protection."""
        target = _sanitize_path(self.config.file_storage_dir, req.path)
        success = False
        if target is not None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(req.data)
                success = True
            except OSError as exc:
                logger.error("File write failed", path=str(target), error=str(exc))

        resp = pb.FileWriteResponse(path=req.path, success=success)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, file_write_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_FILE_WRITE_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_file_read(self, req: pb.FileRead) -> None:
        """Read file from Linux filesystem requested by MCU with path traversal protection."""
        target = _sanitize_path(self.config.file_storage_dir, req.path)
        data = b""
        success = False
        if target is not None and target.is_file():
            try:
                data = target.read_bytes()
                success = True
            except OSError as exc:
                logger.error("File read failed", path=str(target), error=str(exc))

        resp = pb.FileReadResponse(path=req.path, data=data, success=success)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, file_read_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_FILE_READ_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_file_remove(self, req: pb.FileRemove) -> None:
        """Remove file from Linux filesystem requested by MCU."""
        target = _sanitize_path(self.config.file_storage_dir, req.path)
        success = False
        if target is not None and target.is_file():
            try:
                target.unlink()
                success = True
            except OSError as exc:
                logger.error("File remove failed", path=str(target), error=str(exc))

        resp = pb.FileRemoveResponse(path=req.path, success=success)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, file_remove_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_FILE_REMOVE_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def _handle_spi_transfer(self, req: pb.SpiTransfer) -> None:
        """SPI loopback transfer for testing."""
        resp = pb.SpiTransferResponse(rx_data=req.tx_data, success=True)
        envelope = pb.RpcEnvelope(version=PROTOCOL_VERSION, spi_transfer_resp=resp)
        frame = build_frame(
            command_id=pb.CMD_SPI_TRANSFER_RESP,
            sequence_id=0,
            payload=envelope.SerializeToString(),
            session_key=self.state.session_key,
        )
        await self.serial.send(frame)

    async def handle_request(self, message: pb.LocalPublishRequest) -> None:
        """Handle local gRPC request published by CGI/CLI clients."""
        logger.debug("Handling local request", topic=message.topic)
        segments = message.topic.strip("/").split("/")
        if len(segments) < 3:
            logger.warning("Invalid topic path format", topic=message.topic)
            return

        domain = segments[1]
        action = segments[2] if len(segments) > 2 else ""

        if domain == "d":  # Digital Pin
            pin = int(action)
            val = int(message.payload.decode() or "0")
            cmd = pb.CMD_PIN_MODE_WRITE if message.properties and "mode" in message.properties else pb.CMD_PIN_WRITE
            env = pb.RpcEnvelope(version=PROTOCOL_VERSION, pin_request=pb.PinRequest(pin=pin, mode=1, value=val))
            frame = build_frame(
                command_id=cmd,
                sequence_id=0,
                payload=env.SerializeToString(),
                session_key=self.state.session_key,
            )
            await self.serial.send(frame)
        elif domain == "a":  # Analog Pin
            pin = int(action)
            val = int(message.payload.decode() or "0")
            env = pb.RpcEnvelope(
                version=PROTOCOL_VERSION,
                pin_request=pb.PinRequest(pin=pin, mode=pb.PIN_MODE_ANALOG, value=val),
            )
            frame = build_frame(
                command_id=pb.CMD_PIN_WRITE,
                sequence_id=0,
                payload=env.SerializeToString(),
                session_key=self.state.session_key,
            )
            await self.serial.send(frame)
        elif domain == "mb":  # Mailbox Write
            env = pb.RpcEnvelope(
                version=PROTOCOL_VERSION,
                mailbox_write=pb.MailboxWrite(msg=message.payload),
            )
            frame = build_frame(
                command_id=pb.CMD_MAILBOX_WRITE,
                sequence_id=0,
                payload=env.SerializeToString(),
                session_key=self.state.session_key,
            )
            await self.serial.send(frame)
        elif domain == "ds":  # Datastore Put
            key = action
            env = pb.RpcEnvelope(
                version=PROTOCOL_VERSION,
                datastore_put=pb.DatastorePut(key=key, value=message.payload),
            )
            frame = build_frame(
                command_id=pb.CMD_DATASTORE_PUT,
                sequence_id=0,
                payload=env.SerializeToString(),
                session_key=self.state.session_key,
            )
            await self.serial.send(frame)

    async def enqueue_cloud(self, message: pb.CloudQueuedPublish) -> None:
        """Enqueue a message to the persistent cloud spool and attempt flush."""
        if self._cloud_spool is not None:
            await self._cloud_spool.append(message.SerializeToString())
            self.state.cloud_spool_pending_messages = await self._cloud_spool.length()

        await self.flush_cloud_spool()

    async def flush_cloud_spool(self) -> None:
        """Flush spooled messages to Cloud Gateway stream when connected."""
        if not self._cloud_stream or self._cloud_spool is None:
            return

        while await self._cloud_spool.length() > 0:
            msg_bytes = await self._cloud_spool.peek()
            msg = pb.CloudQueuedPublish()
            msg.ParseFromString(msg_bytes)
            try:
                await self._cloud_stream.send_message(msg)
                await self._cloud_spool.popleft()
                self.state.cloud_spool_pending_messages = await self._cloud_spool.length()
            except (OSError, RuntimeError) as exc:
                logger.warning("Cloud stream flush failed", error=str(exc))
                break

    async def _cloud_loop(self) -> None:
        """Supervised task maintaining gRPC connection to Cloud Gateway."""
        if not self.config.cloud_enabled:
            logger.info("Cloud link disabled in configuration")
            return

        while True:
            try:
                logger.info(
                    "Connecting to Cloud Gateway",
                    host=self.config.cloud_host,
                    port=self.config.cloud_port,
                )
                self._cloud_channel = Channel(self.config.cloud_host, self.config.cloud_port)
                stub = CloudGatewayStub(self._cloud_channel)
                async with stub.ConnectStream.open() as stream:
                    self._cloud_stream = stream
                    self.state.cloud_connected = True
                    metrics.set_cloud_connected(True)
                    logger.info("Cloud Gateway stream established")

                    await self.flush_cloud_spool()

                    while True:
                        resp = await stream.recv_message()
                        if resp is None:
                            break
                        logger.debug("Received Cloud Gateway response", response=str(resp))
            except (OSError, RuntimeError) as exc:
                logger.warning("Cloud Gateway connection error", error=str(exc))
            finally:
                self.state.cloud_connected = False
                metrics.set_cloud_connected(False)
                self._cloud_stream = None
                if self._cloud_channel:
                    self._cloud_channel.close()
                    self._cloud_channel = None
                await asyncio.sleep(5.0)

    async def _ipc_server_loop(self) -> None:
        """Start gRPC server on UNIX domain socket for local CGI/CLI clients."""
        socket_path = Path(self.config.ipc_socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)

        from grpclib.server import Server

        server = Server([LocalBridgeService(self)])
        await server.start(path=str(socket_path))
        logger.info("gRPC IPC server listening", path=str(socket_path))
        try:
            await server.wait_closed()
        finally:
            socket_path.unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Explicitly cleanup and close the spool cache database connection (SIL 2)."""
        socket_path = Path(os.environ.get("MCUBRIDGE_SOCKET_PATH", "/var/run/mcubridge.sock"))
        try:
            socket_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not remove UNIX socket during cleanup", path=socket_path, error=str(exc))

        self.serial = None
        if self._cloud_spool is not None:
            self._cloud_spool.detach_connection()
        self._cloud_spool = None

        state = getattr(self, "state", None)
        if state is not None:
            state.cleanup()
