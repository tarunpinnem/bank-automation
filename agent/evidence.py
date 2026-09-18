"""Structured evidence capture, shared by discovery and replay.

Every run gets its own directory under /evidence/<run_id>/ containing:
  - run.jsonl   -- one structured line per event (step, decision, outcome)
  - a screenshot on failure (and optionally at each step for discovery)
Log lines are redacted before being written.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .safety import redact_text

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"


class EvidenceWriter:
    def __init__(self, run_kind: str, capability_id: str, sensitive_values: list[str] | None = None):
        self.run_id = f"{run_kind}_{capability_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.dir = EVIDENCE_ROOT / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "run.jsonl"
        self.sensitive_values = sensitive_values or []
        self._log_fh = open(self.log_path, "a")

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, default=str)
        line = redact_text(line, self.sensitive_values)
        self._log_fh.write(line + "\n")
        self._log_fh.flush()

    def screenshot(self, page, name: str) -> str:
        path = self.dir / f"{name}.png"
        try:
            page.screenshot(path=str(path))
        except Exception as e:
            self.log("screenshot_failed", name=name, error=str(e))
            return ""
        return str(path)

    def dom_snapshot(self, page, name: str) -> str:
        path = self.dir / f"{name}.html"
        try:
            content = redact_text(page.content(), self.sensitive_values)
            path.write_text(content)
        except Exception as e:
            self.log("dom_snapshot_failed", name=name, error=str(e))
            return ""
        return str(path)

    def close(self) -> None:
        self._log_fh.close()
