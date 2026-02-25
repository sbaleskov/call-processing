"""
Remote audio transcription — offloads whisper to a server via SSH.

Flow:
  1. scp audio file → remote server
  2. ssh: run worker.py (transcribe + optional diarize)
  3. scp transcription text ← back
  4. cleanup remote temp files
"""

import logging
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def transcribe_audio_remote(file_path: str, config) -> Optional[str]:
    """
    Transcribe an audio file on a remote server via SSH + scp.

    Uses the same whisper/diarization settings from config, but executes
    on the remote machine specified by REMOTE_HOST / REMOTE_USER.

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

    try:
        # Ensure remote work dir exists
        _ssh(target, ssh_opts, f"mkdir -p {work_dir}")

        # Upload audio
        logger.info("Uploading %s to %s...", local_file.name, host)
        _scp_to(ssh_opts, str(local_file), f"{target}:{remote_audio}")

        # Run transcription
        logger.info("Remote transcription started on %s...", host)
        worker_cmd = _worker_command(
            python_bin, worker_bin, remote_audio, remote_transcript, config,
        )
        _ssh(target, ssh_opts, worker_cmd, timeout=1800)  # 30 min max

        # Download result
        logger.info("Downloading transcription...")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            local_tmp = tmp.name
        _scp_from(ssh_opts, f"{target}:{remote_transcript}", local_tmp)
        transcription = Path(local_tmp).read_text(encoding="utf-8")
        Path(local_tmp).unlink(missing_ok=True)

        # Cleanup remote files (non-critical — don't discard transcription on failure)
        try:
            _ssh(target, ssh_opts,
                 f"rm -f {shlex.quote(remote_audio)} {shlex.quote(remote_transcript)}",
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
    opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if key_path:
        opts.extend(["-i", key_path])
    return opts


def _worker_command(python_bin, worker_bin, audio_path, output_path, config) -> str:
    """Build the shell command to run worker.py on the remote server."""
    parts = [
        shlex.quote(python_bin),
        shlex.quote(worker_bin),
        shlex.quote(audio_path),
        "-o", shlex.quote(output_path),
        "--model", shlex.quote(config.whisper_model_size),
        "--language", shlex.quote(config.language),
        "--compute-type", shlex.quote(config.whisper_compute_type),
        "--threads", str(config.cpu_threads),
    ]
    device = getattr(config, "whisper_device", "cpu")
    if device != "cpu":
        parts.extend(["--device", shlex.quote(device)])
    if getattr(config, "diarize", False):
        parts.append("--diarize")
        num = getattr(config, "num_speakers", 0)
        if num > 0:
            parts.extend(["--num-speakers", str(num)])
    return " ".join(parts)


def _ssh(target, opts, command, timeout=60):
    result = subprocess.run(
        ["ssh"] + opts + [target, command],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _scp_to(opts, local_path, remote_path):
    result = subprocess.run(
        ["scp"] + opts + [local_path, remote_path],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SCP upload failed: {result.stderr.strip()}")


def _scp_from(opts, remote_path, local_path):
    result = subprocess.run(
        ["scp"] + opts + [remote_path, local_path],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SCP download failed: {result.stderr.strip()}")
