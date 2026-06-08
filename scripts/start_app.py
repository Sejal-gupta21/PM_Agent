#!/usr/bin/env python3
"""
PM-Agent Application Startup Script (K8s/AKS Compatible).

Starts the PM Agent server (port 10005) and Streamlit UI (port 8501) in one command.
All logs from all modules are streamed to stdout/stderr for container log collection.

Usage:
    python scripts/start_app.py           # Start and stream all logs
    python scripts/start_app.py --no-logs # Start without streaming logs

Features:
    - Real-time log streaming from PM Agent and Streamlit to stdout
    - Unified log format for easy request tracing
    - Designed for Kubernetes/AKS container environments
    - No OS-specific logic - works on any platform
"""
import os
import sys
import time
import subprocess
import argparse
from pathlib import Path
import threading
from typing import Optional, IO

# Ensure UTF-8 encoding for console output
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config import config

APP_PATH = ROOT_DIR / "app" / "chat_ai.py"


def get_python() -> str:
    """Get the Python executable path (venv or system)."""
    # Try venv locations
    venv_paths = [
        ROOT_DIR / ".venv" / "Scripts" / "python.exe",  # Windows venv
        ROOT_DIR / ".venv" / "bin" / "python",          # Unix venv
    ]
    
    for venv_py in venv_paths:
        if venv_py.exists():
            return str(venv_py)
    
    return sys.executable


def check_env() -> bool:
    """Check environment variables for required credentials."""
    # In K8s, credentials come from environment variables (secrets/configmaps)
    # Check both env vars and .env file
    
    # Check required config values
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


def load_env_file() -> dict:
    """Load environment variables from config.yaml for subprocess calls."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR)
    env["PYTHONUNBUFFERED"] = "1"  # Ensure unbuffered output for real-time logs
    
    # Populate environment from config.yaml (not from .env file)
    env["ADO_ORG_URL"] = config.ado_org_url or ""
    env["ADO_ORG_NAME"] = config.ado_org_name or ""
    env["ADO_PROJECT"] = config.ado_project or ""
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
    
    return env


def stream_output(pipe: IO, prefix: str) -> None:
    """
    Stream output from a subprocess pipe to console.
    Runs in a background thread.
    
    Args:
        pipe: Subprocess stdout/stderr pipe
        prefix: Prefix to add to each line (e.g., "[PM_AGENT]")
    """
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            line = line.rstrip('\n\r')
            if line:
                # Print to console with prefix (goes to container stdout)
                print(f"{prefix} {line}", flush=True)
    except Exception as e:
        print(f"[LOG_ERROR] {prefix}: {e}", flush=True)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def start_pm_agent(env: dict) -> subprocess.Popen:
    """Start PM Agent server on port 10005 with real-time log streaming."""
    python = get_python()
    
    # Start process with pipes for real-time streaming
    proc = subprocess.Popen(
        [python, "-m", "agents.pm_agent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT_DIR),
        env=env,
        text=True,
        bufsize=1,
    )
    
    # Start threads to stream stdout and stderr
    stdout_thread = threading.Thread(
        target=stream_output,
        args=(proc.stdout, "[PM_AGENT]"),
        daemon=True
    )
    stderr_thread = threading.Thread(
        target=stream_output,
        args=(proc.stderr, "[PM_AGENT]"),
        daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    
    print(f"  PM Agent PID: {proc.pid}")
    
    # Wait briefly for startup
    time.sleep(2)
    if proc.poll() is not None:
        print(f"✗ PM Agent exited with code {proc.returncode}")
    else:
        print("✓ PM Agent starting on port 10005")
    
    return proc


def start_streamlit(env: dict) -> subprocess.Popen:
    """Start Streamlit UI on port 8501 with real-time log streaming."""
    python = get_python()
    
    cmd = [
        python, "-m", "streamlit", "run", str(APP_PATH),
        "--server.port", "8501",
        "--server.headless", "true",
        "--server.address", "0.0.0.0",  # Bind to all interfaces for K8s
        "--logger.level", "info"
    ]
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT_DIR),
        env=env,
        text=True,
        bufsize=1
    )
    
    # Start thread to stream output
    stdout_thread = threading.Thread(
        target=stream_output,
        args=(proc.stdout, "[STREAMLIT]"),
        daemon=True
    )
    stdout_thread.start()
    
    print(f"  Streamlit PID: {proc.pid}")
    return proc


def wait_for_shutdown(pm_proc: subprocess.Popen, st_proc: subprocess.Popen) -> None:
    """Keep the main thread alive while log streaming threads run."""
    print("\n📋 Logs streaming to stdout (Ctrl+C or SIGTERM to stop)...")
    print("-" * 60)
    
    try:
        while True:
            # Check if processes are still alive
            if pm_proc.poll() is not None:
                print(f"[WARNING] PM Agent exited with code {pm_proc.returncode}")
            if st_proc.poll() is not None:
                print(f"[WARNING] Streamlit exited with code {st_proc.returncode}")
            
            # If both died, exit
            if pm_proc.poll() is not None and st_proc.poll() is not None:
                print("[ERROR] Both processes have exited!")
                break
            
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n✓ Received shutdown signal...")
    
    # Graceful shutdown
    print("Terminating processes...")
    for proc in [pm_proc, st_proc]:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print("✓ All processes stopped")


def main():
    parser = argparse.ArgumentParser(description="Start PM-Agent application (K8s compatible)")
    parser.add_argument("--no-logs", action="store_true", help="Don't stream logs to stdout")
    args = parser.parse_args()
    
    print("=" * 60)
    print("🚀 PM-Agent Startup (K8s/AKS Mode)")
    print("=" * 60)
    
    # Step 1: Check credentials
    print("\n[1/3] Checking credentials...")
    if not check_env():
        print("\n⚠ Configure credentials and retry")
        sys.exit(1)
    
    # Step 2: Load environment
    print("\n[2/3] Loading environment...")
    env = load_env_file()
    print(f"✓ Environment loaded (LOG_LEVEL={env.get('LOG_LEVEL', 'INFO')})")
    
    # Step 3: Start services
    print("\n[3/3] Starting services...")
    
    print("  Starting PM Agent...")
    pm_proc = start_pm_agent(env)
    
    print("  Starting Streamlit...")
    st_proc = start_streamlit(env)
    time.sleep(2)
    
    # Summary
    print("\n" + "=" * 60)
    print("✓ Application running!")
    print("=" * 60)
    print(f"""
PM Agent:  http://0.0.0.0:10005  (PID: {pm_proc.pid})
Streamlit: http://0.0.0.0:8501   (PID: {st_proc.pid})

All logs are streaming to stdout for container log collection.
Send SIGTERM or Ctrl+C to gracefully stop.
""")
    
    # Keep running and streaming logs (unless --no-logs)
    if not args.no_logs:
        wait_for_shutdown(pm_proc, st_proc)
    else:
        print("Running in background mode (--no-logs). Logs written to files only.")


if __name__ == "__main__":
    main()
