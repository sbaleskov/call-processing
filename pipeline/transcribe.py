"""
Audio transcription via local faster-whisper with optional speaker diarization (MFCC + clustering).
"""

import logging
from pathlib import Path
from typing import Optional, List, Tuple

import numpy as np
import soundfile as sf
from python_speech_features import mfcc
from sklearn.cluster import AgglomerativeClustering
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Model cache — load once, reuse between calls
_model_cache = {}


def _get_model(config) -> WhisperModel:
    """Return a (cached) WhisperModel instance."""
    key = (config.whisper_model_size, config.whisper_device, config.whisper_compute_type)
    if key not in _model_cache:
        cpu_threads = getattr(config, "cpu_threads", 4)
        logger.info(
            "Loading Whisper model: %s (device=%s, compute=%s, threads=%d)",
            config.whisper_model_size,
            config.whisper_device,
            config.whisper_compute_type,
            cpu_threads,
        )
        _model_cache[key] = WhisperModel(
            config.whisper_model_size,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
            cpu_threads=cpu_threads,
        )
    return _model_cache[key]


def _load_audio(file_path: str) -> Tuple[np.ndarray, int]:
    """Load audio file and convert to mono float32."""
    data, samplerate = sf.read(file_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, samplerate


def _extract_speaker_features(
    audio: np.ndarray, samplerate: int, start: float, end: float
) -> np.ndarray:
    """
    Extract MFCC features for an audio segment.
    Returns averaged feature vector (1D).
    """
    start_sample = int(start * samplerate)
    end_sample = int(end * samplerate)
    chunk = audio[start_sample:end_sample]

    if len(chunk) < samplerate * 0.1:  # < 100ms — too short
        return np.zeros(13)

    features = mfcc(chunk, samplerate=samplerate, numcep=13, nfilt=26, nfft=2048)
    return features.mean(axis=0)


def _diarize_segments(
    segments: List[dict],
    audio: np.ndarray,
    samplerate: int,
    num_speakers: int = 0,
) -> List[dict]:
    """
    Cluster segments by speaker voice.

    segments: list of dicts with keys start, end, text
    num_speakers: number of speakers (0 = auto-detect, max 10)

    Returns segments with added 'speaker' field (int).
    """
    if not segments:
        return segments

    # Extract features for each segment
    features = []
    valid_indices = []
    for i, seg in enumerate(segments):
        feat = _extract_speaker_features(audio, samplerate, seg["start"], seg["end"])
        if np.any(feat != 0):
            features.append(feat)
            valid_indices.append(i)

    if len(features) < 2:
        for seg in segments:
            seg["speaker"] = 1
        return segments

    X = np.array(features)

    # Auto-detect number of speakers via silhouette score
    if num_speakers <= 0:
        num_speakers = _estimate_num_speakers(X)

    num_speakers = min(num_speakers, len(features))

    clustering = AgglomerativeClustering(
        n_clusters=num_speakers,
        metric="cosine",
        linkage="average",
    )
    labels = clustering.fit_predict(X)

    # Re-assign labels: most frequent speaker → Speaker 1
    from collections import Counter
    label_counts = Counter(labels)
    rank = {label: idx + 1 for idx, (label, _) in enumerate(label_counts.most_common())}

    # Assign speaker labels
    label_map = {}
    for idx, vi in enumerate(valid_indices):
        label_map[vi] = rank[labels[idx]]

    for i, seg in enumerate(segments):
        seg["speaker"] = label_map.get(i, 1)

    return segments


def _estimate_num_speakers(X: np.ndarray, max_speakers: int = 6) -> int:
    """
    Estimate optimal number of speakers via silhouette score.
    Tries from 2 to max_speakers.
    """
    from sklearn.metrics import silhouette_score

    if len(X) < 3:
        return 2

    best_k = 2
    best_score = -1.0

    for k in range(2, min(max_speakers + 1, len(X))):
        try:
            clustering = AgglomerativeClustering(
                n_clusters=k,
                metric="cosine",
                linkage="average",
            )
            labels = clustering.fit_predict(X)
            if len(set(labels)) < 2:
                continue
            score = silhouette_score(X, labels, metric="cosine")
            if score > best_score:
                best_score = score
                best_k = k
        except Exception:
            continue

    logger.info("Speaker auto-detection: %d (silhouette=%.3f)", best_k, best_score)
    return best_k


def transcribe_audio(file_path: str, config) -> Optional[str]:
    """
    Transcribe an audio file to text via local faster-whisper.
    If diarization is enabled, speaker labels are added.

    Args:
        file_path: Path to the audio file
        config: Configuration object

    Returns:
        Text transcription or None on error
    """
    try:
        file_path_obj = Path(file_path)

        if not file_path_obj.exists():
            logger.error("File not found: %s", file_path)
            return None

        logger.info("Starting transcription: %s", file_path_obj.name)

        model = _get_model(config)

        segments_iter, info = model.transcribe(
            file_path,
            language=config.language,
            beam_size=5,
        )

        # Collect segments into a list
        raw_segments = []
        seg_count = 0
        for segment in segments_iter:
            seg_count += 1
            if seg_count % 50 == 0:
                logger.info("Transcription: processed %d segments (up to %.1f min)...", seg_count, segment.end / 60)
            raw_segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
            })

        if not raw_segments:
            logger.warning("Transcription returned no segments")
            return ""

        # Diarization
        diarize = getattr(config, "diarize", False)
        if diarize:
            logger.info("Running speaker diarization...")
            try:
                audio, samplerate = _load_audio(file_path)
                num_speakers = getattr(config, "num_speakers", 0)
                raw_segments = _diarize_segments(raw_segments, audio, samplerate, num_speakers)
                logger.info("Diarization complete")
            except Exception as e:
                logger.warning("Diarization error, continuing without speaker labels: %s", e)
                diarize = False

        # Build output text
        if diarize:
            # Merge consecutive segments from same speaker
            merged = _merge_speaker_segments(raw_segments)
            lines = []
            for seg in merged:
                lines.append(f"Speaker {seg['speaker']}: {seg['text']}")
            transcription = "\n".join(lines)
        else:
            text_parts = [seg["text"] for seg in raw_segments]
            transcription = " ".join(text_parts)

        logger.info("Transcription complete (%d chars)", len(transcription))
        return transcription

    except Exception as e:
        logger.error("Transcription error: %s", e, exc_info=True)
        return None


def _merge_speaker_segments(segments: List[dict]) -> List[dict]:
    """
    Merge consecutive segments from the same speaker into single blocks.
    """
    if not segments:
        return []

    merged = []
    current = {
        "speaker": segments[0]["speaker"],
        "text": segments[0]["text"],
        "start": segments[0]["start"],
        "end": segments[0]["end"],
    }

    for seg in segments[1:]:
        if seg["speaker"] == current["speaker"]:
            current["text"] += " " + seg["text"]
            current["end"] = seg["end"]
        else:
            merged.append(current)
            current = {
                "speaker": seg["speaker"],
                "text": seg["text"],
                "start": seg["start"],
                "end": seg["end"],
            }

    merged.append(current)
    return merged
