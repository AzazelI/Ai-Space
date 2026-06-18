import os
import re
import glob
import subprocess
import requests
from pathlib import Path
from config import WORKSPACE_DIR

def get_workspace_path() -> Path:
    """Returns the workspace path as a Path object."""
    return Path(WORKSPACE_DIR).resolve()

def list_files(pattern: str = None) -> list[str]:
    """
    Lists files in the workspace (excluding directories like .git, .obsidian, __pycache__, .claude).
    If pattern is specified, filters files matching that pattern.
    """
    workspace = get_workspace_path()
    all_files = []
    
    # Excluded directories
    exclude_dirs = {".git", ".obsidian", ".claude", "__pycache__", ".trash", "node_modules", "venv", ".venv"}
    
    for root, dirs, files in os.walk(workspace):
        # In-place modify dirs to skip excluded folders
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        
        for file in files:
            full_path = Path(root) / file
            rel_path = full_path.relative_to(workspace)
            rel_str = str(rel_path).replace("\\", "/")
            
            # Check pattern filter
            if pattern:
                try:
                    if not re.search(pattern, rel_str, re.IGNORECASE):
                        continue
                except re.error:
                    # Fallback to simple substring match if regex is invalid
                    if pattern.lower() not in rel_str.lower():
                        continue
            
            all_files.append(rel_str)
            
    return sorted(all_files)

# Files that must never be returned, even to an authorized user (defense in depth).
_SECRET_NAMES = {".env"}
_SECRET_SUFFIXES = (".env",)


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    if name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES):
        return True
    # Block anything resembling a key/secret store
    if any(tok in name for tok in ("secret", "credential", "_keys", "id_rsa")):
        return True
    return False


def read_file(rel_path: str) -> str:
    """
    Safely reads file contents from the workspace.
    Prevents directory traversal (boundary-correct) and refuses secret files.
    """
    workspace = get_workspace_path()
    safe_path = (workspace / rel_path).resolve()

    # Boundary-correct containment check (no sibling-prefix bypass like
    # "Aftesale_backup" satisfying startswith("Aftesale")).
    if not safe_path.is_relative_to(workspace):
        raise ValueError("Security violation: Attempt to read files outside of workspace.")

    # Never hand back secrets (.env, key stores) regardless of authorization.
    if _is_secret_file(safe_path):
        raise PermissionError("Refused: secret files cannot be read through the bot.")

    if not safe_path.exists():
        raise FileNotFoundError(f"File not found: {rel_path}")

    if safe_path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {rel_path}")

    # Read text file
    with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

def get_git_status() -> str:
    """Gets Git status for the workspace."""
    workspace = get_workspace_path()
    try:
        res = subprocess.run(
            ["git", "status", "-s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True
        )
        status = res.stdout.strip()
        if not status:
            return "Clean (No changes)"
        return status
    except Exception as e:
        return f"Error retrieving git status: {e}"

def check_backend_status() -> dict:
    """Checks the local FastAPI server status by pinging health and metrics endpoints."""
    results = {
        "running": False,
        "url": "http://localhost:8000",
        "health_endpoint": "unknown",
        "message": "Offline"
    }
    
    # Try /health
    try:
        res = requests.get("http://localhost:8000/health", timeout=2)
        if res.status_code == 200:
            results["running"] = True
            results["health_endpoint"] = "/health"
            results["message"] = "Online (Healthy)"
            return results
    except Exception:
        pass
        
    # Try /api/obd/metrics
    try:
        res = requests.get("http://localhost:8000/api/obd/metrics", timeout=2)
        if res.status_code == 200:
            results["running"] = True
            results["health_endpoint"] = "/api/obd/metrics"
            results["message"] = "Online (Telemetry Available)"
            return results
    except Exception:
        pass
        
    return results
