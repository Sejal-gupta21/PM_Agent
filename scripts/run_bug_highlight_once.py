#!/usr/bin/env python3
"""
One-off runner to execute the bug areas highlight detection and email flow immediately.

Usage: python3 scripts/run_bug_highlight_once.py
"""

import sys
import yaml
from pathlib import Path

# Ensure repo root is on sys.path so imports work when running this script
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utilities.bug_areas_highlight import run_task_from_config


def load_config(path: Path):
	if not path.exists():
		return {}
	with open(path, "r", encoding="utf-8") as fh:
		return yaml.safe_load(fh) or {}


if __name__ == "__main__":
	cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
	cfg = load_config(cfg_path)
	# gather task options (first task) and recipients
	task_opts = {}
	if cfg.get("schedulerConfig"):
		tasks = cfg.get("schedulerConfig", {}).get("tasks", [])
		if tasks:
			task_opts = tasks[0].get("options", {})

	task_cfg = {
		"options": task_opts,
		"reportEmailRecipients": cfg.get("reportEmailRecipients", []),
	}
	run_task_from_config(task_cfg)
