"""Structurally compare a converted save against a genuine 4.04 save.

usage: python compare_saves.py <converted.sav> <reference_4.04.sav>
"""
import re, struct, sys
from collections import Counter
from pathlib import Path

from w3_downgrade import (CHUNK, TARGET, Save, StreamWalker, object_class, parse_container,
                          read_props, read_struct_array)

results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f'[{"PASS" if ok else "FAIL"}] {name}' + (f'\n         {detail}' if detail else ''))


def info(name, detail):
    print(f'[info] {name}\n         {detail}')


def container_layout(data):
    nchunks, hdr = struct.unpack_from('<II', data, 8)
    table = [struct.unpack_from('<III', data, 16 + 12 * k) for k in range(nchunks)]
    pos, problems = hdr, []
    for k, (comp, decomp, end) in enumerate(table):
        pos += comp
        last = k == nchunks - 1
        if decomp != CHUNK and not last: problems.append(f'chunk {k} decompressed size {decomp}')
        if end != (0 if last else pos): problems.append(f'chunk {k} end offset {end}')
    if any(data[16 + 12 * nchunks:hdr]): problems.append('header padding not zero')
    return data[:8], hdr, nchunks, problems


def stamps(body):
    return Counter(struct.unpack_from('<III', body, m.start() + 4) for m in re.finditer(b'SXAP', body))


def stream_layout(s):
    """{owning root block: set of section tags that follow its nested streams' version stamps}."""
    roots = sorted((off, s.name(n)) for n, off in s.roots)
    sizes = dict(s.records)
    out = {}
    for m in re.finditer(b'SXAP', s.body):
        owner = next(name for off, name in reversed(roots) if off <= m.start() < off + sizes[off])
        t = s.body[m.start() + 16:m.start() + 20]
        out.setdefault(owner, set()).add(t.decode() if t.isalpha() and t.isupper() else '<count>')
    return out


def tags(s):
    known = {b'BS', b'VL', b'SS', b'OP'}
    c = Counter(s.body[o:o+2] for o, _ in s.records)
    return {t.decode(): n for t, n in c.items() if t in known}, sum(n for t, n in c.items() if t not in known)


def schema(s):
    """(parent block, variable, type) for every variable, with nesting from the record index."""
    out, stack = set(), []
    for off, size in sorted(s.records, key=lambda r: (r[0], -r[1])):
        while stack and off >= stack[-1][0]:
            stack.pop()
        tag, n, t = s.record(off)
        if tag == b'VL':
            out.add((stack[-1][1] if stack else '', s.name(n), s.name(t)))
        elif tag == b'BS':
            stack.append((off + size, s.name(n)))
    return out


class StreamContents:
    """Every (block, property) -> types seen while walking a save's property streams
    with the 4.04 item layout; `error` is set if any stream cannot be walked that way."""

    def __init__(self, s):
        self.props = {}
        self.error = None
        add = lambda block, name, typ: self.props.setdefault((block, name), set()).add(typ)
        self.walker = StreamWalker(s, 0, on_value=add)
        try:
            self.walker.walk()
        except (ValueError, struct.error, IndexError) as e:
            self.error = str(e)
        self.blocks = {block for block, _ in self.props}


def skill_trees(s):
    """Per player ability manager: [(skillType, field names, bought level)] in array order."""
    trees = []

    def grab(block, name, typ, v):
        if name == 'abilityManager' and object_class(s, v) == 'W3PlayerAbilityManager':
            props, _ = read_props(v, 8)
            skills = next(val for n, _, val in props if s.name(n) == 'skills')
            rows = []
            for el in read_struct_array(skills):
                f = {s.name(n): w for n, _, w in el}
                kind = s.name(struct.unpack('<H', f['skillType'])[0]) if 'skillType' in f else None
                bought = f.get('isCoreSkill', b'\0') == b'\0' and struct.unpack('<i', f.get('level', bytes(4)))[0]
                rows.append((kind, tuple(sorted(f)), bought))
            trees.append(rows)
    try:
        StreamWalker(s, 0, rewrite=grab).walk()
    except (ValueError, struct.error, IndexError):
        return None
    return trees


