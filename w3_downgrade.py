"""Downgrade a Witcher 3 next-gen (5.0.x, saveVersion 66/67) .sav to the 4.04 format (saveVersion 64).

Layout of a .sav (all little-endian):
  container : "SNFHFZLC", u32 chunk count, u32 header size (0xC0C),
              chunk table of (u32 compressed, u32 decompressed, u32 end offset),
              then independent LZ4 blocks of <= 1 MiB decompressed each.
  body      : "SAV3", u32 saveVersion, u32 gameVersion, u32 build
              records: "BS" u16 name                 block start
                       "VL" u16 name u16 type value  variable
                       "SS" u32 size "SXAP" ver ver ver ...  nested stream (own version stamp)
              "RB" u32 n (u16 name, u32 offset)*n   index of root blocks
              "NM" "MANU" u32 n u32 0 (u8 len, chars)*n u32 0 "ENOD"
              u32 NM offset, u32 RB offset
              "SC" u32 n (u32 offset, u32 size)*n   index of every record; the last entry is MANU..ENOD
              u32 SC count offset, "SE"
  Every stored offset is relative to the container start, i.e. body offset + 0xC0C.
  Most SS records hold property streams; see StreamWalker for their layout.
"""
import argparse, bisect, re, shutil, struct
from collections import Counter
from pathlib import Path

HDR = 0xC0C
CHUNK = 0x100000
TARGET = (64, 27, 163)  # what the 4.04 game writes
SOURCE_VERSIONS = {65, 66, 67}
REMOVED_ROOT_BLOCKS = ('InitialBuildIDBlock', 'CModHandlerSystem')
# per-entry fields 5.0 added to existing lists; 4.04 stores only Tag + Type per pin
REMOVED_VARIABLES = ('CustomAgentMapPinShowAlways', 'CustomEntityMapPinShowAlways')
# values 5.0 appends to existing blocks inside property streams
REMOVED_STREAM_VALUES = ('HasScar',)
# bytes 5.0 appends to every inventory item (zero in every save seen so far)
ITEM_EXTRA_5_0 = 2
INVENTORY = 'CInventoryComponent'


# ---------------------------------------------------------------- LZ4 block format

def lz4_decompress(src, out_size):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255: break
        dst += src[i:i+lit]; i += lit
        if i >= n: break
        off = src[i] | (src[i+1] << 8); i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start+ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    if len(dst) != out_size:
        raise ValueError(f'LZ4 chunk decoded to {len(dst)} bytes, expected {out_size}')
    return bytes(dst)


def _lz4_len(n):
    out = bytearray()
    while n >= 255:
        out.append(255); n -= 255
    out.append(n)
    return out


def lz4_compress(src):
    """Greedy single-hash LZ4 block compressor (valid per the LZ4 block spec)."""
    n = len(src)
    out = bytearray()
    table = {}
    anchor = i = 0
    limit = n - 12  # last match must start >= 12 bytes before the end
    while i < limit:
        key = src[i:i+4]
        ref = table.get(key)
        table[key] = i
        if ref is None or i - ref > 0xFFFF:
            i += 1
            continue
        ml = 4
        mmax = n - 5 - i  # last 5 bytes are always literals
        while ml < mmax and src[ref + ml] == src[i + ml]:
            ml += 1
        lit = i - anchor
        tok = (min(lit, 15) << 4) | min(ml - 4, 15)
        out.append(tok)
        if lit >= 15: out += _lz4_len(lit - 15)
        out += src[anchor:i]
        out += struct.pack('<H', i - ref)
        if ml - 4 >= 15: out += _lz4_len(ml - 4 - 15)
        i += ml
        anchor = i
    lit = n - anchor
    out.append(min(lit, 15) << 4)
    if lit >= 15: out += _lz4_len(lit - 15)
    out += src[anchor:]
    return bytes(out)


def read_container(path):
    return parse_container(Path(path).read_bytes())


def parse_container(data):
    if data[:8] != b'SNFHFZLC':
        raise ValueError(f'not a Witcher 3 save (magic {data[:8]!r})')
    nchunks, hdr = struct.unpack_from('<II', data, 8)
    if hdr != HDR:
        raise ValueError(f'unexpected container header size {hdr:#x}')
    pos, body = hdr, bytearray()
    for k in range(nchunks):
        comp, decomp, end = struct.unpack_from('<III', data, 16 + 12 * k)
        body += lz4_decompress(data[pos:pos+comp], decomp)
        pos += comp
    if pos != len(data):
        raise ValueError(f'container has {len(data) - pos} trailing bytes')
    return bytes(body)


def write_container(body):
    pieces = [lz4_compress(body[k:k+CHUNK]) for k in range(0, len(body), CHUNK)]
    if 16 + 12 * len(pieces) > HDR:
        raise ValueError('save too large for the container chunk table')
    head = bytearray(b'SNFHFZLC' + struct.pack('<II', len(pieces), HDR))
    pos = HDR
    for k, p in enumerate(pieces):
        pos += len(p)
        decomp = min(CHUNK, len(body) - k * CHUNK)
        # the game writes 0 as the last chunk's end offset
        head += struct.pack('<III', len(p), decomp, pos if k < len(pieces) - 1 else 0)
    head += bytes(HDR - len(head))
    return bytes(head) + b''.join(pieces)


