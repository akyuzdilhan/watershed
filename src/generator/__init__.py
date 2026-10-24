"""Seeded synthetic football matches: an event feed with its hidden ground truth."""

from generator.engine import generate_match
from generator.feed import GENERATOR_VERSION, SCHEMA_VERSION

__all__ = ["GENERATOR_VERSION", "SCHEMA_VERSION", "generate_match"]
