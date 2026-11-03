"""Piecewise release shares, evaluated at a valid visit lifecycle boundary."""
from .distributions import rng_for


def mix(curve, elapsed_minutes):
    points = curve['points']
    if elapsed_minutes <= points[0]['minute']: return dict(points[0]['shares'])
    if elapsed_minutes >= points[-1]['minute']: return dict(points[-1]['shares'])
    for left, right in zip(points, points[1:]):
        if left['minute'] <= elapsed_minutes < right['minute']:
            f = (elapsed_minutes - left['minute']) / (right['minute'] - left['minute'])
            return {v: left['shares'].get(v, 0) * (1-f) + right['shares'].get(v, 0) * f
                    for v in left['shares'].keys() | right['shares'].keys()}
    raise ValueError('Release curve has no interval.')


def versions(target, now_ms=0):
    from .browser_session import releases
    p = target['platform']
    return releases(target.get(p, {}), now_ms) if p == 'web' else target[p]['versions']


def validate(curves, spec):
    from .spec import normalized, parse_time
    spec = normalized(spec)
    targets = {t['id']: t for t in spec.get('targets', [])}
    issues, used = [], set()
    for i, curve in enumerate(curves):
        path = f'$.modeling.releases[{i}]'
        target = targets.get(curve['target'])
        if not target:
            issues.append((path, 'Choose a configured release target.')); continue
        if curve['target'] in used: issues.append((path, 'Use one release curve per target.'))
        used.add(curve['target'])
        available = {v['version'] for v in versions(target)}
        last = -1
        for point in curve['points']:
            if point['minute'] <= last: issues.append((path, 'Curve minutes must be strictly increasing.'))
            last = point['minute']
            if set(point['shares']) - available: issues.append((path, 'Curve refers to a version outside the target build catalog.'))
            if abs(sum(point['shares'].values()) - 100) > .001: issues.append((path, 'Release shares at each point must total 100%.'))
        if target['platform'] == 'web':
            # A version needs to be deployed before its interval gains weight.
            for v in target['web'].get('releases', []):
                for j, point in enumerate(curve['points']):
                    if point['shares'].get(v['version'], 0) <= 0: continue
                    minute = curve['points'][max(0, j-1)]['minute']
                    if v.get('deployed') and parse_time(v['deployed']) > parse_time(curve['start']) + minute * 60000:
                        issues.append((path, 'A web release must be deployed before its adoption interval.')); break
    return issues


def choose(target, persona, start_ms, model, seed, fallback):
    from .spec import parse_time
    curve = next((c for c in model.get('releases', []) if c['target'] == target['id']), None)
    if not curve: return fallback
    catalog = versions(target, start_ms)
    # Preserve declared catalog build tuples. Order by build number (native) or
    # deployment time (web), independent of JSON map insertion order.
    catalog = sorted(catalog, key=lambda v: (v.get('deployed', ''), int(v.get('version_code', 0)), v['version']))
    shares = mix(curve, (start_ms - parse_time(curve['start'])) / 60000)
    q = rng_for('release-adoption', seed, target['id'], persona['device_id'], curve.get('salt', '')).random() * 100
    picked = catalog[-1]
    for v in catalog:
        q -= shares.get(v['version'], 0)
        if q < 0:
            picked = v; break
    if target['platform'] == 'web':
        if picked.get('deployed') and parse_time(picked['deployed']) > start_ms:
            return fallback
        return (picked['version'],)
    previous = next((v for v in catalog if v['version'] == persona.get('installed_version')), None)
    if previous and catalog.index(previous) > catalog.index(picked) and not curve.get('allow_rollback', False): picked = previous
    persona['installed_version'] = picked['version']
    code = picked.get('version_code')
    if not code:
        parts = [int(x) for x in picked['version'].split('.')] + [0, 0]
        code = str(parts[0] * 100000 + parts[1] * 1000 + parts[2] * 10)
    return (picked['version'], str(code), picked.get('sdk_version') or target[target['platform']].get('sdk_version', fallback[2]))
