"""Bounded, passive reliability evidence; speed and availability stay separate."""
from __future__ import annotations

import math

WINDOW = 100
STORED_SAMPLES = 1000
MAX_AGE = 24 * 3600
TIME_HALF_LIFE = 6 * 3600
REQUEST_HALF_LIFE = 20
PRIOR_RATE = .9
PRIOR_WEIGHT = 2.0


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


def summarize(samples, now, *, model=None, path=None, stream=None):
    recent = [s for s in samples if 0 <= now - s['at'] < MAX_AGE
              and (model is None or s['model'] == model)
              and (path is None or s['path'] == path)][-WINDOW:]
    weight = successes = 0.0
    speed_weight = speed_sum = 0.0
    for index, sample in enumerate(reversed(recent)):
        w = 2 ** (-(now - sample['at']) / TIME_HALF_LIFE - index / REQUEST_HALF_LIFE)
        weight += w
        successes += w * sample['ok']
        if sample['ok'] and sample.get('latency') is not None and (stream is None or sample['stream'] == stream):
            speed_weight += w
            speed_sum += w * sample['latency']
    count = len(recent)
    score = 100 * (successes + PRIOR_RATE * PRIOR_WEIGHT) / (weight + PRIOR_WEIGHT) if count else None
    confidence = ('none' if not count else 'low' if count < 5 or weight < 3
                  else 'medium' if count < 20 or weight < 10 else 'high')
    return dict(score=score, business_samples=count, effective_samples=round(weight, 2),
                success_rate=sum(s['ok'] for s in recent) / count if count else None,
                confidence=confidence, last_sample_at=recent[-1]['at'] if count else None,
                latency=speed_sum / speed_weight if speed_weight else None,
                window_hours=24, max_samples=WINDOW, time_half_life_hours=6,
                request_half_life=REQUEST_HALF_LIFE, prior_rate=PRIOR_RATE, prior_weight=PRIOR_WEIGHT)


def affects_health(error):
    """Client input, quota and throttling are not reliability failures."""
    if getattr(error, 'quota', False) or getattr(error, 'status_code', 0) == 429:
        return False
    code = getattr(error, 'status_code', 0)
    return code != 413 and not (code in (400, 422) and not getattr(error, 'retryable', False))
