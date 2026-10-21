"""Logging to the journal (native protocol) and to stderr."""

import logging
import os
import socket
import struct
import sys
import tempfile

import pytest

from solarlife import daemon, journal
from solarlife.journal import (
    JournalHandler,
    journal_field,
    log_handler,
    stderr_is_journal,
    syslog_priority,
)

unix_sockets = pytest.mark.skipif(sys.platform.startswith("win"), reason="no Unix sockets")


def parse_datagram(data: bytes) -> dict[str, str]:
    """The fields of one native-protocol datagram."""
    fields = {}
    while data:
        line, _, rest = data.partition(b"\n")
        if b"=" in line:
            key, _, value = line.partition(b"=")
            fields[key.decode()] = value.decode()
            data = rest
        else:
            (length,) = struct.unpack("<Q", rest[:8])
            fields[line.decode()] = rest[8 : 8 + length].decode()
            assert rest[8 + length : 9 + length] == b"\n"
            data = rest[9 + length :]
    return fields


@pytest.fixture
def journald():
    """A datagram socket standing in for journald; yields (path, receive)."""
    if sys.platform.startswith("win"):
        pytest.skip("no Unix sockets")
    # unix socket paths are limited to ~100 characters (104 on macOS), pytest's tmp_path is too long
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="sl-") as directory:
        path = os.path.join(directory, "socket")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(1)

        def receive() -> dict[str, str]:
            return parse_datagram(server.recv(65536))

        yield path, receive
        server.close()


def make_logger(handler: logging.Handler) -> logging.Logger:
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logger = logging.getLogger("test.journal")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


def handler_logger(handler: logging.Handler) -> logging.Logger:
    """A logger that only writes through the given handler, keeping its formatter."""
    logger = logging.getLogger("solarlife.x")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


@pytest.fixture(autouse=True)
def reset_loggers():
    yield
    for name in ("test.journal", "solarlife.x"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


@pytest.mark.parametrize(
    "level, priority",
    [(logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6), (logging.DEBUG, 7), (25, 6)],
)
def test_syslog_priority(level, priority):
    assert syslog_priority(level) == priority


def test_journal_field_plain_and_multiline():
    assert journal_field("MESSAGE", "hello") == b"MESSAGE=hello\n"
    assert journal_field("MESSAGE", "a\nb") == b"MESSAGE\n" + struct.pack("<Q", 3) + b"a\nb\n"


def test_handler_sends_fields(journald):
    path, receive = journald
    logger = make_logger(JournalHandler("solarlifed", path))

    logger.warning("Cannot read %s", "/dev/x")
    fields = receive()

    assert fields["MESSAGE"] == "test.journal: Cannot read /dev/x"
    assert fields["PRIORITY"] == "4"
    assert fields["SYSLOG_IDENTIFIER"] == "solarlifed"
    assert fields["CODE_FILE"] == __file__
    assert fields["CODE_FUNC"] == "test_handler_sends_fields"
    assert int(fields["CODE_LINE"]) > 0


def test_traceback_is_one_entry(journald):
    path, receive = journald
    logger = make_logger(JournalHandler("solarlifed", path))

    try:
        raise ValueError("broken")
    except ValueError:
        logger.exception("Failed")
    fields = receive()

    assert fields["PRIORITY"] == "3"
    assert fields["MESSAGE"].startswith("test.journal: Failed\nTraceback")
    assert fields["MESSAGE"].endswith("ValueError: broken")


def test_send_failure_falls_back_to_stderr(journald, capsys):
    path, _ = journald
    handler = JournalHandler("solarlifed", path)
    logger = make_logger(handler)
    handler.socket.close()

    logger.error("Lost")

    assert capsys.readouterr().err == "<3>test.journal: Lost\n"


@unix_sockets
def test_missing_socket_raises(tmp_path):
    with pytest.raises(OSError):
        JournalHandler("solarlifed", str(tmp_path / "missing"))


def test_stderr_is_journal(monkeypatch, tmp_path):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert not stderr_is_journal()

    with open(tmp_path / "stderr", "w") as stderr:
        stat = os.fstat(stderr.fileno())
        monkeypatch.setattr(journal.sys, "stderr", stderr)
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino}")
        assert stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino + 1}")
        assert not stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", "garbage")
        assert not stderr_is_journal()


def test_log_handler_under_systemd(monkeypatch, journald):
    path, receive = journald
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(JournalHandler.__init__, "__defaults__", (path,))

    handler_logger(log_handler("solarlifed")).info("Started")
    fields = receive()

    assert fields["MESSAGE"] == "solarlife.x: Started"
    assert fields["SYSLOG_IDENTIFIER"] == "solarlifed"
    assert fields["PRIORITY"] == "6"


def test_log_handler_on_terminal(monkeypatch, capsys):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: False)

    handler_logger(log_handler("solarlifed")).warning("Careful")

    assert capsys.readouterr().err == "WARNING solarlife.x: Careful\n"


@unix_sockets
def test_log_handler_without_journald(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(JournalHandler.__init__, "__defaults__", (str(tmp_path / "missing"),))

    handler_logger(log_handler("solarlifed")).warning("Careful")

    assert capsys.readouterr().err == "WARNING solarlife.x: Careful\n"


def test_daemon_logs_as_solarlifed(monkeypatch, journald, tmp_path):
    path, receive = journald
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(JournalHandler.__init__, "__defaults__", (path,))
    # pytest's own handlers on the root logger would make basicConfig a no-op: take its arguments instead
    basic_config = {}
    monkeypatch.setattr(daemon.logging, "basicConfig", lambda **kwargs: basic_config.update(kwargs))

    class FakeDaemon:
        def __init__(self, config):
            pass

        async def run(self):
            return 0

    monkeypatch.setattr(daemon, "Daemon", FakeDaemon)
    config = tmp_path / "solarlife.yml"
    config.write_text("daemon:\n  log_level: WARNING\n", encoding="utf-8")

    assert daemon.main(["--config", str(config)]) == 0
    assert basic_config["level"] == logging.WARNING
    (handler,) = basic_config["handlers"]
    handler_logger(handler).warning("Careful")

    assert receive()["SYSLOG_IDENTIFIER"] == "solarlifed"
