
import sys, re, os, json
import urllib.request, urllib.error
from collections import defaultdict

def _normalize_ace_targets(ace_targets=None):
    """Return {ace_index: physical_head}. Missing entries deliberately
    fall back to the legacy head=slot mapping so standalone CLI use
    remains usable when no printer route is available."""
    out = {}
    if not ace_targets:
        return out
    if isinstance(ace_targets, str):
        try:
            ace_targets = json.loads(ace_targets)
        except ValueError:
            return out
    if not isinstance(ace_targets, dict):
        return out
    for k, v in ace_targets.items():
        if v is None or v == '':
            continue
        try:
            ace = int(k)
            head = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= ace <= 15 and 0 <= head <= 3:
            out[ace] = head
    return out

def _route_virtual_tool(t_index, ace_targets=None, slots_per_ace=4):
    ace_targets = _normalize_ace_targets(ace_targets)
    ace = int(t_index) // slots_per_ace
    slot = int(t_index) % slots_per_ace
    head = ace_targets.get(ace, slot)
    return head, ace, slot

def _normalize_tool_targets(tool_targets=None):
    """Return {slicer_t: normalized_target}.

    A target is either:
      {'kind': 'native', 'head': H}
      {'kind': 'ace', 'head': H, 'ace': A, 'slot': S}

    Missing/invalid entries are ignored so legacy callers can keep using
    ace_targets and the old T -> ACE/slot formula.
    """
    out = {}
    if not tool_targets:
        return out
    if isinstance(tool_targets, str):
        try:
            tool_targets = json.loads(tool_targets)
        except ValueError:
            return out
    if not isinstance(tool_targets, dict):
        return out
    for k, v in tool_targets.items():
        if not isinstance(v, dict):
            continue
        try:
            t = int(k)
            head = int(v.get('head'))
        except (TypeError, ValueError):
            continue
        if t < 0 or t > 63 or head < 0 or head > 3:
            continue
        kind = str(v.get('kind') or '').lower()
        if kind == 'native':
            out[t] = {'kind': 'native', 'head': head}
            if v.get('source'):
                out[t]['source'] = v.get('source')
            continue
        if kind != 'ace':
            continue
        try:
            ace = int(v.get('ace'))
            slot = int(v.get('slot'))
        except (TypeError, ValueError):
            continue
        if ace < 0 or ace > 15 or slot < 0 or slot > 3:
            continue
        out[t] = {'kind': 'ace', 'head': head, 'ace': ace, 'slot': slot}
        if v.get('source'):
            out[t]['source'] = v.get('source')
    return out

def _normalize_route_plan_targets(route_plan=None):
    """Return {slicer_t: normalized_target} from a route_plan.

    route_plan is the new source-graph rewrite contract. It may be a dict or
    JSON string and should contain tool_map entries like:
      "0": {"source": "ace:0:1", "head": "head:3", "target": {...}}

    The existing rewrite implementation still works in terms of normalized
    native/ACE targets, so this adapter is intentionally narrow.
    """
    if not route_plan:
        return {}
    if isinstance(route_plan, str):
        try:
            route_plan = json.loads(route_plan)
        except ValueError:
            return {}
    if not isinstance(route_plan, dict):
        return {}
    raw_targets = {}
    tool_map = route_plan.get('tool_map') or {}
    if isinstance(tool_map, dict):
        for k, v in tool_map.items():
            if not isinstance(v, dict):
                continue
            target = v.get('target') if isinstance(v.get('target'), dict) else v
            raw_targets[k] = target
    if not raw_targets:
        for event in route_plan.get('events') or []:
            if not isinstance(event, dict):
                continue
            t = event.get('slicer_tool')
            target = event.get('target')
            if t is None or not isinstance(target, dict):
                continue
            raw_targets[str(t)] = target
    return _normalize_tool_targets(raw_targets)

def _route_plan_events(route_plan=None):
    if not route_plan:
        return []
    if isinstance(route_plan, str):
        try:
            route_plan = json.loads(route_plan)
        except ValueError:
            return []
    if not isinstance(route_plan, dict):
        return []
    events = route_plan.get('events') or []
    if not isinstance(events, list):
        return []
    out = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            int(event.get('slicer_tool'))
        except (TypeError, ValueError):
            continue
        out.append(event)
    return out

def _route_plan_event_cursor(route_plan=None):
    by_tool = {}
    for event in _route_plan_events(route_plan):
        t = int(event.get('slicer_tool'))
        by_tool.setdefault(t, []).append(event)
    return {'by_tool': by_tool, 'offsets': {}}

def _commands_from_route_event(event):
    if not isinstance(event, dict):
        return []
    steps = event.get('steps') or []
    if isinstance(steps, list):
        out = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            cmd = step.get('command')
            if cmd is None:
                continue
            s = str(cmd).strip()
            if s:
                out.append(s)
        if out:
            return out
    commands = event.get('commands') or []
    if not isinstance(commands, list):
        return []
    out = []
    for cmd in commands:
        if cmd is None:
            continue
        s = str(cmd).strip()
        if s:
            out.append(s)
    return out

def _route_event_for_tool(cursor, t_index):
    if not cursor:
        return None
    try:
        t = int(t_index)
    except (TypeError, ValueError):
        return None
    rows = cursor.get('by_tool', {}).get(t) or []
    if not rows:
        return None
    offsets = cursor.setdefault('offsets', {})
    idx = int(offsets.get(t, 0) or 0)
    if idx >= len(rows):
        idx = len(rows) - 1
    offsets[t] = idx + 1
    return rows[idx]

def _commands_for_tool_event(t_index, cursor, ace_targets=None, tool_targets=None):
    event = _route_event_for_tool(cursor, t_index)
    commands = _commands_from_route_event(event)
    if commands:
        return commands
    target = _target_for_tool(int(t_index), ace_targets, tool_targets)
    head = target['head']
    if target['kind'] != 'ace':
        return ['T%d' % head]
    return ['T%d' % head,
            'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d'
            % (head, target['ace'], target['slot'])]

def _target_for_tool(t_index, ace_targets=None, tool_targets=None,
                     route_plan=None):
    route_targets = _normalize_route_plan_targets(route_plan)
    t = int(t_index)
    if t in route_targets:
        return route_targets[t]
    tool_targets = _normalize_tool_targets(tool_targets)
    if t in tool_targets:
        return tool_targets[t]
    head, ace, slot = _route_virtual_tool(t, ace_targets)
    return {'kind': 'ace', 'head': head, 'ace': ace, 'slot': slot}

def _route_ace_targets_from_slots(live_slots):
    targets = {}
    for s in live_slots or []:
        try:
            ace = int(s.get('ace'))
            head = s.get('target_head')
            if head is None:
                continue
            targets[ace] = int(head)
        except (TypeError, ValueError):
            continue
    return targets

def _initial_marker(head, ace, slot):
    return '; multiACE initial-load HEAD=%d ACE=%d SLOT=%d' % (
        head, ace, slot)

