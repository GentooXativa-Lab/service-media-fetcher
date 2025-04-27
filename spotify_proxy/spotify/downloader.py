# spotify_proxy/spotify/downloader.py

"""Spotify Downloader module"""

import asyncio
import os
import re  # Import re for sanitization
import shutil  # Import shutil for finding executable
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional, Union

import spotipy
import structlog
from spotipy.oauth2 import SpotifyClientCredentials

# Importar para type hinting y asegurar que las dependencias estén claras
from spotify_proxy.config.config import SpotifyConfig
from spotify_proxy.mqtt.client import MQTTAdapter
from spotify_proxy.mqtt.models import SpotifyRequest

logger = structlog.get_logger(__name__).bind(component="SpotifyDownloader")


class DownloadStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # Added for explicit cancellation


@dataclass
class DownloadTask:
    request: SpotifyRequest
    status: DownloadStatus = DownloadStatus.PENDING
    progress: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    last_log_line_ts: Optional[float] = None
    spotify_playlist_name: Optional[str] = None
    process: Optional[subprocess.Popen] = field(
        default=None, repr=False
    )  # Store the Popen object
    thread: Optional[threading.Thread] = field(
        default=None, repr=False
    )  # Store the thread running the task
    spotdl_command: Optional[List[str]] = field(
        default=None, repr=False
    )  # Store command for restarts/retries
    # --- NUEVO CAMPO ---
    final_status_published: bool = field(
        default=False, init=False
    )  # Track if terminal state published