# ---------------------------------------------------------------- body layout

class Save:
    def __init__(self, body):
        if body[:4] != b'SAV3' or body[-2:] != b'SE':
            raise ValueError('decompressed body is not SAV3')
        self.body = body
        self.version = struct.unpack_from('<III', body, 4)
        sc = struct.unpack_from('<I', body, len(body) - 6)[0] - HDR
        assert body[sc-2:sc] == b'SC'
        n, = struct.unpack_from('<I', body, sc)
        v = struct.unpack_from(f'<{2 * n}I', body, sc + 4)
        assert sc + 4 + 8 * n + 6 == len(body)
        self.records = [(v[k] - HDR, v[k+1]) for k in range(0, 2 * n, 2)]
        # the last index entry is the string table itself
        man, man_size = self.records.pop()
        enod = man + man_size - 4
        assert body[man:man+4] == b'MANU' and body[enod:enod+4] == b'ENOD'
        nm, rb = (x - HDR for x in struct.unpack_from('<II', body, enod + 4))
        assert body[nm:nm+2] == b'NM' and nm + 2 == man and body[rb:rb+2] == b'RB'
        assert sc - 2 == enod + 12

        n, _ = struct.unpack_from('<II', body, man + 4)
        i, self.names = man + 12, []
        for _ in range(n):
            ln = body[i]; i += 1
            self.names.append(body[i:i+ln].decode('latin-1')); i += ln
        assert i + 4 == enod

        n, = struct.unpack_from('<I', body, rb + 2)
        self.roots = [struct.unpack_from('<HI', body, rb + 6 + 6 * k) for k in range(n)]
        self.roots = [(name, off - HDR) for name, off in self.roots]
        assert rb + 6 + 6 * n == nm

        self.data_end = rb  # everything before RB is record data

    def name(self, idx):
        return self.names[idx - 1]

    def record(self, off):
        tag = self.body[off:off+2]
        name, = struct.unpack_from('<H', self.body, off + 2)
        typ = struct.unpack_from('<H', self.body, off + 4)[0] if tag == b'VL' else None
        return tag, name, typ

    def build(self, data, names, roots, records):
        """Serialise a body from record data and the three index tables."""
        out = bytearray(data)
        rb = len(out)
        out += b'RB' + struct.pack('<I', len(roots))
        for name, off in roots:
            out += struct.pack('<HI', name, off + HDR)
        nm = len(out)
        out += b'NM'
        man = len(out)
        out += b'MANU' + struct.pack('<II', len(names), 0)
        for s in names:
            b = s.encode('latin-1')
            out.append(len(b)); out += b
        out += bytes(4)
        enod = len(out)
        out += b'ENOD' + struct.pack('<II', nm + HDR, rb + HDR)
        out += b'SC'
        sc = len(out)
        out += struct.pack('<I', len(records) + 1)
        for off, size in records + [(man, enod + 4 - man)]:
            out += struct.pack('<II', off + HDR, size)
        out += struct.pack('<I', sc + HDR) + b'SE'
        return bytes(out)


# ---------------------------------------------------------------- property streams

