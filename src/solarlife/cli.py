"""solarlife: talk to the running solarlifed."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solarlife import __version__, ipc
from solarlife.config import DEFAULT_CONFIG_PATH, DEFAULT_SOCKET_PATH, load_config
from solarlife.errors import ConfigError, DaemonError, DaemonUnreachable, InvalidWriteError, SolarlifeError

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3


# -- formatting -----------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def table(rows: list[list[str]], header: list[str] | None = None) -> str:
    rows = ([header] if header else []) + rows
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join(lines)


def format_status(status: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = parse_time(status.get("since"))
    state = status["state"]
    if since is not None:
        age = (now - since).total_seconds()
        state += f" since {since.astimezone().strftime('%H:%M:%S')} ({fmt_duration(age)})"
    lines = [("state", state)]
    device = status.get("device")
    if device:
        parts = [device["address"]]
        if device.get("name"):
            parts.append(device["name"])
        if device.get("rssi") is not None:
            parts.append(f"rssi {device['rssi']} dBm (at connect)")
        if device.get("pinned"):
            parts.append("pinned")
        if not device.get("connected", True):
            parts.append("not connected")
        lines.append(("device", "  ".join(parts)))
    else:
        lines.append(("device", "none"))
    for name, block in status["blocks"].items():
        if block["age_s"] is None:
            text = "no data yet"
        else:
            text = f"{'stale' if block['stale'] else 'ok'} {fmt_duration(block['age_s'])} ago"
        text += f"   (every {fmt_duration(block['interval_s'])})"
        if block.get("last_error"):
            text += f"   last error: {block['last_error']}"
        lines.append((name, text))
    c = status["connection"]
    text = f"{c['connect_attempts']} attempts, {c['connects']} connects, {c['disconnects']} disconnects"
    if c.get("last_error"):
        text += f", last error: {c['last_error']}"
    if c.get("backoff_s") is not None:
        text += f", retrying in {c['backoff_s']:g}s"
    lines.append(("connection", text))
    scan = status["scan"]
    last = parse_time(scan.get("last"))
    text = "never" if last is None else f"{fmt_duration((now - last).total_seconds())} ago, {scan['devices']} device(s)"
    if scan.get("next_in_s") is not None:
        text += f", next in {fmt_duration(scan['next_in_s'])}"
    lines.append(("scan", text))
    if status.get("unsolicited"):
        lines.append(("unsolicited", f"{status['unsolicited']} notification(s) ignored"))
    lines.append(("daemon", f"solarlifed {status['version']}, up {fmt_duration(status['uptime_s'])}"))
    width = max(len(k) for k, _ in lines)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in lines)


def format_values(values: list[dict[str, Any]]) -> str:
    return table([[v["name"], fmt_value(v["value"]), v.get("unit", "")] for v in values], ["NAME", "VALUE", "UNIT"])


def format_blocks(result: dict[str, Any], *, live: bool) -> str:
    out = []
    for name, block in result["blocks"].items():
        if live:
            head = f"{name} (live, {result.get('device')})"
        elif block["time"] is None:
            head = f"{name}: no data yet"
        else:
            head = f"{name} ({result.get('device')}, {fmt_duration(block['age_s'])} old{', stale' if block['stale'] else ''})"
        out.append(head)
        if block["values"]:
            out.append(format_values(block["values"]))
        out.append("")
    return "\n".join(out).rstrip()


def format_scan(scan: dict[str, Any]) -> str:
    if not scan["devices"]:
        return "no SolarLife devices found" + ("" if scan["time"] else " (no scan yet)")
    def rssi(d: dict[str, Any]) -> str:
        if d["rssi"] is None:
            return "-"
        return f"{d['rssi']} dBm" + ("" if d.get("advertising", True) else " (when connected)")

    rows = [[d["address"], d.get("name") or "", rssi(d), str(d["samples"]), "*" if d["current"] else ""] for d in scan["devices"]]
    return table(rows, ["ADDRESS", "NAME", "RSSI", "SAMPLES", "CONNECTED"])


def format_variables(rows: list[dict[str, Any]]) -> str:
    def allowed(row):
        return ", ".join(repr(a) for a in row["allowed"][:6]) + (" ..." if len(row["allowed"]) > 6 else "")

    return table(
        [[r["name"], r["address"], r["access"], r["block"] or "-", r["unit"], allowed(r)] for r in rows],
        ["NAME", "ADDRESS", "ACCESS", "BLOCK", "UNIT", "ALLOWED"],
    )


# -- commands -------------------------------------------------------------------


def resolve_socket(args: argparse.Namespace) -> Path:
    if args.socket is not None:
        return args.socket
    if args.config is not None:
        return load_config(args.config).daemon.socket  # an explicit config must be usable
    if DEFAULT_CONFIG_PATH.is_file():
        try:
            return load_config().daemon.socket
        except ConfigError:
            pass
    return DEFAULT_SOCKET_PATH


def parse_assignments(items: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise InvalidWriteError(f"expected NAME=VALUE, got {item!r}")
        values[name.strip()] = value.strip()
    return values


def cmd_variables(args: argparse.Namespace) -> int:
    from solarlife.registers import variables_table

    rows = variables_table()
    if args.writable:
        rows = [r for r in rows if r["access"] == "rw"]
    if args.block:
        rows = [r for r in rows if r["block"] == args.block]
    print(json.dumps(rows, indent=2, ensure_ascii=False) if args.json else format_variables(rows))
    return EXIT_OK


def cmd_write(args: argparse.Namespace, socket: Path) -> int:
    from solarlife.registers import build_write

    values = parse_assignments(args.assignments)
    results = build_write(values, force=args.force)  # fail early, before asking
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to write without confirmation, use --yes", file=sys.stderr)
            return EXIT_USAGE
        print("About to write:")
        for r in results:
            print(f"  {r.name} = {fmt_value(r.value)} {r.unit}".rstrip())
        if input("Write to the device? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return EXIT_ERROR
    result = ipc.request(socket, "write", {"values": values, "force": args.force})
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK
    written = {w["name"] for w in result["written"]}
    for rows in result["readback"].values():
        confirmed = [v for v in rows if v["name"] in written]
        if confirmed:
            print("written, read back:")
            print(format_values(confirmed))
    unconfirmed = written - {v["name"] for rows in result["readback"].values() for v in rows}
    if unconfirmed:
        print(f"written (not read back): {', '.join(sorted(unconfirmed))}")
    for block, error in result.get("readback_errors", {}).items():
        print(f"warning: could not read {block} back: {error}", file=sys.stderr)
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if args.cmd == "variables":
        return cmd_variables(args)
    socket = resolve_socket(args)
    if args.cmd == "write":
        return cmd_write(args, socket)

    def show(result: Any, text: str) -> int:
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else text)
        return EXIT_OK

    if args.cmd == "status":
        result = ipc.request(socket, "status")
        return show(result, format_status(result))
    if args.cmd == "cached":
        result = ipc.request(socket, "cached", {"block": args.block})
        return show(result, format_blocks(result, live=False))
    if args.cmd == "read":
        if args.range:
            address, _, count = args.range.partition(":")
            try:
                int(address, 0), int(count or 1, 0)
            except ValueError:
                print("error: --range expects ADDRESS[:COUNT], e.g. 0x3000:4", file=sys.stderr)
                return EXIT_USAGE
            result = ipc.request(socket, "read", {"address": address, "count": int(count or 1, 0)})
            return show(result, format_values(result["values"]))
        result = ipc.request(socket, "read", {"block": args.block})
        return show(result, format_blocks(result, live=True))
    if args.cmd == "reconnect":
        result = ipc.request(socket, "reconnect")
        return show(result, "reconnect requested")
    if args.cmd == "rescan":
        result = ipc.request(socket, "rescan")
        return show(result, format_scan(result))
    if args.cmd == "scan":
        result = ipc.request(socket, "scan")
        return show(result, format_scan(result))
    raise AssertionError(args.cmd)


def build_parser() -> argparse.ArgumentParser:
    def add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
        """The global options work before and after the command (SUPPRESS keeps the first value)."""
        keep = argparse.SUPPRESS
        parser.add_argument("--config", type=Path, default=keep if suppress else None, help="config file, used to find the daemon socket")
        parser.add_argument("--socket", type=Path, default=keep if suppress else None, help=f"daemon socket (default: from the config, else {DEFAULT_SOCKET_PATH})")
        parser.add_argument("--json", action="store_true", default=keep if suppress else False, help="print the raw JSON result")

    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, suppress=True)
    parser = argparse.ArgumentParser(prog="solarlife", description="Talk to the running solarlifed: cached and live values, writes, reconnect, scan.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("status", parents=[common], help="connection state, device, age of the last reads")
    cached = sub.add_parser("cached", parents=[common], help="last values read by the daemon (no radio traffic)")
    cached.add_argument("block", nargs="?", default="all", choices=["all", "status", "parameters"])
    read = sub.add_parser("read", parents=[common], help="read live values through the held connection")
    read.add_argument("block", nargs="?", default="all", choices=["all", "status", "parameters"])
    read.add_argument("--range", metavar="ADDRESS[:COUNT]", help="read raw registers instead, e.g. 0x3032:2")
    write = sub.add_parser("write", parents=[common], help="write registers, e.g. write float_voltage=13.6")
    write.add_argument("assignments", nargs="+", metavar="NAME=VALUE")
    write.add_argument("--force", action="store_true", help="allow protected variables (restore/clear, slave id, baud rate, password)")
    write.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    sub.add_parser("reconnect", parents=[common], help="drop the connection and connect again")
    sub.add_parser("rescan", parents=[common], help="scan now and re-select the device")
    sub.add_parser("scan", parents=[common], help="show the last scan results")
    variables = sub.add_parser("variables", parents=[common], help="list known variables (works without the daemon)")
    variables.add_argument("--writable", action="store_true")
    variables.add_argument("--block", choices=["status", "parameters"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except DaemonUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except DaemonError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except (InvalidWriteError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SolarlifeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
