#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional


@dataclass
class CodexRunResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    workspace: str
    modified_files: List[str]
    invocation_style: str

    def to_dict(self):
        return asdict(self)


class CodexRunner:

    def __init__(
        self,
        codex_bin: str = "codex",
        model: str = "gpt-5.4",
        reasoning_effort: str = "medium",
        sandbox_mode: Optional[str] = None,
    ):
        self.codex_bin = codex_bin
        self.model = model
        self.reasoning_effort = reasoning_effort
        # Allow the caller or environment to tune Codex execution permission level.
        self.sandbox_mode = sandbox_mode or os.getenv("CODEX_SANDBOX_MODE", "danger-full-access")

    def build_command(self, prompt: str, repo_dir: Path) -> List[str]:
        """
        IMPORTANT:
        sandbox MUST be passed to exec subcommand
        """

        return [
            self.codex_bin,
            "exec",
            "-m", self.model,
            "-c", f"reasoning_effort={self.reasoning_effort}",
            "-s", self.sandbox_mode,
            "-C", str(repo_dir),
            prompt,
        ]

    def run(self, prompt: str, repo_dir: Path) -> CodexRunResult:

        cmd = self.build_command(prompt, repo_dir)

        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )

        modified = self.detect_modified_files(repo_dir)

        return CodexRunResult(
            command=cmd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            workspace=str(repo_dir),
            modified_files=modified,
            invocation_style=f"exec-subcommand-sandbox:{self.sandbox_mode}",
        )

    @staticmethod
    def detect_modified_files(repo_dir: Path) -> List[str]:
        try:
            p = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            files = []
            for line in p.stdout.splitlines():
                if line.strip():
                    files.append(line[3:])
            return files
        except Exception:
            return []

    @staticmethod
    def shell(cmd: List[str]) -> str:
        return " ".join(shlex.quote(x) for x in cmd)
