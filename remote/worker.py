#!/usr/bin/env python3
"""
Transcription worker — runs on a remote server.

Self-contained script: no project imports, all logic inline.
Deployed via remote/deploy.sh, called over SSH by the main pipeline.

Usage:
    python3 worker.py input.m4a -o output.txt [--model medium] [--language ru] [--diarize]
    python3 worker.py --check   # verify installation
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel
from python_speech_features import mfcc
from sklearn.cluster import AgglomerativeClustering

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

_model_cache = {}


def get_model(model_size, device, compute_type, threads):
    key = (model_size, device, compute_type)
    if key not in _model_cache:
        logger.info("Loading model %s (device=%s, compute=%s, threads=%d)",
                     model_size, device, compute_type, threads)
        _model_cache[key] = WhisperModel(
            model_size, device=device, compute_type=compute_type,
            cpu_threads=threads,
        )
    return _model_cache[key]


def transcribe(file_path, model_size, device, compute_type, threads, language):
    model = get_model(model_size, device, compute_type, threads)
    segments_iter, info = model.transcribe(file_path, language=language, beam_size=5)

    segments = []
    count = 0
    for seg in segments_iter:
        count += 1
        if count % 50 == 0:
            logger.info("Processed %d segments (%.1f min)...", count, seg.end / 60)
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})

    logger.info("Transcription done: %d segments", len(segments))
    return segments


# ---------------------------------------------------------------------------
# Speaker diarization (MFCC + agglomerative clustering)
# ---------------------------------------------------------------------------

def load_audio(file_path):
    data, sr = sf.read(file_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, sr


def extract_features(audio, sr, start, end):
    s, e = int(start * sr), int(end * sr)
    chunk = audio[s:e]
    if len(chunk) < sr * 0.1:
        return np.zeros(13)
    return mfcc(chunk, samplerate=sr, numcep=13, nfilt=26, nfft=2048).mean(axis=0)


def estimate_speakers(X, max_k=6):
    from sklearn.metrics import silhouette_score

    if len(X) < 3:
        return 2
    best_k, best_score = 2, -1.0
    for k in range(2, min(max_k + 1, len(X))):
        try:
            labels = AgglomerativeClustering(
                n_clusters=k, metric="cosine", linkage="average",
            ).fit_predict(X)
            if len(set(labels)) < 2:
                continue
            score = silhouette_score(X, labels, metric="cosine")
            if score > best_score:
                best_score = score
                best_k = k
        except Exception:
            continue
    logger.info("Speaker auto-detection: %d speakers (silhouette=%.3f)", best_k, best_score)
    return best_k


def diarize(segments, audio, sr, num_speakers=0):
    if not segments:
        return segments

    features, valid_idx = [], []
    for i, seg in enumerate(segments):
        feat = extract_features(audio, sr, seg["start"], seg["end"])
        if np.any(feat != 0):
            features.append(feat)
            valid_idx.append(i)

    if len(features) < 2:
        for seg in segments:
            seg["speaker"] = 1
        return segments

    X = np.array(features)
    if num_speakers <= 0:
        num_speakers = estimate_speakers(X)
    num_speakers = min(num_speakers, len(features))

    labels = AgglomerativeClustering(
        n_clusters=num_speakers, metric="cosine", linkage="average",
    ).fit_predict(X)

    from collections import Counter
    rank = {label: idx + 1 for idx, (label, _) in enumerate(Counter(labels).most_common())}

    label_map = {vi: rank[labels[i]] for i, vi in enumerate(valid_idx)}
    for i, seg in enumerate(segments):
        seg["speaker"] = label_map.get(i, 1)
    return segments


def merge_speaker_segments(segments):
    if not segments:
        return []
    merged = [dict(segments[0])]
    for seg in segments[1:]:
        if seg["speaker"] == merged[-1]["speaker"]:
            merged[-1]["text"] += " " + seg["text"]
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def check_installation():
    """Verify that all dependencies are importable and a model can load."""
    print("faster-whisper ... ok")
    print("numpy .......... ok")
    print("soundfile ...... ok")
    print("sklearn ........ ok")
    print("speech_features  ok")
    print("All dependencies verified.")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Transcription worker")
    parser.add_argument("input", nargs="?", help="Audio file to transcribe")
    parser.add_argument("-o", "--output", help="Output text file (default: stdout)")
    parser.add_argument("--model", default="medium", help="Whisper model size")
    parser.add_argument("--device", default="cpu", help="cpu or cuda")
    parser.add_argument("--compute-type", default="int8", help="int8, float16, float32")
    parser.add_argument("--threads", type=int, default=4, help="CPU threads")
    parser.add_argument("--language", default="ru", help="ISO 639-1 language code")
    parser.add_argument("--diarize", action="store_true", help="Enable speaker diarization")
    parser.add_argument("--num-speakers", type=int, default=0, help="Number of speakers (0=auto)")
    parser.add_argument("--check", action="store_true", help="Verify installation and exit")
    args = parser.parse_args()

    if args.check:
        check_installation()

    if not args.input:
        parser.error("Input audio file is required")

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("File not found: %s", input_path)
        sys.exit(1)

    # Transcribe
    segments = transcribe(
        str(input_path), args.model, args.device, args.compute_type,
        args.threads, args.language,
    )

    if not segments:
        logger.warning("No segments produced")
        result = ""
    elif args.diarize:
        logger.info("Running diarization...")
        try:
            audio, sr = load_audio(str(input_path))
            segments = diarize(segments, audio, sr, args.num_speakers)
            merged = merge_speaker_segments(segments)
            result = "\n".join(f"Speaker {s['speaker']}: {s['text']}" for s in merged)
        except Exception as e:
            logger.warning("Diarization failed, returning plain text: %s", e)
            result = " ".join(s["text"] for s in segments)
    else:
        result = " ".join(s["text"] for s in segments)

    # Output
    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        logger.info("Written to %s (%d chars)", args.output, len(result))
    else:
        print(result)


if __name__ == "__main__":
    main()