class SpotifyDownloader:
    """Spotify Downloader class using threading and subprocess"""

    # Timeouts
    last_log_line_timeout: int = 60
    process_wait_timeout: int = 60
    terminate_wait_timeout: float = 5.0

    def __init__(self, config: SpotifyConfig):
        """Initialize Spotify downloader"""
        self.config = config
        self.downloads: Dict[str, DownloadTask] = {}
        self._lock = threading.RLock()
        self._status_update_thread: Optional[threading.Thread] = None
        self._slot_monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._mqtt_adapter: MQTTAdapter | None = None
        self._topic: str | None = None

        if not self.config.client_id or not self.config.client_secret:
            raise ValueError("Spotify client ID and secret must be provided")

        client_credentials = SpotifyClientCredentials(
            client_id=self.config.client_id,
            client_secret=self.config.client_secret,
        )
        self.sp = spotipy.Spotify(
            client_credentials_manager=client_credentials
        )

        dest_folder = self.config.destination_folder
        dest_path = Path(dest_folder)
        if not dest_path.exists():
            try:
                dest_path.mkdir(parents=True, exist_ok=True)
                logger.info("Created destination folder", path=dest_folder)
            except OSError as e:
                logger.error(
                    "Failed to create destination folder",
                    path=dest_folder,
                    error=str(e),
                )
                raise FileNotFoundError(
                    f"Destination folder {dest_folder} does not exist and could not be created"
                ) from e

        logger.info(
            "Spotify downloader initialized (using threading/subprocess)",
            download_path=self.config.destination_folder,
            max_concurrent_downloads=self.config.max_concurrent_downloads,
        )

    def _get_task_id(self, request: SpotifyRequest) -> str:
        """Generate a unique task ID for a download request"""
        return f"{request.channel_id}_{request.message_id}"

    def _terminate_process(
        self, process: Optional[subprocess.Popen], task_id: str
    ):
        """Attempts to terminate and then kill a subprocess.Popen process."""
        if process and process.poll() is None:
            pid = process.pid
            logger.warning(
                "Attempting to terminate process", task_id=task_id, pid=pid
            )
            try:
                process.terminate()
                try:
                    process.wait(timeout=self.terminate_wait_timeout)
                    logger.info(
                        "Process terminated successfully",
                        task_id=task_id,
                        pid=pid,
                        return_code=process.returncode,
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Process did not terminate gracefully, killing.",
                        task_id=task_id,
                        pid=pid,
                    )
                    process.kill()
                    try:
                        process.wait(timeout=self.terminate_wait_timeout / 2)
                        logger.info(
                            "Process killed successfully",
                            task_id=task_id,
                            pid=pid,
                            return_code=process.returncode,
                        )
                    except subprocess.TimeoutExpired:
                        logger.error(
                            "Process failed to exit even after kill",
                            task_id=task_id,
                            pid=pid,
                        )
                    except Exception as kill_wait_err:
                        logger.error(
                            "Error waiting after process kill",
                            task_id=task_id,
                            pid=pid,
                            error=str(kill_wait_err),
                        )
                except Exception as term_wait_err:
                    logger.error(
                        "Error waiting after process terminate",
                        task_id=task_id,
                        pid=pid,
                        error=str(term_wait_err),
                    )
            except ProcessLookupError:
                logger.warning(
                    "Process already finished when termination/kill was attempted.",
                    task_id=task_id,
                    pid=pid,
                )
            except Exception as term_err:
                logger.error(
                    "Error during process termination/kill attempt",
                    task_id=task_id,
                    pid=pid,
                    error=str(term_err),
                )
        elif process and process.poll() is not None:
            logger.debug(
                "Process already terminated before explicit call",
                task_id=task_id,
                pid=process.pid,
                return_code=process.returncode,
            )

    def _read_stream_output(
        self,
        stream,
        task_id: str,
        stream_name: str,
        output_queue: Optional[Queue] = None,  # noqa: ARG002 - Not used currently
    ):
        """Reads output from a stream (stdout/stderr)."""
        try:
            for line in iter(stream.readline, ""):
                if self._stop_event.is_set():
                    logger.warning(
                        f"{stream_name} reader stopping due to shutdown signal.",
                        task_id=task_id,
                    )
                    break

                line_str = line.strip()
                if line_str:
                    # Update timestamp directly as this runs in the task thread
                    with self._lock:
                        if task_id in self.downloads:
                            self.downloads[
                                task_id
                            ].last_log_line_ts = time.time()
                            if stream_name == "stdout":
                                logger.debug(
                                    "spotdl output",
                                    line=line_str,
                                    task_id=task_id,
                                )
                            else:  # stderr
                                logger.warning(
                                    "spotdl stderr",
                                    line=line_str,
                                    task_id=task_id,
                                )
                                current_error = self.downloads[task_id].error
                                err_line = f"stderr: {line_str}"
                                # Append stderr lines to error message if not already failed
                                if (
                                    self.downloads[task_id].status
                                    != DownloadStatus.FAILED
                                    and self.downloads[task_id].status
                                    != DownloadStatus.CANCELLED
                                ):
                                    if current_error:
                                        self.downloads[
                                            task_id
                                        ].error = (
                                            f"{current_error}; {err_line}"
                                        )
                                    else:
                                        self.downloads[
                                            task_id
                                        ].error = err_line
        except ValueError:
            logger.debug(
                f"{stream_name} stream closed during read.", task_id=task_id
            )
        except Exception as e:
            logger.error(
                f"Error reading {stream_name}",
                task_id=task_id,
                error=str(e),
                exc_info=True,
            )
        finally:
            try:
                stream.close()
            except Exception:
                pass
            logger.debug(f"{stream_name} reader finished.", task_id=task_id)

    def _run_spotdl_command_sync(
        self, task_id: str, command: List[str]
    ) -> None:
        """Run spotdl command synchronously using subprocess.Popen in a thread"""
        process: Optional[subprocess.Popen] = None
        task_start_time = time.time()
        playlist_name = "UnknownPlaylist"
        return_code = -1

        try:
            with self._lock:
                if task_id not in self.downloads:
                    logger.error("Task not found at start", task_id=task_id)
                    return
                task = self.downloads[task_id]
                if task.status not in [
                    DownloadStatus.PENDING,
                    DownloadStatus.FAILED,  # Allow restart if failed
                ]:
                    logger.warning(
                        "Task already running or completed, cannot start.",
                        task_id=task_id,
                        status=task.status,
                    )
                    return

                task.status = DownloadStatus.DOWNLOADING
                task.started_at = task_start_time
                task.last_log_line_ts = task_start_time
                task.error = None
                task.process = None
                task.thread = threading.current_thread()
                task.final_status_published = False  # Reset flag on (re)start
                playlist_name = task.spotify_playlist_name or playlist_name

            download_dir = Path(self.config.destination_folder) / playlist_name
            download_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Ensured download sub-directory exists", path=str(download_dir)
            )

            # Find spotdl path (ensure correct path finding)
            venv_path = Path(sys.executable).parent
            spotdl_path_venv = venv_path / "spotdl"
            spotdl_path_scripts = (
                venv_path / "Scripts" / "spotdl.exe"
            )  # Windows venv Scripts folder
            spotdl_path_bin = (
                venv_path / "bin" / "spotdl"
            )  # Linux/macOS venv bin folder
            spotdl_path_exe = Path(shutil.which("spotdl") or "")

            if spotdl_path_venv.is_file():
                spotdl_path = spotdl_path_venv
            elif spotdl_path_scripts.is_file():  # Check Windows Scripts first
                spotdl_path = spotdl_path_scripts
            elif spotdl_path_bin.is_file():  # Then Linux/macOS bin
                spotdl_path = spotdl_path_bin
            elif spotdl_path_exe.is_file():
                spotdl_path = spotdl_path_exe
                logger.info(
                    "Found spotdl executable in system PATH",
                    path=str(spotdl_path),
                )
            else:
                paths_checked = [
                    str(p)
                    for p in [
                        spotdl_path_venv,
                        spotdl_path_scripts,
                        spotdl_path_bin,
                    ]
                ] + ["System PATH"]
                logger.error(
                    "spotdl executable not found", checked_paths=paths_checked
                )
                raise FileNotFoundError("spotdl executable not found")

            command[0] = str(spotdl_path)

            logger.info(
                "Starting sync download process",
                command=" ".join(
                    f'"{c}"' if " " in c else c for c in command
                ),  # Quote args with spaces
                cwd=str(download_dir),
                task_id=task_id,
            )

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(download_dir),
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
                start_new_session=(os.name != "nt"),
            )

            with self._lock:
                if task_id in self.downloads:
                    self.downloads[task_id].process = process
                else:
                    logger.warning(
                        "Task disappeared after process start", task_id=task_id
                    )
                    self._terminate_process(process, f"{task_id}_orphaned")
                    return

            logger.info("Process started", pid=process.pid, task_id=task_id)

            stdout_thread = threading.Thread(
                target=self._read_stream_output,
                args=(process.stdout, task_id, "stdout"),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._read_stream_output,
                args=(process.stderr, task_id, "stderr"),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            stdout_thread.join()
            stderr_thread.join()

            logger.debug(
                "Waiting for spotdl process exit",
                task_id=task_id,
                pid=process.pid,
            )
            while process.poll() is None:
                if self._stop_event.is_set():
                    logger.warning(
                        "Shutdown signalled, terminating process.",
                        task_id=task_id,
                        pid=process.pid,
                    )
                    self._terminate_process(process, task_id)
                    with self._lock:
                        if task_id in self.downloads:
                            task = self.downloads[task_id]
                            task.status = DownloadStatus.CANCELLED
                            task.error = "Cancelled due to shutdown"
                            task.completed_at = time.time()
                            task.final_status_published = (
                                False  # Ensure cancelled state is published
                            )
                    return
                time.sleep(0.2)

            return_code = process.wait()

            logger.info(
                "spotdl process finished",
                task_id=task_id,
                pid=process.pid,
                return_code=return_code,
            )

        except FileNotFoundError as e:
            logger.error("Setup error", error=str(e), task_id=task_id)
            with self._lock:
                if task_id in self.downloads:
                    task = self.downloads[task_id]
                    task.status = DownloadStatus.FAILED
                    task.error = f"Setup error: {e}"
                    task.completed_at = time.time()
                    task.final_status_published = (
                        False  # Ensure failed state published
                    )
        except Exception as e:
            logger.exception(
                "Error running sync spotdl command",
                error=str(e),
                task_id=task_id,
            )
            if process and process.poll() is None:
                self._terminate_process(process, task_id)
            with self._lock:
                if task_id in self.downloads:
                    task = self.downloads[task_id]
                    task.status = DownloadStatus.FAILED
                    exec_err = f"Execution error: {e!s}"
                    if not task.error or exec_err not in task.error:
                        task.error = f"{task.error or ''}; {exec_err}".strip(
                            "; "
                        )
                    task.completed_at = time.time()
                    task.final_status_published = (
                        False  # Ensure failed state published
                    )
        finally:
            # Final Status Update
            with self._lock:
                if task_id in self.downloads:
                    task = self.downloads[task_id]
                    # Only update status if it's still 'DOWNLOADING'
                    # (prevents overwriting CANCELLED status set by shutdown signal)
                    if task.status == DownloadStatus.DOWNLOADING:
                        if return_code == 0:
                            task.status = DownloadStatus.COMPLETED
                            task.progress = 100
                            task.error = None  # Clear error on success
                        else:
                            task.status = DownloadStatus.FAILED
                            err_msg = f"Process exited with non-zero code: {return_code}"
                            if task.error and "stderr:" in task.error:
                                if str(return_code) not in task.error:
                                    task.error += f" ({err_msg})"
                            elif not task.error:
                                task.error = err_msg
                            logger.error(
                                "Download failed",
                                task_id=task_id,
                                pid=getattr(process, "pid", "N/A"),
                                return_code=return_code,
                                final_error=task.error,
                            )
                        task.completed_at = time.time()
                        # IMPORTANT: Reset flag here so status update loop publishes final state
                        task.final_status_published = False

                    # Clear process and thread references
                    task.process = None
                    task.thread = None
                else:
                    logger.warning(
                        "Task disappeared before final status update",
                        task_id=task_id,
                    )

    def get_download_status(
        self, task_id: str
    ) -> Optional[Dict[str, Union[str, int, float, bool, None]]]:
        """Get the status of a download task"""
        with self._lock:
            if task_id not in self.downloads:
                return None
            task = self.downloads[task_id]
            return {
                "status": task.status.value,
                "progress": task.progress,
                "last_log_line_ts": task.last_log_line_ts,
                "spotify_playlist_name": task.spotify_playlist_name,
                "message_id": task.request.message_id,
                "channel_id": task.request.channel_id,
                "download_id": task.request.download_id,
                "url": task.request.download_url,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
                "error": task.error,
                "pid": task.process.pid if task.process else None,
                # Optionally include the flag for debugging, but not strictly needed for MQTT payload
                # "final_status_published": task.final_status_published,
            }

    def get_all_active_downloads(
        self,
    ) -> Dict[str, Dict[str, Union[str, int, float, bool, None]]]:
        """Get the status of all active (pending or downloading) downloads"""
        result = {}
        now = time.time()
        with self._lock:
            for task_id, task in self.downloads.items():
                if task.status in [
                    DownloadStatus.PENDING,
                    DownloadStatus.DOWNLOADING,
                ]:
                    is_stuck = False
                    if (
                        task.status == DownloadStatus.DOWNLOADING
                        and task.last_log_line_ts is not None
                        and (now - task.last_log_line_ts)
                        > self.last_log_line_timeout
                    ):
                        is_stuck = True
                    status_info = self.get_download_status(task_id)
                    if status_info:
                        status_info["is_stuck"] = is_stuck
                        result[task_id] = status_info
        return result

    def cleanup_completed_downloads(self, max_age_seconds: int = 3600) -> None:
        """Clean up COMPLETED, FAILED, or CANCELLED downloads older than max_age_seconds,
        REGARDLESS of the final_status_published flag."""
        now = time.time()
        to_remove = []
        with self._lock:
            to_remove = [
                task_id
                for task_id, task in self.downloads.items()
                if task.status
                in [
                    DownloadStatus.COMPLETED,
                    DownloadStatus.FAILED,
                    DownloadStatus.CANCELLED,
                ]
                and task.completed_at is not None
                and (now - task.completed_at) > max_age_seconds
            ]

            if to_remove:
                logger.info(
                    "Cleaning up old tasks",
                    count=len(to_remove),
                    task_ids=to_remove,
                )
                for task_id in to_remove:
                    removed_task = self.downloads.pop(task_id, None)
                    if removed_task:
                        logger.debug(
                            "Removed task entry from memory", task_id=task_id
                        )
                    else:
                        logger.warning(
                            "Attempted to remove task already gone",
                            task_id=task_id,
                        )

    def get_download_slot_usage(self) -> Dict[str, int]:
        """Get current download slot usage information"""
        with self._lock:
            downloading_count = 0
            pending_count = 0
            for task in self.downloads.values():
                if task.status == DownloadStatus.DOWNLOADING:
                    downloading_count += 1
                elif task.status == DownloadStatus.PENDING:
                    pending_count += 1

            total_slots = self.config.max_concurrent_downloads
            used_slots = downloading_count
            free_slots = max(0, total_slots - used_slots)

            return {
                "total": total_slots,
                "used": used_slots,
                "pending": pending_count,
                "free": free_slots,
            }

    def _process_pending_downloads(self) -> None:
        """Processes pending downloads if slots are available."""
        slot_usage = self.get_download_slot_usage()
        available_slots = slot_usage["free"]

        if available_slots <= 0 or slot_usage["pending"] == 0:
            return

        tasks_to_start: List[str] = []
        with self._lock:
            pending_ids = [
                task_id
                for task_id, task in self.downloads.items()
                if task.status == DownloadStatus.PENDING
            ]
            tasks_to_start = pending_ids[:available_slots]

        if not tasks_to_start:
            return

        logger.info(
            f"Found {len(tasks_to_start)} pending tasks to start.",
            task_ids=tasks_to_start,
            available_slots=available_slots,
        )

        for task_id in tasks_to_start:
            command_to_run = None
            with self._lock:
                if (
                    task_id in self.downloads
                    and self.downloads[task_id].status
                    == DownloadStatus.PENDING
                ):
                    task = self.downloads[task_id]
                    if task.spotdl_command:
                        command_to_run = task.spotdl_command
                    else:
                        logger.error(
                            "No command found for pending task, cannot start.",
                            task_id=task_id,
                        )
                        task.status = DownloadStatus.FAILED
                        task.error = (
                            "Internal error: Missing command for queued task."
                        )
                        task.completed_at = time.time()
                        task.final_status_published = (
                            False  # Ensure failure state is published
                        )
                else:
                    logger.warning(
                        "Pending task status changed before starting, skipping.",
                        task_id=task_id,
                    )

            if command_to_run:
                logger.info("Starting queued download task", task_id=task_id)
                thread = threading.Thread(
                    target=self._run_spotdl_command_sync,
                    args=(task_id, command_to_run),
                    name=f"spotdl_dl_{task_id}",
                    daemon=True,
                )
                thread.start()

    def _run_slot_monitor(self, interval_seconds: int) -> None:
        """Periodically logs slot usage and triggers processing of pending downloads."""
        logger.info("Slot monitor thread started", interval=interval_seconds)
        try:
            while not self._stop_event.is_set():
                stopped = self._stop_event.wait(timeout=interval_seconds)
                if stopped:
                    break

                try:
                    slot_usage = self.get_download_slot_usage()
                    logger.info(
                        "Download slots usage",
                        total=slot_usage["total"],
                        used=slot_usage["used"],
                        pending=slot_usage["pending"],
                        free=slot_usage["free"],
                    )

                    if slot_usage["free"] > 0 and slot_usage["pending"] > 0:
                        self._process_pending_downloads()

                except Exception as e:
                    logger.error(
                        "Error in slot monitor iteration",
                        error=str(e),
                        exc_info=True,
                    )
                    if not self._stop_event.wait(timeout=5):
                        continue
                    else:
                        break

        except Exception as e:
            logger.exception(
                "Fatal error in slot monitor thread", error=str(e)
            )
        finally:
            logger.info("Slot monitor thread stopped.")

    def start_slot_monitor(
        self, interval_seconds: Optional[int] = None
    ) -> None:
        """Start the periodic download slot monitoring thread."""
        if self._slot_monitor_thread and self._slot_monitor_thread.is_alive():
            logger.warning("Slot monitor thread is already running.")
            return
        monitor_interval = (
            interval_seconds or self.config.slot_monitor_interval_seconds
        )
        self._slot_monitor_thread = threading.Thread(
            target=self._run_slot_monitor,
            args=(monitor_interval,),
            name="SlotMonitor",
            daemon=False,  # Make it non-daemon to ensure it's joined on shutdown
        )
        self._slot_monitor_thread.start()
        logger.info("Started slot monitor thread", interval=monitor_interval)

    def start_status_updates(
        self, mqtt_adapter: MQTTAdapter, topic: str, interval_seconds: int = 10
    ) -> None:
        """Start periodic status updates thread."""
        if (
            self._status_update_thread
            and self._status_update_thread.is_alive()
        ):
            logger.warning("Status updates thread is already running.")
            return

        self._mqtt_adapter = mqtt_adapter
        self._topic = topic

        self._status_update_thread = threading.Thread(
            target=self._run_status_updates,
            args=(interval_seconds,),
            name="StatusUpdater",
            daemon=False,  # Non-daemon to allow cleanup during shutdown
        )
        self._status_update_thread.start()
        logger.info("Started status updates thread", interval=interval_seconds)
        self.start_slot_monitor()  # Start slot monitor alongside status updates

    def stop_all_tasks(self) -> None:
        """Signals all background threads and attempts cleanup."""
        if not self._stop_event.is_set():
            logger.info(
                "Initiating shutdown of downloader tasks and threads..."
            )
            self._stop_event.set()

            threads_to_join: List[threading.Thread] = []
            processes_to_terminate: List[subprocess.Popen] = []

            with self._lock:
                logger.info(
                    f"Found {len(self.downloads)} tasks to potentially stop."
                )
                for task_id, task in self.downloads.items():
                    if (
                        task.status == DownloadStatus.DOWNLOADING
                        and task.process
                        and task.process.poll() is None
                    ):
                        logger.warning(
                            f"Requesting termination for running download process {task.process.pid}",
                            task_id=task_id,
                        )
                        processes_to_terminate.append(task.process)
                        # Don't set status here, let _run_spotdl handle final state if possible,
                        # or the shutdown signal check within _run_spotdl
            # Terminate processes outside the lock
            for process in processes_to_terminate:
                try:
                    self._terminate_process(
                        process, f"task_pid_{process.pid}_shutdown"
                    )
                except Exception as e:
                    logger.error(
                        f"Error terminating process {process.pid} during shutdown",
                        error=str(e),
                    )

            # Join background threads
            if (
                self._status_update_thread
                and self._status_update_thread.is_alive()
            ):
                threads_to_join.append(self._status_update_thread)
            if (
                self._slot_monitor_thread
                and self._slot_monitor_thread.is_alive()
            ):
                threads_to_join.append(self._slot_monitor_thread)

            # Join downloader threads (only if non-daemon, though currently daemon=True)
            with self._lock:
                for task in self.downloads.values():
                    if (
                        task.thread
                        and task.thread.is_alive()
                        and not task.thread.daemon
                    ):
                        if task.thread not in threads_to_join:
                            threads_to_join.append(task.thread)

            join_timeout = 10  # seconds
            logger.info(
                f"Waiting up to {join_timeout}s for {len(threads_to_join)} background threads to join..."
            )
            start_join_time = time.time()
            current_thread = (
                threading.current_thread()
            )  # Get current thread once
            for thread in threads_to_join:
                if thread == current_thread:  # Avoid joining self
                    continue
                remaining_time = max(
                    0, join_timeout - (time.time() - start_join_time)
                )
                if remaining_time <= 0:
                    logger.warning(
                        f"Timeout reached before joining thread {thread.name}"
                    )
                    break  # Stop waiting if timeout exceeded
                try:
                    thread.join(timeout=remaining_time)
                    if thread.is_alive():
                        logger.warning(
                            f"Thread {thread.name} did not join within timeout."
                        )
                    else:
                        logger.debug(f"Thread {thread.name} joined.")
                except Exception as e:
                    logger.error(
                        f"Error joining thread {thread.name}", error=str(e)
                    )

            self._status_update_thread = None
            self._slot_monitor_thread = None
            logger.info("Downloader task shutdown process complete.")
        else:
            logger.info("Downloader shutdown already initiated.")

    async def stop_status_updates(self) -> None:
        """Stops the status update and slot monitor threads."""
        logger.info("Stopping status updates and related threads...")
        # Run the synchronous stop logic in a separate thread to avoid blocking asyncio event loop
        await asyncio.to_thread(self.stop_all_tasks)
        await asyncio.sleep(0.1)  # Short pause

    def _run_status_updates(self, interval_seconds: int) -> None:
        """Periodically checks download status and publishes updates via MQTT (using thread-safe calls)."""
        if not self._mqtt_adapter or not self._topic:
            logger.error(
                "MQTT adapter or topic not set. Exiting status updates."
            )
            return
        if not hasattr(
            self._mqtt_adapter, "publish_spotify_response_threadsafe"
        ):
            logger.error(
                "MQTT adapter lacks 'publish_spotify_response_threadsafe'. Exiting."
            )
            return

        logger.info("Status update thread started.")
        try:
            while not self._stop_event.is_set():
                stopped = self._stop_event.wait(timeout=interval_seconds)
                if stopped:
                    logger.debug("Stop event set, exiting status update loop.")
                    break

                try:
                    now = time.time()
                    tasks_to_publish: Dict[str, Dict[str, Any]] = {}
                    stuck_tasks_terminated: List[str] = []
                    tasks_to_mark_published: List[
                        str
                    ] = []  # Tasks whose final state was just collected

                    with self._lock:
                        # Check for stuck processes
                        for task_id, task in list(self.downloads.items()):
                            if (
                                task.status == DownloadStatus.DOWNLOADING
                                and task.last_log_line_ts is not None
                                and (now - task.last_log_line_ts)
                                > self.last_log_line_timeout
                                and task.process
                                and task.process.poll() is None
                            ):
                                logger.warning(
                                    "Download stuck, flagging for termination.",
                                    task_id=task_id,
                                    pid=task.process.pid,
                                )
                                stuck_tasks_terminated.append(task_id)
                                task.status = DownloadStatus.FAILED
                                task.error = f"Process stuck (no output for > {self.last_log_line_timeout}s)"
                                task.completed_at = now
                                task.final_status_published = (
                                    False  # Mark FAILED state to be published
                                )

                        # Collect status for tasks whose final state hasn't been published
                        for task_id, task in self.downloads.items():
                            if (
                                not task.final_status_published
                            ):  # Check the flag
                                status_payload = self.get_download_status(
                                    task_id
                                )
                                if status_payload:
                                    tasks_to_publish[task_id] = status_payload
                                    # If this task is now terminal, mark it to set the flag later
                                    if task.status in [
                                        DownloadStatus.COMPLETED,
                                        DownloadStatus.FAILED,
                                        DownloadStatus.CANCELLED,
                                    ]:
                                        tasks_to_mark_published.append(task_id)

                        # Set the flag for tasks that were just collected and are terminal
                        if tasks_to_mark_published:
                            logger.debug(
                                "Marking terminal tasks as published",
                                task_ids=tasks_to_mark_published,
                            )
                            for task_id_to_mark in tasks_to_mark_published:
                                if (
                                    task_id_to_mark in self.downloads
                                ):  # Re-check existence
                                    self.downloads[
                                        task_id_to_mark
                                    ].final_status_published = True

                    # Terminate stuck processes (outside lock)
                    if stuck_tasks_terminated:
                        logger.info(
                            "Terminating stuck tasks",
                            task_ids=stuck_tasks_terminated,
                        )
                        processes_to_kill = []
                        with self._lock:  # Get process objects safely
                            processes_to_kill = [
                                self.downloads[tid].process
                                for tid in stuck_tasks_terminated
                                if tid in self.downloads
                                and self.downloads[tid].process
                            ]
                        for proc in processes_to_kill:
                            self._terminate_process(
                                proc, f"task_pid_{proc.pid}_stuck"
                            )

                    # Publish status updates using thread-safe method
                    if tasks_to_publish:
                        logger.debug(
                            f"Scheduling {len(tasks_to_publish)} status updates..."
                        )
                        for task_id, payload in tasks_to_publish.items():
                            try:
                                self._mqtt_adapter.publish_spotify_response_threadsafe(
                                    self._topic, payload
                                )
                            except Exception as schedule_err:
                                logger.error(
                                    "Failed to schedule status update",
                                    task_id=task_id,
                                    error=str(schedule_err),
                                    exc_info=True,
                                )

                    # Clean up old downloads (based on age, not the flag)
                    self.cleanup_completed_downloads()

                except Exception as loop_err:
                    logger.error(
                        "Error in status update loop iteration",
                        error=str(loop_err),
                        exc_info=True,
                    )
                    if not self._stop_event.wait(timeout=10):
                        continue
                    else:
                        break  # Exit if stopped during error recovery

        except Exception as outer_err:
            logger.exception(
                "Fatal error in status update thread", error=str(outer_err)
            )
        finally:
            logger.info("Status update thread stopped.")

    def process_download_request(
        self, request: SpotifyRequest
    ) -> Optional[Dict[str, Union[str, int, float, bool, None]]]:
        """Processes a download request, validates, prepares, and schedules/starts the download thread."""
        logger.info(
            "Processing download request",
            user_id=request.requested_by,
            channel_id=request.channel_id,
            url=request.download_url,
            message_id=request.message_id,
            download_id=request.download_id,
        )
        task_id = self._get_task_id(request)

        with self._lock:
            if task_id in self.downloads:
                existing_task = self.downloads[task_id]
                if existing_task.status in [
                    DownloadStatus.PENDING,
                    DownloadStatus.DOWNLOADING,
                ]:
                    logger.warning(
                        "Download task already active",
                        task_id=task_id,
                        status=existing_task.status.value,
                    )
                    return self.get_download_status(task_id)
                logger.info(
                    "Task exists but is finished/failed, allowing new attempt.",
                    task_id=task_id,
                    status=existing_task.status.value,
                )
                # Overwrite entry below, flag will be reset by _run_spotdl

        metadata: Optional[Dict[str, Any]] = None
        media_name = "UnknownMedia"
        playlist_name = "UnknownPlaylist"  # Sanitized name for folder/m3u

        try:
            # Fetch metadata
            url_type = None
            if "playlist" in request.download_url:
                url_type = "playlist"
                metadata = self.sp.playlist(request.download_url)
            elif "album" in request.download_url:
                url_type = "album"
                metadata = self.sp.album(request.download_url)
            elif "track" in request.download_url:
                url_type = "track"
                metadata = self.sp.track(request.download_url)
            else:
                raise ValueError(
                    "URL must be a Spotify playlist, album, or track"
                )

            if not metadata:
                raise ValueError("Unable to fetch metadata from Spotify")
            logger.debug("Successfully fetched metadata", url_type=url_type)

            # Determine and sanitize name for folder/m3u
            fetched_name = metadata.get("name")
            if url_type == "track":
                album_name = metadata.get("album", {}).get(
                    "name", "UnknownAlbum"
                )
                artists = metadata.get("artists", [])
                artist_name = (
                    artists[0].get("name", "UnknownArtist")
                    if artists
                    else "UnknownArtist"
                )
                track_name = metadata.get("name", "UnknownTrack")
                media_name = f"{artist_name} - {track_name}"  # Use detailed name for tracks
            else:
                media_name = fetched_name if fetched_name else "UnknownMedia"

            # Sanitize the name for use in paths
            # Remove invalid path characters, replace spaces, limit length
            invalid_path_chars = r'[<>:"/\\|?*\x00-\x1f]'  # Control chars too
            # sanitized_name = re.sub(
            #     r"\s+", "_", media_name
            # )  # Replace spaces first
            sanitized_name = re.sub(
                invalid_path_chars, "", media_name
            )  # Remove invalid chars
            sanitized_name = sanitized_name.strip(
                "._"
            )  # Remove leading/trailing dots/underscores
            max_len = 100  # Max path component length often limited
            sanitized_name = (
                sanitized_name[:max_len]
                if len(sanitized_name) > max_len
                else sanitized_name
            )
            if not sanitized_name:
                sanitized_name = (
                    f"Sanitized_{url_type}_{time.time_ns()}"  # Fallback
                )
            playlist_name = sanitized_name  # Use this sanitized name

            logger.info(
                "Using sanitized name for folder/m3u",
                original_name=media_name,
                sanitized_name=playlist_name,
            )

            # Prepare spotdl command
            spotdl_command_base = [
                "spotdl",  # Placeholder, replaced by actual path later
                "--client-id",
                self.config.client_id,
                "--client-secret",
                self.config.client_secret,
                "--output",
                ".",  # Output relative to cwd (which is set to playlist_name folder)
                "--format",
                self.config.format,
            ]
            # Add m3u only for playlists/albums, not single tracks
            if url_type != "track":
                spotdl_command_base.extend(["--m3u", f"{playlist_name}.m3u8"])

            # Optional Args
            # Example: Bitrate mapping might be needed based on format
            # if self.config.quality != 'best': spotdl_command_base.extend(["--bitrate", self.config.quality])
            if self.config.download_lyrics:
                spotdl_command_base.extend(
                    ["--lyrics", "genius", "--generate-lrc"]
                )  # Example

            spotdl_command_final = spotdl_command_base + [
                "download",
                request.download_url,
            ]
            spotdl_command = [str(arg) for arg in spotdl_command_final if arg]
            logger.debug(
                "Prepared spotdl command", command_args=spotdl_command
            )  # Log args, not joined string

            # Register Task and Decide Start
            start_immediately = False
            with self._lock:
                download_task = DownloadTask(
                    request=request,
                    spotify_playlist_name=playlist_name,  # Store sanitized name
                    spotdl_command=spotdl_command,
                )
                self.downloads[task_id] = download_task

                slot_usage = self.get_download_slot_usage()
                if slot_usage["free"] > 0:
                    logger.info(
                        "Slot available, starting download immediately.",
                        task_id=task_id,
                    )
                    start_immediately = True
                else:
                    logger.info(
                        "No free slots, queuing download.",
                        task_id=task_id,
                        used=slot_usage["used"],
                        total=slot_usage["total"],
                    )
                    # Status remains PENDING

            # Start Thread if necessary
            if start_immediately:
                logger.info(
                    "Starting download thread immediately.", task_id=task_id
                )
                thread = threading.Thread(
                    target=self._run_spotdl_command_sync,
                    args=(task_id, spotdl_command),
                    name=f"spotdl_dl_{task_id}",
                    daemon=True,  # Daemon=True allows main app to exit even if these hang (use with caution)
                )
                thread.start()

            # Return initial status (PENDING or DOWNLOADING if started immediately)
            return self.get_download_status(task_id)

        except (
            ValueError,
            spotipy.exceptions.SpotifyException,
            FileNotFoundError,
        ) as e:
            logger.error(
                "Setup or Spotify API error processing request",
                error=str(e),
                url=request.download_url,
                exc_info=True,
            )
            # Return a FAILED status immediately if setup fails
            failed_status = {
                "status": DownloadStatus.FAILED.value,
                "progress": 0,
                "message_id": request.message_id,
                "channel_id": request.channel_id,
                "download_id": request.download_id,
                "url": request.download_url,
                "error": f"Failed to start download: {e}",
                "final_status_published": False,  # Ensure this failure is published
                # Other fields can be None or default
                "last_log_line_ts": None,
                "spotify_playlist_name": playlist_name,
                "started_at": None,
                "completed_at": time.time(),
                "pid": None,
            }
            # Publish this immediate failure state? Yes, good idea.
            if self._mqtt_adapter and self._topic:
                try:
                    self._mqtt_adapter.publish_spotify_response_threadsafe(
                        self._topic, failed_status
                    )
                except Exception as pub_err:
                    logger.error(
                        "Failed to publish immediate setup failure",
                        error=str(pub_err),
                    )
            return failed_status  # Return the failure status directly
        except Exception as e:
            logger.exception(
                "Unexpected error during download request processing",
                error=str(e),
                url=request.download_url,
            )
            # Similar immediate failure reporting
            failed_status = {
                "status": DownloadStatus.FAILED.value,
                "progress": 0,
                "message_id": request.message_id,
                "channel_id": request.channel_id,
                "url": request.download_url,
                "error": f"Unexpected error: {e}",
                "final_status_published": False,
                "last_log_line_ts": None,
                "spotify_playlist_name": playlist_name,
                "started_at": None,
                "completed_at": time.time(),
                "pid": None,
            }
            if self._mqtt_adapter and self._topic:
                try:
                    self._mqtt_adapter.publish_spotify_response_threadsafe(
                        self._topic, failed_status
                    )
                except Exception as pub_err:
                    logger.error(
                        "Failed to publish unexpected setup failure",
                        error=str(pub_err),
                    )
            return failed_status  # Return the failure status


# Final closing brace for the class if needed (assuming it's part of the class structure)
# } # This might not be needed depending on indentation