class StreamWalker:
    """Walk every property stream (SS record) and decode the inventories inside.

      stream     : "SXAP" ver ver ver, sections
      section    : "BLCK" u16 name u32 size <sections>
                   "AVAL" / "PORP" u16 name u16 type u32 size <value>
                   "STOR"
                   "ROTS" u32 size <stream>
                   component list: u16 name-or-0, u8 n, n x component
                   or a hand-back to outer BS/VL records for the rest of the span
      component  : a tagged "BLCK", or u32 id, u16 class, class data (only inventories are known)
      inventory  : u16 next id, u16 count, count x item, 01 00 00 00 00, u8 n, n x 12 bytes
      item       : u16 name, u16 id, u8 mask, u8 flags, u32 quantity, 3 bytes,
                   u16 dye, u16 dye colour, u16, f32 durability,
                   u8 n, n x (u16 name, u32 value, u8 type)             modifiers
                   u16 unique id, u8 n, n x (u16 name, u16 id, u8, u8, u8 k, k x u16)   slotted items
                   u8 n, n x u16                                        abilities
                   [mask & 0x20 (mounted on the owner): u16 category]  [5.0 only: u16]
    Changes are collected in `edits` as (start, end, replacement, positions of every u32 size
    field enclosing the range): dropped inventory fields and values, PORP values that
    `rewrite(block, name, type, value)` replaces by returning new bytes, AVAL values that
    `rewrite_block(block, [(name, type, value), ...])` replaces by returning {index: new bytes}
    for the direct AVAL children of a block, and item masks that
    `rewrite_item(inventory position, item name, unique id, mask)` replaces by returning a new
    mask (it may clear the mounted bit 0x20, which also drops the stored category).
    """
    SECTION = (b'BLCK', b'AVAL', b'PORP', b'STOR', b'ROTS')
    OUTER = (b'VL', b'BS', b'OP')

    def __init__(self, save, item_extra, drop_values=(), on_value=None, rewrite=None, rewrite_block=None,
                 rewrite_item=None):
        self.s, self.b = save, save.body
        self.item_extra = item_extra
        self.drop_values = {save.names.index(n) + 1 for n in drop_values if n in save.names}
        self.inv = save.names.index(INVENTORY) + 1 if INVENTORY in save.names else -1
        self.on_value = on_value
        self.rewrite = rewrite
        self.rewrite_block = rewrite_block
        self.rewrite_item = rewrite_item
        self.edits = []
        self.streams = self.inventories = self.items = self.nonzero_extra = self.dropped_values = 0

    def walk(self):
        b = self.b
        for off, size in self.s.records:
            # facts (SBDF) and ContainerManagerSaveInfo streams have their own fixed layouts
            if b[off:off+2] == b'SS' and b[off+22:off+26] == b'BLCK':
                self.section(off + 22, off + size, '', (off + 2,))
                self.streams += 1
        return self

    def valid(self, n):
        return 0 < n <= len(self.s.names)

    def section(self, i, end, block, sizes):
        b = self.b
        avals = []  # direct AVAL children: (position, value size)
        while i < end:
            t = b[i:i+4]
            if t == b'BLCK':
                n, size = struct.unpack_from('<HI', b, i + 4)
                self.section(i + 10, i + 10 + size, self.s.name(n), sizes + (i + 6,))
                i += 10 + size
            elif t in (b'AVAL', b'PORP'):
                n, typ, size = struct.unpack_from('<HHI', b, i + 4)
                if self.on_value:
                    self.on_value(block, self.s.name(n), self.s.name(typ))
                if t == b'AVAL' and n in self.drop_values:
                    self.edits.append((i, i + 12 + size, b'', sizes))
                    self.dropped_values += 1
                elif t == b'AVAL':
                    avals.append((i, size))
                elif t == b'PORP' and self.rewrite:
                    new = self.rewrite(block, self.s.name(n), self.s.name(typ), b[i+12:i+12+size])
                    if new is not None:
                        self.edits.append((i + 12, i + 12 + size, new, sizes + (i + 8,)))
                i += 12 + size
            elif t == b'STOR':
                i += 4
            elif t == b'ROTS':
                size, = struct.unpack_from('<I', b, i + 4)
                if b[i+8:i+12] != b'SXAP':
                    raise ValueError(f'nested stream without SXAP at {i}')
                self.section(i + 24, i + 8 + size, block, sizes + (i + 4,))
                i += 8 + size
            elif self.component_list(i, end):
                i = self.components(i, sizes)
            elif t[:2] in self.OUTER:
                break  # the rest of this span is outer records, covered by the record index
            else:
                raise ValueError(f'unknown stream data {t.hex()} at {i} in block {block!r}')
        else:
            if i != end:
                raise ValueError(f'stream section overran its size by {i - end} at {i}')
        if self.rewrite_block and avals:
            values = [(self.s.name(struct.unpack_from('<H', b, p + 4)[0]),
                       self.s.name(struct.unpack_from('<H', b, p + 6)[0]), b[p+12:p+12+size])
                      for p, size in avals]
            for k, new in (self.rewrite_block(block, values) or {}).items():
                p, size = avals[k]
                self.edits.append((p + 12, p + 12 + size, new, sizes + (p + 8,)))

    def component_list(self, i, end):
        b = self.b
        name, n = struct.unpack_from('<HB', b, i)
        if name > len(self.s.names) or n > 16:
            return False
        if n == 0:
            return i + 3 == end or b[i+3:i+7] in self.SECTION or b[i+3:i+5] in self.OUTER
        return b[i+3:i+7] == b'BLCK' or struct.unpack_from('<H', b, i + 7)[0] == self.inv

    def components(self, i, sizes):
        b = self.b
        n = b[i + 2]
        i += 3
        for _ in range(n):
            if b[i:i+4] == b'BLCK':
                name, size = struct.unpack_from('<HI', b, i + 4)
                self.section(i + 10, i + 10 + size, self.s.name(name), sizes + (i + 6,))
                i += 10 + size
            elif struct.unpack_from('<H', b, i + 4)[0] == self.inv:
                i = self.inventory(i + 6, sizes)
            else:
                raise ValueError(f'unknown component class at {i}')
        return i

    def inventory(self, i, sizes):
        b, valid = self.b, self.valid
        inv = i
        count, = struct.unpack_from('<H', b, i + 2)
        i += 4
        for k in range(count):
            start = i
            name, mask = struct.unpack_from('<H', b, i)[0], b[i+4]
            dye, = struct.unpack_from('<H', b, i + 13)
            if not valid(name) or dye and not (valid(dye) and self.s.name(dye).startswith('Dye')):
                raise ValueError(f'inventory item {k} unreadable at {i}')
            i += 23
            n = b[i]
            for j in range(n):
                if not valid(struct.unpack_from('<H', b, i + 1 + 7 * j)[0]) or b[i + 7 + 7 * j] > 8:
                    raise ValueError(f'inventory item {k} modifier unreadable at {i}')
            i += 1 + 7 * n
            uid, = struct.unpack_from('<H', b, i)
            i += 2
            n = b[i]
            i += 1
            for _ in range(n):
                if not valid(struct.unpack_from('<H', b, i)[0]):
                    raise ValueError(f'inventory item {k} slot unreadable at {i}')
                i += 7 + 2 * b[i+6]
            n = b[i]
            if not all(valid(struct.unpack_from('<H', b, i + 1 + 2 * j)[0]) for j in range(n)):
                raise ValueError(f'inventory item {k} ability unreadable at {i}')
            i += 1 + 2 * n
            new = self.rewrite_item(inv, self.s.name(name), uid, mask) if self.rewrite_item else None
            if new is not None and new != mask:
                if new & 0x20 and not mask & 0x20:
                    raise ValueError(f'inventory item {k}: cannot mount an item (no category to store)')
                self.edits.append((start + 4, start + 5, bytes([new]), sizes))
            if mask & 0x20:
                if new is not None and not new & 0x20:
                    self.edits.append((i, i + 2, b'', sizes))  # only mounted items store a category
                i += 2
            if self.item_extra:
                if any(b[i:i+self.item_extra]):
                    self.nonzero_extra += 1
                self.edits.append((i, i + self.item_extra, b'', sizes))
                i += self.item_extra
            self.items += 1
        if b[i:i+5] != b'\x01\x00\x00\x00\x00':
            raise ValueError(f'inventory trailer unreadable at {i}')
        self.inventories += 1
        return i + 6 + 12 * b[i+5]


