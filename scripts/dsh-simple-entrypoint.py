#!/usr/bin/env python3
"""Start optional services in the process group managed by tini -g."""

import os
from pathlib import Path
import subprocess
import sys
import time


def enabled(name):
    return os.environ.get(name, "1") == "1"


def main():
    command = sys.argv[1:]
    if not command:
        sys.exit("Missing command to run")

    desktop = enabled("DSH_DESKTOP_ENABLED")
    relay = enabled("DSH_SOCAT_ENABLED")
    if not desktop and not relay:
        os.execvp(command[0], command)

    services = []

    def start(*args):
        process = subprocess.Popen(args)
        services.append(process)
        return process

    if desktop:
        os.environ["DISPLAY"] = ":99"
        # This container owns :99; abrupt shutdown can leave its files behind.
        Path("/tmp/.X99-lock").unlink(missing_ok=True)
        Path("/tmp/.X11-unix/X99").unlink(missing_ok=True)
        xvfb = start("Xvfb", ":99", "-screen", "0", "1600x900x24",
                     "-ac", "-nolisten", "tcp")
        for _ in range(50):
            if xvfb.poll() is not None:
                sys.exit("Xvfb exited before the display became ready")
            ready = subprocess.run(
                ["xdpyinfo", "-display", ":99"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            sys.exit("Timed out waiting for Xvfb")

        start("openbox")
        start("x11vnc", "-display", ":99", "-forever", "-shared", "-repeat",
              "-noxdamage", "-rfbport", "5900", "-localhost", "-nopw")
        start("websockify", "--web=/usr/share/novnc",
              "0.0.0.0:6080", "127.0.0.1:5900")

    if relay:
        start("socat", "TCP4-LISTEN:3081,bind=0.0.0.0,reuseaddr,fork",
              "TCP4:127.0.0.1:3080")

    start(*command)
    # Any service exit ends the container. All services retain tini's group.
    _, status = os.wait()
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