def equipment(s):
    """Player level, skill slots unlocked and mutagens equipped above it (4.04 unlock levels),
    and the equipped mutagens as each of their three references sees them."""
    st = {'level': None, 'early': [], 'in_mutagen_slots': set(), 'in_item_slots': set(), 'mounted': set()}
    opened = []  # (kind, number, level it opens at) of unlocked skill slots and filled mutagen slots
    i32 = lambda d, k: struct.unpack('<i', d.get(k, bytes(4)))[0]

    def grab(block, name, typ, v):
        cls = object_class(s, v)
        if cls == 'W3LevelManager' and st['level'] is None:
            props, _ = read_props(v, 8)
            st['level'] = i32({s.name(n): w for n, _, w in props}, 'level')
        elif name == 'abilityManager' and cls == 'W3PlayerAbilityManager':
            props = {s.name(n): w for n, _, w in read_props(v, 8)[0]}
            for el in read_struct_array(props.get('skillSlots', bytes(4))):
                d = {s.name(n): w for n, _, w in el}
                if d.get('unlocked', b'\0') != b'\0':
                    opened.append(('skill slot', i32(d, 'id'), i32(d, 'unlockedOnLevel')))
            for k, el in enumerate(read_struct_array(props.get('mutagenSlots', bytes(4)))):
                d = {s.name(n): w for n, _, w in el}
                item = read_props(d['item'], 0)[0] if 'item' in d else []
                if item:
                    st['in_mutagen_slots'].add(struct.unpack('<I', item[0][2])[0])
                    opened.append(('mutagen slot', k + 1, i32(d, 'unlockedAtLevel')))
        elif name == 'itemSlots' and typ == 'array:2,0,SItemUniqueId':
            slots = read_struct_array(v)  # indexed by EEquipmentSlots; EES_SkillMutagen1-4 = 22-25
            st['in_item_slots'] |= {struct.unpack('<I', el[0][2])[0] for el in slots[22:26] if el}

    def item(inv, name, uid, mask):
        if mask & 0x20 and 'mutagen' in name.lower():
            st['mounted'].add(uid)
    try:
        StreamWalker(s, 0, rewrite=grab, rewrite_item=item).walk()
    except (ValueError, struct.error, IndexError):
        return None
    st['early'] = [x for x in opened if st['level'] is not None and x[2] > st['level']]
    return st


def ss_sizes_ok(s):
    bad = [(o, z) for o, z in s.records
           if s.body[o:o+2] == b'SS' and struct.unpack_from('<I', s.body, o + 2)[0] + 6 != z]
    return bad


