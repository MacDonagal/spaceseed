from datetime import datetime, timezone


def calculate_score(
    *,
    seeders: int,
    leechers: int,
    freeleech: bool,
    published_at: datetime | None,
    size_bytes: int,
) -> float:
    seeders = max(seeders, 0)
    leechers = max(leechers, 0)

    demand_score = min(leechers * 4.0, 120.0)
    competition_penalty = min(seeders * 1.5, 100.0)
    ratio_bonus = min((leechers / max(seeders, 1)) * 20.0, 80.0)
    freeleech_bonus = 60.0 if freeleech else 0.0

    age_bonus = 0.0
    if published_at:
        published = published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_minutes = max((datetime.now(timezone.utc) - published).total_seconds() / 60, 0)
        if age_minutes <= 30:
            age_bonus = 50.0
        elif age_minutes <= 120:
            age_bonus = 30.0
        elif age_minutes <= 360:
            age_bonus = 10.0

    size_gb = size_bytes / (1024 ** 3)
    size_bonus = 10.0 if 0 < size_gb <= 20 else 0.0

    return round(demand_score + ratio_bonus + freeleech_bonus + age_bonus + size_bonus - competition_penalty, 2)
