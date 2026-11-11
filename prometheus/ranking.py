"""Distancia exploratoria de descriptores, sin porcentajes de similitud."""

from .data import FEATURES


def rank_candidates(target, candidates):
    results = []
    for candidate in candidates:
        differences = {key: abs(candidate[key] - target[key]) for key in FEATURES}
        distance = sum(differences[key] / scale for key, (_, scale) in FEATURES.items()) / len(FEATURES)
        results.append({"site": candidate, "distance": distance, "differences": differences})
    # Los empates se ordenan por nombre solo para una presentación estable.
    return sorted(results, key=lambda row: (row["distance"], row["site"]["name"]))
