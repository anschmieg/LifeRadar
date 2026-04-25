#!/usr/bin/env python3
"""
Beeper Desktop API proxy — dynamic port discovery, WebSocket-aware, HTTP body-correct.
"""

import errno
import socket
import struct
import threading
import time

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 23373
UPSTREAM_CONNECT_TIMEOUT_SEC = 5.0

# ── Port discovery ───────────────────────────────────────────────────────────

_last_port = None
_last_port_lock = threading.Lock()


def invalidate_port():
    global _last_port
    with _last_port_lock:
        _last_port = None


def get_port():
    with _last_port_lock:
        return _last_port


def find_beeper_port():
    for _ in range(3):
        for port in range(30000, 60001):
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(UPSTREAM_CONNECT_TIMEOUT_SEC)
                s.connect(("127.0.0.1", port))
                s.sendall(b"GET /v1/info HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                resp = s.recv(20)
                s.close()
                if resp and b"HTTP" in resp:
                    return port
            except Exception:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                continue
        time.sleep(0.5)
    return None


def ensure_port():
    p = get_port()
    if p:
        return p
    p = find_beeper_port()
    if p:
        with _last_port_lock:
            _last_port = p
    return p


# ── WebSocket frame primitives ───────────────────────────────────────────────

OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def _build_frame(opcode, payload, mask_key):
    length = len(payload)
    frame = bytearray()
    frame.append(0x80 | opcode)
    if mask_key is None:
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(payload)
    else:
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        frame.extend(masked_payload)
    return frame


def _relay_frame(upstream_sock, opcode, payload, second_byte, _source_sock):
    try:
        data = bytearray()
        data.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            data.append(second_byte & 0x80 | length)
        elif length < 65536:
            data.append(second_byte & 0x80 | 126)
            data.extend(struct.pack(">H", length))
        else:
            data.append(second_byte & 0x80 | 127)
            data.extend(struct.pack(">Q", length))
        data.extend(payload)
        upstream_sock.sendall(bytes(data))
    except (socket.error, OSError):
        pass


def write_masked_frame(sock, opcode, payload):
    try:
        sock.sendall(_build_frame(opcode, payload, mask_key=b"\x00\x00\x00\x00"))
    except (socket.error, OSError):
        pass


def write_unmasked_frame(sock, opcode, payload):
    try:
        sock.sendall(_build_frame(opcode, payload, mask_key=None))
    except (socket.error, OSError):
        pass


def read_frame(sock):
    first_byte = sock.recv(1)
    if not first_byte:
        return None, b"", 0
    b = first_byte[0]
    opcode = b & 0xF

    second_byte = sock.recv(1)
    if not second_byte:
        return None, b"", 0
    masked = (second_byte[0] & 0x80) != 0
    length = second_byte[0] & 0x7F

    if length == 126:
        raw = sock.recv(2)
        if len(raw) < 2:
            return None, b"", 0
        length = struct.unpack(">H", raw)[0]
    elif length == 127:
        raw = sock.recv(8)
        if len(raw) < 8:
            return None, b"", 0
        length = struct.unpack(">Q", raw)[0]

    payload = b""
    if masked:
        mask = sock.recv(4)
        if len(mask) < 4:
            return None, b"", 0
        raw_payload = b""
        while len(raw_payload) < length:
            chunk = sock.recv(length - len(raw_payload))
            if not chunk:
                break
            raw_payload += chunk
        if len(raw_payload) < length:
            return None, b"", 0
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw_payload))
    else:
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk
        if len(payload) < length:
            return None, b"", 0

    return opcode, payload, second_byte[0]


# ── WebSocket relay ───────────────────────────────────────────────────────────

def relay_client_to_upstream(client_sock, upstream_sock):
    client_sock.settimeout(60)
    try:
        while True:
            opcode, payload, second_byte = read_frame(client_sock)
            if opcode is None:
                break
            if opcode == OPCODE_CLOSE:
                _relay_frame(upstream_sock, opcode, payload, second_byte, client_sock)
                break
            if opcode in (OPCODE_TEXT, OPCODE_BINARY):
                _relay_frame(upstream_sock, opcode, payload, second_byte, client_sock)
            elif opcode == OPCODE_PING:
                _relay_frame(upstream_sock, OPCODE_PING, payload, second_byte, client_sock)
            elif opcode == OPCODE_PONG:
                pass
    except socket.timeout:
        pass
    except (socket.error, OSError) as e:
        if e.errno not in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.EBADF):
            log(f"[proxy] c->u relay error: {e}")


