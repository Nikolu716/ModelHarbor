"""Passive reliability with a bounded speed contribution; protection is separate."""
from __future__ import annotations

import math

WINDOW = 100
STORED_SAMPLES = 1000
MAX_AGE = 24 * 3600
TIME_HALF_LIFE = 6 * 3600
REQUEST_HALF_LIFE = 20
PRIOR_RATE = .9
PRIOR_WEIGHT = 2.0
SPEED_WEIGHT = .15
SPEED_PRIOR = 50.0
SPEED_PRIOR_WEIGHT = 2.0
# Product defaults, not provider SLAs. Streaming measures first content;
# non-streaming measures the complete response and has a wider allowance.
SPEED_BOUNDS = {True: (2.0, 30.0), False: (10.0, 120.0)}


def validate_speed_weight(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("scoring_speed_weight 必须是 0 到 1 之间的数字")
    return float(value)


def speed_score(latency, stream):
    """Clamp each observation before averaging so one outlier stays bounded."""
    fast, slow = SPEED_BOUNDS[bool(stream)]
    if latency <= fast:
        return 100.0
    if latency >= slow:
        return 0.0
    return 100 * (1 - math.log(latency / fast) / math.log(slow / fast))


def confidence(count, weight):
    return ('none' if not count else 'low' if count < 5 or weight < 3
            else 'medium' if count < 20 or weight < 10 else 'high')


def valid_samples(samples, now):
    """Validate persisted samples without refreshing their original timestamps."""
    clean = []
    if not isinstance(samples, (list, tuple)):
        return clean
    for sample in samples[-STORED_SAMPLES:]:
        if not isinstance(sample, dict) or not isinstance(sample.get('ok'), bool):
            continue
        at = sample.get('at')
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
            continue
        if not 0 <= now - at < MAX_AGE:
            continue
        model, path = sample.get('model', ''), sample.get('path', '')
        if not isinstance(model, str) or not isinstance(path, str):
            continue
        latency = sample.get('latency')
        if (isinstance(latency, bool) or not isinstance(latency, (int, float))
                or not math.isfinite(latency) or latency < 0):
            latency = None
        clean.append(dict(at=at, ok=sample['ok'], model=model[:256], path=path[:128],
                          stream=bool(sample.get('stream')), latency=latency))
    return sorted(clean, key=lambda item: item['at'])


def summarize(samples, now, *, model=None, path=None, stream=None, speed_weight=SPEED_WEIGHT):
    recent = [s for s in samples if 0 <= now - s['at'] < MAX_AGE
              and (model is None or s['model'] == model)
              and (path is None or s['path'] == path)][-WINDOW:]
    weight = successes = 0.0
    speed_evidence_weight = speed_sum = latency_sum = 0.0
    speed_count = 0
    for index, sample in enumerate(reversed(recent)):
        w = 2 ** (-(now - sample['at']) / TIME_HALF_LIFE - index / REQUEST_HALF_LIFE)
        weight += w
        successes += w * sample['ok']
        if sample['ok'] and sample.get('latency') is not None and (stream is None or sample['stream'] == stream):
            speed_evidence_weight += w
            latency_sum += w * sample['latency']
            speed_sum += w * speed_score(sample['latency'], sample['stream'])
            speed_count += 1
    count = len(recent)
    reliability = 100 * (successes + PRIOR_RATE * PRIOR_WEIGHT) / (weight + PRIOR_WEIGHT) if count else None
    speed = ((speed_sum + SPEED_PRIOR * SPEED_PRIOR_WEIGHT) / (speed_evidence_weight + SPEED_PRIOR_WEIGHT)
             if speed_count else None)
    effective_speed = speed if speed is not None else SPEED_PRIOR
    factor = 1 - speed_weight + speed_weight * effective_speed / 100
    score = reliability * factor if reliability is not None else None
    return dict(score=score, reliability_score=reliability, speed_score=speed,
                speed_weight=speed_weight, speed_factor=factor,
                speed_penalty=reliability - score if score is not None else None,
                speed_samples=speed_count, speed_confidence=confidence(speed_count, speed_evidence_weight),
                speed_prior=SPEED_PRIOR, speed_prior_weight=SPEED_PRIOR_WEIGHT,
                speed_bounds={"stream": list(SPEED_BOUNDS[True]), "nonstream": list(SPEED_BOUNDS[False])},
                business_samples=count, effective_samples=round(weight, 2),
                success_rate=sum(s['ok'] for s in recent) / count if count else None,
                confidence=confidence(count, weight), last_sample_at=recent[-1]['at'] if count else None,
                latency=latency_sum / speed_evidence_weight if speed_evidence_weight else None,
                window_hours=24, max_samples=WINDOW, time_half_life_hours=6,
                request_half_life=REQUEST_HALF_LIFE, prior_rate=PRIOR_RATE, prior_weight=PRIOR_WEIGHT)


def affects_health(error):
    """Client input, quota and throttling are not reliability failures."""
    if getattr(error, 'quota', False) or getattr(error, 'status_code', 0) == 429:
        return False
    code = getattr(error, 'status_code', 0)
    return code != 413 and not (code in (400, 422) and not getattr(error, 'retryable', False))
