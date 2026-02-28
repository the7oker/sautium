"""
Git-based auto-updater for Music AI DJ.

The application is a git clone. Updates are done via git pull.
After update: check requirements.txt changes, run pip install if needed,
run pending DB migrations, restart backend+tracker.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional, Callable, Tuple, List

logger = logging.getLogger(__name__)


def _git_cmd(args: list, cwd: Optional[str] = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command."""
    cmd = ["git"] + args
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "cwd": cwd or str(Path(__file__).parent.parent),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def get_project_root() -> Path:
    """Get the project root directory (git repo root)."""
    return Path(__file__).parent.parent


def is_git_repo() -> bool:
    """Check if the project is a git repository."""
    result = _git_cmd(["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def check_for_updates() -> Tuple[bool, int, str]:
    """
    Check if updates are available.

    Returns:
        (has_updates, commit_count, current_hash)
    """
    if not is_git_repo():
        return False, 0, ""

    # Fetch latest from remote
    result = _git_cmd(["fetch", "origin", "main"], timeout=30)
    if result.returncode != 0:
        logger.warning(f"git fetch failed: {result.stderr}")
        return False, 0, ""

    # Get current HEAD
    current = _git_cmd(["rev-parse", "HEAD"])
    current_hash = current.stdout.strip() if current.returncode == 0 else ""

    # Count commits ahead of us
    result = _git_cmd(["rev-list", "HEAD..origin/main", "--count"])
    if result.returncode != 0:
        return False, 0, current_hash

    count = int(result.stdout.strip())
    return count > 0, count, current_hash


def get_update_changelog(old_hash: str) -> List[str]:
    """Get commit messages between old hash and current HEAD."""
    result = _git_cmd(["log", "--oneline", f"{old_hash}..HEAD"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def requirements_changed(old_hash: str) -> bool:
    """Check if requirements.txt changed between old hash and current HEAD."""
    result = _git_cmd(["diff", "--name-only", old_hash, "HEAD"])
    if result.returncode != 0:
        return False
    changed_files = result.stdout.strip().split("\n")
    return any("requirements" in f for f in changed_files)


def has_new_migrations(old_hash: str) -> bool:
    """Check if new migration files were added since old_hash."""
    result = _git_cmd(["diff", "--name-only", "--diff-filter=A", old_hash, "HEAD"])
    if result.returncode != 0:
        return False
    added_files = result.stdout.strip().split("\n")
    return any("migrations/" in f and f.endswith(".sql") for f in added_files)


def pull_updates() -> Tuple[bool, str]:
    """
    Pull updates from origin/main.

    Returns:
        (success, old_hash before pull)
    """
    # Save current hash
    current = _git_cmd(["rev-parse", "HEAD"])
    old_hash = current.stdout.strip() if current.returncode == 0 else ""

    # Pull
    result = _git_cmd(["pull", "origin", "main"], timeout=120)
    if result.returncode != 0:
        logger.error(f"git pull failed: {result.stderr}")
        return False, old_hash

    logger.info(f"git pull successful: {result.stdout.strip()}")
    return True, old_hash


def install_requirements(progress_cb: Optional[Callable] = None) -> bool:
    """Run pip install -r requirements.txt for the backend."""
    if progress_cb:
        progress_cb("Installing updated dependencies...")

    project_root = get_project_root()
    req_file = project_root / "backend" / "requirements.txt"
    if not req_file.exists():
        return True

    python = sys.executable
    cmd = [python, "-m", "pip", "install", "-r", str(req_file), "--quiet"]

    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": 300,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        logger.error(f"pip install failed: {result.stderr}")
        return False

    logger.info("Dependencies updated")
    return True


def perform_update(
    service_manager,
    config: dict,
    progress_cb: Optional[Callable] = None,
) -> Tuple[bool, List[str]]:
    """
    Full update sequence:
    1. Stop backend + tracker (keep PostgreSQL)
    2. git pull
    3. pip install if requirements changed
    4. Run migrations if new ones exist
    5. Restart backend + tracker

    Returns:
        (success, changelog_lines)
    """
    if progress_cb:
        progress_cb("Stopping services for update...")

    # Stop backend and tracker
    service_manager.stop_tracker()
    service_manager.stop_backend()

    # Pull
    if progress_cb:
        progress_cb("Downloading updates...")

    success, old_hash = pull_updates()
    if not success:
        # Restart services even if pull failed
        service_manager.start_backend(progress_cb)
        service_manager.start_tracker(progress_cb)
        return False, []

    changelog = get_update_changelog(old_hash)

    # Check if requirements changed
    if requirements_changed(old_hash):
        if not install_requirements(progress_cb):
            logger.warning("pip install failed, continuing anyway")

    # Check for new migrations
    if has_new_migrations(old_hash):
        if progress_cb:
            progress_cb("Running database migrations...")
        try:
            from desktop.db_init import run_migrations
            password = config.get("postgres_password", "changeme")
            port = config.get("ports", {}).get("postgres", 5432)
            run_migrations(password, port=port, progress_cb=progress_cb)
        except Exception as e:
            logger.error(f"Migration after update failed: {e}")

    # Restart services
    if progress_cb:
        progress_cb("Restarting services...")

    service_manager.start_backend(progress_cb)
    service_manager.start_tracker(progress_cb)

    if progress_cb:
        progress_cb("Update complete!")

    return True, changelog