def relay_upstream_to_client(upstream_sock, client_sock):
    upstream_sock.settimeout(60)
    try:
        while True:
            opcode, payload, _second_byte = read_frame(upstream_sock)
            if opcode is None:
                break
            if opcode == OPCODE_CLOSE:
                write_unmasked_frame(client_sock, OPCODE_CLOSE, payload)
                break
            if opcode in (OPCODE_TEXT, OPCODE_BINARY):
                write_unmasked_frame(client_sock, opcode, payload)
            elif opcode == OPCODE_PING:
                write_unmasked_frame(client_sock, OPCODE_PONG, payload)
            elif opcode == OPCODE_PONG:
                pass
    except socket.timeout:
        pass
    except (socket.error, OSError) as e:
        if e.errno not in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.EBADF):
            log(f"[proxy] u->c relay error: {e}")


# ── HTTP request body handling ───────────────────────────────────────────────

def parse_headers(header_bytes):
    header_str = header_bytes.decode("latin-1", errors="replace")
    lines = header_str.split("\r\n")
    if not lines:
        return None, None, {}, len(header_bytes)
    request_line = lines[0].split(" ", 2)
    if len(request_line) < 2:
        return None, None, {}, len(header_bytes)
    method = request_line[0]
    path = request_line[1]
    headers = {}
    body_start = header_bytes.find(b"\r\n\r\n")
    if body_start == -1:
        body_start = len(header_bytes)
    else:
        body_start += 4
    for line in lines[1:]:
        if line == "":
            break
        if ": " in line:
            key, val = line.split(": ", 1)
            headers[key] = val
    return method, path, headers, body_start


def receive_full_body(sock, headers, max_body_size=16 * 1024 * 1024):
    content_length = headers.get("Content-Length")
    transfer_encoding = headers.get("Transfer-Encoding", "")

    if transfer_encoding.lower() == "chunked":
        body = b""
        while True:
            chunk_size_line = b""
            while b"\r\n" not in chunk_size_line:
                chunk_size_line += sock.recv(1)
            chunk_size_str = chunk_size_line.strip().split(b";")[0]
            try:
                chunk_size = int(chunk_size_str.decode("ascii"), 16)
            except ValueError:
                break
            if chunk_size == 0:
                trailing = b""
                while b"\r\n\r\n" not in trailing:
                    trailing += sock.recv(1)
                break
            remaining = chunk_size + 2
            while remaining > 0:
                chunk = sock.recv(min(remaining, 65536))
                if not chunk:
                    break
                body += chunk
                remaining -= len(chunk)
            if len(body) > max_body_size:
                break
        return body

    if content_length:
        try:
            content_len = int(content_length)
            body = b""
            while len(body) < content_len:
                chunk = sock.recv(min(content_len - len(body), 65536))
                if not chunk:
                    break
                body += chunk
            return body
        except ValueError:
            pass

    return b""


# ── Connection handler ───────────────────────────────────────────────────────

def is_websocket_upgrade(headers):
    upgrade = headers.get("Upgrade", "")
    connection = headers.get("Connection", "")
    return upgrade.lower() == "websocket" and "upgrade" in connection.lower()


