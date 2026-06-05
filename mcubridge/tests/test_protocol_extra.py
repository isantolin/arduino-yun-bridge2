"""Extra coverage for mcubridge.protocol components."""


def test_topics_handshake_topic() -> None:
    from mcubridge.protocol.topics import (
        Topic,
        topic_path,
    )

    assert topic_path("prefix", Topic.SYSTEM, "handshake") == "prefix/system/handshake"
    assert topic_path("p", Topic.DIGITAL, "13", "read") == "p/d/13/read"
    assert topic_path("p", Topic.SPI, "transfer") == "p/spi/transfer"
    assert topic_path("p", Topic.DATASTORE, "key", "get") == "p/datastore/key/get"
    assert topic_path("p", Topic.FILE, "path/to/file", "read") == "p/file/path/to/file/read"
    assert topic_path("p", Topic.SHELL, "123", "kill") == "p/sh/123/kill"
    assert topic_path("p", Topic.CONSOLE, "write") == "p/console/write"
    assert topic_path("p", Topic.MAILBOX, "push") == "p/mailbox/push"
