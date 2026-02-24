"""
YouTrack REST API client.
Task creation, subtasks, comments, status updates.
"""

import re
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class YouTrackClient:
    """YouTrack REST API client."""

    def __init__(self, base_url: str, token: str, project: str = ""):
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Project ID cache (lazy-loaded)
        self._project_id: Optional[str] = None

    # ─────────────────────────── helpers ──────────────────────────────────

    @staticmethod
    def extract_issue_id(url: str) -> Optional[str]:
        """Extract issue ID (e.g. PRE-21) from a YouTrack URL."""
        m = re.search(r"/issue/([A-Za-z]+-\d+)", url)
        return m.group(1) if m else None

    def issue_url(self, issue_id: str) -> str:
        """Build full issue URL."""
        return f"{self.base_url}/issue/{issue_id}/"

    def _api(self, method: str, path: str, **kwargs) -> requests.Response:
        """Execute API request."""
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, **kwargs)
        if not resp.ok:
            logger.error(
                "YT API %s %s → %d: %s",
                method, path, resp.status_code, resp.text[:500],
            )
        return resp

    def _get_project_id(self) -> Optional[str]:
        """Get internal project ID by short name."""
        if self._project_id:
            return self._project_id
        resp = self._api("GET", f"/api/admin/projects?fields=id,shortName&query={self.project}")
        if resp.ok:
            for p in resp.json():
                if p.get("shortName") == self.project:
                    self._project_id = p["id"]
                    return self._project_id
        logger.error("Project not found: %s", self.project)
        return None

    # ─────────────────────────── read ─────────────────────────────────────

    def get_issue(self, issue_id: str) -> Optional[Dict]:
        """Get issue by ID."""
        resp = self._api(
            "GET",
            f"/api/issues/{issue_id}?fields=idReadable,summary,description,"
            "resolved,customFields(name,value(name)),"
            "links(direction,linkType(name),issues(idReadable))",
        )
        if resp.ok:
            return resp.json()
        return None

    def get_parent_id(self, yt_issue: Dict) -> Optional[str]:
        """Extract parent issue ID from links (subtask of)."""
        for link in yt_issue.get("links", []):
            link_type = link.get("linkType", {}).get("name", "")
            direction = link.get("direction", "")
            # "subtask of" with direction INWARD on the child points to the parent
            if link_type == "Subtask" and direction == "INWARD":
                issues = link.get("issues", [])
                if issues:
                    return issues[0].get("idReadable")
        return None

    # ─────────────────────────── create ───────────────────────────────────

    def create_issue(self, summary: str, description: str = "") -> Optional[str]:
        """
        Create issue in project.
        Returns idReadable (e.g. PRE-42) or None.
        """
        project_id = self._get_project_id()
        if not project_id:
            return None

        body = {
            "project": {"id": project_id},
            "summary": summary,
        }
        if description:
            body["description"] = description

        resp = self._api(
            "POST",
            "/api/issues?fields=idReadable",
            json=body,
        )
        if resp.ok:
            issue_id = resp.json().get("idReadable")
            logger.info("Created issue %s: %s", issue_id, summary[:80])
            return issue_id
        return None

    def create_subtask(
        self, parent_id: str, summary: str, description: str = ""
    ) -> Optional[str]:
        """
        Create subtask and link to parent via Subtask relation.
        Returns idReadable of the new issue.
        """
        child_id = self.create_issue(summary, description)
        if not child_id:
            return None

        self.link_subtask(child_id, parent_id)
        return child_id

    def link_subtask(self, child_id: str, parent_id: str) -> bool:
        """
        Link child_id as subtask of parent_id via Commands API.
        Uses command 'subtask of PARENT' applied to child.
        """
        resp = self._api(
            "POST",
            "/api/commands",
            json={
                "query": f"subtask of {parent_id}",
                "issues": [{"idReadable": child_id}],
            },
        )
        if resp.ok:
            logger.info("Linked subtask %s → parent %s", child_id, parent_id)
            return True
        logger.warning(
            "Failed to link %s to %s", child_id, parent_id
        )
        return False

    # ─────────────────────────── comments ─────────────────────────────────

    def add_comment(self, issue_id: str, text: str) -> bool:
        """Add comment to issue."""
        resp = self._api(
            "POST",
            f"/api/issues/{issue_id}/comments",
            json={"text": text},
        )
        if resp.ok:
            logger.info("Comment added to %s", issue_id)
            return True
        return False

    def format_summary_comment(self, summary: Dict) -> str:
        """Format call summary as markdown comment for YouTrack."""
        lines = [
            f"## Call Summary",
            "",
            f"**Project:** {summary.get('project', 'N/A')}",
            "",
            summary.get("summary", ""),
            "",
        ]

        topics = summary.get("topics", [])
        if topics:
            lines.append("### Discussion Topics")
            for i, t in enumerate(topics, 1):
                if isinstance(t, dict):
                    lines.append(f"**{i}. {t.get('title', '')}**")
                    if t.get("what_discussed") and t["what_discussed"] != "Not specified":
                        lines.append(f"What was discussed: {t['what_discussed']}")
                    if t.get("decisions") and t["decisions"] != "Not specified":
                        lines.append(f"Decisions: {t['decisions']}")
                    lines.append("")

        action_items = summary.get("action_items", [])
        if action_items:
            lines.append("### Action Items")
            for i, item in enumerate(action_items, 1):
                desc = item.get("description", "")
                assignee = item.get("assignee", "")
                due = item.get("due_date", "")
                line = f"{i}. {desc}"
                if assignee:
                    line += f" — {assignee}"
                if due:
                    line += f" (by {due})"
                lines.append(line)

        return "\n".join(lines)

    # ─────────────────────────── update ───────────────────────────────────

    def update_issue(
        self,
        issue_id: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Update issue summary and/or description."""
        body: Dict = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if not body:
            return True

        resp = self._api("POST", f"/api/issues/{issue_id}", json=body)
        if resp.ok:
            logger.info("Updated issue %s", issue_id)
            return True
        return False

    def set_state(self, issue_id: str, state: str) -> bool:
        """Set issue state via Commands API."""
        resp = self._api(
            "POST",
            "/api/commands",
            json={
                "query": f"state {state}",
                "issues": [{"idReadable": issue_id}],
            },
        )
        if resp.ok:
            logger.info("Issue %s → state '%s'", issue_id, state)
            return True
        return False

    def resolve_issue(self, issue_id: str) -> bool:
        """Set issue status to 'Done'."""
        return self.set_state(issue_id, "Done")
