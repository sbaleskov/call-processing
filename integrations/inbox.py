"""
Markdown task inbox file integration.
Parses Markdown structure (task trees), extracts YT links,
inserts new tasks and writes YT links back.
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────── regex patterns ────────────────────────────────

# Task checkbox at any indent level
_TASK_RE = re.compile(r"^(?P<indent>[\t ]*)- \[(?P<check>[ x])\] (?P<rest>.+)$")

# YouTrack link: [→YT](https://...issue/PRE-21/)
_YT_LINK_RE = re.compile(r"\s*\[→YT\]\((?P<url>[^)]+)\)")

# Date: 📅 YYYY-MM-DD
_DATE_RE = re.compile(r"\s*📅\s*(?P<date>\d{4}-\d{2}-\d{2})")

# Completion marker: ✅ YYYY-MM-DD
_DONE_RE = re.compile(r"\s*✅\s*\d{4}-\d{2}-\d{2}")

# Child task status tags: #backlog, #in-progress
_STATUS_TAG_RE = re.compile(r"\s*(?P<tag>#(?:backlog|in-progress))\b")

# YAML frontmatter delimiter
_FRONTMATTER_RE = re.compile(r"^---\s*$")

# Kanban column heading: ## Column Name
_COLUMN_RE = re.compile(r"^## (.+)$")


# ────────────────────────── data model ────────────────────────────────────

class InboxItem:
    """Inbox task tree item (parent, child, grandchild...)."""

    def __init__(
        self,
        title: str,
        line_number: int,
        indent_level: int,
        is_completed: bool = False,
        youtrack_url: str = "",
        due_date: str = "",
        column: str = "",
        status_tag: str = "",
    ):
        self.title = title              # Clean title (without checkbox, link, date)
        self.line_number = line_number
        self.indent_level = indent_level  # 0=parent, 1=child, 2=grandchild ...
        self.is_completed = is_completed
        self.youtrack_url = youtrack_url
        self.youtrack_id = ""
        if youtrack_url:
            m = re.search(r"/issue/([A-Za-z]+-\d+)", youtrack_url)
            self.youtrack_id = m.group(1) if m else ""
        self.due_date = due_date
        self.column = column            # Kanban column name (from ## heading)
        self.status_tag = status_tag    # Status tag (#backlog, #in-progress, #done)
        self.body_lines: List[int] = []   # Line numbers of body (non-task lines)
        self.children: List["InboxItem"] = []

    @property
    def body(self) -> str:
        """Task body — populated by InboxManager during parsing."""
        return self._body if hasattr(self, "_body") else ""

    @body.setter
    def body(self, value: str):
        self._body = value

    @property
    def last_line(self) -> int:
        """Line number of the last line belonging to this task (including children and body)."""
        candidates = [self.line_number]
        if self.body_lines:
            candidates.append(max(self.body_lines))
        for child in self.children:
            candidates.append(child.last_line)
        return max(candidates)

    def __repr__(self):
        yt = f" [{self.youtrack_id}]" if self.youtrack_id else ""
        return f"InboxItem(L{self.line_number}, lvl={self.indent_level}, '{self.title[:40]}'{yt})"


# ────────────────────────── manager ───────────────────────────────────────

class InboxManager:
    """Manage reading, parsing, and writing the inbox file."""

    def __init__(self, inbox_path: str):
        self.inbox_path = Path(inbox_path)

    # ─────────────── reading ──────────────────────────────────────────────

    def read_lines(self) -> List[str]:
        """Read file, return list of lines (with newlines)."""
        if not self.inbox_path.exists():
            logger.error("Inbox file not found: %s", self.inbox_path)
            return []
        return self.inbox_path.read_text(encoding="utf-8").splitlines(keepends=True)

    def write_lines(self, lines: List[str]):
        """Write lines back to file."""
        self.inbox_path.write_text("".join(lines), encoding="utf-8")

    # ─────────────── parsing ──────────────────────────────────────────────

    @staticmethod
    def _indent_level(indent_str: str) -> int:
        """Determine indent level (counts tabs and 4-space blocks)."""
        level = 0
        remaining = indent_str
        while remaining:
            if remaining.startswith("\t"):
                level += 1
                remaining = remaining[1:]
            elif remaining.startswith("    "):
                level += 1
                remaining = remaining[4:]
            else:
                break
        return level

    @staticmethod
    def _parse_task_text(rest: str) -> Tuple[str, str, str, bool, str]:
        """
        Parse text after checkbox.
        Returns (title, youtrack_url, due_date, is_completed_marker, status_tag).
        """
        yt_url = ""
        m_yt = _YT_LINK_RE.search(rest)
        if m_yt:
            yt_url = m_yt.group("url")
            rest = rest[: m_yt.start()] + rest[m_yt.end() :]

        due_date = ""
        m_date = _DATE_RE.search(rest)
        if m_date:
            due_date = m_date.group("date")
            rest = rest[: m_date.start()] + rest[m_date.end() :]

        # Status tag (#backlog, #in-progress, #done)
        status_tag = ""
        m_tag = _STATUS_TAG_RE.search(rest)
        if m_tag:
            status_tag = m_tag.group("tag")
            rest = rest[: m_tag.start()] + rest[m_tag.end() :]

        # Remove completion marker ✅
        rest = _DONE_RE.sub("", rest)

        title = rest.strip()
        return title, yt_url, due_date, False, status_tag

    def parse_tree(self) -> List[InboxItem]:
        """
        Parse inbox into InboxItem tree.
        Supports kanban format (YAML frontmatter + ## Column headings).
        Returns list of root (parent) items.
        """
        lines = self.read_lines()
        if not lines:
            return []

        roots: List[InboxItem] = []
        # Stack: (indent_level, InboxItem)
        stack: List[Tuple[int, InboxItem]] = []
        current_column = ""
        in_frontmatter = False
        frontmatter_ended = False

        for i, line in enumerate(lines):
            stripped = line.rstrip("\n\r")

            # Handle YAML frontmatter (--- ... ---)
            if _FRONTMATTER_RE.match(stripped):
                if not frontmatter_ended:
                    if in_frontmatter:
                        in_frontmatter = False
                        frontmatter_ended = True
                    else:
                        in_frontmatter = True
                    continue
            if in_frontmatter:
                continue

            # Handle kanban column headings (## Column Name)
            m_col = _COLUMN_RE.match(stripped)
            if m_col:
                current_column = m_col.group(1).strip()
                stack = []  # Reset stack at column boundary
                continue

            m_task = _TASK_RE.match(stripped)
            if m_task:
                indent_str = m_task.group("indent")
                level = self._indent_level(indent_str)
                is_completed = m_task.group("check") == "x"
                rest = m_task.group("rest")

                title, yt_url, due_date, _, status_tag = self._parse_task_text(rest)

                item = InboxItem(
                    title=title,
                    line_number=i,
                    indent_level=level,
                    is_completed=is_completed,
                    youtrack_url=yt_url,
                    due_date=due_date,
                    column=current_column,
                    status_tag=status_tag,
                )

                # Attach to tree
                if level == 0:
                    roots.append(item)
                    stack = [(0, item)]
                else:
                    # Find closest parent with lower indent level
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    if stack:
                        stack[-1][1].children.append(item)
                    else:
                        # No parent found — add as root (fallback)
                        roots.append(item)
                    stack.append((level, item))

            else:
                # Non-task line → body of the nearest task in stack
                if stripped.strip() == "":
                    # Blank line — may be part of body
                    if stack:
                        stack[-1][1].body_lines.append(i)
                elif stripped.startswith("\t") or stripped.startswith("    "):
                    if stack:
                        stack[-1][1].body_lines.append(i)

        # Collect body text for each item
        self._collect_bodies(roots, lines)

        return roots

    def _collect_bodies(self, items: List[InboxItem], lines: List[str]):
        """Recursively collect task bodies from body_lines."""
        for item in items:
            if item.body_lines:
                body_texts = []
                for ln in item.body_lines:
                    if 0 <= ln < len(lines):
                        # Strip one indent level (relative to task)
                        text = lines[ln].rstrip("\n\r")
                        # Strip task-level + 1 indentation
                        stripped = text
                        for _ in range(item.indent_level + 1):
                            if stripped.startswith("\t"):
                                stripped = stripped[1:]
                            elif stripped.startswith("    "):
                                stripped = stripped[4:]
                        body_texts.append(stripped)
                item.body = "\n".join(body_texts).strip()
            self._collect_bodies(item.children, lines)

    # ─────────────── compatibility: flat parse ────────────────────────────

    def parse(self) -> Tuple[List[str], List["InboxItem"]]:
        """
        Backward compatibility: returns (lines, parent_items).
        parent_items — root elements only, with last_child_line for insertion.
        """
        lines = self.read_lines()
        roots = self.parse_tree()

        # For backward compatibility add last_child_line
        for root in roots:
            root.last_child_line = root.last_line  # type: ignore[attr-defined]

        return lines, roots

    def get_parent_task_titles(self) -> List[str]:
        """Return list of parent task titles (for LLM prompt)."""
        roots = self.parse_tree()
        return [r.title for r in roots]

    # ─────────────── writing YT links ─────────────────────────────────────

    def write_youtrack_link(self, line_number: int, youtrack_url: str):
        """
        Insert/update [→YT](url) in the specified line.
        Placed before 📅 or at end of line.
        """
        lines = self.read_lines()
        if line_number < 0 or line_number >= len(lines):
            logger.error("Invalid line number: %d", line_number)
            return

        line = lines[line_number].rstrip("\n")

        # Remove old link if present
        line = _YT_LINK_RE.sub("", line)

        yt_marker = f" [→YT]({youtrack_url})"

        # Insert before 📅 if present
        m_date = _DATE_RE.search(line)
        if m_date:
            insert_pos = m_date.start()
            line = line[:insert_pos].rstrip() + yt_marker + " " + line[m_date.start():]
        else:
            line = line.rstrip() + yt_marker

        lines[line_number] = line + "\n"
        self.write_lines(lines)

    def write_youtrack_links_batch(self, updates: List[Tuple[int, str]]):
        """
        Batch insert YT links: [(line_number, youtrack_url), ...].
        More efficient than calling write_youtrack_link one at a time.
        """
        if not updates:
            return

        lines = self.read_lines()

        for line_number, youtrack_url in updates:
            if line_number < 0 or line_number >= len(lines):
                continue

            line = lines[line_number].rstrip("\n")

            # Remove old link
            line = _YT_LINK_RE.sub("", line)

            yt_marker = f" [→YT]({youtrack_url})"

            m_date = _DATE_RE.search(line)
            if m_date:
                insert_pos = m_date.start()
                line = line[:insert_pos].rstrip() + yt_marker + " " + line[m_date.start():]
            else:
                line = line.rstrip() + yt_marker

            lines[line_number] = line + "\n"

        self.write_lines(lines)

    # ─────────────── kanban helpers ───────────────────────────────────────

    def get_columns(self) -> List[str]:
        """Return ordered list of kanban column names from ## headings."""
        lines = self.read_lines()
        columns = []
        for line in lines:
            m = _COLUMN_RE.match(line.rstrip("\n\r"))
            if m:
                columns.append(m.group(1).strip())
        return columns

    def _find_column_end(self, lines: List[str], column_name: str) -> int:
        """
        Find line number of last content line in a column section.
        Returns line before next ## heading (or end of file).
        """
        in_target = False
        last_content_line = -1

        for i, line in enumerate(lines):
            stripped = line.rstrip("\n\r")
            m = _COLUMN_RE.match(stripped)
            if m:
                if in_target:
                    # Found next column — return last content line
                    return last_content_line if last_content_line > 0 else i - 1
                if m.group(1).strip() == column_name:
                    in_target = True
                    last_content_line = i
            elif in_target:
                last_content_line = i

        # Column is last in file
        if in_target:
            return last_content_line if last_content_line > 0 else len(lines) - 1
        return len(lines) - 1

    # ─────────────── inserting tasks ──────────────────────────────────────

    def insert_tasks(self, classified_items: List[Dict]) -> bool:
        """
        Insert action items into the inbox file.

        classified_items: list of dicts with fields:
            - description: str
            - parent_task: str — name of existing parent task,
              "__NEW__:Name" for a new parent, or "" for unassigned
            - due_date: str (YYYY-MM-DD)
            - youtrack_url: str (optional)

        If parent_task matches an existing root task → inserted as child (4-space indent).
        If parent_task = "__NEW__:Name" → new root is created, then child task inserted under it.
        If parent_task is empty → inserted as top-level.

        Returns True on success.
        """
        if not classified_items:
            logger.info("No action items to add to inbox")
            return True

        lines = self.read_lines()
        roots = self.parse_tree()
        default_column = "Active Projects"

        # Map: title → InboxItem (for parent lookup)
        parent_map: Dict[str, InboxItem] = {r.title: r for r in roots}

        # Track line offset from insertions
        offset = 0

        # Group items: with parent, with new parent, without parent
        orphans: List[Dict] = []           # parent_task is empty
        by_parent: Dict[str, List[Dict]] = {}   # parent_task → [items]
        new_parents: Dict[str, List[Dict]] = {} # __NEW__:Name → [items]

        for item in classified_items:
            pt = item.get("parent_task", "").strip()
            if not pt:
                orphans.append(item)
            elif pt.startswith("__NEW__:"):
                new_name = pt[len("__NEW__:"):]
                new_parents.setdefault(new_name, []).append(item)
            else:
                by_parent.setdefault(pt, []).append(item)

        # 1. Insert child tasks under existing parents
        for parent_title, items in by_parent.items():
            parent = parent_map.get(parent_title)
            if parent is None:
                # Parent not found — insert as top-level
                orphans.extend(items)
                continue

            insert_pos = parent.last_line + 1 + offset
            for item in items:
                task_line = self._format_task_line(item, indent_level=1)
                lines.insert(insert_pos, task_line)
                offset += 1
                insert_pos += 1

        # 2. Create new parents with child tasks
        # NB: _find_column_end scans already-modified lines, no offset needed
        if new_parents:
            col_end = self._find_column_end(lines, default_column)
            for parent_name, items in new_parents.items():
                # Insert root task
                root_line = f"- [ ] {parent_name}\n"
                lines.insert(col_end + 1, root_line)
                insert_pos = col_end + 2
                # Insert children
                for item in items:
                    task_line = self._format_task_line(item, indent_level=1)
                    lines.insert(insert_pos, task_line)
                    insert_pos += 1
                col_end = insert_pos - 1

        # 3. Insert orphans (without parent) as top-level
        if orphans:
            col_end = self._find_column_end(lines, default_column)
            for idx, item in enumerate(orphans):
                task_line = self._format_task_line(item, indent_level=0)
                lines.insert(col_end + 1 + idx, task_line)

        self.write_lines(lines)
        logger.info("Added %d action items to inbox", len(classified_items))
        return True

    @staticmethod
    def _format_task_line(item: Dict, indent_level: int = 0) -> str:
        """Format action item as a kanban card with proper indent level."""
        desc = item.get("description", "")
        due = item.get("due_date", "")
        yt_url = item.get("youtrack_url", "")
        indent = "    " * indent_level
        line = f"{indent}- [ ] {desc}"
        if yt_url:
            line += f" [→YT]({yt_url})"
        if due and due not in ("Not specified", "N/A", ""):
            line += f" 📅 {due}"
        line += "\n"
        return line

    # ─────────────── comment extraction ─────────────────────────────────

    @staticmethod
    def extract_comment(item: "InboxItem") -> str:
        """
        Extract comment text from the task body.
        Looks for lines starting with '#comment:'.
        Returns comment text or empty string.
        """
        if not item.body:
            return ""
        for line in item.body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#comment:"):
                return stripped[len("#comment:"):].strip()
        return ""

    # ─────────────── task deletion ──────────────────────────────────────

    def delete_tasks(self, items: List["InboxItem"]):
        """
        Delete tasks from inbox file (task line + its body_lines).
        Does not delete child tasks — only the task line itself and body.
        Before deletion, verifies the line contains expected task text.
        """
        if not items:
            return

        lines = self.read_lines()

        # Collect all line numbers to delete with content verification
        lines_to_remove: set = set()
        skipped = 0
        for item in items:
            ln = item.line_number
            if ln < 0 or ln >= len(lines):
                logger.warning(
                    "Skipping deletion: line %d out of range (total %d lines): %s",
                    ln, len(lines), item.title[:60],
                )
                skipped += 1
                continue

            # Verify line contains YT ID or task title
            line_content = lines[ln].rstrip("\n\r")
            yt_id_match = item.youtrack_id and item.youtrack_id in line_content
            title_fragment = item.title[:30] if item.title else ""
            title_match = title_fragment and title_fragment in line_content

            if not yt_id_match and not title_match:
                logger.warning(
                    "Skipping deletion: line %d does not contain expected text.\n"
                    "  Expected: YT=%s, title='%s'\n"
                    "  Found: '%s'",
                    ln, item.youtrack_id, title_fragment, line_content[:100],
                )
                skipped += 1
                continue

            lines_to_remove.add(ln)
            for bl in item.body_lines:
                if 0 <= bl < len(lines):
                    lines_to_remove.add(bl)

        if skipped:
            logger.warning("Skipped %d tasks due to line mismatch", skipped)

        # Filter lines, keeping only those not in the removal set
        new_lines = [
            line for i, line in enumerate(lines) if i not in lines_to_remove
        ]

        self.write_lines(new_lines)
        logger.info("Deleted %d tasks (%d lines) from inbox", len(items) - skipped, len(lines_to_remove))
