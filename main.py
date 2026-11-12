import asyncio
import asyncssh
import fcntl
import json
import os
import pty
import signal
import stat
import struct
import subprocess
import termios
import threading
import urllib.request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from pyngrok import ngrok, conf
import uvicorn

app = FastAPI()

# Credentials & Ngrok Token (Reads from Env Vars first, falls back to defaults)
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS", "morning")
NGROK_TOKEN = os.getenv(
    "NGROK_TOKEN",
    "3DJO7gEeVEZLoTbTAPjXI6Z2xUP_2UCYDdJ7TnuXeGdx1wrJv"
)
HTTP_PORT = int(os.getenv("PORT", 8000))
INTERNAL_SSH_PORT = 2222

WORK_DIR = "/tmp/ubuntu22_env"
ROOTFS_DIR = os.path.join(WORK_DIR, "rootfs")
PROOT_BIN = os.path.join(WORK_DIR, "proot")
HOST_KEY_PATH = os.path.join(WORK_DIR, "ssh_host_key")

UBUNTU_ROOTFS_URL = "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz"
PROOT_URL = "https://github.com/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-x86_64-static"

STATE = {
    "status": "Bootstrapping Ubuntu 22.04...",
    "ready": False,
    "termius_host": "Waiting for ngrok...",
    "termius_port": "Waiting...",
    "termius_user": SSH_USER,
    "ssh_command": "Starting tunnel..."
}


def get_proot_cmd():
    """Command to launch an interactive Ubuntu 22.04 bash shell as fake root."""
    if os.path.exists(os.path.join(ROOTFS_DIR, "bin", "bash")):
        return [
            PROOT_BIN, "-0", "-r", ROOTFS_DIR,
            "-b", "/dev", "-b", "/proc", "-b", "/sys", "-w", "/root",
            "/usr/bin/env", "-i",
            "HOME=/root", "USER=root", "TERM=xterm-256color",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "/bin/bash", "-l"
        ]
    # Fallback to host shell if someone connects while Ubuntu is still extracting
    return ["/bin/bash", "-i"] if os.path.exists("/bin/bash") else ["/bin/sh", "-i"]


def start_ngrok_tcp():
    """Starts the ngrok TCP tunnel on port 2222 and extracts Host + Port for Termius."""
    try:
        os.makedirs(WORK_DIR, exist_ok=True)
        # Store ngrok binary and config in writable /tmp directory
        pyngrok_Conf = conf.PyngrokConfig(
            ngrok_path=os.path.join(WORK_DIR, "ngrok"),
            config_path=os.path.join(WORK_DIR, "ngrok.yml")
        )
        conf.set_default(pyngrok_Conf)
        ngrok.set_auth_token(NGROK_TOKEN)

        tunnel = ngrok.connect(INTERNAL_SSH_PORT, "tcp")
        # tunnel.public_url looks like "tcp://0.tcp.in.ngrok.io:14892"
        clean_url = tunnel.public_url.replace("tcp://", "")
        host, port = clean_url.split(":")

        STATE["termius_host"] = host
        STATE["termius_port"] = port
        STATE["ssh_command"] = f"ssh {SSH_USER}@{host} -p {port}"
        print(f"\n==========================================")
        print(f"[+] NGROK TCP TUNNEL ONLINE FOR TERMIUS!")
        print(f"    Host / IP : {host}")
        print(f"    Port      : {port}")
        print(f"    Username  : {SSH_USER}")
        print(f"    Password  : {SSH_PASS}")
        print(f"==========================================\n")
    except Exception as e:
        STATE["termius_host"] = f"Ngrok Error: {e}"
        print(f"[!] Ngrok error: {e}")


def setup_ubuntu():
    """Downloads PRoot and Ubuntu 22.04 RootFS in the background."""
    os.makedirs(WORK_DIR, exist_ok=True)
    try:
        if not os.path.exists(PROOT_BIN):
            STATE["status"] = "Downloading static PRoot..."
            urllib.request.urlretrieve(PROOT_URL, PROOT_BIN)
            os.chmod(PROOT_BIN, os.stat(PROOT_BIN).st_mode | stat.S_IEXEC)

        if not os.path.exists(os.path.join(ROOTFS_DIR, "bin", "bash")):
            STATE["status"] = "Downloading Ubuntu 22.04 RootFS (~85MB)..."
            os.makedirs(ROOTFS_DIR, exist_ok=True)
            archive = os.path.join(WORK_DIR, "rootfs.tar.xz")
            urllib.request.urlretrieve(UBUNTU_ROOTFS_URL, archive)

            STATE["status"] = "Extracting Ubuntu 22.04 RootFS..."
            subprocess.run(["tar", "-xJf", archive, "-C", ROOTFS_DIR, "--exclude=dev/*"], check=False)
            os.remove(archive)

            resolv = os.path.join(ROOTFS_DIR, "etc", "resolv.conf")
            if os.path.islink(resolv) or os.path.exists(resolv):
                os.remove(resolv)
            with open(resolv, "w") as f:
                f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")

            apt_conf = os.path.join(ROOTFS_DIR, "etc", "apt", "apt.conf.d")
            os.makedirs(apt_conf, exist_ok=True)
            with open(os.path.join(apt_conf, "99sandbox"), "w") as f:
                f.write('APT::Sandbox::User "root";\n')

            STATE["status"] = "Installing curl, wget, unzip in Ubuntu 22.04..."
            cmd = get_proot_cmd()[:-2] + [
                "/bin/bash", "-c",
                "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y curl wget unzip"
            ]
            subprocess.run(cmd, check=False)

        STATE["ready"] = True
        STATE["status"] = "Ubuntu 22.04 Ready"
        print("[+] Ubuntu 22.04 RootFS is completely installed and ready!")
    except Exception as e:
        STATE["status"] = f"Ubuntu Setup Error: {e}"
        print(f"[!] Setup error: {e}")


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


HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Termius & Ubuntu 22.04 Gateway</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: radial-gradient(circle at top left, #141629, #07080e);
      color: #e2e8f0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card {
      background: rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 16px;
      padding: 32px;
      width: 420px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7);
    }
    h2 { font-size: 1.25rem; margin-bottom: 6px; text-align: center; }
    .status { font-size: 0.85rem; color: #a5b4fc; text-align: center; margin-bottom: 20px; }
    input {
      width: 100%; padding: 12px; background: rgba(0,0,0,0.4);
      border: 1px solid rgba(255,255,255,0.15); border-radius: 8px;
      color: #fff; margin-bottom: 14px; outline: none;
    }
    button {
      width: 100%; padding: 12px; background: linear-gradient(135deg, #6366f1, #8b5cf6);
      border: none; border-radius: 8px; color: #fff; font-weight: 600; cursor: pointer;
    }
    .info-row {
      display: flex; justify-content: space-between; padding: 10px 0;
      border-bottom: 1px solid rgba(255,255,255,0.08); font-size: 0.9rem;
    }
    .val { font-family: Consolas, monospace; color: #38bdf8; user-select: all; }
    #details { display: none; margin-top: 16px; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Ubuntu 22.04 SSH Details</h2>
    <div class="status" id="status-text">Checking status...</div>
    <div id="login-box">
      <input type="password" id="pass" placeholder="Enter SSH_PASS (default: morning)" autofocus />
      <button onclick="fetchDetails()">Show Termius Login Details</button>
    </div>
    <div id="details">
      <div class="info-row"><span>Hostname / IP:</span><span class="val" id="t-host">...</span></div>
      <div class="info-row"><span>Port:</span><span class="val" id="t-port">...</span></div>
      <div class="info-row"><span>Username:</span><span class="val" id="t-user">...</span></div>
      <div class="info-row"><span>CLI Command:</span><span class="val" id="t-cmd">...</span></div>
    </div>
  </div>
  <script>
    let savedPass = "";
    async function poll() {
      const res = await fetch('/info' + (savedPass ? `?passkey=${encodeURIComponent(savedPass)}` : ''));
      const data = await res.json();
      document.getElementById('status-text').innerText = data.status;
      if (data.termius_host && data.termius_host !== "Locked") {
        document.getElementById('login-box').style.display = 'none';
        document.getElementById('details').style.display = 'block';
        document.getElementById('t-host').innerText = data.termius_host;
        document.getElementById('t-port').innerText = data.termius_port;
        document.getElementById('t-user').innerText = data.termius_user;
        document.getElementById('t-cmd').innerText = data.ssh_command;
      }
    }
    function fetchDetails() {
      savedPass = document.getElementById('pass').value;
      poll();
    }
    setInterval(poll, 3000);
    poll();
  </script>
</body>
</html>
"""


@app.on_event("startup")
async def startup_event():
    os.makedirs(WORK_DIR, exist_ok=True)
    if not os.path.exists(HOST_KEY_PATH):
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(HOST_KEY_PATH)

    # 1. Start pure-Python SSH server on local port 2222
    await asyncssh.create_server(
        UbuntuSSHServer, "127.0.0.1", INTERNAL_SSH_PORT,
        server_host_keys=[HOST_KEY_PATH],
        process_factory=handle_ssh_client,
        encoding="utf-8"
    )

    # 2. Launch ngrok TCP tunnel and Ubuntu 22.04 bootstrapper in background threads
    threading.Thread(target=start_ngrok_tcp, daemon=True).start()
    threading.Thread(target=setup_ubuntu, daemon=True).start()


@app.get("/")
async def home():
    return HTMLResponse(HTML_PAGE)


@app.get("/info")
async def info_endpoint(passkey: str = Query("")):
    if passkey == SSH_PASS:
        return JSONResponse(STATE)
    return JSONResponse({"status": STATE["status"], "termius_host": "Locked"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