# ---------------------------------------------------------------- skills

# Script objects inside a PORP value: u8 0, u32 1, u8 0, u16 class, then a property list:
#   u8 0, (u16 name, u16 type, u32 size including itself, value)*, u16 0
# An array of structs is u32 count followed by one property list per element.

def read_props(b, i):
    if b[i] != 0:
        raise ValueError(f'property list does not start with 0 at {i}')
    i += 1
    out = []
    while True:
        n, = struct.unpack_from('<H', b, i)
        if n == 0:
            return out, i + 2
        t, size = struct.unpack_from('<HI', b, i + 2)
        out.append((n, t, bytes(b[i+8:i+4+size])))
        i += 4 + size


def write_props(props):
    return b'\0' + b''.join(struct.pack('<HHI', n, t, 4 + len(v)) + v for n, t, v in props) + b'\0\0'


def read_struct_array(v):
    count, = struct.unpack_from('<I', v, 0)
    i, items = 4, []
    for _ in range(count):
        props, i = read_props(v, i)
        items.append(props)
    if i != len(v):
        raise ValueError('struct array has trailing bytes')
    return items


def write_struct_array(items):
    return struct.pack('<I', len(items)) + b''.join(write_props(p) for p in items)


def object_class(save, v):
    return save.name(struct.unpack_from('<H', v, 6)[0]) if len(v) > 8 and v[:2] == b'\0\x01' else None


NAME_LISTS = ('array:2,0,CName', 'array:2,0,ESkill')


def make_skill_template(save_path, out_path):
    """Extract the 4.04 skill tree (definitions in 4.04's array order) from a 4.04 save,
    with every bought skill at level 0. Values that reference names are stored by name."""
    import json
    s = Save(read_container(save_path))
    found = []

    def grab(block, name, typ, v):
        if name == 'abilityManager' and object_class(s, v) == 'W3PlayerAbilityManager':
            found.append(v)
    StreamWalker(s, 0, rewrite=grab).walk()
    if not found:
        raise ValueError(f'{save_path}: no player ability manager found')
    props, _ = read_props(found[0], 8)
    skills = next(v for n, t, v in props if s.name(n) == 'skills')
    elements = []
    for el in read_struct_array(skills):
        core = any(s.name(n) == 'isCoreSkill' and v != b'\0' for n, t, v in el)
        out = []
        for n, t, v in el:
            name, typ = s.name(n), s.name(t)
            if typ == 'CName' or typ.startswith('E') and len(v) == 2:
                val = {'name': s.name(struct.unpack('<H', v)[0])}
            elif typ in NAME_LISTS:
                k, = struct.unpack_from('<I', v)
                val = {'names': [s.name(struct.unpack_from('<H', v, 4 + 2 * j)[0]) for j in range(k)]}
                assert len(v) == 4 + 2 * k
            elif name == 'level' and not core:
                val = {'hex': '00000000'}
            else:
                val = {'hex': v.hex()}
            out.append([name, typ, val])
        elements.append(out)
    template = {'source': Path(save_path).name, 'version': list(s.version), 'skills': elements}
    # the template must re-encode to the original bytes (apart from the zeroed levels)
    names = list(s.names)
    check = SkillReset(s, names, template).template_skills()
    zeroed = [[(n, t, b'\0\0\0\0' if s.name(n) == 'level' and not any(
        s.name(m) == 'isCoreSkill' and w != b'\0' for m, _, w in el) else v) for n, t, v in el]
        for el in read_struct_array(skills)]
    assert check == write_struct_array(zeroed) and names == s.names, 'skill template does not round-trip'
    Path(out_path).write_text(json.dumps(template, indent=1))
    return template


