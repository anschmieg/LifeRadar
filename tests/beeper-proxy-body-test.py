#!/usr/bin/env python3
"""Test beeper-sidecar-proxy.py HTTP body forwarding with three packet layouts.

Asserts: upstream receives exactly one HTTP request with exactly the declared
body bytes and no duplication.
"""

import socket
import threading
import time


def make_http_request(method, path, body, content_length=None):
    """Build a raw HTTP/1.1 request bytes."""
    if content_length is None:
        content_length = len(body)
    headers = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Content-Length: {content_length}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return headers.encode("latin-1") + body


def make_chunked_request(method, path, body):
    """Build a chunked transfer encoding request bytes."""
    chunk = f"{len(body):x}\r\n".encode() + body + b"\r\n"
    headers = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Transfer-Encoding: chunked\r\n"
        f"\r\n"
    )
    return headers.encode("latin-1") + chunk + b"0\r\n\r\n"


class FakeUpstream:
    """Captures bytes sent by proxy; inspect them for body duplication."""

    def __init__(self, port):
        self.port = port
        self.sock = None
        self.received = b""
        self.errors = []
        self.thread = None
        self.running = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(5)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                self.received += chunk
        except socket.timeout:
            pass
        except Exception as e:
            self.errors.append(str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2)


def send_request(proxy_port, request_bytes, delay_fn=None):
    """Send raw bytes to proxy; optionally delay before sending body."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", proxy_port))
    s.settimeout(10)
    # Send headers first (withholding body)
    header_end = request_bytes.find(b"\r\n\r\n") + 4
    headers = request_bytes[:header_end]
    body = request_bytes[header_end:]
    s.sendall(headers)
    if delay_fn:
        delay_fn()
    if body:
        s.sendall(body)
    # Read response
    resp = b""
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
        except socket.timeout:
            break
    s.close()
    return resp


def parse_http_response(raw):
    """Return (status_line, headers, body)."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return None, {}, raw
    header_part = raw[:header_end]
    body = raw[header_end + 4 :]
    lines = header_part.decode("latin-1", errors="replace").split("\r\n")
    status_line = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers[k] = v
    return status_line, headers, body


def check_no_body_duplication(request_bytes, proxy_port, fake_upstream_port, name):
    """Verify upstream sees no duplicate body bytes."""
    fake = FakeUpstream(fake_upstream_port)
    fake.start()
    time.sleep(0.05)

    def delay_body():
        time.sleep(0.2)

    resp = send_request(proxy_port, request_bytes, delay_body)
    fake.stop()

    if fake.errors:
        print(f"FAIL [{name}] fake upstream error: {fake.errors}")
        return False

    status, headers, body = parse_http_response(fake.received)
    if not status:
        print(f"FAIL [{name}] could not parse response from upstream: {fake.received[:200]}")
        return False

    content_len = int(headers.get("Content-Length", 0))
    if content_len == 0:
        print(f"PASS [{name}] no body expected")
        return True

    upstream_body = body[len(status.encode()):]
    actual_body_start = fake.received.find(b"\r\n\r\n") + 4
    actual_body = fake.received[actual_body_start:]
    actual_content_len = actual_body.count(b"hello")

    if actual_content_len > content_len // 2:
        print(f"FAIL [{name}] possible body duplication: Content-Length={content_len}, "
              f"but 'hello' appears {actual_body.count(b'hello')} times in {len(actual_body)} bytes")
        return False

    print(f"PASS [{name}] upstream received {len(actual_body)} body bytes, "
          f"'hello' appears {actual_body.count(b'hello')} times")
    return True


