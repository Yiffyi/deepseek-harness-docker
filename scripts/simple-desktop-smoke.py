#!/usr/bin/env python3
"""Smoke-test a built simple image using disposable Docker containers."""

import base64
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time


IMAGE = sys.argv[1] if len(sys.argv) > 1 else "dsh:simple"
ROOT = Path(__file__).resolve().parent.parent
containers = []


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def launch(*args):
    container = docker("run", "-d", *args)
    containers.append(container)
    return container


def wait_ready(container, relay, desktop):
    check = f"""
import socket, urllib.request
from urllib.error import HTTPError
try:
    urllib.request.urlopen('http://127.0.0.1:{3081 if relay else 3080}/', timeout=1)
except HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError('DSH should require authentication')
for port, expected in [(3081, {relay}), (6080, {desktop})]:
    with socket.socket() as sock:
        sock.settimeout(1)
        assert (sock.connect_ex(('127.0.0.1', port)) == 0) == expected
if {desktop}:
    assert urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=1).status == 200
"""
    for _ in range(60):
        result = subprocess.run(["docker", "exec", container, "python3", "-c", check],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        assert docker("inspect", "-f", "{{.State.Running}}", container) == "true"
        time.sleep(0.5)
    raise AssertionError("Services did not become ready")


def stop(container):
    docker("stop", "-t", "3", container)
    assert docker("inspect", "-f", "{{.State.ExitCode}}", container) in ("0", "143")


try:
    password = "desktop-smoke-only"
    hashed = docker("run", "--rm", "caddy:2.11-alpine", "caddy", "hash-password",
                    "--plaintext", password)
    gateway = launch("-p", "127.0.0.1::80", "-e", "DSH_WEB_HOST=localhost",
                     "-e", "DSH_WEB_USERNAME=smoke", "-e", f"DSH_WEB_PASSWORD_HASH={hashed}",
                     "--mount", f"type=bind,src={ROOT / 'caddy'},dst=/etc/caddy,readonly",
                     "caddy:2.11-alpine")
    container = launch("--network", f"container:{gateway}",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=512m",
                       "-e", "NARB_DISABLE_NATIVE_CACHE=1", IMAGE)
    wait_ready(container, True, True)
    display = docker("exec", container, "xdpyinfo", "-display", ":99")
    assert "1600x900 pixels" in display
    # Complete a VNC handshake and verify the advertised framebuffer dimensions.
    docker("exec", container, "python3", "-c", r"""
import socket, struct
with socket.create_connection(('127.0.0.1', 5900), timeout=3) as sock:
    stream = sock.makefile('rb')
    assert stream.read(12) == b'RFB 003.008\n'
    sock.sendall(b'RFB 003.008\n')
    count = stream.read(1)[0]
    assert 1 in stream.read(count)
    sock.sendall(b'\x01')
    assert stream.read(4) == b'\x00' * 4
    sock.sendall(b'\x01')
    assert struct.unpack('!HH', stream.read(4)) == (1600, 900)
""")
    bindings = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", gateway))
    port = int(bindings["80/tcp"][0]["HostPort"])
    auth = "Basic " + base64.b64encode(f"smoke:{password}".encode()).decode()
    for path in ("/desktop/vnc.html", "/desktop/core/rfb.js", "/desktop/websockify"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        assert connection.getresponse().status == 401, path
        connection.close()
    for path in ("/desktop/vnc.html", "/desktop/core/rfb.js"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path, headers={"Authorization": auth})
        assert connection.getresponse().status == 200, path
        connection.close()
    # Verify the authenticated WebSocket route reaches the VNC server.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(("GET /desktop/websockify HTTP/1.1\r\nHost: localhost\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Version: 13\r\n"
                      "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      f"Authorization: {auth}\r\n\r\n").encode())
        stream = sock.makefile("rb")
        assert b"101" in stream.readline()
        while stream.readline() != b"\r\n":
            pass
        frame = stream.read(2)
        assert frame[0] == 0x82
        assert stream.read(frame[1]) == b"RFB 003.008\n"
    print("1600x900 display, VNC handshake, Caddy authentication and WebSocket passed")
    docker("exec", container, "pkill", "-TERM", "-x", "socat")
    assert docker("wait", container) == "143"
    print("Service failure ends the container")

    for relay, desktop in ((False, True), (True, False), (False, False)):
        container = launch("-e", f"DSH_SOCAT_ENABLED={int(relay)}",
                           "-e", f"DSH_DESKTOP_ENABLED={int(desktop)}", IMAGE)
        wait_ready(container, relay, desktop)
        stop(container)
        if desktop:
            docker("start", container)
            wait_ready(container, relay, desktop)
            stop(container)
        print(f"relay={relay}, desktop={desktop}: startup and stop passed")
    container = launch("-e", "DSH_SOCAT_ENABLED=0", "-e", "DSH_DESKTOP_ENABLED=0",
                       IMAGE, "python3", "-c", "raise SystemExit(23)")
    assert docker("wait", container) == "23"
    print("Command override preserves exit status")
finally:
    for container in reversed(containers):
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