class SkillReset:
    """Reset the player's skills the way 5.0 does when it loads an older save: the skill tree
    is rebuilt from the 4.04 template (every bought skill at level 0), skill slots are emptied,
    every spent skill point becomes a free one again, and the abilities that equipped skills
    granted are taken off Geralt's character stats.

    Mutagens go back into the inventory: an equipped mutagen is referenced by its mutagen slot
    (mutagenSlots[].item), by the player's equipment slots (itemSlots, EES_SkillMutagen1-4) and
    by the item itself (mounted bit 0x20 plus a stored category). 5.0 unlocks skill and mutagen
    slots at lower levels than 4.04, so skill slots that 4.04 opens only above Geralt's level are
    locked again (4.04 derives mutagen slot access from the level and stores no flag).

    `scan` and `scan_item` are the hooks of a read-only pre-pass that finds Geralt's level and
    the equipped mutagens, because the streams do not hold them before the data they affect."""

    # one per level of an equipped skill in that path (verified in a 4.04 and a 5.0 save)
    PATH_BONUSES = {'sword_adrenalinegain': 'ESP_Sword', 'magic_staminaregen': 'ESP_Signs'}
    # one per equipped skill whose colour matches the mutagen in its slot group
    MUTAGEN_BONUS = re.compile(r'\w*mutagen_color_\w+_x')
    # item mask: 0x20 = mounted (category stored); an unequipped mutagen is 0x0f in both
    # versions, an equipped one 0x2b
    MOUNTED, UNMOUNTED_MUTAGEN = 0x20, 0x04

    def __init__(self, save, names, template):
        self.s, self.names, self.template = save, names, template
        self.managers = 0
        self.refunded = []   # (free, used) before the reset, per level manager
        self.learned = []    # bought skill levels found, per ability manager
        self.equipped = []   # Counter(skill path -> equipped levels), per ability manager
        self.bonuses = Counter()  # abilities removed from character stats
        self.level = None    # Geralt's level (pre-pass)
        self.mutagens = {}   # unique id of an equipped mutagen -> its mutagen slot number (pre-pass)
        self.mounted = {}    # inventory position -> {unique id of a mounted item} (pre-pass)
        self.player_inventory = None
        self.unequipped = []    # (mutagen slot number, item name) taken off
        self.cleared_slots = 0  # itemSlots entries emptied
        self.locked = []        # (skill slot id, unlockedOnLevel) locked again

    def idx(self, name):
        if name not in self.names:
            self.names.append(name)
        return self.names.index(name) + 1

    def template_skills(self):
        items = []
        for el in self.template['skills']:
            props = []
            for name, typ, val in el:
                if 'name' in val:
                    raw = struct.pack('<H', self.idx(val['name']))
                elif 'names' in val:
                    raw = struct.pack('<I', len(val['names'])) + b''.join(
                        struct.pack('<H', self.idx(x)) for x in val['names'])
                else:
                    raw = bytes.fromhex(val['hex'])
                props.append((self.idx(name), self.idx(typ), raw))
            items.append(props)
        return write_struct_array(items)

    @staticmethod
    def item_id(v):
        """SItemUniqueId value: a property list holding `value : Uint32`; None when empty."""
        props, _ = read_props(v, 0)
        return struct.unpack('<I', props[0][2])[0] if props else None

    def scan(self, block, name, typ, v):
        cls = object_class(self.s, v)
        if name == 'levelManager' and cls == 'W3LevelManager' and self.level is None:
            props, _ = read_props(v, 8)
            level = next((w for n, _, w in props if self.s.name(n) == 'level'), None)
            if level is not None:
                self.level, = struct.unpack('<i', level)
        elif name == 'abilityManager' and cls == 'W3PlayerAbilityManager':
            props, _ = read_props(v, 8)
            for n, _, val in props:
                if self.s.name(n) == 'mutagenSlots':
                    for k, el in enumerate(read_struct_array(val)):
                        item = next((w for m, _, w in el if self.s.name(m) == 'item'), None)
                        if item is not None and self.item_id(item) is not None:
                            self.mutagens[self.item_id(item)] = k + 1
        return None

    def scan_item(self, inv, name, uid, mask):
        if mask & self.MOUNTED:
            self.mounted.setdefault(inv, set()).add(uid)
        return None

    def find_player_inventory(self):
        """After the pre-pass: the one inventory holding every equipped mutagen as a mounted item."""
        if not self.mutagens:
            return
        found = [inv for inv, uids in self.mounted.items() if set(self.mutagens) <= uids]
        if len(found) != 1:
            raise ValueError(f'equipped mutagens {sorted(self.mutagens)} are mounted in {len(found)} '
                             f'inventories, expected exactly one (the player\'s)')
        self.player_inventory = found[0]

    def __call__(self, block, name, typ, v):
        cls = object_class(self.s, v)
        if name == 'abilityManager' and cls == 'W3PlayerAbilityManager':
            return self.ability_manager(v)
        if name == 'levelManager' and cls == 'W3LevelManager':
            return self.level_manager(v)
        if name == 'itemSlots' and typ == 'array:2,0,SItemUniqueId':
            return self.item_slots(v)
        return None

    def item_slots(self, v):
        """Empty the player's equipment slots that hold an equipped mutagen. Only the array
        that holds all of them is the player's (the horse has its own itemSlots)."""
        slots = read_struct_array(v)
        ids = [struct.unpack('<I', el[0][2])[0] if el else None for el in slots]
        if not self.mutagens or not set(self.mutagens) <= set(ids):
            return None
        self.cleared_slots += sum(uid in self.mutagens for uid in ids)
        return write_struct_array([[] if uid in self.mutagens else el for el, uid in zip(slots, ids)])

    def unequip_mutagen(self, inv, name, uid, mask):
        if inv == self.player_inventory and uid in self.mutagens and mask & self.MOUNTED:
            self.unequipped.append((self.mutagens[uid], name))
            return mask & ~self.MOUNTED | self.UNMOUNTED_MUTAGEN
        return None

    def ability_manager(self, v):
        props, end = read_props(v, 8)
        if end != len(v):
            raise ValueError('ability manager has trailing bytes')
        nm = lambda w: self.s.name(struct.unpack('<H', w)[0])
        by_name = {self.s.name(n): val for n, _, val in props}
        levels = {}  # skill -> (path, level) before the reset
        for el in read_struct_array(by_name.get('skills', struct.pack('<I', 0))):
            d = {self.s.name(m): w for m, _, w in el}
            if 'skillType' in d:
                levels[nm(d['skillType'])] = (nm(d['skillPath']) if 'skillPath' in d else None,
                                              struct.unpack('<i', d.get('level', bytes(4)))[0])
        equipped = Counter()
        for el in read_struct_array(by_name.get('skillSlots', struct.pack('<I', 0))):
            d = {self.s.name(m): w for m, _, w in el}
            if 'socketedSkill' in d and nm(d['socketedSkill']) in levels:
                path, level = levels[nm(d['socketedSkill'])]
                equipped[path] += level
        self.equipped.append(equipped)
        out = []
        for n, t, val in props:
            name = self.s.name(n)
            if name == 'skills':
                learned = 0
                for el in read_struct_array(val):
                    d = {self.s.name(m): w for m, _, w in el}
                    if d.get('isCoreSkill', b'\0') == b'\0' and 'level' in d:
                        learned += struct.unpack('<i', d['level'])[0]
                self.learned.append(learned)
                val = self.template_skills()
            elif name == 'skillSlots':
                slots = []
                for el in read_struct_array(val):
                    d = {self.s.name(m): w for m, _, w in el}
                    opens = struct.unpack('<i', d.get('unlockedOnLevel', bytes(4)))[0]
                    lock = self.level is not None and opens > self.level and d.get('unlocked', b'\0') != b'\0'
                    if lock:
                        self.locked.append((struct.unpack('<i', d.get('id', bytes(4)))[0], opens))
                    drop = {'socketedSkill', 'unlocked'} if lock else {'socketedSkill'}
                    slots.append([p for p in el if self.s.name(p[0]) not in drop])
                val = write_struct_array(slots)
            elif name == 'mutagenSlots':
                val = write_struct_array([[p for p in el if self.s.name(p[0]) != 'item']
                                          for el in read_struct_array(val)])
            elif name == 'skillAbilities':
                val = struct.pack('<I', 0)
            elif name == 'pathPointsSpent':
                k, = struct.unpack_from('<I', val)
                val = struct.pack('<I', k) + bytes(4 * k)
            out.append((n, t, val))
        self.managers += 1
        return v[:8] + write_props(out)

    def character_stats(self, block, values):
        """StreamWalker block hook: drop skill-granted abilities from Geralt's ability list
        (characterStats: count : Uint32, value : array:2,0,CName)."""
        if block != 'characterStats':
            return None
        names = [n for n, _, _ in values]
        if 'count' not in names or 'value' not in names:
            return None
        ci, vi = names.index('count'), names.index('value')
        v = values[vi][2]
        k, = struct.unpack_from('<I', v)
        if values[vi][1] != 'array:2,0,CName' or len(v) != 4 + 2 * k:
            return None
        ids = [struct.unpack_from('<H', v, 4 + 2 * j)[0] for j in range(k)]
        abilities = [self.s.name(x) for x in ids]
        if 'GeraltSkills' not in abilities:
            return None
        drop = lambda a: a in self.PATH_BONUSES or self.MUTAGEN_BONUS.fullmatch(a)
        keep = [x for x, a in zip(ids, abilities) if not drop(a)]
        if len(keep) == k:
            return None
        self.bonuses.update(a for a in abilities if drop(a))
        new = {vi: struct.pack('<I', len(keep)) + b''.join(struct.pack('<H', x) for x in keep)}
        if struct.unpack('<I', values[ci][2])[0] == k:
            new[ci] = struct.pack('<I', len(keep))
        else:
            raise ValueError('characterStats count does not match its ability list')
        return new

    def level_manager(self, v):
        props, end = read_props(v, 8)
        if end != len(v):
            raise ValueError('level manager has trailing bytes')
        out = []
        for n, t, val in props:
            if self.s.name(n) == 'points':
                # points[0] holds skill points, points[1] experience
                items = read_struct_array(val)
                skill = {self.s.name(m): struct.unpack('<i', w)[0] for m, _, w in items[0]}
                free, used = skill.get('free', 0), skill.get('used', 0)
                self.refunded.append((free, used))
                new = {'free': struct.pack('<i', free + used), 'used': struct.pack('<i', 0)}
                items[0] = [(m, tt, new.pop(self.s.name(m), w)) for m, tt, w in items[0]]
                items[0] += [(self.idx(k), self.idx('Int32'), w) for k, w in new.items()]
                val = write_struct_array(items)
            out.append((n, t, val))
        return v[:8] + write_props(out)


