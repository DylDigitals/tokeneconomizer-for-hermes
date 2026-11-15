"""Drive an interactive command in a pseudo-terminal: send lines, capture all output.

Usage: python pty_drive.py OUT_FILE STEP_TIMEOUT -- CMD... < script
Script lines: `send <text>` writes text+Enter; `wait <regex>` waits for output match;
`sleep <s>` pauses. The child is killed at the end if still running.
"""
import os, pty, re, select, signal, sys, time

def main():
    out_path, step_timeout = sys.argv[1], float(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    steps = [l.rstrip("\n") for l in sys.stdin if l.strip()]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    buf = b""
    def pump(deadline):
        nonlocal buf
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                buf += data
        return True
    rc = 0
    for step in steps:
        op, _, arg = step.partition(" ")
        if op == "send":
            os.write(fd, arg.encode())
            pump(time.time() + 0.8)
            os.write(fd, b"\r")
            pump(time.time() + 0.5)
        elif op == "wait":
            end = time.time() + step_timeout
            pat = re.compile(arg.encode())
            start = len(buf)
            while time.time() < end and not pat.search(re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", buf)):
                if not pump(time.time() + 0.5):
                    break
            if not pat.search(re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", buf)):
                print(f"TIMEOUT waiting for {arg!r}", file=sys.stderr)
                rc = 3
                break
        elif op == "sleep":
            pump(time.time() + float(arg))
    pump(time.time() + 1)
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    with open(out_path, "wb") as f:
        f.write(buf)
    sys.exit(rc)

main()
