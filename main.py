import asyncio
import asyncssh
from contextlib import asynccontextmanager
import fcntl
import json
import os
import pty
import shutil
import signal
import stat
import struct
import subprocess
import termios
import threading
import urllib.request
import zipfile
import io
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ================= CONFIGURATION =================
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS", "morning")

# Paste your Localtonet AuthToken here
LOCALTONET_TOKEN = "vPOeMSz9xkyojL2J1gVTFhmaBwZ687suA"

HTTP_PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", 8000)))
INTERNAL_SSH_PORT = 2222

BASE_DIR = "/home/container" if os.path.exists("/home/container") else os.getcwd()
WORK_DIR = os.path.join(BASE_DIR, ".ubuntu22")
ROOTFS_DIR = os.path.join(WORK_DIR, "rootfs")
PROOT_BIN = os.path.join(WORK_DIR, "proot")
LOCALTONET_BIN = os.path.join(WORK_DIR, "localtonet")
HOST_KEY_PATH = os.path.join(WORK_DIR, "ssh_host_key")

UBUNTU_ROOTFS_URL = "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz"
PROOT_URL = "https://github.com/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-x86_64-static"
LOCALTONET_URL = "https://localtonet.com/download/localtonet-linux-x64.zip"

STATE = {"status": "Booting..."}

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


def setup_ubuntu():
    os.makedirs(WORK_DIR, exist_ok=True)
    try:
        if not os.path.exists(PROOT_BIN):
            urllib.request.urlretrieve(PROOT_URL, PROOT_BIN)
            os.chmod(PROOT_BIN, os.stat(PROOT_BIN).st_mode | stat.S_IEXEC)

        if not os.path.exists(os.path.join(ROOTFS_DIR, "bin", "bash")):
            STATE["status"] = "Streaming Ubuntu 22.04 RootFS..."
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

        STATE["status"] = "Ubuntu 22.04 Ready"
    except Exception as e:
        STATE["status"] = f"Ubuntu Error: {e}"


def start_localtonet():
    os.makedirs(WORK_DIR, exist_ok=True)
    try:
        if not os.path.exists(LOCALTONET_BIN):
            print("[*] Downloading Localtonet Agent...")
            req = urllib.request.urlopen(LOCALTONET_URL)
            with zipfile.ZipFile(io.BytesIO(req.read())) as z:
                z.extractall(WORK_DIR)
            os.chmod(LOCALTONET_BIN, 0o755)

        if not LOCALTONET_TOKEN or "PASTE_" in LOCALTONET_TOKEN:
            print("[!] LOCALTONET_TOKEN is missing. Tunnel offline.")
            return

        print("[+] Starting Localtonet TCP Tunnel...")
        # Run in background and pipe input so it doesn't freeze waiting for a keyboard
        proc = subprocess.Popen(
            [LOCALTONET_BIN, "-t", LOCALTONET_TOKEN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        # Auto-send an "Enter" keystroke in case it prompts to select a tunnel
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
        except:
            pass

        for line in proc.stdout:
            print(f"[Localtonet] {line.strip()}")
            
    except Exception as e:
        print(f"[!] Localtonet error: {e}")


class UbuntuSSHServer(asyncssh.SSHServer):
    def password_auth_supported(self):
        return True
    def validate_password(self, username, password):
        return username == SSH_USER and password == SSH_PASS


def set_winsize(fd, row, col):
    winsize = struct.pack("HHHH", row, col, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


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

    await asyncssh.create_server(
        UbuntuSSHServer, "127.0.0.1", INTERNAL_SSH_PORT,
        server_host_keys=[HOST_KEY_PATH],
        process_factory=handle_ssh_client,
        encoding="utf-8"
    )

    threading.Thread(target=setup_ubuntu, daemon=True).start()
    threading.Thread(target=start_localtonet, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return HTMLResponse("<html><body style='background:#07080e;color:#fff;text-align:center;padding:50px;font-family:sans-serif;'><h2>Ubuntu 22.04 & Localtonet Tunnel Active</h2><p>Connect via Termius using your permanent Localtonet IP.</p></body></html>")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