# ---------------------------------------------------------------- downgrade

def encode_string_ansi(s):
    b = s.encode('latin-1') + b'\0'
    if len(b) > 255:
        raise ValueError('StringAnsi too long')
    return bytes([len(b)]) + b


def decode_short_string(raw):
    """RED 'String' value: u8 header (0x80 | length) for short ANSI strings."""
    h = raw[0]
    if not h & 0x80 or h & 0x40:
        raise ValueError(f'unsupported String encoding header {h:#x}')
    ln = h & 0x3F
    return raw[1:1+ln].decode('latin-1')


def load_skill_template(path):
    import json
    if not Path(path).is_file():
        raise ValueError(f'skill template {path} not found; create one from a 4.04 save with '
                         f'"python w3_downgrade.py <4.04 save> --make-skill-template -t <template.json>"')
    return json.loads(Path(path).read_text())


def downgrade(save, skill_template, keep_new_blocks=False, log=print):
    body = bytearray(save.body)
    names = list(save.names)
    edits = []  # (start, end, replacement) on the data section, non-overlapping

    # inside property streams: drop the per-item inventory field, HasScar (unless kept),
    # reset the skill tree and unequip mutagens, and resize every size field enclosing a
    # changed range; a read-only pre-pass first finds Geralt's level and equipped mutagens
    reset = SkillReset(save, names, skill_template)
    StreamWalker(save, ITEM_EXTRA_5_0, rewrite=reset.scan, rewrite_item=reset.scan_item).walk()
    reset.find_player_inventory()
    streams = StreamWalker(save, ITEM_EXTRA_5_0, () if keep_new_blocks else REMOVED_STREAM_VALUES,
                           rewrite=reset, rewrite_block=reset.character_stats,
                           rewrite_item=reset.unequip_mutagen).walk()
    grow = Counter()
    for s, e, r, sizes in streams.edits:
        edits.append((s, e, r))
        for q in sizes:
            grow[q] += len(r) - (e - s)
    for q, d in grow.items():
        struct.pack_into('<I', body, q, struct.unpack_from('<I', body, q)[0] + d)
    log(f'  inventories: removed the 5.0 item field from {streams.items} items in {streams.inventories} inventories'
        + (f' ({streams.nonzero_extra} held a non-zero value, which 4.04 cannot store)' if streams.nonzero_extra else ''))
    if streams.dropped_values:
        log(f'  remove {streams.dropped_values} x {"/".join(REMOVED_STREAM_VALUES)} from property streams')
    if not reset.managers or not reset.refunded:
        raise ValueError('player skills not found (no W3PlayerAbilityManager / W3LevelManager)')
    for free, used in reset.refunded:
        log(f'  skills: reset to the 4.04 tree ({len(reset.template["skills"])} entries, from '
            f'{reset.template["source"]}) in {reset.managers} ability manager(s); '
            f'skill points {free} free + {used} used -> {free + used} free')
        if used not in reset.learned:
            log(f'  note: {used} used skill points, but the skill tree held {reset.learned} bought levels')
    if reset.bonuses:
        log('  skills: removed abilities granted by equipped skills from Geralt: '
            + ', '.join(f'{c} x {a}' for a, c in sorted(reset.bonuses.items())))
    equipped = reset.equipped[0] if reset.equipped else Counter()
    path_removed = Counter()
    for a, c in reset.bonuses.items():
        if a in SkillReset.PATH_BONUSES:
            path_removed[SkillReset.PATH_BONUSES[a]] += c
    for path in sorted(set(equipped) | set(path_removed)):
        if equipped[path] != path_removed[path]:
            log(f'  note: {equipped[path]} equipped skill levels in {path}, but {path_removed[path]} '
                f'path bonus abilities removed (bonus ability for this path unknown or out of sync)')
    if reset.locked:
        log(f'  skill slots: locked {len(reset.locked)} again that 4.04 opens only above level {reset.level}: '
            + ', '.join(f'slot {i} (level {lvl})' for i, lvl in reset.locked))
    if reset.unequipped:
        log(f'  mutagens: unequipped {len(reset.unequipped)} back into the inventory: '
            + ', '.join(f'{name} (slot {k})' for k, name in sorted(reset.unequipped)))
    if len(reset.unequipped) != len(reset.mutagens) or reset.cleared_slots != len(reset.mutagens):
        raise ValueError(f'{len(reset.mutagens)} equipped mutagens, but {len(reset.unequipped)} items '
                         f'unmounted and {reset.cleared_slots} equipment slots emptied')

    if not keep_new_blocks:
        root_off = {save.name(n): off for n, off in save.roots}
        sizes = dict(save.records)
        for blk in REMOVED_ROOT_BLOCKS:
            if blk in root_off:
                off = root_off[blk]
                edits.append((off, off + sizes[off], b''))
                log(f'  remove root block {blk} ({sizes[off]} bytes)')

    ss = [(off, off + size) for off, size in save.records if body[off:off+2] == b'SS']
    dropped = Counter()
    for off, size in save.records:
        tag, n, _ = save.record(off)
        if not keep_new_blocks and tag == b'VL' and save.name(n) in REMOVED_VARIABLES:
            # a nested stream carries its own inline size, so edits must stay outside them
            assert not any(s <= off < e for s, e in ss), f'{save.name(n)} inside nested stream'
            edits.append((off, off + size, b''))
            dropped[save.name(n)] += 1
    for name, k in dropped.items():
        log(f'  remove {k} x {name}')

    # saveInfo.playthroughId: String (5.0) -> StringAnsi (4.04)
    if 'StringAnsi' not in names:
        names.append('StringAnsi')
    ansi = names.index('StringAnsi') + 1
    for off, size in save.records:
        tag, n, t = save.record(off)
        if tag == b'VL' and save.name(n) == 'playthroughId' and save.name(t) == 'String':
            s = decode_short_string(body[off+6:off+size])
            edits.append((off, off + size, b'VL' + struct.pack('<HH', n, ansi) + encode_string_ansi(s)))
            log(f'  playthroughId String -> StringAnsi ({s})')
    edits.sort()
    for a, b in zip(edits, edits[1:]):
        assert a[1] <= b[0], 'overlapping edits'
    starts = [s for s, _, _ in edits]
    ends = [e for _, e, _ in edits]
    shift = [0]
    for s, e, r in edits:
        shift.append(shift[-1] + len(r) - (e - s))

    def remap(p):
        return p + shift[bisect.bisect_right(ends, p)]

    def removed(off, size):
        k = bisect.bisect_right(starts, off) - 1
        return k >= 0 and not edits[k][2] and off + size <= edits[k][1]

    data = bytearray()
    pos = 0
    for s, e, r in edits:
        data += body[pos:s] + r
        pos = e
    data += body[pos:save.data_end]

    records = [(remap(off), remap(off + size) - remap(off))
               for off, size in save.records if not removed(off, size)]
    roots = [(n, remap(off)) for n, off in save.roots if not removed(off, 1)]

    # version stamps: file header plus every nested stream header
    data[4:16] = struct.pack('<III', *TARGET)
    stamped = 0
    i = data.find(b'SXAP')
    while i != -1:
        v = struct.unpack_from('<III', data, i + 4)
        if v[0] in SOURCE_VERSIONS:
            data[i+4:i+16] = struct.pack('<III', *TARGET)
            stamped += 1
        elif v[0] > TARGET[0]:
            raise ValueError(f'unexpected nested stream version {v} at {i}')
        i = data.find(b'SXAP', i + 4)
    log(f'  restamped header + {stamped} nested streams to {TARGET}')

    return save.build(bytes(data), names, roots, records)


