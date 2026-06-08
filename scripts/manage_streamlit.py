#!/usr/bin/env python3
"""
PM-Agent Application Startup Script.

Starts the PM Agent server (port 10005) and Streamlit UI (port 8501) in one command.
All logs from all modules are streamed to the terminal in real-time for easy tracing.

Usage:
    python scripts/manage_streamlit.py           # Start and tail all logs
    python scripts/manage_streamlit.py --no-logs # Start without tailing logs

Features:
    - Real-time log streaming from PM Agent and Streamlit
    - Unified log format for easy request tracing
    - Works in both local development and AKS environments
"""
import os
import sys
import time
import subprocess
import signal
import webbrowser
import argparse
from pathlib import Path
import platform
import threading
import queue
from typing import Optional, IO

# Fix Windows console encoding for Unicode
if platform.system() == 'Windows':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT_DIR = Path(__file__).resolve().parent.parent

# Add repo root to PYTHONPATH so 'config' module can be imported
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
APP_PATH = ROOT_DIR / "app" / "chat_ai.py"
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Log files
STREAMLIT_LOG = LOG_DIR / "streamlit.log"
PM_AGENT_LOG = LOG_DIR / "pm_agent.log"
PM_AGENT_ERR_LOG = LOG_DIR / "pm_agent_err.log"
SCHEDULER_LOG = LOG_DIR / "scheduler.log"
SCHEDULER_ERR_LOG = LOG_DIR / "scheduler_err.log"

# Global queue for log lines from all sources
log_queue: queue.Queue = queue.Queue()


def build_env_from_config() -> dict:
    """Build environment dict from config.yaml for subprocess calls."""
    from config import config
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    env["ADO_ORG_URL"] = config.ado_org_url or ""
    env["ADO_ORG_NAME"] = config.ado_org_name or ""
    env["ADO_PROJECT"] = config.ado_project or ""
    env["ADO_TEAM"] = config.ado_team or ""
    env["ADO_ITERATION"] = config.ado_iteration or ""
    env["ADO_PAT"] = config.ado_pat or ""
    env["ADO_MCP_AUTH_TOKEN"] = config.ado_mcp_auth_token or ""
    env["OPENAI_API_KEY"] = config.openai_api_key or ""
    env["SMTP_HOST"] = config.smtp_host or ""
    env["SMTP_PORT"] = str(config.smtp_port or "")
    env["SMTP_USERNAME"] = config.smtp_username or ""
    env["SMTP_PASSWORD"] = config.smtp_password or ""
    env["SMTP_FROM_EMAIL"] = config.smtp_from_email or ""
    env["LOG_LEVEL"] = config.log_level or "INFO"
    env["LANGFUSE_SECRET_KEY"] = config.langfuse_secret_key or ""
    env["LANGFUSE_PUBLIC_KEY"] = config.langfuse_public_key or ""
    env["LANGFUSE_BASE_URL"] = config.langfuse_base_url or ""
    # Capacity Triaging environment variables
    env["CAPACITY_SOURCE_TYPE"] = config.capacity_source_type or "ado"
    env["CAPACITY_SOURCE_URL"] = config.capacity_csv_file_path or config.capacity_google_sheets_url or ""
    env["CAPACITY_GOOGLE_CREDS_PATH"] = config.capacity_google_credentials_path or "credentials/google_sheets_creds.json"
    env["CAPACITY_DEVIATION_THRESHOLD"] = str(config.capacity_deviation_threshold)
    env["SPRINT_PROGRESS_THRESHOLD"] = str(config.sprint_progress_threshold)
    return env


