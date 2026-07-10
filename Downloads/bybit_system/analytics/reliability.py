from dataclasses import dataclass


@dataclass(frozen=True)
class ReliabilityThresholds:
    insufficient: int = 20
    preliminary: int = 50
    usable: int = 200


def reliability_status(sample_size: int, thresholds: ReliabilityThresholds = ReliabilityThresholds()) -> str:
    if sample_size < thresholds.insufficient:
        return "insufficient"
    if sample_size < thresholds.preliminary:
        return "preliminary"
    if sample_size < thresholds.usable:
        return "usable"
    return "strong"
