import asyncio
import asyncssh
from contextlib import asynccontextmanager
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
import urllib.request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ================= CONFIGURATION =================
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS", "morning")
HTTP_PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", 8000)))
INTERNAL_SSH_PORT = 2222

# Paste your Reusable Tailscale Auth Key here or set TS_AUTHKEY in Botkeep Env Vars
TS_AUTHKEY = "tskey-auth-kEgfhYXhXF11CNTRL-jxdxRgaf4jAKogGoSJC7jA4pEEur9EUa"

# Store everything in persistent /home/container (avoids /tmp RAM disk full error)
BASE_DIR = "/home/container" if os.path.exists("/home/container") else os.getcwd()
WORK_DIR = os.path.join(BASE_DIR, ".ubuntu22")
ROOTFS_DIR = os.path.join(WORK_DIR, "rootfs")
PROOT_BIN = os.path.join(WORK_DIR, "proot")
HOST_KEY_PATH = os.path.join(WORK_DIR, "ssh_host_key")

TS_DIR = os.path.join(WORK_DIR, "tailscale")
TS_STATE_DIR = os.path.join(WORK_DIR, "ts_state")
TS_SOCK = os.path.join(WORK_DIR, "tailscaled.sock")

UBUNTU_ROOTFS_URL = "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz"
PROOT_URL = "https://github.com/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-x86_64-static"
TAILSCALE_URL = "https://pkgs.tailscale.com/stable/tailscale_1.76.6_amd64.tgz"

STATE = {
    "status": "Initializing...",
    "permanent_ip": "Waiting for Tailscale...",
    "permanent_port": str(INTERNAL_SSH_PORT),
    "backup_host": "Allocating backup...",
    "backup_port": "Waiting...",
    "termius_user": SSH_USER,
}


def get_disk_free_mb(path):
    try:
        _, _, free = shutil.disk_usage(path)
        return free // (1024 * 1024)
    except Exception:
        return 0


def get_proot_cmd():
    if os.path.exists(os.path.join(ROOTFS_DIR, "bin", "bash")) and os.path.exists(PROOT_BIN):
        return [
            PROOT_BIN, "-0", "-r", ROOTFS_DIR,
            "-b", "/dev", "-b", "/proc", "-b", "/sys", "-w", "/root",
            "/usr/bin/env", "-i",
            "HOME=/root", "USER=root", "TERM=xterm-256color",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/bash", "-l"
        ]
    return ["/bin/bash", "-i"] if os.path.exists("/bin/bash") else ["/bin/sh", "-i"]