def rewrite(gcode, ace_targets=None, tool_targets=None, route_plan=None):
    ace_targets = _normalize_ace_targets(ace_targets)
    tool_targets = _normalize_tool_targets(tool_targets)
    route_plan_targets = _normalize_route_plan_targets(route_plan)
    if route_plan_targets:
        tool_targets = route_plan_targets
    route_cursor = _route_plan_event_cursor(route_plan)

    def _fix_m104(m):
        return re.sub(r'T(1[0-5]|[0-9])',
                      lambda t: 'T%d' % _target_for_tool(
                          int(t.group(1)), ace_targets, tool_targets)['head'],
                      m.group(0))
    gcode = re.sub(r'^M10[49][^\n]*',
                   _fix_m104, gcode, flags=re.MULTILINE)

    gcode = re.sub(
        r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(1[0-5]|[0-9])',
        lambda m: 'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d'
        % _target_for_tool(int(m.group(1)), ace_targets, tool_targets)['head'],
        gcode)

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    if m is None:
        pre, body = gcode, ''
    else:
        pre, body = gcode[:m.start()], gcode[m.start():]

    def _expand_initial(m):
        target = _target_for_tool(int(m.group(1)), ace_targets, tool_targets)
        head = target['head']
        if target['kind'] != 'ace':
            return 'T%d' % head
        return 'T%d\n%s' % (
            head, _initial_marker(head, target['ace'], target['slot']))

    pre = re.sub(r'^T(1[0-5]|[0-9])\s*$',
                 _expand_initial, pre, flags=re.MULTILINE)

    def _expand_swap(m):
        return '\n'.join(_commands_for_tool_event(
            int(m.group(1)), route_cursor, ace_targets, tool_targets))

    body = re.sub(r'^T(1[0-5]|[0-9])\s*$',
                  _expand_swap, body, flags=re.MULTILINE)

    head_loaded = {}
    filtered_lines = []
    lines = body.splitlines()
    i = 0
    skipped = 0
    swapbacks = 0
    while i < len(lines):
        line = lines[i]

        m_t = re.match(r'^T([0-3])\s*$', line)
        if m_t:
            filtered_lines.append(line)
            i += 1
            continue
        m_s = re.match(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$', line)
        if m_s:
            head = int(m_s.group(1))
            ace = int(m_s.group(2))
            slot = int(m_s.group(3))
            key = (ace, slot)
            if head_loaded.get(head) == key:
                filtered_lines.append('; %s  ; skipped (already loaded)' % line)
                skipped += 1
                i += 1
                continue
            head_loaded[head] = key
        filtered_lines.append(line)
        i += 1
    body = '\n'.join(filtered_lines)
    if pre and body and not pre.endswith(('\n', '\r')):
        pre += '\n'

    total_active = len([l for l in filtered_lines if l.startswith('ACE_SWAP_HEAD')])
    return pre + body, total_active, skipped, swapbacks

def parse_toolchanges(gcode):
    """Yield the ORIGINAL T-index in order of appearance.

    Uses the "; Change Tool X -> Tool Y" comment as the source of
    truth for the target tool, since after post-processing the bare
    T<n> line always reads T<head> (head = original_T % 4) and the
    ACE-slot info is moved into ACE_SWAP_HEAD. The comment line is
    preserved in both pre- and post-rewrite gcode, so parsing it
    lets the analyzer work on either input."""
    change_re = re.compile(
        r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')
    bare_re = re.compile(r'^T(\d{1,2})\b')
    saw_change = False
    for line in gcode.splitlines():
        s = line.strip()
        if not s:
            continue
        m = change_re.match(s)
        if m:
            saw_change = True
            yield int(m.group(1))
            continue

        if saw_change or s.startswith(';'):
            continue
        mb = bare_re.match(s)
        if mb:
            yield int(mb.group(1))

def lookup_live_slots(host, port=80, path='/multiace/api/state', timeout=5.0):
    """Query the printer's multiACE web for current slot occupation.

    host may include ":port" (e.g. "192.168.1.42:8080") which overrides
    the port arg. Returns a list of dicts:
        {'ace': N, 'slot': S, 'material': str, 'color': '#rrggbb' (lower)}
    for every non-empty slot, or None on any HTTP/parse error."""
    if ':' in host:
        host, _, port_str = host.partition(':')
        try:
            port = int(port_str)
        except ValueError:
            pass
    url = 'http://%s:%d%s' % (host, port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print('WARNING: live-lookup to %s failed: %s' % (url, e),
              file=sys.stderr)
        return None
    out = []
    for ace in data.get('aces', []) or []:
        ace_idx = ace.get('idx')
        for slot in ace.get('slots', []) or []:
            if slot.get('state') == 'empty':
                continue
            color = (slot.get('color') or '').strip().lower()
            material = (slot.get('material') or '').strip()
            if color or material:
                out.append({
                    'ace': ace_idx,
                    'slot': slot.get('idx'),
                    'target_head': slot.get('target_head'),
                    'material': material,
                    'color': color,
                })
    return out

def check_material_availability(filament_types, live_slots):
    """Pre-check before matching. Returns sorted list of materials that
    the slicer needs (per `filament_types`) but that aren't loaded in
    any slot on the printer. An empty list means every required
    material has at least one slot available - matching can proceed
    even if individual colours fall back."""
    loaded = set()
    for s in live_slots or []:
        m = (s.get('material') or '').strip().lower()
        if m:
            loaded.add(m)
    required = set()
    if filament_types:
        for v in filament_types.values():
            m = (v or '').strip().lower()
            if m:
                required.add(m)
    return sorted(required - loaded)

def _hex_to_rgb_internal(s):
    s = (s or '').strip().lower().lstrip('#')
    if len(s) < 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None

def _find_color_match(candidates, slicer_color, strict_color, fuzzy_max_distance):
    """Pick the best slot from `candidates` for `slicer_color`. Returns
    (slot_dict, tier_str) or (None, None). Tier is one of:
      'exact_hex' | 'name_exact' | 'name_base' | 'name_canon' | 'fuzzy'."""
    if not slicer_color:
        return None, None

    for s in candidates:
        if s['color'].lstrip('#') == slicer_color:
            return s, 'exact_hex'
    if strict_color:
        return None, None

    slicer_name = approx_color_name('#' + slicer_color) or ''
    if slicer_name and slicer_name != '?':
        slicer_base = _strip_color_qualifier(slicer_name)
        slicer_canon = _COLOR_SYNONYMS.get(slicer_base, slicer_base)
        for stage_tier in (('name_exact', 'exact'),
                           ('name_base',  'base'),
                           ('name_canon', 'canon')):
            tier, stage = stage_tier
            for s in candidates:
                slot_name = approx_color_name(s['color']) or ''
                if not slot_name or slot_name == '?':
                    continue
                if stage == 'exact':
                    ok = (slot_name == slicer_name)
                elif stage == 'base':
                    ok = (_strip_color_qualifier(slot_name) == slicer_base)
                else:
                    slot_base  = _strip_color_qualifier(slot_name)
                    slot_canon = _COLOR_SYNONYMS.get(slot_base, slot_base)
                    ok = (slot_canon == slicer_canon)
                if ok:
                    return s, tier

    if fuzzy_max_distance is not None:
        slicer_rgb = _hex_to_rgb_internal(slicer_color)
        if slicer_rgb is not None:
            best, best_d = None, None
            for s in candidates:
                sr = _hex_to_rgb_internal(s['color'])
                if sr is None:
                    continue
                d2 = ((sr[0] - slicer_rgb[0]) ** 2
                      + (sr[1] - slicer_rgb[1]) ** 2
                      + (sr[2] - slicer_rgb[2]) ** 2)
                if best_d is None or d2 < best_d:
                    best_d, best = d2, s
            if best is not None and best_d ** 0.5 <= fuzzy_max_distance:
                return best, 'fuzzy'
    return None, None

def match_colors_to_slots(color_names, live_slots, num_heads=4,
                          filament_types=None,
                          strict_color=False,
                          fuzzy_max_distance=None):
    """Build a remap {original_T -> synthetic_T} for the rewrite formula
    (ace = T // num_heads, slot = T % num_heads), choosing the physical
    slot whose colour best matches the slicer's colour for that T.

    Match algorithm is TIER-MAJOR: each tier is tried against EVERY
    still-unmatched slicer T-index globally before any later tier
    runs. That prevents the greedy-per-T failure mode where T0
    (Blue) grabs the only DarkBlue slot via the name_base fallback,
    leaving T1 (DarkBlue) - which would have matched exact_hex -
    stuck on a worse tier.

    Tier order (every tier stays within the slicer head's material —
    a different material is never substituted, even on fallback):
        1.  exact_hex                            every T tried
        2.  name_exact                           every T tried (skip if strict_color)
        3.  name_base   ('DarkRed' -> 'Red')     every T tried (skip if strict_color)
        4.  name_canon  (synonym table)          every T tried (skip if strict_color)
        5.  fuzzy RGB distance                   every T tried (skip if strict_color
                                                 or fuzzy_max_distance is None)
      Last resort (still material-matched):
        6.  any unclaimed slot of the same material  → tier='fallback'
        7.  share an already-claimed same-material slot → tier='duplicate'
        8.  nothing available                    → tier='no_slot'

    A slot is claimed once and removed from contention. T-indices
    whose matched physical slot equals their slicer index are
    omitted from the remap (no-op rewrite).

    Returns (remap, info, used_slots) where info[t_idx] = {
      'tier':       str   (see tier list above, or 'no_slot'),
      'slot':       dict  (the matched live_slot, or None),
      'loose_mat':  bool  (always False; kept for API compatibility),
    }."""
    filament_types = filament_types or {}

    def _hex_to_rgb(s):
        s = (s or '').strip().lower().lstrip('#')
        if len(s) < 6:
            return None
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return None

    def _name_keys(hex_str):
        name = approx_color_name(hex_str) or ''
        if not name or name == '?':
            return '', '', ''
        base  = _strip_color_qualifier(name)
        canon = _COLOR_SYNONYMS.get(base, base)
        return name, base, canon

    t_meta = {}
    for t in sorted(color_names.keys()):
        c = (color_names[t] or '').strip().lower().lstrip('#')
        mat = (filament_types.get(t) or '').strip().lower()
        name, base, canon = _name_keys('#' + c) if c else ('', '', '')
        t_meta[t] = {
            'color': c, 'mat': mat,
            'name': name, 'base': base, 'canon': canon,
            'rgb': _hex_to_rgb(c) if c else None,
        }

    slot_meta = []
    for s in live_slots:
        c = (s.get('color') or '').strip().lower().lstrip('#')
        name, base, canon = _name_keys('#' + c) if c else ('', '', '')
        slot_meta.append({
            'slot':  s,
            'color': c,
            'mat':   (s.get('material') or '').lower(),
            'name':  name, 'base': base, 'canon': canon,
            'rgb':   _hex_to_rgb(c) if c else None,
        })

    used: set = set()
    info: dict = {}
    pending = list(t_meta.keys())

    def _candidate_slots(t, strict_mat):
        """Iterate unclaimed slot_meta entries, optionally restricted
        to the slicer T's material."""
        t_mat = t_meta[t]['mat']
        for sm in slot_meta:
            s = sm['slot']
            if (s['ace'], s['slot']) in used:
                continue
            if strict_mat and t_mat and sm['mat'] != t_mat:
                continue
            yield sm

    def _match_pass(tier_name, strict_mat, predicate):
        """Iterate all currently-pending T-indices, in T-order. For
        each, claim the first unclaimed slot satisfying `predicate`."""
        for t in list(pending):
            tm = t_meta[t]
            chosen = None
            for sm in _candidate_slots(t, strict_mat):
                if predicate(tm, sm):
                    chosen = sm
                    break
            if chosen is None:
                continue
            s = chosen['slot']
            used.add((s['ace'], s['slot']))
            info[t] = {
                'tier': ('loose_' + tier_name) if not strict_mat else tier_name,
                'slot': s,
                'loose_mat': (not strict_mat) and bool(tm['mat']),
            }
            pending.remove(t)

    def _fuzzy_predicate(tm, sm):
        if fuzzy_max_distance is None:
            return False
        if tm['rgb'] is None or sm['rgb'] is None:
            return False
        d2 = ((tm['rgb'][0] - sm['rgb'][0]) ** 2
              + (tm['rgb'][1] - sm['rgb'][1]) ** 2
              + (tm['rgb'][2] - sm['rgb'][2]) ** 2)
        return d2 ** 0.5 <= fuzzy_max_distance

    color_tiers = [
        ('exact_hex',  False,
            lambda tm, sm: bool(tm['color']) and tm['color'] == sm['color']),
        ('name_exact', True,
            lambda tm, sm: bool(tm['name'])  and tm['name']  == sm['name']),
        ('name_base',  True,
            lambda tm, sm: bool(tm['base'])  and tm['base']  == sm['base']),
        ('name_canon', True,
            lambda tm, sm: bool(tm['canon']) and tm['canon'] == sm['canon']),
        ('fuzzy',      True, _fuzzy_predicate),
    ]

    for tier_name, skip_when_strict, pred in color_tiers:
        if skip_when_strict and strict_color:
            continue
        if tier_name == 'fuzzy' and fuzzy_max_distance is None:
            continue
        _match_pass(tier_name, True, pred)


    for t in list(pending):
        tm = t_meta[t]
        t_mat = (tm.get('mat') or '').strip().lower()
        chosen = None
        for sm in slot_meta:
            s = sm['slot']
            if (s['ace'], s['slot']) in used:
                continue
            if t_mat and sm['mat'] and sm['mat'] != t_mat:
                continue
            chosen = sm
            break
        if chosen is None:
            continue
        s = chosen['slot']
        used.add((s['ace'], s['slot']))
        info[t] = {
            'tier': 'fallback',
            'slot': s,
            'loose_mat': False,
        }
        pending.remove(t)

    if pending:
        already = [sm for sm in slot_meta
                   if (sm['slot']['ace'], sm['slot']['slot']) in used]
        for t in list(pending):
            tm = t_meta[t]
            t_mat = (tm.get('mat') or '').strip().lower()
            candidates = [sm for sm in already
                          if not t_mat or not sm['mat']
                          or sm['mat'] == t_mat]
            best = None
            best_d = None
            if tm['rgb'] is not None:
                for sm in candidates:
                    if sm['rgb'] is None:
                        continue
                    d2 = ((tm['rgb'][0] - sm['rgb'][0]) ** 2
                          + (tm['rgb'][1] - sm['rgb'][1]) ** 2
                          + (tm['rgb'][2] - sm['rgb'][2]) ** 2)
                    if best_d is None or d2 < best_d:
                        best_d, best = d2, sm
            if best is None:
                best = candidates[0] if candidates else None
            if best is None:
                info[t] = {'tier': 'no_slot', 'slot': None, 'loose_mat': False}
                pending.remove(t)
                continue
            s = best['slot']
            info[t] = {
                'tier': 'duplicate',
                'slot': s,
                'loose_mat': False,
            }
            pending.remove(t)

    remap = {}
    for t, entry in info.items():
        s = entry['slot']
        if s is None:
            continue
        synthetic_T = s['ace'] * num_heads + s['slot']
        if synthetic_T != t:
            remap[t] = synthetic_T
    return remap, info, used

def parse_filament_types(gcode):
    """Best-effort lookup table T-index -> material name (PLA, PETG, …).
    Slicers emit `filament_type = PLA;PETG;PLA` similar to filament_colour."""
    types = {}
    all_lines = gcode.splitlines()
    scan = all_lines[:300] + all_lines[-2000:]
    for line in scan:
        m = re.search(r';\s*filament[_ ]type\s*[:=]\s*(.+)', line, re.I)
        if m:
            for i, p in enumerate(re.split(r'[;,]', m.group(1))):
                p = p.strip()
                if p:
                    types[i] = p
            if types:
                break
    return types

def parse_color_names(gcode):
    """Best-effort lookup table T-index -> color name. Orca writes
    the filament_colour line at the end of the gcode, Bambu/Prusa
    often near the top - scan both."""
    names = {}
    all_lines = gcode.splitlines()
    scan = all_lines[:300] + all_lines[-2000:]
    for line in scan:
        m = re.search(r';\s*filament[_ ]colou?r\s*[:=]\s*(.+)', line, re.I)
        if m:
            for i, p in enumerate(re.split(r'[;,]', m.group(1))):
                p = p.strip()
                if p and p != '#':
                    names[i] = p
            if names:
                break
    return names

_NAMED_COLORS = (
    ('Black',      (0x00, 0x00, 0x00)),
    ('White',      (0xFF, 0xFF, 0xFF)),
    ('Gray',       (0x80, 0x80, 0x80)),
    ('DarkGray',   (0x40, 0x40, 0x40)),
    ('LightGray',  (0xD3, 0xD3, 0xD3)),
    ('Silver',     (0xC0, 0xC0, 0xC0)),
    ('Red',        (0xE0, 0x20, 0x20)),
    ('DarkRed',    (0x8B, 0x00, 0x00)),
    ('Pink',       (0xFF, 0xC0, 0xCB)),
    ('Orange',     (0xFF, 0x8C, 0x00)),
    ('Yellow',     (0xFF, 0xE0, 0x20)),
    ('Gold',       (0xDA, 0xA5, 0x20)),
    ('Brown',      (0x8B, 0x45, 0x13)),
    ('Beige',      (0xE6, 0xD6, 0xA5)),
    ('Green',      (0x20, 0xA0, 0x20)),
    ('DarkGreen',  (0x00, 0x64, 0x00)),
    ('LightGreen', (0x90, 0xEE, 0x90)),
    ('Cyan',       (0x20, 0xD0, 0xD0)),
    ('Blue',       (0x30, 0x50, 0xF0)),
    ('DarkBlue',   (0x00, 0x00, 0x8B)),
    ('LightBlue',  (0xAD, 0xD8, 0xE6)),
    ('Purple',     (0x80, 0x20, 0x80)),
    ('Magenta',    (0xE0, 0x20, 0xE0)),
)

_COLOR_QUALIFIERS = ('Dark', 'Light')

_COLOR_SYNONYMS = {
    'Silver': 'Gray',
    'Gold':   'Yellow',
}

def _strip_color_qualifier(name):
    """'DarkRed' -> 'Red', 'LightBlue' -> 'Blue', otherwise unchanged."""
    if not name:
        return ''
    for q in _COLOR_QUALIFIERS:
        if name.startswith(q) and len(name) > len(q):
            return name[len(q):]
    return name

def _canonical_color_name(name):
    """Apply qualifier-strip + synonym table.
       'DarkRed' -> 'Red', 'Silver' -> 'Gray', 'LightGray' -> 'Gray'."""
    base = _strip_color_qualifier(name)
    return _COLOR_SYNONYMS.get(base, base)

def approx_color_name(hex_str):
    """Nearest named color from #RRGGBB, or hex unchanged if not parseable."""
    if not hex_str:
        return '?'
    s = hex_str.strip().lstrip('#')
    if len(s) < 6:
        return hex_str
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return hex_str
    best, best_d = None, 1 << 30
    for name, (nr, ng, nb) in _NAMED_COLORS:
        d = (r - nr) ** 2 + (g - ng) ** 2 + (b - nb) ** 2
        if d < best_d:
            best_d, best = d, name
    return best

def _hex_to_rgb_tuple(hex_str):
    """('#rrggbb' or 'rrggbb') -> (r, g, b) ints, or None."""
    s = (hex_str or '').strip().lstrip('#')
    if len(s) < 6:
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None

def format_color_hex_rgb(hex_str):
    """'#831100' -> '#831100 RGB(131,17,0)' for paste-into-slicer convenience."""
    rgb = _hex_to_rgb_tuple(hex_str)
    if rgb is None:
        return hex_str or '?'
    return '%s RGB(%d,%d,%d)' % (hex_str.lower(), rgb[0], rgb[1], rgb[2])

def format_color(t_index, color_names):
    hex_val = color_names.get(t_index)
    if not hex_val:
        return '?'
    name = approx_color_name(hex_val)
    full = format_color_hex_rgb(hex_val)
    if hex_val.lstrip('#').lower() == name.lower():
        return full
    return '%s (%s)' % (name, full)

def infer_num_aces(gcode):
    """Detect how many ACEs the slicer's T-index assignment uses.

    For every used T<n> command (n >= 0), the canonical ACE is n // 4.
    Inferred count = max(ACE) + 1 across all used Ts. Returns at least
    1 (single-color prints have only T0 → ACE 0).

    This eliminates the need for a manual --aces flag: the slicer
    already knows which physical ACE/slot each colour lives in
    (because the user assigned cartridges that way), so the gcode
    itself is the source of truth.
    """
    lines = gcode.splitlines()
    max_ace = 0
    for line in lines:
        s = line.strip()
        m = re.match(r'^T(\d{1,2})\s*$', s)
        if m:
            ace = int(m.group(1)) // 4
            if ace > max_ace:
                max_ace = ace
    return max_ace + 1

def plan_loadout(gcode, num_aces=3):

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    body_gcode = gcode[m.start():] if m else ''

    events = list(parse_toolchanges(body_gcode))
    if not events:
        return None
    color_names = parse_color_names(gcode)

    counts = defaultdict(int)
    for t in events:
        counts[t] += 1

    colors = sorted(counts.keys())
    plan = {}
    for c in colors:
        head = c % 4
        ace = c // 4
        if ace == 0:
            plan[c] = {'ace': 0, 'slot': head, 'head': head, 'role': 'initial'}
        else:
            plan[c] = {'ace': ace, 'slot': head, 'head': head, 'role': 'swap'}

    head_current = {h: h for h in range(4)}
    swaps = 0
    for t in events:
        info = plan.get(t)
        if info is None:
            continue
        h = info['head']
        if head_current.get(h) != t:
            swaps += 1
            head_current[h] = t

    layer_info = compute_layer_swap_plan(body_gcode, num_aces=num_aces)

    return {
        'plan': plan, 'counts': counts, 'color_names': color_names,
        'swaps': swaps,
        'total_changes': len(events), 'events': events,
        'layer_info': layer_info,
    }

def _suggest_layer_friendly_remap(layer_colors, num_aces):
    """When the current T-index assignment causes same-head conflicts in
    some layers, search for a remap of T-indices to head buckets that
    eliminates all conflicts while requiring minimal physical
    reordering.

    Each color (= existing T-index) lives at head c%4 today. We may
    reassign it to any head 0-3 (= new T-index k where k%4 = new_head).
    Constraints:
      - No two colors on the same head within any layer.
      - Per-head color count <= num_aces.
    Objective: minimize the number of colors moved off their current
    head (so the user has to physically rearrange as few cartridges as
    possible).

    Returns dict {old_T: new_T} or None if no feasible remap exists.
    Brute-force over 4^N head assignments where N = #colors. Practical
    up to ~12 colors (4^12 = ~17M).
    """
    colors = sorted({c for s in layer_colors for c in s})
    n = len(colors)
    if n == 0 or n > 12:
        return None
    current_head = {c: c % 4 for c in colors}

    from itertools import product

    best_assignment = None
    best_moved = n + 1

    layer_lists = [list(s) for s in layer_colors]

    for assignment in product(range(4), repeat=n):

        head_count = [0, 0, 0, 0]
        for h in assignment:
            head_count[h] += 1
        if any(c > num_aces for c in head_count):
            continue

        head_for_color = {colors[i]: assignment[i] for i in range(n)}

        conflict = False
        for layer_list in layer_lists:
            heads_used = set()
            for c in layer_list:
                h = head_for_color[c]
                if h in heads_used:
                    conflict = True
                    break
                heads_used.add(h)
            if conflict:
                break
        if conflict:
            continue

        moved = sum(1 for i, c in enumerate(colors)
                    if assignment[i] != current_head[c])
        if moved < best_moved:
            best_moved = moved
            best_assignment = assignment
            if moved == 0:
                break

    if best_assignment is None:
        return None

    head_groups = {h: [] for h in range(4)}
    for i, c in enumerate(colors):
        head_groups[best_assignment[i]].append(c)
    new_t = {}
    for h, cs in head_groups.items():
        used_aces = set()

        for c in cs:
            if c % 4 != h:
                continue
            cur_ace = c // 4
            if cur_ace not in used_aces and cur_ace < num_aces:
                new_t[c] = h + 4 * cur_ace
                used_aces.add(cur_ace)

        for c in cs:
            if c % 4 != h or c in new_t:
                continue
            for ace in range(num_aces):
                if ace not in used_aces:
                    new_t[c] = h + 4 * ace
                    used_aces.add(ace)
                    break

        for c in cs:
            if c in new_t:
                continue
            for ace in range(num_aces):
                if ace not in used_aces:
                    new_t[c] = h + 4 * ace
                    used_aces.add(ace)
                    break

    return new_t if any(v != k for k, v in new_t.items()) else None

def compute_swap_aware_layout(events, num_aces, num_heads=4,
                              layer_color_sets=None):
    """Search head assignments per color (free distribution - colors
    are NOT bound to head=T%4) for the one that minimizes the runtime
    swap count.

    Swap count under a given assignment c->head:
      Per head, walk the toolchange sequence; each time the head's
      currently-loaded color differs from the next event on that head
      counts as 1 swap. The first appearance on each head is free
      (covered by auto-load, not a runtime swap).

    Args:
        events: toolchange sequence as a list of T-indices in print order
        num_aces: max colors per head (= ACE capacity)
        num_heads: 4 (physical extruders on Snapmaker U1)
        layer_color_sets: optional list of {colors in each layer} sets;
            when provided, assignments that put 2+ colors on the same
            head within ANY single layer are rejected (= layer-only
            swap mode, no mid-layer changes).

    Returns:
        (color_to_head dict, swap_count) on success
        (None, None) if no assignment satisfies the constraints

    Brute-force over num_heads^N (N = distinct colors). Practical up
    to ~12 colors (4^12 ≈ 17M)."""
    from itertools import product

    colors_list = sorted(set(events))
    n = len(colors_list)
    if n == 0:
        return {}, 0
    if n > 12:
        return None, None

    best_assignment = None
    best_swaps = None

    for assignment in product(range(num_heads), repeat=n):
        head_count = [0] * num_heads
        for h in assignment:
            head_count[h] += 1
        if any(c > num_aces for c in head_count):
            continue

        c2h = {colors_list[i]: assignment[i] for i in range(n)}

        if layer_color_sets is not None:
            conflict = False
            for lset in layer_color_sets:
                heads_used = set()
                for c in lset:
                    h = c2h.get(c)
                    if h is None:
                        continue
                    if h in heads_used:
                        conflict = True
                        break
                    heads_used.add(h)
                if conflict:
                    break
            if conflict:
                continue

        head_current = [None] * num_heads
        swaps = 0
        for t in events:
            h = c2h[t]
            if head_current[h] != t:
                if head_current[h] is not None:
                    swaps += 1
                head_current[h] = t

        if best_swaps is None or swaps < best_swaps:
            best_swaps = swaps
            best_assignment = c2h

    if best_assignment is None:
        return None, None
    return best_assignment, best_swaps


def compute_layer_swap_plan(body_gcode, num_aces=4):
    """Analyze whether the print can be served with layer-boundary-only
    swaps (no mid-layer toolchanges) on a 4-slot printhead with at most
    `num_aces` physical ACE units.

    Walks the body gcode layer-by-layer (;LAYER_CHANGE markers), tracks
    the set of distinct colors active within each layer, then - if every
    layer fits in 4 slots - runs a budget-aware Belady cache-replacement
    that prefers to spread swaps across heads so no head's ACE index
    exceeds num_aces - 1.

    Returns a dict: {feasible, max_per_layer, num_layers, layer_swaps,
    aces_needed, initial_loadout, events, color_slots, histogram}.
    """

    lines = body_gcode.splitlines()
    current = None
    mfirst = re.match(r';\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)',
                      lines[0] if lines else '')
    if mfirst:
        current = int(mfirst.group(1))

    change_re = re.compile(
        r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')

    layer_seqs = []
    cur = None
    for line in lines:
        s = line.strip()
        if s.startswith(';LAYER_CHANGE'):
            if cur is not None:
                layer_seqs.append(cur)
            cur = []
            if current is not None:
                cur.append(current)
            continue
        mc = change_re.match(s)
        if mc:
            current = int(mc.group(1))
            if cur is not None:
                cur.append(current)
    if cur is not None:
        layer_seqs.append(cur)

    layer_colors = [set(seq) for seq in layer_seqs]
    n_layers = len(layer_colors)
    if n_layers == 0:
        return {'feasible': False, 'num_layers': 0, 'max_per_layer': 0,
                'layer_swaps': None, 'initial_loadout': None,
                'histogram': {}}

    max_per_layer = max(len(s) for s in layer_colors)
    histogram = {}
    for s in layer_colors:
        histogram[len(s)] = histogram.get(len(s), 0) + 1

    if max_per_layer > 4:
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram,
                'reason': 'too_many_colors',
                'reason_detail': '>4 distinct colors in some layer',
                'layer_color_sets': [sorted(s) for s in layer_colors]}

    head_conflict_layers = []
    for li, layer_set in enumerate(layer_colors):
        per_head = {}
        for c in layer_set:
            per_head.setdefault(c % 4, []).append(c)
        conflicts = {h: cs for h, cs in per_head.items() if len(cs) > 1}
        if conflicts:
            head_conflict_layers.append((li, conflicts))
    if head_conflict_layers:

        examples = []
        for li, conflicts in head_conflict_layers[:3]:
            parts = ['head %d: %s' % (
                h, ', '.join('T%d' % c for c in sorted(cs)))
                for h, cs in sorted(conflicts.items())]
            examples.append('layer %d (%s)' % (li, '; '.join(parts)))
        more = (' +%d more' % (len(head_conflict_layers) - 3)
                if len(head_conflict_layers) > 3 else '')

        suggestion = _suggest_layer_friendly_remap(
            layer_colors, num_aces)
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram,
                'reason': 'head_conflict',
                'reason_detail': 'same-head conflict in %d layer(s): %s%s' % (
                    len(head_conflict_layers), '; '.join(examples), more),
                'suggestion': suggestion,
                'layer_color_sets': [sorted(s) for s in layer_colors]}

    def next_use(col, since):
        for j in range(since, n_layers):
            if col in layer_colors[j]:
                return j
        return 1 << 30

    all_colors = sorted({c for s in layer_colors for c in s})

    def simulate(fixed_initial):
        """Strict-c%4 simulator. Each color c lives on head c%4 (its
        physical destination - ACE c//4 / Slot c%4 feeds head c%4).
        No free choice of head: when a layer needs c and head c%4 is
        occupied by another color c', evict c' and load c. If c' is
        also needed in the same layer (= layer uses two colors with
        the same %4), the print is infeasible at layer granularity
        (would need a mid-layer swap, which our caller filters out
        via max_per_layer check).

        Each color is loaded into its slicer-canonical ACE position
        (c // 4). Feasibility: c // 4 must be < num_aces. Distinct
        colors per head (= aces_needed) is the count that matters,
        not the total number of swaps - the same two colors can
        cycle on a head infinitely with only 2 ACE slots.

        Returns (swaps, aces_needed, events, color_slots,
        materialized_initial_loadout) or None if infeasible.
        """
        cache = [None, None, None, None]
        init_loadout = {}
        for c, h in fixed_initial.items():
            if h != c % 4:
                return None
            if cache[h] is not None:
                return None
            if c // 4 >= num_aces:
                return None
            cache[h] = c
            init_loadout[c] = h

        head_distinct_colors = [set(), set(), set(), set()]
        for c in init_loadout:
            head_distinct_colors[c % 4].add(c)

        events = []
        color_slots = {c: [(0, h, c // 4)] for c, h in init_loadout.items()}
        swaps = 0

        for i, needed in enumerate(layer_colors):
            loaded = set(c for c in cache if c is not None)
            for c in sorted(needed - loaded):
                h = c % 4
                if cache[h] is None:

                    if c // 4 >= num_aces:
                        return None
                    cache[h] = c
                    init_loadout[c] = h
                    head_distinct_colors[h].add(c)
                    color_slots.setdefault(c, []).append((i, h, c // 4))
                    continue
                if cache[h] in needed:

                    return None

                if c // 4 >= num_aces:
                    return None

                evicted = cache[h]
                cache[h] = c
                head_distinct_colors[h].add(c)
                events.append((i, c, evicted, h))
                color_slots.setdefault(c, []).append((i, h, c // 4))
                swaps += 1
                loaded = set(c for c in cache if c is not None)

        aces_needed = max(len(s) for s in head_distinct_colors)
        return (swaps, aces_needed, events, color_slots, init_loadout)

    fixed_initial = {}
    used_heads = set()
    seen = set()
    for layer_set in layer_colors:
        if len(used_heads) == 4:
            break
        for c in sorted(layer_set):
            if c in seen:
                continue
            seen.add(c)
            h = c % 4
            if h in used_heads:
                continue
            fixed_initial[c] = h
            used_heads.add(h)
            if len(used_heads) == 4:
                break

    best = simulate(fixed_initial)
    if best is None:

        best = simulate({})
    if best is None:
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram}

    swaps, aces_needed, events, color_slots, initial_loadout = best

    return {'feasible': True, 'num_layers': n_layers,
            'max_per_layer': max_per_layer, 'layer_swaps': swaps,
            'initial_loadout': initial_loadout, 'events': events,
            'color_slots': color_slots, 'aces_needed': aces_needed,
            'histogram': histogram,
            'layer_color_sets': [sorted(s) for s in layer_colors]}

def compute_optimal_remap(result):
    """Return ({old_T: new_T}, best_swaps) that minimizes mid-print swaps,
    or (None, None) if no improvement is possible over the slicer's
    layout. Mirrors the optimizer loop used for printing recommendations,
    then converts the chosen primary/extra assignments into concrete
    T-index targets (primaries go to T0..T3, extras to T<head + 4*ace>).
    """
    from itertools import combinations
    counts = result['counts']
    colors = sorted(counts.keys())
    if len(colors) <= 4:
        return None, None

    best_swaps = sum(counts.values()) + 1
    best_primaries = None
    for primaries in combinations(colors, 4):
        primary_set = set(primaries)
        head_for_color = {c: i for i, c in enumerate(primaries)}
        head_extra_count = [0] * 4
        for c in sorted((c for c in colors if c not in primary_set),
                        key=lambda x: -counts[x]):
            h = min(range(4), key=lambda h: head_extra_count[h])
            head_for_color[c] = h
            head_extra_count[h] += 1
        head_loaded = {}
        sim_swaps = 0
        for t in result.get('events', []):
            if t not in head_for_color:
                continue
            h = head_for_color[t]
            if head_loaded.get(h) is None:
                head_loaded[h] = t
            elif head_loaded[h] != t:
                sim_swaps += 1
                head_loaded[h] = t
        if sim_swaps < best_swaps:
            best_swaps = sim_swaps
            best_primaries = primaries

    if best_primaries is None or best_swaps >= result['swaps']:
        return None, None

    primary_set = set(best_primaries)
    remap = {c: i for i, c in enumerate(best_primaries)}
    head_extra_count = [0] * 4
    for c in sorted((c for c in colors if c not in primary_set),
                    key=lambda x: -counts[x]):
        h = min(range(4), key=lambda h: head_extra_count[h])
        head_extra_count[h] += 1
        remap[c] = h + 4 * head_extra_count[h]

    if all(k == v for k, v in remap.items()):
        return None, None
    return remap, best_swaps

def apply_remap(gcode, remap):
    """Rewrite every T-index reference in the gcode according to the
    permutation `remap` ({old_T: new_T}). Touches bare T<n> lines,
    M104/M109 T<n> heater commands and SM_PRINT_PREEXTRUDE_FILAMENT
    INDEX=<n>. The `; Change Tool<a> -> Tool<b>` comments are left
    untouched so they remain the canonical source of the original
    slicer tool indices - this keeps the analyzer/optimizer idempotent
    across repeated runs on the same file. The downstream rewrite()
    logic only uses those comments as split markers and doesn't care
    about the numbers.
    """
    if not remap:
        return gcode

    def rm(n):
        return remap.get(int(n), int(n))

    def _bare_t(m):
        return 'T%d' % rm(m.group(1))

    def _m104_m109(m):
        return re.sub(r'T(\d+)',
                      lambda t: 'T%d' % rm(t.group(1)),
                      m.group(0))

    def _preextrude(m):
        return 'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d' % rm(m.group(1))

    gcode = re.sub(r'^T(\d{1,2})\s*$', _bare_t,
                   gcode, flags=re.MULTILINE)
    gcode = re.sub(r'^M10[49][^\n]*', _m104_m109,
                   gcode, flags=re.MULTILINE)
    gcode = re.sub(r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(\d+)',
                   _preextrude, gcode)
    return gcode

def apply_layer_remap(gcode, layer_info):
    """Rewrite T-references so the print uses layer-boundary-only swaps.

    Strategy: walk the gcode, tracking the current layer index via
    ;LAYER_CHANGE markers. For each `; Change Tool X -> Tool Y` we look
    up Tool Y's current (head, ace) slot from the Belady schedule and
    rewrite the bare T<Y> (and any following M104/M109 T<Y>) inside
    that toolchange block to T<head + 4*ace>. The downstream rewrite()
    step then emits ACE_SWAP_HEAD with HEAD=head SLOT=head ACE=ace, and
    its built-in skip logic marks the ~115 non-swap toolchanges as
    `; skipped (already loaded)` - leaving only the Belady-optimal
    swaps as real filament changes.

    Returns (rewritten_gcode, physical_loadout) where physical_loadout
    is a dict (ace, slot) -> original T index, so we can print the
    physical cartridge plan for the user.
    """
    if not layer_info or not layer_info.get('feasible'):
        return gcode, None

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    if m is None:
        return gcode, None
    pre, body = gcode[:m.start()], gcode[m.start():]

    initial = layer_info['initial_loadout']
    events = layer_info['events']

    current_slot = {c: (h, c // 4) for c, h in initial.items()}

    events_by_layer = {}
    head_ace_counter = [0, 0, 0, 0]
    for i, c_in, c_out, h in events:
        events_by_layer.setdefault(i, []).append((c_in, c_out, h))

    loadout = {}
    for c, h in initial.items():
        loadout[(0, h)] = c

    ace_counter_pre = [0, 0, 0, 0]

    for (i, c_in, c_out, h) in events:
        ace_counter_pre[h] += 1
        loadout[(ace_counter_pre[h], h)] = c_in

    body_lines = body.splitlines()
    out = []
    layer_idx = 0

    pending_target = None

    change_re = re.compile(
        r'^(;\s*Change Tool\s*\d+\s*->\s*Tool\s*)(\d+)(.*)$')
    bare_re = re.compile(r'^T(\d{1,2})\s*$')
    m104_re = re.compile(r'^(M10[49]\b.*)$')

    def advance_to_layer(new_idx):
        for ll in range(layer_idx + 1, new_idx + 1):
            for c_in, c_out, h in events_by_layer.get(ll, []):
                head_ace_counter[h] += 1
                ace = head_ace_counter[h]
                current_slot[c_in] = (h, ace)

    for line in body_lines:
        s = line.strip()
        if s.startswith(';LAYER_CHANGE'):

            advance_to_layer(layer_idx + 1)
            layer_idx += 1
            out.append(line)
            continue

        mc = change_re.match(s)
        if mc:
            orig_y = int(mc.group(2))
            pending_target = orig_y

            out.append(line)
            continue

        mb = bare_re.match(s)
        if mb and pending_target is not None:

            h, ace = current_slot.get(pending_target,
                                      (pending_target % 4,
                                       pending_target // 4))
            out.append('T%d' % (h + 4 * ace))
            pending_target = None
            continue

        mh = m104_re.match(s)
        if mh:
            def _repl(mm, pt=pending_target):
                n = int(mm.group(1))

                if pt is not None and n == pt:
                    h, ace = current_slot.get(pt,
                                              (pt % 4, pt // 4))
                    return 'T%d' % (h + 4 * ace)
                return mm.group(0)
            out.append(re.sub(r'T(\d{1,2})', _repl, line))
            continue

        out.append(line)

    return pre + '\n'.join(out), loadout

def print_recommendation(result, num_aces, file=None):
    from itertools import combinations

    def p(*args):
        if file is not None:
            print(*args, file=file)
        else:
            print(*args)

    counts = result['counts']
    colors = sorted(counts.keys())
    n_colors = len(colors)
    max_slots = num_aces * 4
    color_names = result.get('color_names', {})

    p('=' * 60)
    p('multiACE plan')
    p('=' * 60)
    p('Colors: %d   Toolchanges: %d   Mid-print swaps: %d (~%.1f min)' % (
        n_colors, result['total_changes'], result['swaps'],
        result['swaps'] * 3.8))

    overflow = [c for c, info in result['plan'].items() if info.get('role') == 'OVERFLOW']
    if overflow:
        p()
        p('!! WARNING: %d color(s) exceed ACE capacity (%d slots, %d ACEs)' % (
            n_colors, max_slots, num_aces))
        p('!! Exceeding colors will NOT be printed.')

    p()
    p('Slicer Loadout:')
    for c in colors:
        info = result['plan'].get(c, {})
        ace = info.get('ace', c // 4)
        slot = info.get('slot', c % 4)
        role = info.get('role', '')
        p('  ACE %d Slot %d  T%-2d  %s  (%dx%s)' % (
            ace, slot, c, format_color(c, color_names),
            counts[c], '' if role != 'OVERFLOW' else ' OVERFLOW'))

    if n_colors > 4:
        best_swaps = sum(counts.values())
        best_primaries = None

        for primaries in combinations(colors, min(4, n_colors)):

            head_color = {}
            primary_set = set(primaries)
            head_for_color = {}
            for i, c in enumerate(primaries):
                head_for_color[c] = i

            non_primaries = [c for c in colors if c not in primary_set]
            primary_by_head = {i: primaries[i] for i in range(len(primaries))}

            head_extra_count = [0] * 4
            for c in sorted(non_primaries, key=lambda x: -counts[x]):
                h = min(range(4), key=lambda h: head_extra_count[h])
                head_for_color[c] = h
                head_extra_count[h] += 1

            head_loaded = {}
            sim_swaps = 0
            for t in result.get('events', []):
                if t not in head_for_color:
                    continue
                h = head_for_color[t]
                if head_loaded.get(h) is None:
                    head_loaded[h] = t
                elif head_loaded[h] != t:
                    sim_swaps += 1
                    head_loaded[h] = t

            if sim_swaps < best_swaps:
                best_swaps = sim_swaps
                best_primaries = primaries

        if best_primaries is not None:
            p()
            savings = result['swaps'] - best_swaps
            if savings > 0:
                p('--- OPTIMIZER: %d swaps possible (%d fewer, %.0f%% less) ---' % (
                    best_swaps, savings,
                    savings / result['swaps'] * 100 if result['swaps'] > 0 else 0))

                primary_set = set(best_primaries)
                head_for_color = {c: i for i, c in enumerate(best_primaries)}
                head_extra_count = [0] * 4
                non_p = [c for c in colors if c not in primary_set]

                extras_order = sorted(non_p, key=lambda x: -counts[x])
                extra_ace_of_color = {}
                for c in extras_order:
                    h = min(range(4), key=lambda h: head_extra_count[h])
                    head_for_color[c] = h
                    head_extra_count[h] += 1
                    extra_ace_of_color[c] = head_extra_count[h]
                p('Optimized Print Loadout:')

                rows = []
                for c in best_primaries:
                    rows.append((0, head_for_color[c], c, 'primary'))
                for c in extras_order:
                    rows.append((extra_ace_of_color[c], head_for_color[c], c, 'swap'))
                for ace, slot, c, role in sorted(rows):
                    p('  ACE %d Slot %d  T%-2d  %s  (%s, %dx)' % (
                        ace, slot, c, format_color(c, color_names),
                        role, counts[c]))
            else:
                p('--- OPTIMIZER: current assignment is already optimal ---')

    layer_info = result.get('layer_info')
    if layer_info:
        p()
        p('Layer-only swap analysis:')
        p('  Layers: %d   Max colors/layer: %d' % (
            layer_info['num_layers'], layer_info['max_per_layer']))
        if layer_info['feasible']:
            aces_needed = layer_info.get('aces_needed', 0)
            fits = aces_needed <= num_aces
            p('  Feasible: YES  Minimum layer-only swaps: %d (~%.1f min)' % (
                layer_info['layer_swaps'],
                layer_info['layer_swaps'] * 3.8))
            if fits:
                p('  ACEs needed: %d (you have %d - fits)' % (
                    aces_needed, num_aces))
            else:
                p('  ACEs needed: %d (you have %d - DOES NOT FIT, --layer will be skipped)' % (
                    aces_needed, num_aces))
            preload = layer_info.get('initial_loadout') or {}
            if preload:

                p('  Pre-load these colors before print:')
                for c, h in sorted(preload.items(), key=lambda kv: kv[1]):
                    p('    ACE %d Slot %d  T%-2d  %s' % (
                        c // 4, c % 4, c, format_color(c, color_names)))
            events = layer_info.get('events') or []
            if events:

                p('  Additional swap cartridges:')
                seen = set(preload.keys())
                for _lyr, c_in, _c_out, h in events:
                    if c_in in seen:
                        continue
                    seen.add(c_in)
                    p('    ACE %d Slot %d  T%-2d  %s' % (
                        c_in // 4, c_in % 4, c_in,
                        format_color(c_in, color_names)))
        else:
            reason = layer_info.get('reason')
            detail = layer_info.get('reason_detail', '')
            if reason == 'too_many_colors':
                p('  Feasible: NO  (%s - needs mid-layer swaps)' % detail)
            elif reason == 'head_conflict':
                p('  Feasible: NO  (%s)' % detail)
                p('    Each head N can only hold one color at a time;')
                p('    colors with the same N (where N = T%%4) compete:')
                p('    head 0: T0, T4, T8, T12   head 1: T1, T5, T9, T13')
                p('    head 2: T2, T6, T10, T14  head 3: T3, T7, T11, T15')
                suggestion = layer_info.get('suggestion')
                if suggestion:
                    moves = [(old, new) for old, new in sorted(suggestion.items())
                             if old != new]
                    p('')
                    p('  Suggested rearrangement (minimal moves to enable layer mode):')
                    for old, new in moves:
                        old_ace, old_slot = old // 4, old % 4
                        new_ace, new_slot = new // 4, new % 4
                        p('    T%-2d  %s   ACE %d Slot %d  →  ACE %d Slot %d  (T%d)' % (
                            old, format_color(old, color_names),
                            old_ace, old_slot,
                            new_ace, new_slot, new))
                    p('    %d color(s) need to move; reslice with the new T-indices' % len(moves))
                    p('    or physically swap cartridges to the suggested ACE/slot.')
                else:
                    p('')
                    p('  No conflict-free remap found within %d ACE budget.' % num_aces)
                    p('  Either reduce the number of colors or increase --aces.')
            else:
                p('  Feasible: NO')

    p('=' * 60)

def inject_auto_load(gcode):
    """Insert ACE_SWAP_HEAD calls for each used head AT the safest point
    that is past G28 + heating but before the first move that needs the
    initial tool's filament.

    Use case: replace the manual preload step before a multi-color
    print. The slicer's start gcode emits heating + G28 + bed leveling
    (and a bare T<initial_extruder> command for heater selection that
    can come BEFORE G28 - that's why we don't inject before the first
    T).

    Injection-point fallback chain (highest priority first):

      1. Right BEFORE the first SM_PRINT_PREEXTRUDE_FILAMENT line.
         This is Snapmaker's stock prime move - it lives AFTER G28 +
         M109 in the slicer's start gcode and BEFORE the first body
         move. It also extrudes from the initial tool, so the initial
         tool's filament must be loaded by then or the runout sensor
         triggers an id=523 pause (observed 2026-04-26 14:56). This is
         the safest anchor for prints that use a single tool or whose
         initial tool is never targeted by a `; Change Tool` marker.

      2. Right BEFORE the first '; Change Tool X -> Tool Y' marker
         (Orca multi-tool prints). This anchor is the boundary between
         start_gcode and the print body - but it is AFTER any prior
         SM_PRINT_PREEXTRUDE_FILAMENT, which is why it is fallback 2,
         not 1.

      3. Right BEFORE the first ACE_SWAP_HEAD HEAD= line. Catches
         single-color prints where rewrite() generated swaps.

    cmd_ACE_SWAP_HEAD's empty-head detection (ace.py) makes this work
    for fresh / unloaded heads - the unload phase is skipped when the
    sensor reports no filament and head_source is None, so the swap
    reduces to a pure load. Already-loaded heads with the correct
    (ACE, slot) hit the 'already on' short-circuit (no-op). Mismatched
    loaded heads get unloaded + reloaded.

    Initial mapping per head is discovered only from explicit
    ACE_SWAP_HEAD HEAD/ACE/SLOT commands. Bare T<n> commands are not
    enough to infer the ACE slot and are left alone.

    Returns (gcode_with_injection, count_of_heads_loaded).
    """
    lines = gcode.split('\n')

    cleaned = []
    in_block = False
    for ln in lines:
        ls = ln.strip()
        if ls.startswith('; multiACE auto-load: load'):
            in_block = True
            continue
        if in_block:
            if ls.startswith('; multiACE auto-load: end'):
                in_block = False
            continue
        cleaned.append(ln)
    lines = cleaned
    inject_idx = None

    for idx, line in enumerate(lines):
        if '画起始线' in line:
            inject_idx = idx
            break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if re.match(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                        line.strip()):
                inject_idx = idx
                break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if 'SM_PRINT_PREEXTRUDE_FILAMENT' in line:
                inject_idx = idx
                break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if line.strip().startswith('ACE_SWAP_HEAD HEAD='):
                inject_idx = idx
                break
    initial = {}

    body_start = inject_idx if inject_idx is not None else 0
    initial_marker_re = re.compile(
        r'^;\s*multiACE initial-load HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$')
    if inject_idx is not None:
        for line in lines[:body_start]:
            m = initial_marker_re.match(line.strip())
            if not m:
                continue
            initial[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))

    for i in range(body_start, len(lines)):
        line = lines[i]
        ls = line.strip()
        m_t = re.match(r'^T([0-3])\s*$', ls)
        if m_t:
            head = int(m_t.group(1))
            if head not in initial:

                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                ace_m = None
                if j < len(lines):
                    ace_m = re.match(
                        r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$',
                        lines[j].strip())
                if ace_m and int(ace_m.group(1)) == head:
                    initial[head] = (int(ace_m.group(2)), int(ace_m.group(3)))
            continue
        m = re.match(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$', ls)
        if m:
            head = int(m.group(1))
            if head not in initial:
                initial[head] = (int(m.group(2)), int(m.group(3)))

    if inject_idx is None or not initial:
        return gcode, 0
    inject = ['', '; multiACE auto-load: load initial filaments']
    for head in sorted(initial):
        ace, slot = initial[head]
        inject.append('ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, slot))
    inject.append('; multiACE auto-load: end')
    inject.append('')
    new_lines = lines[:inject_idx] + inject + lines[inject_idx:]
    return '\n'.join(new_lines), len(initial)

def rewrite_for_mode(gcode, result, mode='optimize', num_aces=None):
    """Server-side entry point: apply the optimize or layer remap to
    an already-live-lookup-rewritten gcode body. `result` is what
    plan_loadout() returned for that body.

    Returns the rewritten gcode (string). Raises ValueError for
    invalid mode or when layer mode isn't feasible for this gcode."""
    if mode not in ('slicer', 'optimize', 'layer'):
        raise ValueError('mode must be slicer/optimize/layer')
    if mode == 'slicer':
        return gcode
    if mode == 'layer':
        layer_info = (result or {}).get('layer_info') or {}
        if not layer_info.get('feasible'):
            raise ValueError('layer mode not feasible for this gcode')
        if num_aces is not None and layer_info.get('aces_needed', 0) > num_aces:
            raise ValueError('layer mode needs %d ACEs, have %d' % (
                layer_info['aces_needed'], num_aces))
        new_gcode, _loadout = apply_layer_remap(gcode, layer_info)
        return new_gcode

    remap, _opt_swaps = compute_optimal_remap(result)
    if remap:
        return apply_remap(gcode, remap)
    return gcode

def _emit_progress(progress, seen, total, last_pr):
    """Helper: invoke `progress` if at least 1 MB has elapsed since
    `last_pr`. Returns the new last_pr value."""
    if progress is None or seen < last_pr + (1 << 20):
        return last_pr
    try:
        progress(seen, total)
    except Exception:
        pass
    return seen

def plan_loadout_from_file(in_path, num_aces=3, progress=None):
    """Memory-efficient wrapper around plan_loadout(). Streams the file
    line-by-line and keeps ONLY the lines plan_loadout actually parses:
    `; Change Tool X -> Tool Y` markers, `;LAYER_CHANGE` markers, the
    `; filament_colour = ...` / `; filament_type = ...` header lines,
    and bare `T<n>` commands. For a typical multi-color gcode this
    proxy is well under 1 % of the source size, so plan_loadout's
    in-memory analysis runs on hundreds of KB instead of tens of MB.

    Returns the same dict shape as plan_loadout() - None when no
    body toolchanges are found."""
    keep_re = re.compile(
        r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$)',
        re.IGNORECASE)
    parts = []
    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            seen += len(line.encode('utf-8', errors='ignore'))
            if keep_re.match(line):
                parts.append(line.rstrip('\n'))
            last_pr = _emit_progress(progress, seen, total, last_pr)
    proxy = '\n'.join(parts)
    del parts
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return plan_loadout(proxy, num_aces=num_aces)

def apply_remap_to_file(in_path, out_path, remap, progress=None):
    """Streaming equivalent of apply_remap(gcode, remap). When remap
    is empty the input is just copied unchanged."""
    import shutil
    if not remap:
        shutil.copyfile(in_path, out_path)
        if progress is not None:
            try:
                size = os.path.getsize(in_path)
                progress(size, size)
            except Exception:
                pass
        return

    def rm(s):
        try:
            return remap.get(int(s), int(s))
        except (TypeError, ValueError):
            return s

    bare_t_re      = re.compile(r'^T(\d{1,2})\s*$')
    m104_re        = re.compile(r'^M10[49]\b')
    pre_extrude_re = re.compile(r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(\d+)')

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(out_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')
            m = bare_t_re.match(stripped)
            if m:
                fout.write('T%d\n' % rm(m.group(1)))
            elif m104_re.match(stripped):
                fout.write(re.sub(
                    r'T(\d+)', lambda t: 'T%d' % rm(t.group(1)), line))
            else:
                fout.write(pre_extrude_re.sub(
                    lambda mm: 'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d' % rm(mm.group(1)),
                    line))
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass

def rewrite_to_file(in_path, out_path, progress=None, ace_targets=None,
                    tool_targets=None, route_plan=None):
    """Streaming equivalent of rewrite(gcode). Same M104/M109 +
    SM_PRINT_PREEXTRUDE_FILAMENT handling, same body T4-T15 expansion
    + ACE_SWAP_HEAD dedupe + swap-back insertion. Returns
    (active_swaps, skipped_swaps, swapback_count) matching the
    in-memory version's contract."""
    m104_re   = re.compile(r'^M10[49]\b')
    preextr_re = re.compile(r'^(SM_PRINT_PREEXTRUDE_FILAMENT INDEX=)(1[0-5]|[0-9])(\b.*)$')
    change_t  = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')
    ace_targets = _normalize_ace_targets(ace_targets)
    tool_targets = _normalize_tool_targets(tool_targets)
    route_plan_targets = _normalize_route_plan_targets(route_plan)
    if route_plan_targets:
        tool_targets = route_plan_targets
    route_cursor = _route_plan_event_cursor(route_plan)
    bare_hi   = re.compile(r'^T(1[0-5]|[0-9])\s*$')
    bare_lo   = re.compile(r'^T([0-3])\s*$')
    swap_re   = re.compile(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$')

    def fix_m104(line):
        return re.sub(r'T(1[0-5]|[0-9])',
                      lambda t: 'T%d' % _target_for_tool(
                          int(t.group(1)), ace_targets, tool_targets)['head'],
                      line)

    in_body = False
    head_loaded = {}
    active = 0
    skipped = 0
    swapbacks = 0

    pending_head = None
    pending_blanks: list[str] = []

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0

    def flush_pending_unmatched(fout):
        """Pending bare T<n> wasn't followed by ACE_SWAP_HEAD - emit
        it as a swap-back if the head currently holds a non-initial
        color. Swap-backs also count toward `active` because the
        in-memory rewrite() counts every ACE_SWAP_HEAD line in the
        output, regardless of provenance."""
        nonlocal pending_head, swapbacks, active
        if pending_head is None:
            return
        head = pending_head
        pending_head = None
        fout.write('T%d\n' % head)
        for b in pending_blanks:
            fout.write(b)
        pending_blanks.clear()

    def flush_pending_paired(fout):
        """Pending bare T was followed by ACE_SWAP_HEAD - emit it
        and let the swap handler update head_loaded."""
        nonlocal pending_head
        if pending_head is None:
            return
        head = pending_head
        pending_head = None
        fout.write('T%d\n' % head)
        for b in pending_blanks:
            fout.write(b)
        pending_blanks.clear()

    def emit_route_commands(fout, commands):
        nonlocal active, skipped
        for cmd in commands:
            stripped_cmd = str(cmd).strip()
            if not stripped_cmd:
                continue
            m_cmd = swap_re.match(stripped_cmd)
            if not m_cmd:
                fout.write(stripped_cmd + '\n')
                continue
            head = int(m_cmd.group(1))
            ace = int(m_cmd.group(2))
            slot = int(m_cmd.group(3))
            key = (ace, slot)
            if head_loaded.get(head) == key:
                fout.write('; ' + stripped_cmd + '  ; skipped (already loaded)\n')
                skipped += 1
            else:
                head_loaded[head] = key
                fout.write(stripped_cmd + '\n')
                active += 1

    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(out_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')

            if m104_re.match(stripped):
                if pending_head is not None:
                    flush_pending_unmatched(fout)
                fout.write(fix_m104(line))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m_pre = preextr_re.match(stripped)
            if m_pre:
                if pending_head is not None:
                    flush_pending_unmatched(fout)
                target = _target_for_tool(
                    int(m_pre.group(2)), ace_targets, tool_targets)
                head = target['head']
                fout.write('%s%d%s\n' % (m_pre.group(1), head, m_pre.group(3)))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            if not in_body and change_t.match(stripped):
                in_body = True

            if not in_body:
                if pending_head is not None:
                    flush_pending_unmatched(fout)
                m = bare_hi.match(stripped)
                if m:
                    target = _target_for_tool(
                        int(m.group(1)), ace_targets, tool_targets)
                    head = target['head']
                    fout.write('T%d\n' % head)
                    if target['kind'] == 'ace':
                        fout.write(_initial_marker(
                            head, target['ace'], target['slot']) + '\n')
                else:
                    fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            if pending_head is not None:
                if not stripped.strip():

                    pending_blanks.append(line)
                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue
                if stripped.startswith('ACE_SWAP_HEAD'):
                    flush_pending_paired(fout)

                else:
                    flush_pending_unmatched(fout)

            m = swap_re.match(stripped)
            if m:
                head = int(m.group(1)); ace = int(m.group(2)); slot = int(m.group(3))
                key = (ace, slot)
                if head_loaded.get(head) == key:
                    fout.write('; ' + stripped + '  ; skipped (already loaded)\n')
                    skipped += 1
                else:
                    head_loaded[head] = key
                    fout.write(line if line.endswith('\n') else (line + '\n'))
                    active += 1
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m = bare_hi.match(stripped)
            if m:
                emit_route_commands(
                    fout,
                    _commands_for_tool_event(
                        int(m.group(1)), route_cursor, ace_targets, tool_targets))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m = bare_lo.match(stripped)
            if m:
                pending_head = int(m.group(1))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)

        if pending_head is not None:
            flush_pending_unmatched(fout)

    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return active, skipped, swapbacks

def inject_auto_load_to_file(in_path, out_path, progress=None):
    """Streaming equivalent of inject_auto_load(gcode).

    Three passes:
      A. Find the injection anchor (highest priority across the four
         anchor types) and the byte ranges of any pre-existing auto-
         load block(s) to strip.
      B. Re-scan from the anchor onwards (skipping stripped ranges) to
         build initial[head] = (ace, slot) only from explicit
         ACE_SWAP_HEAD HEAD/ACE/SLOT commands. Bare T<head> is not
         enough to infer the ACE slot.
      C. Write the output, injecting the auto-load block right before
         the anchor line and dropping any old-block lines.

    The previous single-pass implementation took the file's first
    ACE_SWAP_HEAD HEAD=X anywhere as initial[X]. That meant the bare
    T<head> at print start (no following swap) was ignored and
    initial[head] inherited from a much later mid-print swap - wrong
    cartridge auto-loaded for the print's initial tool.

    Anchor priority (must match the in-memory inject_auto_load):
      1. First line containing the Snapmaker prime-line section header
         ('画起始线') - anchors BEFORE the inline prime so the auto-
         load completes before the runout sensor fires on prime.
      2. First '; Change Tool X -> Tool Y' marker (Orca multi-tool).
      3. First SM_PRINT_PREEXTRUDE_FILAMENT line (fallback for
         single-tool prints without Change Tool markers).
      4. First ACE_SWAP_HEAD HEAD= line."""
    preextr_re = re.compile(r'^SM_PRINT_PREEXTRUDE_FILAMENT\b')
    chg_re     = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')
    swap_re    = re.compile(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$')
    bare_t_re  = re.compile(r'^T([0-3])\s*$')
    initial_re = re.compile(
        r'^;\s*multiACE initial-load HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$')
    auto_load_re = re.compile(r'^;\s*multiACE auto-load:\s')

    first_huaqi   = None
    first_chg     = None
    first_preextr = None
    first_swap    = None

    in_old_block = False
    old_block_ranges: list[tuple[int, int]] = []
    block_start = None
    last_line_no = -1
    total = os.path.getsize(in_path) or 1

    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        for line_no, line in enumerate(fin):
            last_line_no = line_no
            stripped = line.rstrip('\r\n')
            ls_strip = stripped.strip()

            if in_old_block:
                if ls_strip.startswith('; multiACE auto-load: end'):
                    old_block_ranges.append((block_start, line_no))
                    in_old_block = False
                    block_start = None
                continue
            if ls_strip.startswith('; multiACE auto-load: load'):
                in_old_block = True
                block_start = line_no
                continue

            if first_huaqi is None and '画起始线' in stripped:
                first_huaqi = line_no
            if first_chg is None and chg_re.match(stripped):
                first_chg = line_no
            if first_preextr is None and preextr_re.match(stripped):
                first_preextr = line_no
            if first_swap is None and stripped.startswith('ACE_SWAP_HEAD HEAD='):
                first_swap = line_no

    if in_old_block and block_start is not None:
        old_block_ranges.append((block_start, last_line_no))

    anchor_line_no = None
    for candidate in (first_huaqi, first_chg, first_preextr, first_swap):
        if candidate is not None:
            anchor_line_no = candidate
            break

    in_block_set = set()
    for (a, b) in old_block_ranges:
        for j in range(a, b + 1):
            in_block_set.add(j)

    initial: dict[int, tuple[int, int]] = {}
    pending_t_head: int | None = None

    if anchor_line_no is not None:
        with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
            for line_no, line in enumerate(fin):
                if line_no < anchor_line_no and line_no not in in_block_set:
                    m_i = initial_re.match(line.strip())
                    if m_i:
                        initial[int(m_i.group(1))] = (
                            int(m_i.group(2)), int(m_i.group(3)))
                    continue
                if line_no < anchor_line_no:
                    continue
                if line_no in in_block_set:
                    continue
                stripped = line.strip()

                if pending_t_head is not None:
                    if stripped == '':
                        continue
                    m_s = swap_re.match(stripped)
                    if m_s and int(m_s.group(1)) == pending_t_head:
                        if pending_t_head not in initial:
                            initial[pending_t_head] = (int(m_s.group(2)),
                                                       int(m_s.group(3)))
                        pending_t_head = None
                        continue
                    pending_t_head = None

                m_t = bare_t_re.match(stripped)
                if m_t:
                    head = int(m_t.group(1))
                    if head not in initial:
                        pending_t_head = head
                    continue

                m_s = swap_re.match(stripped)
                if m_s:
                    head = int(m_s.group(1))
                    if head not in initial:
                        initial[head] = (int(m_s.group(2)), int(m_s.group(3)))

    inject_block: list[str] = []
    if initial:
        inject_heads = sorted(initial.keys())
        inject_block.append('; multiACE auto-load: load %d head(s)\n' %
                            len(inject_heads))
        for h in inject_heads:
            a, s = initial[h]
            inject_block.append(
                'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d\n' % (h, a, s))
        inject_block.append('; multiACE auto-load: end\n')

    seen = 0
    last_pr = 0
    injected = False
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(out_path, 'w', encoding='utf-8') as fout:
        for line_no, line in enumerate(fin):
            seen += len(line.encode('utf-8', errors='ignore'))
            if line_no in in_block_set:
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue
            if (not injected and anchor_line_no is not None
                    and line_no == anchor_line_no and inject_block):
                for b in inject_block:
                    fout.write(b)
                injected = True
            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return len(initial)

def apply_layer_remap_to_file(in_path, out_path, layer_info, progress=None):
    """Streaming equivalent of apply_layer_remap. Mirrors the in-memory
    semantics exactly: parses the original target Y from each
    `; Change Tool X -> Tool Y` comment, and on the NEXT bare T or
    M104/M109 line rewrites the T value to `T<head + 4*ace>` derived
    from current_slot[Y] (which advances as layer events fire on
    each ;LAYER_CHANGE)."""
    if not layer_info or not layer_info.get('feasible'):
        total = os.path.getsize(in_path) or 1
        seen = 0
        last_pr = 0
        with open(in_path, 'r', encoding='utf-8', errors='replace') as fin, \
             open(out_path, 'w', encoding='utf-8') as fout:
            for line in fin:
                seen += len(line.encode('utf-8', errors='ignore'))
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
        if progress is not None:
            try:
                progress(total, total)
            except Exception:
                pass
        return None

    initial = dict(layer_info.get('initial_loadout') or {})
    events = list(layer_info.get('events') or [])

    current_slot = {c: (h, c // 4) for c, h in initial.items()}
    events_by_layer: dict[int, list[tuple[int, int, int]]] = {}
    for i, c_in, c_out, h in events:
        events_by_layer.setdefault(i, []).append((c_in, c_out, h))

    loadout: dict[tuple[int, int], int] = {}
    for c, h in initial.items():
        loadout[(0, h)] = c
    ace_counter_pre = [0, 0, 0, 0]
    for (i, c_in, c_out, h) in events:
        ace_counter_pre[h] += 1
        loadout[(ace_counter_pre[h], h)] = c_in

    change_re = re.compile(
        r'^(;\s*Change Tool\s*\d+\s*->\s*Tool\s*)(\d+)(.*)$')
    bare_re   = re.compile(r'^T(\d{1,2})\s*$')
    m104_re   = re.compile(r'^(M10[49]\b.*)$')
    layer_re  = re.compile(r'^;\s*LAYER_CHANGE')
    chg_t_re  = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')

    layer_idx = 0
    pending_target: int | None = None
    head_ace_counter = [0, 0, 0, 0]
    in_body = False

    def advance_to_layer(new_idx: int) -> None:
        nonlocal layer_idx
        for ll in range(layer_idx + 1, new_idx + 1):
            for c_in, c_out, h in events_by_layer.get(ll, []):
                head_ace_counter[h] += 1
                ace = head_ace_counter[h]
                current_slot[c_in] = (h, ace)
        layer_idx = new_idx

    def m104_repl(line: str, pt: int) -> str:
        h, ace = current_slot.get(pt, (pt % 4, pt // 4))
        target = 'T%d' % (h + 4 * ace)
        def _r(mm):
            return target if int(mm.group(1)) == pt else mm.group(0)
        return re.sub(r'T(\d{1,2})', _r, line)

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(out_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')
            s = stripped.strip()

            if not in_body:
                if chg_t_re.match(s):
                    in_body = True
                else:
                    fout.write(line)
                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue

            if layer_re.match(s):
                advance_to_layer(layer_idx + 1)
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mc = change_re.match(s)
            if mc:
                pending_target = int(mc.group(2))
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mb = bare_re.match(s)
            if mb and pending_target is not None:
                h, ace = current_slot.get(pending_target,
                                          (pending_target % 4,
                                           pending_target // 4))
                fout.write('T%d\n' % (h + 4 * ace))
                pending_target = None
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mh = m104_re.match(s)
            if mh and pending_target is not None:
                fout.write(m104_repl(line.rstrip('\n'), pending_target) + '\n')
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return loadout

def main():

    args = sys.argv[1:]
    num_aces = None
    optimize = False
    layer_mode = False
    auto_load = True
    live_lookup_host = None
    strict_color = False
    fuzzy_max_distance = None
    if '--aces' in args:
        i = args.index('--aces')
        num_aces = int(args[i + 1])
        del args[i:i + 2]
    if '--optimize' in args:
        args.remove('--optimize')
        optimize = True
    if '--layer' in args:
        args.remove('--layer')
        layer_mode = True
    if '--no-auto-load' in args:
        args.remove('--no-auto-load')
        auto_load = False
    if '--auto-load' in args:

        args.remove('--auto-load')
        auto_load = True
    if '--live-lookup' in args:
        i = args.index('--live-lookup')

        next_arg = args[i + 1] if i + 1 < len(args) else None
        if next_arg and not next_arg.lower().endswith(('.gcode', '.gco', '.g')):
            live_lookup_host = next_arg
            del args[i:i + 2]
        else:
            live_lookup_host = os.environ.get('MULTIACE_HOST', '127.0.0.1')
            del args[i]
    if '--strict-material' in args:

        args.remove('--strict-material')
    if '--strict-color' in args:
        args.remove('--strict-color')
        strict_color = True
    if '--fuzzy-color' in args:
        i = args.index('--fuzzy-color')
        next_arg = args[i + 1] if i + 1 < len(args) else None
        if next_arg and not next_arg.lower().endswith(('.gcode', '.gco', '.g')):
            try:
                fuzzy_max_distance = int(next_arg)
                del args[i:i + 2]
            except ValueError:
                fuzzy_max_distance = 30
                del args[i]
        else:
            fuzzy_max_distance = 30
            del args[i]
    filepath = args[0]

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        gcode = f.read()

    if num_aces is None:
        num_aces = infer_num_aces(gcode)
        print('Auto-detected %d ACE(s) from slicer T-index assignment '
              '(override with --aces N if needed)' % num_aces)

    if live_lookup_host is not None:
        live_slots = lookup_live_slots(live_lookup_host)
        if live_slots is None:
            print('ERROR: live-lookup failed (printer unreachable). '
                  'Either fix connectivity or remove --live-lookup.',
                  file=sys.stderr)
            sys.exit(1)
        if not live_slots:
            print('ERROR: live-lookup returned 0 loaded slots - load filaments '
                  'from the multiACE dashboard first and try again.',
                  file=sys.stderr)
            sys.exit(1)

        print('Read printer loadout (live, %d slot(s)):' % len(live_slots))
        for s in sorted(live_slots, key=lambda x: (x['ace'], x['slot'])):
            color_str = format_color_hex_rgb(s['color']) if s['color'] else '(no color)'
            material = s['material'] or '?'
            name = approx_color_name(s['color']) if s['color'] else ''
            label = (' %s' % name) if name and name.lower() != (s['color'] or '').lstrip('#').lower() else ''
            print('  ACE %d Slot %d  %-6s %s%s' % (
                s['ace'], s['slot'], material, color_str, label))
        slicer_colors = parse_color_names(gcode)
        slicer_types = parse_filament_types(gcode)

        missing_mats = check_material_availability(slicer_types, live_slots)
        if missing_mats:
            print('ERROR: the slicer needs filament(s) of material(s) '
                  'that are not loaded anywhere on the printer:',
                  file=sys.stderr)
            for m in missing_mats:

                ts = sorted(t for t, mat in slicer_types.items()
                            if (mat or '').strip().lower() == m)
                ts_str = ', '.join('T%d' % t for t in ts) if ts else ''
                print('  %s%s' % (m.upper(),
                                   ('  (needed by ' + ts_str + ')') if ts_str else ''),
                      file=sys.stderr)
            print('Load filament of the missing material(s) into any slot '
                  'from the multiACE dashboard and re-run.', file=sys.stderr)
            sys.exit(1)

        live_remap, match_info, _ = match_colors_to_slots(
            slicer_colors, live_slots, num_heads=4,
            filament_types=slicer_types,
            strict_color=strict_color,
            fuzzy_max_distance=fuzzy_max_distance)

        _tier_label = {
            'exact_hex':         'Exact match',
            'name_exact':        'Name match (exact)',
            'name_base':         'Name match (qualifier)',
            'name_canon':        'Name match (synonym)',
            'fuzzy':             'Fuzzy match',
            'loose_exact_hex':   'Loose-material exact',
            'loose_name_exact':  'Loose-material name',
            'loose_name_base':   'Loose-material name (qualifier)',
            'loose_name_canon':  'Loose-material name (synonym)',
            'loose_fuzzy':       'Loose-material fuzzy',
            'fallback':          'Fallback (no colour match)',
            'duplicate':         'Duplicate (shared slot - wrong colour)',
            'no_slot':           'No slot available',
        }
        _tier_warn = {'loose_exact_hex', 'loose_name_exact', 'loose_name_base',
                      'loose_name_canon', 'loose_fuzzy', 'fallback',
                      'duplicate', 'no_slot'}

        by_tier = {}
        for t in sorted(match_info.keys()):
            by_tier.setdefault(match_info[t]['tier'], []).append(t)
        print('Live-lookup match (slicer T -> physical ACE/Slot):')

        order = ('exact_hex', 'name_exact', 'name_base', 'name_canon',
                 'fuzzy', 'loose_exact_hex', 'loose_name_exact',
                 'loose_name_base', 'loose_name_canon', 'loose_fuzzy',
                 'fallback', 'no_slot')
        any_warn = False
        for tier in order:
            ts = by_tier.get(tier)
            if not ts:
                continue
            mark = '! ' if tier in _tier_warn else '  '
            if tier in _tier_warn:
                any_warn = True
            print('  [%s]' % _tier_label[tier])
            for t in ts:
                hex_c = slicer_colors.get(t, '?')
                mat = slicer_types.get(t, '?')
                inf = match_info[t]
                if inf['slot'] is None:
                    print('  %sT%-2d %-6s %s  ->  (no unclaimed slot left)' % (
                        mark, t, mat, format_color_hex_rgb(hex_c)))
                    continue
                s = inf['slot']
                slot_mat = s['material'] or '?'
                slot_hex = s['color']
                print('  %sT%-2d %-6s %s  ->  ACE %d Slot %d  %-6s %s' % (
                    mark, t, mat, format_color_hex_rgb(hex_c),
                    s['ace'], s['slot'], slot_mat,
                    format_color_hex_rgb(slot_hex)))
        if any_warn:
            print('  Note: tiers marked ! are degraded matches - the '
                  'print will proceed but colours/materials at those '
                  'tools will differ from what the slicer assumed.')

        no_slot_ts = by_tier.get('no_slot') or []
        if no_slot_ts:
            print('ERROR: too few loaded slots - the slicer uses %d '
                  'tools but only %d slots are loaded. Load more '
                  'filament and re-run.' % (
                      len(match_info), len(live_slots)),
                  file=sys.stderr)
            sys.exit(1)

        if live_remap:
            gcode = apply_remap(gcode, live_remap)

    result = plan_loadout(gcode, num_aces=num_aces)
    if result is not None:
        print_recommendation(result, num_aces)

    remap_info = None
    layer_remap_applied = False
    if layer_mode and result is not None:
        layer_info = result.get('layer_info')
        if (layer_info and layer_info.get('feasible')
                and layer_info.get('aces_needed', 0) <= num_aces):
            gcode, _loadout = apply_layer_remap(gcode, layer_info)
            layer_remap_applied = True
            print()
            print('--- LAYER MODE applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], layer_info['layer_swaps'],
                result['swaps'] - layer_info['layer_swaps']))
            print('Load cartridges per the Pre-load + Additional swap lists above.')
        elif layer_info and layer_info.get('feasible'):
            print()
            print('--- LAYER MODE skipped: plan needs %d ACEs, you have %d (pass --aces %d to enable) ---' % (
                layer_info['aces_needed'], num_aces, layer_info['aces_needed']))

    if optimize and not layer_remap_applied and result is not None:
        remap, opt_swaps = compute_optimal_remap(result)
        if remap:
            gcode = apply_remap(gcode, remap)
            remap_info = (remap, result['swaps'], opt_swaps)
            print()
            print('--- AUTO-REMAP applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], opt_swaps, result['swaps'] - opt_swaps))
            print('Load filaments per the Optimized Print Loadout above.')
            print('T remap (old -> new): %s' % ', '.join(
                'T%d->T%d' % (k, v) for k, v in sorted(remap.items())))

    ace_targets = _route_ace_targets_from_slots(live_slots) if live_lookup_host is not None else None

    gcode, active_swaps, skipped_swaps, swapback_count = rewrite(
        gcode, ace_targets=ace_targets)
    if active_swaps + skipped_swaps + swapback_count > 0:
        print('Rewrite: %d active ACE_SWAP_HEAD, %d skipped, %d swap-backs inserted' % (
            active_swaps, skipped_swaps, swapback_count))

    auto_load_count = 0
    if auto_load:
        gcode, auto_load_count = inject_auto_load(gcode)
        if auto_load_count > 0:
            print('Auto-load: injected ACE_SWAP_HEAD for %d head(s) before first T command' % auto_load_count)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(gcode)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    logpath = os.path.join(script_dir, 'multiace_postprocess.log')
    try:
        import io
        from datetime import datetime
        logbuf = io.StringIO()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print('=== %s  %s ===' % (ts, os.path.abspath(filepath)), file=logbuf)
        if result is not None:
            print_recommendation(result, num_aces, file=logbuf)
        if layer_remap_applied and result is not None:
            li = result['layer_info']
            print('--- LAYER MODE applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], li['layer_swaps'],
                result['swaps'] - li['layer_swaps']), file=logbuf)
        if remap_info is not None:
            print('--- AUTO-REMAP applied: %d swaps -> %d (%d saved) ---' % (
                remap_info[1], remap_info[2], remap_info[1] - remap_info[2]),
                file=logbuf)
            print('T remap (old -> new): %s' % ', '.join(
                'T%d->T%d' % (k, v) for k, v in sorted(remap_info[0].items())),
                file=logbuf)
        if active_swaps + skipped_swaps + swapback_count > 0:
            print('Rewrite: %d active ACE_SWAP_HEAD, %d skipped, %d swap-backs inserted' % (
                active_swaps, skipped_swaps, swapback_count), file=logbuf)
        if auto_load_count > 0:
            print('Auto-load: injected ACE_SWAP_HEAD for %d head(s) before first T command' % auto_load_count, file=logbuf)
        with open(logpath, 'a', encoding='utf-8') as f:
            f.write(logbuf.getvalue())
            if not logbuf.getvalue().endswith('\n'):
                f.write('\n')
    except Exception:
        pass

if __name__ == '__main__':
    main()