def main(conv_path, ref_path):
    raw_c, raw_r = Path(conv_path).read_bytes(), Path(ref_path).read_bytes()
    c, r = Save(parse_container(raw_c)), Save(parse_container(raw_r))
    print(f'converted: {conv_path}\nreference: {ref_path}\n')

    lc, lr = container_layout(raw_c), container_layout(raw_r)
    check('container magic + header size match', lc[:2] == lr[:2], f'{lc[:2]} vs {lr[:2]}')
    check('container chunk table well-formed (converted)', not lc[3], '; '.join(lc[3]))
    check('container chunk table well-formed (reference)', not lr[3], '; '.join(lr[3]))

    check('SAV3 header version matches 4.04', c.version == r.version == TARGET, f'{c.version} vs {r.version}')
    check('trailer layout parses (RB / NM / MANU / ENOD / SC / SE)', True)

    bad_c = [x for x in c.records if not (0 <= x[0] and x[0] + x[1] <= c.data_end)]
    check('every index entry lies inside the record data', not bad_c, str(bad_c[:5]))
    roots_ok = all(c.record(o)[0] == b'BS' and c.record(o)[1] == n for n, o in c.roots)
    check('every root-index entry points at its named block', roots_ok)
    bad_ss = ss_sizes_ok(c)
    check('every nested stream inline size agrees with the index', not bad_ss, str(bad_ss[:5]))

    rc, rr = [c.name(n) for n, _ in c.roots], [r.name(n) for n, _ in r.roots]
    check('root blocks: same names in same order', rc == rr,
          f'only converted: {[x for x in rc if x not in rr]}  only 4.04: {[x for x in rr if x not in rc]}')

    sc_, sr = stamps(c.body), stamps(r.body)
    check('no nested stream is stamped newer than 4.04', all(v[0] <= TARGET[0] for v in sc_),
          f'converted {dict(sc_)}  reference {dict(sr)}')
    lc_, lr_ = stream_layout(c), stream_layout(r)
    check('nested streams in each block start with the same section layout',
          lc_.keys() == lr_.keys() and all(lc_[k] <= lr_[k] for k in lc_), f'converted {lc_}\n         reference {lr_}')

    tc, tr = tags(c), tags(r)
    info('record tag counts (converted vs 4.04)', f'{tc[0]} + {tc[1]} raw  vs  {tr[0]} + {tr[1]} raw')

    # the 4.04 save is a different playthrough, so judge format by what 4.04 *knows*
    # (its string table), not by what that particular save happens to contain
    a, b = schema(c), schema(r)
    known = set(r.names)
    types_c = {t for _, _, t in a}
    check('every variable type is one the 4.04 save knows', types_c <= known,
          f'unknown types: {sorted(types_c - known)}')
    fields = {n for p, n, _ in a if p in {q for q, _, _ in b}}
    check('every field in blocks shared with 4.04 has a name 4.04 knows', fields <= known,
          f'unknown fields: {sorted(fields - known)}')
    check('no 5.0-only blocks remain', not ({'InitialBuildIDBlock', 'CModHandlerSystem', 'ModNameSaveData'}
                                              & {p for p, _, _ in a}))

    parents = {p for p, _, _ in a} & {p for p, _, _ in b}
    ta = {(p, n): t for p, n, t in a}; tb = {(p, n): t for p, n, t in b}
    clash = sorted(f'{k}: {ta[k]} vs {tb[k]}' for k in ta.keys() & tb.keys() if ta[k] != tb[k])
    check('no variable has a different type than in 4.04', not clash, '\n         '.join(clash))
    extra = sorted(f'{x}' for x in a - b if x[0] in parents)
    info(f'{len(extra)} variable(s) in shared blocks that this 4.04 save lacks '
         '(may be game-progress differences, not format differences)', '\n         '.join(extra) or '-')

    si = lambda s: [(n, t) for p, n, t in sorted(schema(s)) if p == 'saveInfo']
    check('saveInfo fields match 4.04', si(c) == si(r), f'{si(c)}\n         vs {si(r)}')

    print('\n-- inside the property streams --')
    sc, sr = StreamContents(c), StreamContents(r)
    for tag, w in [('converted', sc), ('reference', sr)]:
        check(f'every property stream walks end to end with the 4.04 item layout ({tag})', not w.error,
              w.error or f'{w.walker.streams} streams, {w.walker.inventories} inventories, '
                          f'{w.walker.items} items, {len(w.props)} distinct block/property pairs')
    stypes = {t for v in sc.props.values() for t in v}
    check('every stream property type is one the 4.04 save knows', stypes <= known,
          f'unknown types: {sorted(stypes - known)}')
    blocks = sc.blocks - {''}  # '' is the top level of a stream
    check('every stream block name is one the 4.04 save knows', blocks <= known,
          f'unknown blocks: {sorted(blocks - known)}')
    # a timer stores Float values when its isRealTime flag is set and GameTime otherwise,
    # in both versions; a save without running real-time timers only shows GameTime
    timer_float = lambda k, t: k[0] == 'timers' and t - sr.props[k] == {'Float'}
    clash = sorted(f'{k}: {sc.props[k]} vs {sr.props[k]}' for k in sc.props.keys() & sr.props.keys()
                   if not sc.props[k] <= sr.props[k] and not timer_float(k, sc.props[k]))
    check('no stream property has a type 4.04 does not use for it', not clash, '\n         '.join(clash))
    unknown = sorted(f'{k}' for k in sc.props if k[1] not in known)
    info(f'{len(unknown)} stream propert(ies) whose name this 4.04 save never uses '
         '(new in 5.0, from a mod, or just unused there; each is self-sized, so a reader that '
         'does not know it can skip it)', '\n         '.join(unknown) or '-')
    progress = [k for k in sc.props.keys() - sr.props.keys() if k[1] in known and k[0] in sr.blocks]
    info('stream properties only in the converted save but named in 4.04 (game-progress differences)',
         str(len(progress)))

    print('\n-- player skills --')
    tc, tr = skill_trees(c), skill_trees(r)
    layout = lambda t: [row[:2] for row in t]
    check('every player skill tree has the 4.04 layout (same skill at every index, same fields)',
          bool(tc and tr) and all(layout(t) == layout(tr[0]) for t in tc),
          f'{len(tc or [])} tree(s); converted {len(tc[0]) if tc else "-"} entries, '
          f'4.04 {len(tr[0]) if tr else "-"} entries')
    if tc:
        bought = sorted({(k, lvl) for t in tc for k, _, lvl in t if lvl})
        info('bought skill levels in the converted save (a 5.0 -> 4.04 reset leaves none)', str(bought or 'none'))
    ec = equipment(c)
    check('no skill slot unlocked and no mutagen equipped above the player\'s level (4.04 unlock levels)',
          bool(ec) and not ec['early'],
          f'level {ec["level"]}: ' + ', '.join(f'{kind} {k} opens at {lvl}' for kind, k, lvl in ec['early'])
          if ec and ec['early'] else '')
    check('equipped mutagens agree across mutagen slots, equipment slots and mounted items',
          bool(ec) and ec['in_mutagen_slots'] == ec['in_item_slots'] == ec['mounted'],
          f'mutagen slots {sorted(ec["in_mutagen_slots"])}, equipment slots {sorted(ec["in_item_slots"])}, '
          f'mounted {sorted(ec["mounted"])}' if ec else 'streams unreadable')

    print(f'\n{sum(results)}/{len(results)} checks passed')
    return all(results)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(0 if main(sys.argv[1], sys.argv[2]) else 1)