def start_permanent_tailscale():
    """Runs Tailscale in unprivileged userspace mode for a 100% permanent IP (100.x.y.z)."""
    os.makedirs(TS_DIR, exist_ok=True)
    os.makedirs(TS_STATE_DIR, exist_ok=True)

    tailscaled_bin = os.path.join(TS_DIR, "tailscaled")
    tailscale_bin = os.path.join(TS_DIR, "tailscale")

    try:
        if not os.path.exists(tailscaled_bin):
            STATE["status"] = "Downloading Tailscale static binaries..."
            print("[*] Streaming Tailscale static binaries...")
            req = urllib.request.urlopen(TAILSCALE_URL)
            tar_proc = subprocess.Popen(
                ["tar", "-xz", "--strip-components=1", "-C", TS_DIR],
                stdin=subprocess.PIPE
            )
            shutil.copyfileobj(req, tar_proc.stdin)
            tar_proc.stdin.close()
            tar_proc.wait()
            os.chmod(tailscaled_bin, 0o755)
            os.chmod(tailscale_bin, 0o755)

        # Start tailscaled in userspace-networking mode (no root or /dev/net/tun required!)
        print("[*] Starting tailscaled in userspace networking mode...")
        subprocess.Popen(
            [
                tailscaled_bin,
                "--tun=userspace-networking",
                f"--state={os.path.join(TS_STATE_DIR, 'tailscaled.state')}",
                f"--socket={TS_SOCK}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(3)

        if not TS_AUTHKEY or "PASTE_" in TS_AUTHKEY:
            STATE["permanent_ip"] = "Set TS_AUTHKEY to enable permanent IP"
            print("[!] TS_AUTHKEY not set. Streaming login link to console logs instead...")
            up_proc = subprocess.Popen(
                [tailscale_bin, f"--socket={TS_SOCK}", "up", "--hostname=ubuntu22-botkeep"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in up_proc.stdout:
                print(f"[Tailscale] {line.strip()}")
                if "https://login.tailscale.com" in line:
                    STATE["permanent_ip"] = f"Approve login: {line.strip()}"
        else:
            subprocess.run(
                [
                    tailscale_bin, f"--socket={TS_SOCK}", "up",
                    f"--authkey={TS_AUTHKEY}",
                    "--hostname=ubuntu22-botkeep",
                    "--reset"
                ],
                check=False
            )

        # Fetch the permanent 100.x.y.z IPv4 address
        ip_out = subprocess.check_output(
            [tailscale_bin, f"--socket={TS_SOCK}", "ip", "-4"], text=True
        ).strip()

        if ip_out:
            STATE["permanent_ip"] = ip_out.splitlines()[0]
            print("\n=======================================================")
            print("[+] PERMANENT TERMIUS IP LOCKED (NEVER CHANGES!)")
            print(f"    Permanent IP : {STATE['permanent_ip']}")
            print(f"    Port         : {INTERNAL_SSH_PORT}")
            print(f"    Username     : {SSH_USER}")
            print(f"    Password     : {SSH_PASS}")
            print("=======================================================\n")

    except Exception as e:
        STATE["permanent_ip"] = f"Tailscale Error: {e}"
        print(f"[!] Tailscale setup error: {e}")


def setup_ubuntu():
    """Streams Ubuntu 22.04 RootFS directly into /home/container/.ubuntu22."""
    os.makedirs(WORK_DIR, exist_ok=True)
    shutil.rmtree("/tmp/ubuntu22_env", ignore_errors=True)
    free_mb = get_disk_free_mb(WORK_DIR)

    try:
        if not os.path.exists(PROOT_BIN):
            urllib.request.urlretrieve(PROOT_URL, PROOT_BIN)
            os.chmod(PROOT_BIN, os.stat(PROOT_BIN).st_mode | stat.S_IEXEC)

        if not os.path.exists(os.path.join(ROOTFS_DIR, "bin", "bash")):
            if free_mb >= 220:
                STATE["status"] = f"Installing Ubuntu 22.04 ({free_mb}MB free)..."
                os.makedirs(ROOTFS_DIR, exist_ok=True)
                req = urllib.request.urlopen(UBUNTU_ROOTFS_URL)
                tar_proc = subprocess.Popen(
                    ["tar", "-xJ", "-C", ROOTFS_DIR, "--exclude=dev/*"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                shutil.copyfileobj(req, tar_proc.stdin)
                tar_proc.stdin.close()
                tar_proc.wait()

                resolv = os.path.join(ROOTFS_DIR, "etc", "resolv.conf")
                if os.path.islink(resolv) or os.path.exists(resolv):
                    os.remove(resolv)
                with open(resolv, "w") as f:
                    f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

                apt_conf = os.path.join(ROOTFS_DIR, "etc", "apt", "apt.conf.d")
                os.makedirs(apt_conf, exist_ok=True)
                with open(os.path.join(apt_conf, "99sandbox"), "w") as f:
                    f.write('APT::Sandbox::User "root";\n')

        STATE["status"] = f"Ubuntu 22.04 Ready ({get_disk_free_mb(WORK_DIR)}MB Free)"
    except Exception as e:
        STATE["status"] = f"Ready ({e})"


async def maintain_backup_tunnel():
    """Keeps Pinggy active as a secondary fallback in case you are on a device without Tailscale."""
    while True:
        try:
            async with asyncssh.connect(
                "a.pinggy.io", port=443, username="tcp", password="",
                known_hosts=None, keepalive_interval=30
            ) as conn:
                await conn.forward_remote_port("", 0, "127.0.0.1", INTERNAL_SSH_PORT)
                async with conn.create_process() as proc:
                    async for line in proc.stdout:
                        match = re.search(r"tcp://([a-zA-Z0-9.\-]+):(\d+)", line)
                        if match:
                            STATE["backup_host"], STATE["backup_port"] = match.group(1), match.group(2)
                await conn.wait_closed()
        except Exception:
            await asyncio.sleep(10)


def set_winsize(fd, row, col):
    winsize = struct.pack("HHHH", row, col, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


class UbuntuSSHServer(asyncssh.SSHServer):
    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == SSH_USER and password == SSH_PASS


async def handle_ssh_client(process: asyncssh.SSHServerProcess):
    pid, master_fd = pty.fork()
    if pid == 0:
        cmd = get_proot_cmd()
        os.execvp(cmd[0], cmd)

    loop = asyncio.get_running_loop()
    term_size = process.get_terminal_size()
    if term_size[0] and term_size[1]:
        set_winsize(master_fd, term_size[1], term_size[0])

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def on_output():
        try:
            data = os.read(master_fd, 4096)
            if not data:
                loop.remove_reader(master_fd)
                process.exit(0)
            else:
                process.stdout.write(data.decode("utf-8", errors="replace"))
        except OSError:
            loop.remove_reader(master_fd)
            process.exit(0)

    loop.add_reader(master_fd, on_output)
    try:
        async for line in process.stdin:
            os.write(master_fd, line.encode("utf-8"))
    except asyncssh.TerminalSizeChanged as exc:
        set_winsize(master_fd, exc.height, exc.width)
    except Exception:
        pass
    finally:
        loop.remove_reader(master_fd)
        try:
            os.close(master_fd)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except OSError:
            pass
        process.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(WORK_DIR, exist_ok=True)
    if not os.path.exists(HOST_KEY_PATH):
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(HOST_KEY_PATH)

    # Listen on 0.0.0.0:2222 so both Tailscale (100.x.y.z:2222) and localhost tunnels can reach it
    await asyncssh.create_server(
        UbuntuSSHServer, "0.0.0.0", INTERNAL_SSH_PORT,
        server_host_keys=[HOST_KEY_PATH],
        process_factory=handle_ssh_client,
        encoding="utf-8"
    )

    threading.Thread(target=setup_ubuntu, daemon=True).start()
    threading.Thread(target=start_permanent_tailscale, daemon=True).start()
    backup_task = asyncio.create_task(maintain_backup_tunnel())
    yield
    backup_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def info_endpoint(passkey: str = Query("")):
    if passkey == SSH_PASS:
        return JSONResponse(STATE)
    return JSONResponse({
        "status": STATE["status"],
        "hint": "Append ?passkey=morning to URL to view your Permanent Termius IP"
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
