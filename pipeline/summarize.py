"""
Transcription summarization and action item extraction.
Uses Claude CLI (claude -p) to invoke LLM without a separate API key.
"""

import json
import logging
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def summarize_transcription(
    transcription: str,
    config,
    parent_tasks: Optional[List[str]] = None,
) -> Optional[Dict]:
    """
    Summarize a transcription, extract action items, and classify them
    against parent tasks from the inbox.

    Args:
        transcription: Text transcription of the call
        config: Configuration object
        parent_tasks: List of parent task titles from the inbox

    Returns:
        Dict with project, summary, topics, action_items, participants
    """
    if not transcription or len(transcription.strip()) < 50:
        logger.warning("Transcription too short for summarization")
        return {
            "summary": "Transcription too short",
            "project": "Not determined",
            "topics": [],
            "action_items": [],
            "participants": [],
        }

    prompt = _create_summarization_prompt(transcription, parent_tasks)

    logger.info("Sending transcription to Claude CLI for summarization...")

    try:
        result_text = _call_claude(prompt)
        if not result_text:
            logger.error("Empty response from Claude CLI")
            return None

        result_text = _extract_json(result_text)
        result = json.loads(result_text)
        return _normalize_summary_result(result)

    except json.JSONDecodeError as e:
        logger.error("Failed to parse JSON from Claude: %s", e)
        logger.debug("Claude response:\n%s", result_text)
        return None
    except Exception as e:
        logger.error("Summarization error: %s", e, exc_info=True)
        return None


def _call_claude(prompt: str) -> Optional[str]:
    """Call Claude CLI in non-interactive mode and return the response."""
    try:
        # Pass prompt directly via -p argument
        # macOS ARG_MAX ~ 1 MB, transcriptions are usually < 100 KB
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes to respond
        )

        if result.returncode != 0:
            logger.error("Claude CLI returned code %d: %s", result.returncode, result.stderr)
            return None

        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error("Claude CLI timeout (300 sec)")
        return None
    except FileNotFoundError:
        logger.error("Claude CLI not found. Make sure 'claude' is installed and available on PATH")
        return None


def _create_summarization_prompt(
    transcription: str,
    parent_tasks: Optional[List[str]] = None,
) -> str:
    """Create prompt for summarization + action item classification."""

    parent_section = ""
    if parent_tasks:
        parent_list = "\n".join(f"- {t}" for t in parent_tasks)
        parent_section = f"""

PARENT TASKS IN INBOX (for action item classification):
{parent_list}

For each action item set the "parent_task" field:
- If the task clearly belongs to one of the parent tasks — use the EXACT name.
- If the task requires a new parent — use the format "__NEW__:Title".
- If classification is not possible — leave an empty string.
"""

    return f"""Analyze the following business call transcription and create a detailed structured summary.

Transcription:
{transcription}
{parent_section}
Return your answer ONLY as JSON (no markdown blocks, no comments) with the following structure:
{{
    "project": "project name or call topic",
    "summary": "brief summary of the call (2-3 sentences)",
    "topics": [
        {{
            "title": "discussion topic title",
            "what_discussed": "detailed description of what specifically was discussed",
            "why_discussed": "reason why this topic was raised",
            "decisions": "specific decisions and agreements reached",
            "key_points": ["key point 1", "key point 2"]
        }}
    ],
    "participants": ["participant name 1", "participant name 2"],
    "action_items": [
        {{
            "description": "detailed task description with context",
            "assignee": "name of the responsible person",
            "due_date": "YYYY-MM-DD or empty string",
            "parent_task": "exact parent task name from inbox, __NEW__:Title, or empty string"
        }}
    ]
}}

CRITICAL INSTRUCTIONS:
1. For each topic extract: WHAT was discussed, WHY, WHAT was decided, key points.
2. Avoid vague language — be specific: numbers, deadlines, names, titles.
3. For action items include full task context.
4. If information is insufficient — use "Not specified" or empty arrays.
5. Reply with ONLY valid JSON, no wrappers of any kind."""


def _extract_json(text: str) -> str:
    """Extract JSON from text, stripping markdown wrappers."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _normalize_summary_result(result: Dict) -> Dict:
    """Normalize the summarization result."""
    topics = result.get("topics", [])
    normalized_topics = []
    for topic in topics:
        if isinstance(topic, str):
            normalized_topics.append({
                "title": topic,
                "what_discussed": "Not specified",
                "why_discussed": "Not specified",
                "decisions": "Not specified",
                "key_points": [],
            })
        elif isinstance(topic, dict):
            normalized_topics.append({
                "title": topic.get("title", "Not specified"),
                "what_discussed": topic.get("what_discussed", "Not specified"),
                "why_discussed": topic.get("why_discussed", "Not specified"),
                "decisions": topic.get("decisions", "Not specified"),
                "key_points": topic.get("key_points", []),
            })

    return {
        "project": result.get("project", "Not determined"),
        "summary": result.get("summary", "Summary not created"),
        "topics": normalized_topics,
        "participants": result.get("participants", []),
        "action_items": result.get("action_items", []),
    }
