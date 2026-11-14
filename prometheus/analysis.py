"""Optional weighting adapter. Original ranking and descriptor scales are unchanged."""

from .data import FEATURES
from .ranking import rank_candidates


def compare_topography(target, candidate, weights):
    active = [key for key in FEATURES if weights[key] > 0]
    missing = [key for key in active if target.get(key) is None or candidate.get(key) is None]
    if missing:
        raise ValueError("Faltan descriptores con peso positivo: " + ", ".join(missing))
    complete = all(target.get(key) is not None and candidate.get(key) is not None for key in FEATURES)
    baseline = rank_candidates(target, [candidate])[0] if complete else None
    differences = baseline["differences"] if baseline else {
        key: abs(candidate[key] - target[key]) for key in FEATURES
        if target.get(key) is not None and candidate.get(key) is not None
    }
    if baseline and len(set(weights.values())) == 1:
        distance = baseline["distance"]
    else:
        total = sum(weights[key] for key in active)
        distance = sum((weights[key] / total) * differences[key] / FEATURES[key][1] for key in active)
    return distance, differences, baseline["distance"] if baseline else None