def handle(client_sock, client_addr):
    log(f"[proxy] connection from {client_addr}")

    upstream = None
    try:
        port = ensure_port()
        if not port:
            log(f"[proxy] could not reach Beeper Desktop API; closing {client_addr}")
            client_sock.close()
            return

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(UPSTREAM_CONNECT_TIMEOUT_SEC)
        upstream.connect(("127.0.0.1", port))
        log(f"[proxy] forwarding to 127.0.0.1:{port}")

        client_sock.settimeout(10)
        header_bytes = b""
        while b"\r\n\r\n" not in header_bytes:
            chunk = client_sock.recv(4096)
            if not chunk:
                break
            header_bytes += chunk
            if len(header_bytes) > 65536:
                break

        method, path, headers, body_start = parse_headers(header_bytes)
        if method is None:
            log(f"[proxy] could not parse request headers from {client_addr}")
            client_sock.close()
            return

        body_already_read = header_bytes[body_start:]

        if is_websocket_upgrade(headers):
            # WebSocket: forward headers + any body, handle upgrade
            upstream.sendall(header_bytes)
            upstream.settimeout(15)

            upstream_resp = b""
            while b"\r\n\r\n" not in upstream_resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                upstream_resp += chunk
                if len(upstream_resp) > 65536:
                    break

            client_sock.sendall(upstream_resp)

            if b"101" in upstream_resp or b"switching protocols" in upstream_resp.lower():
                log("[proxy] WebSocket upgrade confirmed, relaying frames")
                t1 = threading.Thread(
                    target=relay_client_to_upstream, args=(client_sock, upstream), daemon=True
                )
                t2 = threading.Thread(
                    target=relay_upstream_to_client, args=(upstream, client_sock), daemon=True
                )
                t1.start()
                t2.start()
                t1.join()
                t2.join()
                log("[proxy] WebSocket session ended")
            else:
                log(f"[proxy] upstream rejected WebSocket upgrade: {upstream_resp[:200]}")
        else:
            # HTTP: forward complete request (headers + body) as one chunk
            # Beeper API requires request to arrive complete, not split into header/body packets

            upstream.sendall(header_bytes)

            # Send body bytes based on declared body, not on whether bytes arrived buffered
            if method in ("POST", "PUT", "PATCH"):
                content_len = headers.get("Content-Length")
                transfer_enc = headers.get("Transfer-Encoding", "")

                if content_len:
                    try:
                        total = int(content_len)
                        already = len(body_already_read)
                        remaining = max(total - already, 0)
                        additional = b""
                        while len(additional) < remaining:
                            chunk = client_sock.recv(min(remaining - len(additional), 65536))
                            if not chunk:
                                break
                            additional += chunk
                        if additional:
                            upstream.sendall(additional)
                    except ValueError:
                        pass
                elif transfer_enc.lower() == "chunked":
                    # Chunked: send any remaining chunked body data from the initial buffer,
                    # then read and forward the rest of the chunked stream from the socket
                    if body_already_read:
                        try:
                            upstream.sendall(body_already_read)
                        except (socket.error, OSError):
                            pass
                    chunked_body = receive_full_body(client_sock, headers)
                    if chunked_body:
                        try:
                            upstream.sendall(chunked_body)
                        except (socket.error, OSError):
                            pass

            upstream.settimeout(30)

            resp = b""
            while True:
                try:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
                    if len(chunk) < 4096:
                        break
                except socket.timeout:
                    break

            client_sock.sendall(resp)
            log(f"[proxy] forwarded {len(resp)} bytes HTTP response to client")

    except (socket.error, OSError) as e:
        if e.errno not in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.EBADF):
            log(f"[proxy] error: {e}")
        invalidate_port()
        log(f"[proxy] upstream refused; will rediscover on next connection")
    finally:
        if upstream:
            try:
                upstream.close()
            except Exception:
                pass
        try:
            client_sock.close()
        except Exception:
            pass


# ── Logging ─────────────────────────────────────────────────────────────────

_log_file = None


def log(msg):
    global _log_file
    if _log_file is None:
        _log_file = open("/tmp/proxy.log", "a", buffering=1)
        _log_file.write(f"\n[proxy] started at {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    _log_file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    _log_file.flush()


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    log("[proxy] waiting for Beeper Desktop API...")
    for _ in range(30):
        port = find_beeper_port()
        if port:
            with _last_port_lock:
                _last_port = port
            log(f"[proxy] Beeper Desktop API found on port {port}, listening on {LISTEN_PORT}")
            break
        time.sleep(1)
    else:
        log("[proxy] WARNING: Beeper Desktop API not found after 30s; will keep trying on each connection")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LISTEN_HOST, LISTEN_PORT))
    listener.listen(10)
    log(f"[proxy] listener ready on {LISTEN_HOST}:{LISTEN_PORT}")

    while True:
        try:
            client_sock, client_addr = listener.accept()
        except (socket.error, OSError):
            continue
        t = threading.Thread(target=handle, args=(client_sock, client_addr), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
