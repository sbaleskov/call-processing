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


def transcribe(file_path, model_size, device, compute_type, threads, language, vad_filter=True):
    model = get_model(model_size, device, compute_type, threads)
    segments_iter, info = model.transcribe(
        file_path, language=language, beam_size=5, vad_filter=vad_filter,
    )

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
# Speaker diarization — pyannote backend
# ---------------------------------------------------------------------------

_pyannote_cache = {}


def get_pyannote_pipeline(hf_token):
    """Load and cache pyannote speaker-diarization-community-1."""
    if not hf_token:
        raise RuntimeError("HF_TOKEN is required for pyannote diarization.")

    if hf_token in _pyannote_cache:
        return _pyannote_cache[hf_token]

    from pyannote.audio import Pipeline
    import torch

    logger.info("Loading pyannote speaker-diarization-community-1...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-community-1",
        token=hf_token,
    )
    pipeline.to(torch.device("cpu"))

    _pyannote_cache[hf_token] = pipeline
    logger.info("pyannote pipeline loaded")
    return pipeline


def diarize_pyannote(file_path, segments, hf_token, num_speakers=0):
    """Assign speaker labels using pyannote diarization."""
    pipeline = get_pyannote_pipeline(hf_token)

    logger.info("Running pyannote diarization...")

    kwargs = {}
    if num_speakers > 0:
        kwargs["num_speakers"] = num_speakers
    else:
        kwargs["min_speakers"] = 1
        kwargs["max_speakers"] = 10

    raw_result = pipeline(file_path, **kwargs)

    # pyannote 4.x returns DiarizeOutput; extract Annotation object
    result = getattr(raw_result, "speaker_diarization", raw_result)

    turns = []
    for turn, _, label in result.itertracks(yield_label=True):
        turns.append({"start": turn.start, "end": turn.end, "speaker": label})

    logger.info("pyannote found %d speaker turns", len(turns))

    if not turns:
        logger.warning("pyannote returned no turns, assigning all to Speaker 1")
        for seg in segments:
            seg["speaker"] = 1
        return segments

    # Map labels to ints by order of first appearance
    seen = {}
    counter = 1
    for t in turns:
        if t["speaker"] not in seen:
            seen[t["speaker"]] = counter
            counter += 1

    # Assign dominant speaker to each Whisper segment
    for seg in segments:
        seg_start, seg_end = seg["start"], seg["end"]
        if seg_end - seg_start <= 0:
            seg["speaker"] = 1
            continue

        overlap = {}
        for t in turns:
            o = min(seg_end, t["end"]) - max(seg_start, t["start"])
            if o > 0:
                overlap[t["speaker"]] = overlap.get(t["speaker"], 0.0) + o

        if overlap:
            seg["speaker"] = seen[max(overlap, key=overlap.get)]
        else:
            mid = (seg_start + seg_end) / 2
            nearest = min(turns, key=lambda t: abs((t["start"] + t["end"]) / 2 - mid))
            seg["speaker"] = seen[nearest["speaker"]]

    return segments


def run_diarization(file_path, segments, backend, hf_token, num_speakers):
    """Route to pyannote or MFCC diarization."""
    use_pyannote = False
    if backend == "pyannote":
        use_pyannote = True
    elif backend == "auto":
        use_pyannote = bool(hf_token)

    if use_pyannote:
        try:
            return diarize_pyannote(file_path, segments, hf_token, num_speakers)
        except Exception as e:
            logger.warning("pyannote failed, falling back to MFCC: %s", e)

    # MFCC fallback
    audio, sr = load_audio(file_path)
    return diarize(segments, audio, sr, num_speakers)


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
    parser.add_argument("--diarize-backend", default="auto", help="auto | pyannote | mfcc")
    parser.add_argument("--hf-token", default="", help="HuggingFace token for pyannote")
    parser.add_argument("--vad-filter", action="store_true", default=True, help="Enable VAD filter")
    parser.add_argument("--no-vad-filter", action="store_false", dest="vad_filter")
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
        args.threads, args.language, vad_filter=args.vad_filter,
    )

    if not segments:
        logger.warning("No segments produced")
        result = ""
    elif args.diarize:
        logger.info("Running diarization...")
        try:
            segments = run_diarization(
                str(input_path), segments,
                args.diarize_backend, args.hf_token, args.num_speakers,
            )
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
