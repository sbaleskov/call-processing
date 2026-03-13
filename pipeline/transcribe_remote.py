"""
Remote audio transcription — offloads whisper to a server via SSH.

Flow:
  1. Kill any lingering worker from a previous run
  2. scp audio file → remote server
  3. ssh: start worker.py in background (nohup)
  4. Poll for completion (check if output file exists)
  5. scp transcription text ← back
  6. Cleanup remote temp files
"""

import logging
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Polling settings
POLL_INTERVAL = 30       # seconds between checks
MAX_POLL_TIME = 14400    # 240 minutes max wait (pyannote on CPU is slow)


def transcribe_audio_remote(file_path: str, config) -> Optional[str]:
    """
    Transcribe an audio file on a remote server via SSH + scp.

    Starts the worker in background (nohup) and polls for completion,
    avoiding long-running SSH sessions that break on flaky connections.

    Returns transcription text or None on error.
    """
    local_file = Path(file_path)
    if not local_file.exists():
        logger.error("File not found: %s", file_path)
        return None

    host = config.remote_host
    user = config.remote_user
    worker_dir = config.remote_worker_dir
    work_dir = config.remote_work_dir

    target = f"{user}@{host}"
    ssh_opts = _ssh_options(getattr(config, "remote_ssh_key", ""))

    python_bin = f"{worker_dir}/venv/bin/python3"
    worker_bin = f"{worker_dir}/worker.py"
    remote_audio = f"{work_dir}/{local_file.name}"
    remote_transcript = f"{work_dir}/{local_file.stem}_transcription.txt"
    remote_done = f"{work_dir}/.done"
    remote_log = f"{work_dir}/worker.log"

    try:
        # Kill any lingering worker from a previous timed-out run
        try:
            _ssh(target, ssh_opts,
                 "pkill -f 'worker.py /tmp/call-processing/' 2>/dev/null; sleep 1; "
                 f"rm -f {work_dir}/*.mp3 {work_dir}/*.txt {work_dir}/.done {work_dir}/worker.log {work_dir}/run.sh",
                 timeout=15)
        except Exception:
            pass  # non-critical

        # Ensure remote work dir exists
        _ssh(target, ssh_opts, f"mkdir -p {work_dir}")

        # Upload audio
        logger.info("Uploading %s to %s...", local_file.name, host)
        _scp_to(ssh_opts, str(local_file), f"{target}:{remote_audio}")

        # Build worker command and start in background via temp script
        # (avoids nested quoting issues with filenames containing spaces/quotes)
        worker_cmd = _worker_command(
            python_bin, worker_bin, remote_audio, remote_transcript, config,
        )
        remote_script = f"{work_dir}/run.sh"
        script_content = (
            f"#!/bin/sh\n"
            f"{worker_cmd} > {shlex.quote(remote_log)} 2>&1\n"
            f"touch {shlex.quote(remote_done)}\n"
        )
        _ssh(target, ssh_opts,
             f"cat > {shlex.quote(remote_script)} << 'WORKER_EOF'\n"
             f"{script_content}"
             f"WORKER_EOF\n"
             f"chmod +x {shlex.quote(remote_script)}",
             timeout=15)

        logger.info("Starting remote transcription on %s (background)...", host)
        _ssh(target, ssh_opts,
             f"nohup {shlex.quote(remote_script)} < /dev/null > /dev/null 2>&1 &",
             timeout=15)

        # Poll for completion
        elapsed = 0
        while elapsed < MAX_POLL_TIME:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                result = _ssh(target, ssh_opts,
                              f"test -f {shlex.quote(remote_done)} && echo DONE || echo WAIT",
                              timeout=15)
                if "DONE" in result:
                    logger.info("Transcription completed after %d min", elapsed // 60)
                    break
                # Check if worker is still running
                ps_result = _ssh(target, ssh_opts,
                                 "pgrep -f 'worker.py /tmp/call-processing/' > /dev/null 2>&1 "
                                 "&& echo RUNNING || echo STOPPED",
                                 timeout=15)
                if "STOPPED" in ps_result and "DONE" not in result:
                    # Worker died without producing .done — check log
                    try:
                        log_tail = _ssh(target, ssh_opts,
                                        f"tail -5 {shlex.quote(remote_log)} 2>/dev/null",
                                        timeout=10)
                        logger.error("Worker died. Log tail: %s", log_tail.strip())
                    except Exception:
                        logger.error("Worker died, couldn't read log")
                    return None
            except Exception as e:
                logger.warning("Poll failed (will retry): %s", e)
                # VPS temporarily unreachable — just retry
                continue
        else:
            logger.error("Transcription timed out after %d min", MAX_POLL_TIME // 60)
            try:
                _ssh(target, ssh_opts,
                     "pkill -f 'worker.py /tmp/call-processing/' 2>/dev/null",
                     timeout=10)
            except Exception:
                pass
            return None

        # Download result
        logger.info("Downloading transcription...")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            local_tmp = tmp.name
        _scp_from(ssh_opts, f"{target}:{remote_transcript}", local_tmp)
        transcription = Path(local_tmp).read_text(encoding="utf-8")
        Path(local_tmp).unlink(missing_ok=True)

        # Cleanup remote files (non-critical)
        try:
            _ssh(target, ssh_opts,
                 f"rm -f {shlex.quote(remote_audio)} {shlex.quote(remote_transcript)} "
                 f"{shlex.quote(remote_done)} {shlex.quote(remote_log)} "
                 f"{shlex.quote(remote_script)}",
                 timeout=10)
        except Exception as e:
            logger.warning("Remote cleanup failed (non-critical): %s", e)

        logger.info("Remote transcription complete (%d chars)", len(transcription))
        return transcription

    except Exception as e:
        logger.error("Remote transcription failed: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ssh_options(key_path: str = "") -> list:
    opts = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
    ]
    if key_path:
        opts.extend(["-i", key_path])
    return opts


def _worker_command(python_bin, worker_bin, audio_path, output_path, config) -> str:
    """Build the shell command to run worker.py on the remote server.

    Uses nice + thread limit to keep VPS responsive during transcription.
    """
    parts = [
        "nice", "-n", "15",
        shlex.quote(python_bin),
        shlex.quote(worker_bin),
        shlex.quote(audio_path),
        "-o", shlex.quote(output_path),
        "--model", shlex.quote(config.whisper_model_size),
        "--language", shlex.quote(config.language),
        "--compute-type", shlex.quote(config.whisper_compute_type),
        "--threads", "2",
    ]
    device = getattr(config, "whisper_device", "cpu")
    if device != "cpu":
        parts.extend(["--device", shlex.quote(device)])
    if getattr(config, "vad_filter", True):
        parts.append("--vad-filter")
    else:
        parts.append("--no-vad-filter")
    if getattr(config, "diarize", False):
        parts.append("--diarize")
        num = getattr(config, "num_speakers", 0)
        if num > 0:
            parts.extend(["--num-speakers", str(num)])
        backend = getattr(config, "diarize_backend", "auto")
        parts.extend(["--diarize-backend", shlex.quote(backend)])
        hf_token = getattr(config, "hf_token", "")
        if hf_token:
            parts.extend(["--hf-token", shlex.quote(hf_token)])
    return " ".join(parts)


def _ssh(target, opts, command, timeout=60):
    result = subprocess.run(
        ["ssh"] + opts + [target, command],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _scp_to(opts, local_path, remote_path, retries=3):
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            ["scp"] + opts + [local_path, remote_path],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            return
        err = result.stderr.strip()
        if attempt < retries:
            logger.warning("SCP upload attempt %d/%d failed: %s", attempt, retries, err)
            time.sleep(5 * attempt)
        else:
            raise RuntimeError(f"SCP upload failed after {retries} attempts: {err}")


def _scp_from(opts, remote_path, local_path, retries=3):
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            ["scp"] + opts + [remote_path, local_path],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            return
        err = result.stderr.strip()
        if attempt < retries:
            logger.warning("SCP download attempt %d/%d failed: %s", attempt, retries, err)
            time.sleep(5 * attempt)
        else:
            raise RuntimeError(f"SCP download failed after {retries} attempts: {err}")
