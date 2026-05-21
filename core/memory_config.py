"""Operational memory configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryConfig:
    enabled: bool = True
    prune_max_age_days: int = 90
    min_signal_score: int = 20
    inject_context_into_prompts: bool = True
