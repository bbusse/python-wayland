# Track sizing for the grid body layout. Pure, no cairo, no pango. The
# renderer measures its own cells and hands the sizes back in


def parse_track(spec):
    s = str(spec).strip().lower()
    if s == "auto":
        return ("auto", 0.0)
    if s.endswith("fr"):
        return ("fr", float(s[:-2] or 1))
    if s.endswith("px"):
        return ("px", float(s[:-2]))

    return ("px", float(s))


def size_tracks(specs, available, gap, auto_sizes=None):
    '''
    specs      list of "auto" | "Nfr" | "Npx" | a bare number of px
    available  total px for every track plus the gaps between them
    gap        px between adjacent tracks
    auto_sizes {index: px} measured content size for the auto tracks

    Returns [(offset, size), ...], one per track. fr tracks share whatever
    the px and auto tracks leave. If they leave nothing the fr tracks are 0
    and the fixed ones overflow
    '''
    auto_sizes = auto_sizes or {}
    parsed = [parse_track(s) for s in specs]
    inner = available - gap * max(0, len(specs) - 1)

    sizes = [0.0] * len(specs)
    fixed = 0.0
    fr_total = 0.0
    for i, (kind, val) in enumerate(parsed):
        if kind == "px":
            sizes[i] = max(0.0, val)
            fixed += sizes[i]
        elif kind == "auto":
            sizes[i] = max(0.0, auto_sizes.get(i, 0.0))
            fixed += sizes[i]
        else:
            fr_total += val

    leftover = max(0.0, inner - fixed)
    for i, (kind, val) in enumerate(parsed):
        if kind == "fr":
            sizes[i] = leftover * val / fr_total if fr_total else 0.0

    out = []
    pos = 0.0
    for sz in sizes:
        out.append((pos, sz))
        pos += sz + gap

    return out


def align_offset(align, content, available):
    slack = max(0.0, available - content)
    if align == "center":
        return slack / 2
    if align == "end":
        return slack

    return 0.0


def cell_rect(cell, cols, rows, gap):
    '''Pixel rectangle (x, y, w, h) a cell spans, given the sized tracks'''
    c0 = cell.get("col", 0)
    r0 = cell.get("row", 0)
    c1 = min(c0 + max(1, cell.get("colspan", 1)), len(cols)) - 1
    r1 = min(r0 + max(1, cell.get("rowspan", 1)), len(rows)) - 1

    x = cols[c0][0]
    y = rows[r0][0]
    w = cols[c1][0] + cols[c1][1] - x
    h = rows[r1][0] + rows[r1][1] - y

    return x, y, max(0.0, w), max(0.0, h)