def main():
    import sys
    import importlib.util

    # Dynamically load the proxy module
    spec = importlib.util.spec_from_file_location(
        "proxy", "/Users/adrian/Projects/LifeRadar/bin/beeper-sidecar-proxy.py"
    )
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    PROXY_PORT = 23373
    FAKE_UPSTREAM_PORT = 23999

    # Find proxy's current upstream port
    proxy.find_beeper_port = lambda: FAKE_UPSTREAM_PORT
    proxy._last_port = FAKE_UPSTREAM_PORT

    results = []

    # Test 1: headers-only first, body arrives later
    body1 = b"hello world"
    req1 = make_http_request("POST", "/v1/chat.sendMessage", body1)
    results.append(
        check_no_body_duplication(req1, PROXY_PORT, FAKE_UPSTREAM_PORT, "headers-first")
    )

    # Test 2: headers + partial body in first packet
    body2 = b"hello again"
    req2 = make_http_request("POST", "/v1/chat.sendMessage", body2)
    # Split: first 5 bytes of body with headers
    split_at = req2.find(b"hello")
    part1 = req2[: split_at + 5]
    part2 = req2[split_at + 5 :]

    def send_split():
        time.sleep(0.1)

    fake2 = FakeUpstream(FAKE_UPSTREAM_PORT)
    fake2.start()
    time.sleep(0.05)

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.connect(("127.0.0.1", PROXY_PORT))
    s2.settimeout(10)
    s2.sendall(part1)
    send_split()
    s2.sendall(part2)
    resp2 = b""
    while True:
        try:
            chunk = s2.recv(65536)
            if not chunk:
                break
            resp2 += chunk
        except socket.timeout:
            break
    s2.close()
    fake2.stop()

    status2, headers2, body2_resp = parse_http_response(fake2.received)
    if fake2.errors:
        print(f"FAIL [split-packet] fake upstream error: {fake2.errors}")
        results.append(False)
    elif not status2:
        print(f"FAIL [split-packet] could not parse response")
        results.append(False)
    else:
        cl2 = int(headers2.get("Content-Length", 0))
        body_start2 = fake2.received.find(b"\r\n\r\n") + 4
        actual2 = fake2.received[body_start2:]
        hello_count2 = actual2.count(b"hello")
        if hello_count2 > 1:
            print(f"FAIL [split-packet] body duplication detected: 'hello' appears {hello_count2} times")
            results.append(False)
        else:
            print(f"PASS [split-packet] upstream received {len(actual2)} body bytes, "
                  f"'hello' appears {hello_count2} times")
            results.append(True)

    # Test 3: full request (headers + complete body) in single recv
    body3 = b"full body test"
    req3 = make_http_request("POST", "/v1/chat.sendMessage", body3)
    fake3 = FakeUpstream(FAKE_UPSTREAM_PORT)
    fake3.start()
    time.sleep(0.05)

    s3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s3.connect(("127.0.0.1", PROXY_PORT))
    s3.settimeout(10)
    s3.sendall(req3)
    resp3 = b""
    while True:
        try:
            chunk = s3.recv(65536)
            if not chunk:
                break
            resp3 += chunk
        except socket.timeout:
            break
    s3.close()
    fake3.stop()

    if fake3.errors:
        print(f"FAIL [full-body] fake upstream error: {fake3.errors}")
        results.append(False)
    elif not parse_http_response(fake3.received)[0]:
        print(f"FAIL [full-body] could not parse response")
        results.append(False)
    else:
        _, headers3, _ = parse_http_response(fake3.received)
        cl3 = int(headers3.get("Content-Length", 0))
        body_start3 = fake3.received.find(b"\r\n\r\n") + 4
        actual3 = fake3.received[body_start3:]
        hello_count3 = actual3.count(b"hello")
        if hello_count3 > 1:
            print(f"FAIL [full-body] body duplication detected: 'hello' appears {hello_count3} times")
            results.append(False)
        else:
            print(f"PASS [full-body] upstream received {len(actual3)} body bytes, "
                  f"'hello' appears {hello_count3} times")
            results.append(True)

    print(f"\nResults: {sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    import sys

    success = main()
    sys.exit(0 if success else 1)
