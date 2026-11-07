"""Mapping between server catalog keys and YAMNet (AudioSet) class display names.

Each catalog sound has one or more label rules. A plain label counts at any score; a rule
may require a minimum score, or be vetoed when a confusable label scores strictly higher
(e.g. "Chime" is a doorbell unless "Wind chime" is stronger). A sound's confidence for a
window is the highest score of any rule that qualifies, else 0.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LabelRule:
    """A YAMNet class that counts towards a catalog sound."""

    name: str
    min_score: float = 0.0
    unless_stronger: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexRule:
    """A ``LabelRule`` resolved to model class indices."""

    index: int
    min_score: float = 0.0
    unless_stronger: tuple[int, ...] = ()


def _rules(labels: Mapping[str, Sequence[str | LabelRule]]) -> dict[str, tuple[LabelRule, ...]]:
    """Plain strings become unconditional rules."""
    return {
        key: tuple(r if isinstance(r, LabelRule) else LabelRule(r) for r in rules)
        for key, rules in labels.items()
    }


# Generic classes (Door, Dog, Breathing, Breaking, Bang, Grunt) are deliberately omitted:
# they fire constantly in ordinary homes and would flood users with false alarms.
YAMNET_LABELS: dict[str, tuple[LabelRule, ...]] = _rules(
    {
        "scream": ("Screaming",),
        "shouting": ("Shout", "Yell", "Bellow"),
        "crying": ("Crying, sobbing", "Baby cry, infant cry", "Whimper"),
        # Grunt omitted: too common in everyday exertion.
        "moaning": ("Groan",),
        # Breathing omitted: fires on normal breathing.
        "gasping": ("Gasp", "Wheeze"),
        "coughing": ("Cough",),
        # Breaking omitted: matches any breaking object. Plain "Glass" also fires on clinks
        # and chimes (a "ding dong" scored Glass 0.41 live), so it needs a strong score.
        "glass_breaking": ("Shatter", LabelRule("Glass", min_score=0.60)),
        # Door omitted: matches every door sound.
        "door_slam": ("Slam",),
        "knocking": ("Knock",),
        "car_alarm": ("Car alarm",),
        # Dog omitted: matches panting and whining, not just barking.
        "dog_barking": ("Bark", "Bow-wow"),
        "smoke_alarm": ("Smoke detector, smoke alarm", "Fire alarm"),
        "siren": (
            "Siren",
            "Emergency vehicle",
            "Police car (siren)",
            "Ambulance (siren)",
            "Fire engine, fire truck (siren)",
            "Civil defense siren",
        ),
        # Bang omitted: matches routine knocks and pops.
        "explosion": ("Explosion",),
        "running_water": (
            "Water tap, faucet",
            "Sink (filling or washing)",
            "Bathtub (filling or washing)",
        ),
        "thud_fall": ("Thump, thud",),
        # Chime-style doorbells score as Chime/Ding rather than Doorbell (live test, 7 Nov).
        "doorbell": (
            "Doorbell",
            "Ding-dong",
            LabelRule("Chime", min_score=0.50, unless_stronger=("Wind chime",)),
            LabelRule("Ding", min_score=0.50),
        ),
    }
)

CATALOG_KEYS: frozenset[str] = frozenset(YAMNET_LABELS)


def build_index(class_names: Sequence[str]) -> dict[str, tuple[IndexRule, ...]]:
    """Resolve each catalog key's rules to model class indices."""
    position = {name: i for i, name in enumerate(class_names)}
    used = [
        name
        for rules in YAMNET_LABELS.values()
        for rule in rules
        for name in (rule.name, *rule.unless_stronger)
    ]
    missing = list(dict.fromkeys(name for name in used if name not in position))
    if missing:
        raise ValueError(f"labels missing from model class map: {missing}")
    return {
        key: tuple(
            IndexRule(position[r.name], r.min_score, tuple(position[n] for n in r.unless_stronger))
            for r in rules
        )
        for key, rules in YAMNET_LABELS.items()
    }


def sound_confidence(scores: np.ndarray, rules: Sequence[IndexRule]) -> float:
    """Highest score among the qualifying rules: at least ``min_score`` and no
    ``unless_stronger`` label scoring strictly higher. 0.0 if none qualifies."""
    best = 0.0
    for rule in rules:
        s = float(scores[rule.index])
        if s < rule.min_score or s <= best:
            continue
        if any(float(scores[i]) > s for i in rule.unless_stronger):
            continue
        best = s
    return best