def find_pids_on_port(port: int) -> list:
    """Find PIDs listening on a port using netstat."""
    pids = []
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if f":{port} " in line or f":{port}\r" in line:
                parts = line.split()
                if parts:
                    try:
                        pids.append(int(parts[-1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return list(set(pids))


def kill_pids(pids: list) -> None:
    """Kill processes by PID."""
    for pid in pids:
        if pid <= 0:
            continue
        try:
            if platform.system() == 'Windows':
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def get_python() -> str:
    """Get the venv Python executable path."""
    if platform.system() == 'Windows':
        venv_py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = ROOT_DIR / ".venv" / "bin" / "python"
    
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def create_venv() -> None:
    """Create virtual environment if missing."""
    venv_path = ROOT_DIR / ".venv"
    if venv_path.exists():
        print("✓ Virtual environment exists")
        return
    
    print("📦 Creating virtual environment...")
    subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True, timeout=120)
    print("✓ Created")


def install_deps() -> None:
    """Install dependencies from requirements.txt if needed."""
    python = get_python()
    
    # Quick check if streamlit is installed
    result = subprocess.run([python, "-c", "import streamlit"],
                           capture_output=True, timeout=10)
    if result.returncode == 0:
        print("✓ Dependencies installed")
        return
    
    print("📦 Installing dependencies...")
    req_file = ROOT_DIR / "requirements.txt"
    if not req_file.exists():
        print(f"✗ {req_file} not found")
        sys.exit(1)
    
    subprocess.run([python, "-m", "pip", "install", "-q", "--upgrade", "pip"], timeout=120)
    subprocess.run([python, "-m", "pip", "install", "-q", "-r", str(req_file)], check=True, timeout=600)
    print("✓ Installed")


def check_env() -> bool:
    """Check config.yaml has required credentials."""
    from config import config
    
    config_checks = {
        "ADO_PAT": config.ado_pat,
        "ADO_ORG_NAME": config.ado_org_name,
        "OPENAI_API_KEY": config.openai_api_key
    }
    missing = [k for k, v in config_checks.items() if not v]
    
    if missing:
        print(f"⚠ Missing required credentials: {missing}")
        print("  Set these in config.yaml")
        return False
    
    print("✓ Credentials configured")
    return True


def kill_existing() -> None:
    """Kill existing processes on ports 8501 and 10005."""
    for port in [8501, 10005]:
        pids = find_pids_on_port(port)
        if pids:
            print(f"  Killing PIDs on port {port}: {pids}")
            kill_pids(pids)
            time.sleep(0.5)
    print("✓ Ports cleared")


def stream_output(pipe: IO, prefix: str, log_file: Optional[Path] = None) -> None:
    """
    Stream output from a subprocess pipe to console and optionally to a file.
    Runs in a background thread.
    
    Args:
        pipe: Subprocess stdout/stderr pipe
        prefix: Prefix to add to each line (e.g., "[PM_AGENT]")
        log_file: Optional file to also write logs to
    """
    try:
        file_handle = None
        if log_file:
            file_handle = open(log_file, "a", encoding="utf-8", errors="replace")
        
        for line in iter(pipe.readline, ''):
            if not line:
                break
            line = line.rstrip('\n\r')
            if line:
                # Print to console with prefix
                print(f"{prefix} {line}", flush=True)
                # Also write to log file
                if file_handle:
                    file_handle.write(f"{line}\n")
                    file_handle.flush()
    except Exception as e:
        print(f"[LOG_ERROR] {prefix}: {e}")
    finally:
        if file_handle:
            file_handle.close()
        try:
            pipe.close()
        except Exception:
            pass


def start_pm_agent(detached: bool = False) -> subprocess.Popen:
    """Start PM Agent server on port 10005.
    
    Args:
        detached: If True, start without piping stdout/stderr (for --no-logs mode).
                  This allows the parent script to exit without affecting the subprocess.
    """
    python = get_python()
    
    # Clear old logs
    for log in [PM_AGENT_LOG, PM_AGENT_ERR_LOG]:
        log.write_text("")
    
    env = build_env_from_config()
    
    print(f"  LOG_LEVEL: {env['LOG_LEVEL']}")
    
    if detached:
        # Start detached - no pipes, process runs independently
        if platform.system() == 'Windows':
            # On Windows, redirect to files and run detached
            stdout_file = open(PM_AGENT_LOG, "w", encoding="utf-8")
            stderr_file = open(PM_AGENT_ERR_LOG, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [python, "-m", "agents.pm_agent"],
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=str(ROOT_DIR),
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            proc = subprocess.Popen(
                [python, "-m", "agents.pm_agent"],
                stdout=open(PM_AGENT_LOG, "w"),
                stderr=open(PM_AGENT_ERR_LOG, "w"),
                cwd=str(ROOT_DIR),
                env=env,
                start_new_session=True
            )
        
        print(f"  PM Agent PID: {proc.pid}")
        
        # Wait for startup by checking log file
        start_time = time.time()
        while time.time() - start_time < 15:
            time.sleep(0.5)
            try:
                logs = PM_AGENT_LOG.read_text(encoding="utf-8", errors="replace")
                logs += PM_AGENT_ERR_LOG.read_text(encoding="utf-8", errors="replace")
                if "Uvicorn running on http://localhost:10005" in logs:
                    print("✓ PM Agent running on port 10005")
                    return proc
            except Exception:
                pass
        
        print("⚠ PM Agent may not have started properly (check logs)")
        return proc
    
    # Start with pipes for real-time streaming
    if platform.system() == 'Windows':
        proc = subprocess.Popen(
            [python, "-m", "agents.pm_agent"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT_DIR),
            env=env,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        proc = subprocess.Popen(
            [python, "-m", "agents.pm_agent"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT_DIR),
            env=env,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            start_new_session=True
        )
    
    # Start threads to stream stdout and stderr
    stdout_thread = threading.Thread(
        target=stream_output,
        args=(proc.stdout, "[PM_AGENT]", PM_AGENT_LOG),
        daemon=True
    )
    stderr_thread = threading.Thread(
        target=stream_output,
        args=(proc.stderr, "[PM_AGENT]", PM_AGENT_ERR_LOG),
        daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    
    print(f"  PM Agent PID: {proc.pid}")
    
    # Wait for startup
    start_time = time.time()
    while time.time() - start_time < 15:
        time.sleep(0.5)
        # Check if process is still running
        if proc.poll() is not None:
            print(f"✗ PM Agent exited with code {proc.returncode}")
            break
        # Check logs for startup confirmation
        try:
            logs = PM_AGENT_ERR_LOG.read_text() + PM_AGENT_LOG.read_text()
            if "Uvicorn running on http://localhost:10005" in logs:
                print("✓ PM Agent running on port 10005")
                return proc
        except Exception:
            pass
    
    print("⚠ PM Agent startup check inconclusive - check logs")
    return proc


def start_streamlit(detached: bool = False) -> subprocess.Popen:
    """Start Streamlit UI on port 8501.
    
    Args:
        detached: If True, start without piping stdout/stderr (for --no-logs mode).
    """
    python = get_python()
    
    # Clear old log
    STREAMLIT_LOG.write_text("")
    
    env = build_env_from_config()
    
    cmd = [
        python, "-m", "streamlit", "run", str(APP_PATH),
        "--server.port", "8501",
        "--server.headless", "true",
        "--logger.level", "info"
    ]
    
    if detached:
        # Start detached - redirect to file
        log_file = open(STREAMLIT_LOG, "w", encoding="utf-8")
        if platform.system() == 'Windows':
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True
            )
        print(f"  Streamlit PID: {proc.pid}")
        return proc
    
    # Start with pipes for real-time streaming
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1
    )
    
    # Start thread to stream output
    stdout_thread = threading.Thread(
        target=stream_output,
        args=(proc.stdout, "[STREAMLIT]", STREAMLIT_LOG),
        daemon=True
    )
    stdout_thread.start()
    
    print(f"  Streamlit PID: {proc.pid}")
    return proc


def start_scheduler(detached: bool = False) -> subprocess.Popen:
    """Start the scheduler for background tasks (daily reports, etc.).
    
    Args:
        detached: If True, start without piping stdout/stderr (for --no-logs mode).
    """
    python = get_python()
    
    # Clear old logs
    for log in [SCHEDULER_LOG, SCHEDULER_ERR_LOG]:
        log.write_text("")
    
    env = build_env_from_config()
    
    cmd = [python, str(ROOT_DIR / "scripts" / "start_scheduler.py")]
    
    if detached:
        # Start detached - redirect to files
        if platform.system() == 'Windows':
            stdout_file = open(SCHEDULER_LOG, "w", encoding="utf-8")
            stderr_file = open(SCHEDULER_ERR_LOG, "w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=str(ROOT_DIR),
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=open(SCHEDULER_LOG, "w"),
                stderr=open(SCHEDULER_ERR_LOG, "w"),
                cwd=str(ROOT_DIR),
                env=env,
                start_new_session=True
            )
        
        print(f"  Scheduler PID: {proc.pid}")
        return proc
    
    # Start with pipes for real-time streaming
    if platform.system() == 'Windows':
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT_DIR),
            env=env,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT_DIR),
            env=env,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            start_new_session=True
        )
    
    # Start threads to stream stdout and stderr
    stdout_thread = threading.Thread(
        target=stream_output,
        args=(proc.stdout, "[SCHEDULER]", SCHEDULER_LOG),
        daemon=True
    )
    stderr_thread = threading.Thread(
        target=stream_output,
        args=(proc.stderr, "[SCHEDULER]", SCHEDULER_ERR_LOG),
        daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    
    print(f"  Scheduler PID: {proc.pid}")
    
    # Brief wait to check if scheduler started without immediate crash
    time.sleep(1)
    if proc.poll() is not None:
        print(f"⚠ Scheduler exited with code {proc.returncode}")
    else:
        print("✓ Scheduler running")
    
    return proc


def tail_logs() -> None:
    """Keep the main thread alive while log streaming threads run."""
    print("\n📋 Logs streaming to terminal (Ctrl+C to stop)...")
    print("-" * 60)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n✓ Stopped - shutting down processes...")


def main():
    parser = argparse.ArgumentParser(description="Start PM-Agent application")
    parser.add_argument("--no-logs", action="store_true", help="Don't follow logs after startup")
    args = parser.parse_args()
    
    print("=" * 60)
    print("🚀 PM-Agent Startup")
    print("=" * 60)
    
    # Step 1: Environment setup
    print("\n[1/6] Checking environment...")
    create_venv()
    install_deps()
    
    # Step 2: Credentials check
    print("\n[2/6] Checking credentials...")
    if not check_env():
        print("\n⚠ Fix config.yaml and retry")
        sys.exit(1)
    
    # Step 3: Kill existing processes
    print("\n[3/6] Clearing ports...")
    kill_existing()
    
    # Step 4: Start PM Agent
    print("\n[4/6] Starting PM Agent...")
    pm_proc = start_pm_agent(detached=args.no_logs)
    
    # Step 5: Start Streamlit
    print("\n[5/6] Starting Streamlit...")
    st_proc = start_streamlit(detached=args.no_logs)
    time.sleep(2)
    
    # Step 6: Start Scheduler
    print("\n[6/6] Starting Scheduler...")
    sched_proc = start_scheduler(detached=args.no_logs)
    
    # Open browser
    url = "http://localhost:8501"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    
    # Summary
    print("\n" + "=" * 60)
    print("✓ Application running!")
    print("=" * 60)
    print(f"""
PM Agent:  http://localhost:10005  (PID: {pm_proc.pid})
Streamlit: {url}  (PID: {st_proc.pid})
Scheduler: Background tasks       (PID: {sched_proc.pid})

All logs are streaming to this terminal in real-time.
Press Ctrl+C to stop all services.
""")
    
    # Keep running and streaming logs (unless --no-logs)
    if not args.no_logs:
        tail_logs()
        # Cleanup on exit
        print("Terminating processes...")
        try:
            pm_proc.terminate()
            st_proc.terminate()
            sched_proc.terminate()
            pm_proc.wait(timeout=5)
            st_proc.wait(timeout=5)
            sched_proc.wait(timeout=5)
        except Exception:
            pass
        print("✓ All processes stopped")


if __name__ == "__main__":
    main()
