"""回収した計測を、サーバーの時刻帯で窓に絞る。"""
import collections
import datetime as dt
import re


def timezone(value):
    if not re.fullmatch(r'[+-]\d{2}:?\d{2}', value):
        return None
    sign = -1 if value[0] == '-' else 1
    digits = value[1:].replace(':', '')
    return dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def sample_time(tokens, meta, offset):
    if not tokens:
        return None
    if re.fullmatch(r'\d{9,}(?:\.\d+)?', tokens[0]):
        return float(tokens[0])
    clock = next((x for x in reversed(tokens) if re.fullmatch(r'\d{2}:\d{2}:\d{2}', x)), None)
    tz = timezone(offset)
    if not clock or tz is None or not meta.get('START'):
        return None
    date = next((x for x in tokens if re.fullmatch(r'\d{4}-\d{2}-\d{2}', x)), None)
    if date:
        return dt.datetime.fromisoformat(date + 'T' + clock).replace(tzinfo=tz).timestamp()
    # pidstat に日付が無い場合も、日付をまたいだ窓に対応する。
    reference = float(meta['START'])
    day = dt.datetime.fromtimestamp(reference, tz).date()
    candidates = [dt.datetime.combine(day + dt.timedelta(days=d), dt.time.fromisoformat(clock), tz).timestamp() for d in [-1, 0, 1]]
    return min(candidates, key=lambda t: abs(t - reference))


def in_window(tokens, meta, offset):
    if not meta.get('START') or not meta.get('END'):
        return False
    t = sample_time(tokens, meta, offset)
    return t is not None and float(meta['START']) <= t < float(meta['END']) + 1


def cpu_table(text, meta, offset):
    cols = {}; samples = []
    for line in text.splitlines():
        cells = line.split()
        if cells and cells[0] == 'r':
            cols = {key: i for i, key in enumerate(cells)}
        elif cells and cells[0].isdigit() and cols and in_window(cells, meta, offset):
            try:
                samples.append([float(cells[cols[k]]) for k in ['us', 'sy', 'wa', 'id', 'r']])
            except (ValueError, KeyError, IndexError):
                continue
    if not samples:
        return None
    return [sum(row[i] for row in samples) / len(samples) for i in range(4)] + [max(row[4] for row in samples), len(samples)]


def processes(text, meta, offset):
    cols = {}; seconds = set(); totals = collections.Counter()
    for line in text.splitlines():
        cells = line.split()
        if '%CPU' in cells and 'Command' in cells:
            if cells[0] == '#':
                cells = cells[1:]
            cols = {key: i for i, key in enumerate(cells)}
        elif cells and cols and in_window(cells[:1], meta, offset):
            try:
                seconds.add(sample_time(cells[:1], meta, offset))
                totals[' '.join(cells[cols['Command']:])] += float(cells[cols['%CPU']])
            except (ValueError, IndexError):
                continue
    return [(name, value / len(seconds)) for name, value in totals.most_common(8)] if seconds else []


def profile(text):
    lines = []; active = False
    for line in text.splitlines():
        if line.startswith('# Profile'):
            active = True
        if active:
            if not line.strip():
                break
            lines.append(line)
    return lines


def journal_counts(text):
    counts = collections.Counter()
    for line in text.splitlines():
        if not re.search(r'error|panic|fatal|warn|fail|timeout|refused', line, re.I):
            continue
        line = re.sub(r'^\S+\s+\S+\s+', '', line)
        line = re.sub(r'\[\d+\]', '', line)
        line = re.sub(r'\d{4}/\d{2}/\d{2} [0-9:.]+', '', line)
        line = re.sub(r'\b[0-9A-Z]{20,}\b', '<id>', line)
        counts[line] += 1
    return [f'{n:6d} {line}' for line, n in counts.most_common(40)]