def verify(body, expect_version, src=None):
    """Re-parse a built body: index entries point at sane records, and every property stream
    walks end to end with the 4.04 item layout, holding the same inventories as the source."""
    s = Save(body)
    assert s.version == expect_version, s.version
    for off, size in s.records:
        assert 0 <= off and off + size <= s.data_end, (off, size)
    for n, off in s.roots:
        tag, name, _ = s.record(off)
        assert tag == b'BS' and name == n, (s.name(n), off, tag)
    w = StreamWalker(s, 0).walk()
    if src is not None:
        assert (w.streams, w.inventories, w.items) == (src.streams, src.inventories, src.items), \
            ((w.streams, w.inventories, w.items), (src.streams, src.inventories, src.items))
    return s


def convert(save_path, out_dir, skill_template, keep_new_blocks=False, log=print):
    """Downgrade save_path into out_dir (with its screenshot), resetting skills to the 4.04 tree
    in the skill_template file; returns the written .sav path."""
    save_path, out_dir = Path(save_path), Path(out_dir)
    template = load_skill_template(skill_template)
    src = Save(read_container(save_path))
    log(f'{save_path.name}: version {src.version}, {len(src.records)} records')
    if src.version[0] <= TARGET[0]:
        raise ValueError(f'save is already in 4.04 format or older (version {src.version})')

    body = downgrade(src, template, keep_new_blocks, log)
    out = write_container(body)
    if parse_container(out) != body:
        raise ValueError('round-trip of recompressed save failed')
    dst = verify(body, TARGET, StreamWalker(src, ITEM_EXTRA_5_0).walk())

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / save_path.name
    target.write_bytes(out)
    png = save_path.with_suffix('.png')
    if png.exists():
        shutil.copyfile(png, target.with_suffix('.png'))
    log(f'wrote {target} ({len(out)} bytes, {len(dst.records)} records, verified)')
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('save', type=Path, help='5.0.x .sav file (or a 4.04 .sav with --make-skill-template)')
    ap.add_argument('-t', '--skill-template', type=Path, required=True,
                    help='4.04 skill tree JSON (e.g. skill_tree_404.json); written by --make-skill-template')
    ap.add_argument('-o', '--out-dir', type=Path, help='folder for the converted save (required when converting)')
    ap.add_argument('--keep-new-blocks', action='store_true',
                    help='leave the 5.0-only blocks and values (build ID, mod list, map-pin ShowAlways, '
                         'HasScar) in place; versions, inventories and skills are still converted')
    ap.add_argument('--make-skill-template', action='store_true',
                    help='extract the 4.04 skill tree from SAVE (a 4.04 save) into the --skill-template file')
    args = ap.parse_args()
    if not args.make_skill_template and args.out_dir is None:
        ap.error('the following arguments are required when converting: -o/--out-dir')
    try:
        if args.make_skill_template:
            t = make_skill_template(args.save, args.skill_template)
            print(f'wrote {args.skill_template} ({len(t["skills"])} skill entries from {args.save.name})')
        else:
            convert(args.save, args.out_dir, args.skill_template, args.keep_new_blocks)
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == '__main__':
    main()
