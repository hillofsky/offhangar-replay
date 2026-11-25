"""Build a native 2.3.1.2 movement replay from an OffHangar fleet track."""
import base64
import datetime
import json
import math
import os
import pickletools
import pickle
from pathlib import Path
import struct
import sys
import traceback
import zlib
from contextlib import contextmanager

from replay_sample import decode, encode


U32 = struct.Struct('<I')
XYZ = struct.Struct('<3f')
FRAME = struct.Struct('<I6fi')

def template_layout(meta, packets):
    if 'v.2.3.1.2 ' not in meta.get('clientVersionFromXml', '') or meta.get('oh2Fleet'):
        raise ValueError('Select a native World of Tanks 2.3.1.2 replay.')
    roster = {int(key): v for key, v in meta['vehicles'].items()}
    cell = next(b for k, t, b in packets if k == 1 and t == 0)
    avatar = U32.unpack_from(cell)[0]
    own = U32.unpack_from(cell, 67)[0]
    if cell[66] != roster[own]['team'] or roster[own]['name'] != meta['playerName']:
        raise ValueError('Unsupported player entity in replay template.')
    for kind, stamp, spawn in packets:
        if kind != 5 or stamp != 0 or len(spawn) < 65 or spawn[4] != 6:
            continue
        generic = U32.unpack_from(spawn)[0]
        if generic not in roster or generic == own:
            continue
        name = bw_string(roster[generic]['name'].encode())
        if spawn[46:52] == bytes.fromhex('0a010005010d') and spawn[53:55] == b'\x80\x0e' and spawn[63:].startswith(name):
            break
    else:
        raise ValueError('No compatible vehicle spawn in this replay.')
    vehicle_keys = [U32.pack(key) + b'\x01' for key in roster]
    roster_packet = next(b for k, t, b in packets if k == 0x32 and t == 0
                         and len(b) > 26 and b[25] == len(roster)
                         and all(key in b for key in vehicle_keys))
    return own, avatar, generic, spawn, roster_packet


def load_track(path, extras=None):
    roster, frames = {}, []
    with path.open('rb') as stream:
        if stream.read(4) != b'OHF1':
            raise ValueError('Invalid track header')
        header = end = None
        while True:
            record = stream.read(5)
            if not record:
                break
            kind, length = struct.unpack('<cI', record)
            if length > 1024 * 1024:
                raise ValueError('Invalid track record size')
            data = stream.read(length)
            if len(data) != length:
                raise ValueError('Incomplete track')
            if kind == b'H':
                header = json.loads(data)
            elif kind == b'V':
                vehicle = json.loads(data)
                roster[vehicle['id']] = vehicle
            elif kind == b'F':
                frames.append((struct.unpack_from('<f', data)[0], list(FRAME.iter_unpack(data[4:]))))
            elif extras is not None and kind in (b'C', b'X', b'A', b'P', b'R', b'O', b'L', b'S', b'I', b'U'):
                if kind == b'A':
                    stamp = struct.unpack_from('<f', data)[0]
                    extras.append((stamp, kind, list(struct.iter_unpack('<IH',data[4:]))))
                else:
                    stamp, *args = json.loads(data)
                    extras.append((stamp, kind, args))
            elif kind == b'E':
                end = json.loads(data)
    if not end or end['vehicles'] != len(roster) or end['frames'] != len(frames) or not frames:
        raise ValueError('Track is incomplete or empty')
    return header, roster, frames


def bw_string(data):
    if len(data) < 255:
        return bytes([len(data)]) + data
    return b'\xff' + len(data).to_bytes(3, 'little') + data


def native_roster(packet, original, slots, roster):
    starts = sorted((packet.index(U32.pack(slot) + b'\x01'), slot) for slot in original)
    assert len(starts) == packet[starts[0][0] - 1] and starts[0][0] == 26
    start, template_slot = starts[0]
    row = packet[start:starts[1][0]]
    i = 5
    for _ in range(4):
        i += row[i] + 1
    session = bw_string(str(template_slot).encode())
    j = row.index(session, i)
    account = row[i:j]
    tail = row[j+len(session):]
    assert account[0] == original[template_slot]['team']
    assert struct.unpack_from('<H', tail, 4)[0] == original[template_slot]['maxHealth']
    data = bytearray(packet[4:start-1]) + bytes([len(roster)])
    for key, slot in slots.items():
        v = roster[key]
        attrs = bytearray(account)
        attrs[0] = v['team']
        details = bytearray(tail)
        struct.pack_into('<H', details, 4, v['maxHealth'])
        name = v['name'].encode()
        data.extend(U32.pack(slot) + b'\x01' + bw_string(base64.b64decode(v['outfit'])) +
                    bw_string(base64.b64decode(v['cd'])) + bw_string(name) + bw_string(name) +
                    attrs + bw_string(str(slot).encode()) + details)
    return U32.pack(len(data)) + data


def avatar_base(payload, player, arena):
    name_end = 15 + payload[14]
    session_end = name_end + 1 + payload[name_end]
    suffix = bytearray(payload[session_end:])
    U32.pack_into(suffix, 8, arena)
    assert suffix[14] == 255
    size = int.from_bytes(suffix[15:18], 'little')
    extra = bytes(suffix[18:18+size])
    ops = list(pickletools.genops(extra))
    i = next(n for n, (_, value, _) in enumerate(ops) if value == 'arenaTypeID') + 2
    assert ops[i][0].name == 'BININT1'
    value = b'K' + bytes([arena]) if 0 <= arena < 256 else b'J' + struct.pack('<i', arena)
    extra = extra[:ops[i][2]] + value + extra[ops[i+1][2]:]
    rest = bytes(suffix[:14]) + bw_string(extra) + bytes(suffix[18+size:])
    data = bw_string(player['name'].encode()) + payload[name_end:session_end] + rest
    return payload[:10] + U32.pack(len(data)) + data


def native_spawn(template, slot, v, pose, own, avatar):
    name, body = v['name'].encode(), template[46:]
    start = 17
    cd = start + 1 + body[start]
    outfit = cd + 1 + body[cd]
    tail = bytearray(body[outfit + 1 + body[outfit]:])
    # PublicInfo after the three strings: index, outfit level, team, platoon,
    # marks, crew groups and commander skin, then maximum HP.
    tail[2] = v['team']
    struct.pack_into('<H', tail, 9 + 2 * tail[8] + 2, v['maxHealth'])
    prefix = bytearray(body[:start])
    if prefix[:6] != bytes.fromhex('0a010005010d') or prefix[7:9] != b'\x80\x0e':
        raise ValueError('Unexpected 2.3.1.2 vehicle seed')
    struct.pack_into('<h', prefix, 9, max(0, min(pose[7], 32767)))
    struct.pack_into('<I', prefix, 12, avatar if slot == own else slot + 1000)
    data = bytes(prefix) + bw_string(name) + bw_string(base64.b64decode(v['cd'])) + bw_string(base64.b64decode(v['outfit'])) + tail
    fixed = bytearray(template[:46])
    U32.pack_into(fixed, 0, slot)
    struct.pack_into('<6f', fixed, 18, *pose[1:7])
    U32.pack_into(fixed, 42, len(data))
    return bytes(fixed) + data


def quaternion(axes):
    # World-space camera right, up, forward are the rotation matrix columns.
    (a,d,g),(b,e,h),(c,f,i) = axes
    trace = a+e+i
    if trace > 0:
        s = math.sqrt(trace+1)*2
        return ((h-f)/s, (c-g)/s, (d-b)/s, s/4)
    if a>=e and a>=i:
        s = math.sqrt(1+a-e-i)*2
        return (s/4,(b+d)/s,(c+g)/s,(h-f)/s)
    if e>=i:
        s = math.sqrt(1+e-a-i)*2
        return ((b+d)/s,s/4,(f+h)/s,(c-g)/s)
    s = math.sqrt(1+i-a-e)*2
    return ((c+g)/s,(f+h)/s,s/4,(d-b)/s)


def message(entity, index, data):
    return struct.pack('<III',entity,index,len(data)) + data


def combat_packets(name, entity, args, slots, avatar):
    mapped = lambda key: slots.get(key, 0)
    slot = mapped(entity)
    if name == 'showTracer':
        who,shot,ric,fx,pref,shell,cal,pos,vel,gravity,dist,gun,installation = args
        data = struct.pack('<IIBBBBf3f3fffBB',mapped(who),shot,ric,fx&255,pref&255,shell,cal,
                           *pos,*vel,gravity,dist,gun&255,installation&255)
        yield 8,message(avatar,37,data)
    elif name == 'stopTracer':
        shot,pos = args
        yield 8,message(avatar,28,struct.pack('<I3f',shot,*pos))
    elif name == 'explodeProjectile':
        shot,fx,pref,mat,shell,cal,pos,direction,speed,objects = args
        data = struct.pack('<IBBBBf3f3ff',shot,fx&255,pref&255,mat&255,shell,cal,*pos,*direction,speed)
        yield 8,message(avatar,56,data + bw_array(objects,lambda i: U32.pack(i & 0xffffffff)))
    elif slot and name == 'showShooting':
        burst,gun,shell = args[:3]
        yield 8,message(slot,1,bytes((burst&255,gun&255,shell&255)))
    elif slot and name == 'onHealthChanged':
        new,old,killer,reason,ext = args
        yield 7,message(slot,14,struct.pack('<h',new))
        yield 8,message(slot,4,struct.pack('<hhIBB',new,old,mapped(killer),reason&255,ext&255))
    elif slot and name == 'showDamageFromShot':
        who,points,fx,pref,damage,factor,shield,speed,gun = args
        def point(p):
            return struct.pack('<IQI',p['networkID'] & 0xffffffff,p['segment'] & 0xffffffffffffffff,
                               p['params'] & 0xffffffff)
        data = U32.pack(mapped(who)) + bw_array(points,point)
        yield 8,message(slot,10,data + struct.pack('<BBiBBfB',fx&255,pref&255,damage,int(factor)&255,
                                                   bool(shield),speed,gun&255))
    elif slot and name == 'showDamageFromExplosion':
        who,center,fx,damage,factor,gun = args
        yield 8,message(slot,7,struct.pack('<I3fBiBB',mapped(who),*center,fx&255,damage,int(factor)&255,gun&255))


def bw_array(items, encode_item):
    count = len(items)
    prefix = bytes([count]) if count < 255 else b'\xff'+count.to_bytes(3,'little')
    return prefix+b''.join(encode_item(item) for item in items)


def append_radio(packets, extras, slots, origin):
    # The offline mod locally echoes some outgoing pings with an assigned
    # marker ID. Pair only opposite directions, keeping deliberate repeats.
    pending = []
    events = []
    for stamp,kind,args in extras:
        if kind != b'R':
            continue
        item, = args
        point = item['point']
        key = (item['action'],item['sender'],tuple(point) if point is not None else item['target'],
               item['text'],item['value'],item['small'])
        pending = [p for p in pending if stamp-p[0] <= .5]
        match = next((p for p in pending if p[1]==key and p[2]!=item['outgoing']),None)
        if match:
            pending.remove(match)
            if not item['outgoing']:
                events[match[3]] = (stamp,item)
        else:
            pending.append((stamp,key,item['outgoing'],len(events)))
            events.append((stamp,item))
    name = b'oh2Fleet.radio'
    for index,(stamp,original) in enumerate(events):
        item = dict(original)
        item['sender'] = slots.get(item['sender'],0)
        if item.pop('vehicle'):
            item['target'] = slots.get(item['target'],0)
        if item.pop('outgoing') and not item['target']:
            if item['point'] is not None:
                item['target'] = 1800000000+index
            elif item['action'] in ('POSITIVE','CONFIRM','NEGATIVE'):
                item['target'] = item['sender']
        payload = pickle.dumps((json.dumps(item,separators=(',',':')),),protocol=2)
        packets.append((0x31,max(0.,stamp-origin),U32.pack(len(name))+name+U32.pack(len(payload))+payload))


def append_periods(packets, extras, frames, origin, avatar):
    phases = [(stamp-origin, *args) for stamp, kind, args in extras if kind == b'P']
    if not phases and extras:
        # Early tracks without phase events still need BATTLE for sniper controls.
        phases = [(0., 3, 900., 900.)]
    if not phases:
        return
    clock = next((struct.unpack('<d', b)[0] for k, _, b in packets if k == 0x15), 0.)
    # The bootstrap belongs to a different battle. Replace its clock messages.
    packets[:] = [(k, t, b) for k, t, b in packets if k not in (0x16, 0x17)
                  and not (k == 8 and b.startswith(struct.pack('<II', avatar, 59)) and b[12:13] == b'\x03')]
    timeline = []
    for stamp, period, length, remaining in phases:
        deadline = stamp + remaining
        # The first sample follows the actual transition by up to one sampling interval.
        start = max(0., deadline-length) if period in (2, 3) and 0 <= remaining <= length else max(0., stamp)
        if timeline:
            start = max(timeline[-1][0], start)
        timeline.append((start, period, length, deadline))
    finish = max(frames[-1][0], max((stamp for stamp, _, _ in extras), default=origin)) - origin
    for i, (start, period, length, deadline) in enumerate(timeline):
        stop = timeline[i+1][0] if i+1 < len(timeline) else finish
        # Old recorders kept the 45-second loading guard and missed the real countdown.
        if period == 2 and length == 45. and i+1 < len(timeline) and timeline[i+1][1] == 3:
            if abs(deadline-stop) > .25:
                deadline, length = stop, stop-start
        info = zlib.compress(pickle.dumps((period, clock+deadline, length, ()), protocol=2))
        packets.append((8, start, message(avatar, 59, b'\x03'+bw_string(info))))
        packets.append((0x16, start, U32.pack(period)))
        # The native replay HUD reads arenaLength, which the live HUD records on each tick.
        tick = start
        while True:
            remaining = max(0., deadline-tick)
            packets.append((0x17, tick, struct.pack('<f', remaining)))
            tick += 1.
            if period not in (2, 3) or remaining == 0 or tick >= stop:
                break


def append_extra(packets, extras, slots, frames, origin, own_key, avatar):
    from bisect import bisect_left
    own = [(t,p[1:4]) for t,frame in frames for p in frame if p[0] == own_key]
    stamps = [t for t,p in own]
    last_mode = None
    last_zoom = None
    append_periods(packets, extras, frames, origin, avatar)
    normal_fov = next((a[3] for _,k,a in extras if k==b'C' and a[0]=='arcade'), 0.93266)
    append_radio(packets,extras,slots,origin)
    for stamp, kind, args in extras:
        t=max(0,stamp-origin)
        if kind == b'I' and args[0] != 'start':
            if args[0] == 'hit':
                _, yaw, attacker, damage, crits, blocked, he, reason = args
                row = ['hit', yaw, slots.get(attacker, 0), damage, crits, blocked, he, reason]
            elif args[0] == 'events':
                # DESTRUCTIBLE_DAMAGED (14) identifies a map object, not a vehicle.
                row = ['events', [[event, target if event == 14 else slots.get(target, 0), count, details]
                                  for event, target, count, details in args[1]]]
            else:
                continue
            name = b'oh2Fleet.incoming'
            payload = pickle.dumps((json.dumps(row, separators=(',', ':')),), protocol=2)
            packets.append((0x31, t, U32.pack(len(name))+name+U32.pack(len(payload))+payload))
        elif kind == b'S':
            name = b'oh2Fleet.spotted'
            payload = pickle.dumps(tuple(args), protocol=2)
            packets.append((0x31, t, U32.pack(len(name))+name+U32.pack(len(payload))+payload))
        elif kind == b'A':
            for key,angle in args:
                if key in slots:
                    packets.append((7,t,message(slots[key],13,struct.pack('<H',angle))))
        elif kind == b'X':
            for packet, data in combat_packets(*args,slots,avatar):
                packets.append((packet,t,data))
        elif kind == b'C' and own:
            mode,position,axes,fov,near = args[:5]
            gun = args[5] if len(args)>5 else None
            ray = axes[2]
            look = [p + 250*d for p,d in zip(position,ray)]
            if gun is not None:
                look,marker,direction,size,yaw,pitch = gun
                packets.append((0x1c,t,struct.pack('<f',yaw)))
                packets.append((0x1d,t,struct.pack('<f',pitch)))
            else:
                marker,direction,size = look,ray,1.
            packets.append((0x1a,t,struct.pack('<15f',*look,size,size,*marker,*direction,size,-1.,-1.,-1.)))
            if mode != last_mode and mode in ('arcade','sniper','strategic','arty','dualgun','postmortem'):
                packets.append((0x1b,t,U32.pack(len(mode))+mode.encode('ascii')))
                packets.append((0x2e,t,bytes([mode=='sniper'])))
                last_mode = mode
            zoom = round(normal_fov / max(0.001, fov), 3) if mode=='sniper' else 1.
            if zoom != last_zoom:
                callback = pickle.dumps((zoom,), protocol=2)
                packets.append((0x31,t,U32.pack(9)+b'applyZoom'+U32.pack(len(callback))+callback))
                last_zoom = zoom
            i=min(bisect_left(stamps,stamp),len(stamps)-1)
            if i and abs(stamps[i-1]-stamp)<abs(stamps[i]-stamp):
                i-=1
            offset=[v-base for v,base in zip(position,own[i][1])]
            packets.append((0x26,t,struct.pack('<10f',*quaternion(axes),*offset,fov,0,near)))


def make(track, seed_path, destination):
    extras = []
    header, roster, frames = load_track(track, extras)
    player_view = bool(header.get('playerViewOnly'))
    metadata, reference = decode(seed_path.read_bytes())
    meta = json.loads(metadata[0])
    own_slot, avatar, generic_slot, generic, roster_packet = template_layout(meta, reference)
    original = {int(i): v for i, v in meta['vehicles'].items()}
    player, = [v for v in roster.values() if v['own']]
    seed_team = original[own_slot]['team']
    slots = {player['id']: own_slot}
    for team in (1, 2):
        local = [key for key,v in sorted(roster.items()) if v['team'] == team and key != player['id']]
        donor_team = seed_team if team == player['team'] else 3 - seed_team
        available = [key for key,v in sorted(original.items()) if v['team'] == donor_team and key != own_slot]
        available.extend(0x06000000 + team * 0x10000 + i for i in range(max(0, len(local)-len(available))))
        slots.update(zip(local, available))
    first = {}
    for _, frame in frames:
        for pose in frame:
            first.setdefault(pose[0], pose)
    if set(roster) != set(first):
        raise ValueError('Missing vehicle positions')
    labels = {}
    initially_visible = {pose[0] for pose in frames[0][1]}
    for key, slot in slots.items():
        v = roster[key]
        baseline = dict(original.get(slot, original[generic_slot]))
        baseline.update(name=v['name'], fakeName='', clanAbbrev='', team=v['team'],
                        vehicleType=v['tank'], maxHealth=v['maxHealth'], avatarSessionID=str(slot),
                        position=[first[key][1], first[key][3]], badges=[], frags=0, tkills=0,
                        vehPostProgression=[], personalMissionIDs=[], prestigeLevel=0, stFrags=0)
        labels[str(slot)] = baseline
        if player_view and key not in initially_visible:
            baseline['position'] = [0.,0.]
    meta.update(playerName=player['name'], playerID=0, playerVehicle=player['tank'].replace(':','-'),
                dateTime=datetime.datetime.fromtimestamp(track.stat().st_mtime).strftime('%d.%m.%Y %H:%M:%S'),
                mapName=header['map'], mapDisplayName=header['map'], serverName='OffHangar',
                vehicles=labels, hasMods=True, oh2Fleet={'version':2 if extras else 1})
    meta['oh2Fleet']['avatarTeamRecorded'] = True
    if header.get('aimStyles'):
        meta['oh2Fleet']['aimStyles'] = header['aimStyles']
    origin = frames[0][0]
    if any(kind == b'I' for _, kind, _ in extras):
        meta['oh2Fleet']['incomingFeedback'] = True
    for _, kind, args in extras:
        if kind == b'I' and args[:2] == ['start', 'history']:
            meta['oh2Fleet']['battleHistory'] = True
            meta['oh2Fleet']['battleLogSettings'] = args[2]
            break
    loadout = [[max(0., stamp-origin)] + args for stamp, kind, args in extras if kind == b'U']
    if loadout:
        meta['oh2Fleet']['loadout'] = sorted(loadout, key=lambda row: row[0])
    outlines = [[max(0., stamp-origin), slots.get(args[0], 0)]
                for stamp, kind, args in extras if kind == b'O']
    reloads = [[max(0., stamp-origin)] + args for stamp, kind, args in extras if kind == b'L']
    sniper_aim = []
    last_clip = None
    for stamp, kind, args in extras:
        if kind != b'C':
            continue
        if args[0] != 'sniper':
            last_clip = None
        elif len(args) > 6 and args[6] is not None:
            clip = [round(value, 5) for value in args[6]]
            if clip != last_clip:
                sniper_aim.append([max(0., stamp-origin)] + clip)
                last_clip = clip
    if sniper_aim:
        meta['oh2Fleet']['sniperAim'] = sniper_aim
    if reloads:
        meta['oh2Fleet']['hudReload'] = reloads
    if outlines:
        meta['oh2Fleet']['outlineTargets'] = outlines
    if player_view:
        meta['oh2Fleet']['playerViewOnly'] = True
    primary = first[player['id']]
    packets = []
    added = False
    spawned = set()
    map_specific = {U32.unpack_from(b)[0] for k,t,b in reference
                    if k == 5 and t == 0 and b[4] in (7, 40)}
    seed_map = json.loads(metadata[0])['mapName']
    for kind, t, b in reference:
        if t > 0.001 or kind == 0xffffffff:
            break
        if kind == 0:
            b = avatar_base(b, player, header['arena'])
        elif kind == 1:
            b = bytearray(b)
            if len(b) < 71 or b[66] != seed_team or U32.unpack_from(b, 67)[0] != own_slot:
                raise ValueError('Unexpected Avatar cell layout in replay template')
            b[66] = player['team']
            struct.pack_into('<6f', b, 14, *primary[1:7])
            b = bytes(b)
        elif kind == 0xf:
            geometry = bw_string(('spaces/'+header['map']).encode()) + b[9+b[8]:]
            b = b[:4] + U32.pack(len(geometry)) + geometry
        elif kind == 5 and U32.unpack_from(b)[0] in original:
            if not added:
                for pose in frames[0][1]:
                    key = pose[0]
                    slot = slots[key]
                    packets.append((5, 0., native_spawn(generic, slot, roster[key], first[key], own_slot, avatar)))
                    spawned.add(key)
                added = True
            continue
        elif header['map'] != seed_map and kind == 5 and U32.unpack_from(b)[0] in map_specific:
            continue
        elif header['map'] != seed_map and kind == 0x32 and any(U32.pack(key) in b for key in map_specific):
            continue
        elif kind == 10:
            if U32.unpack_from(b)[0] != avatar:
                continue
        elif player_view and kind in (7,8) and U32.unpack_from(b)[0] in original:
            continue
        elif kind == 0x32 and b == roster_packet:
            b = native_roster(b, original, slots, roster)
        elif kind in (6, 0x31):
            # The online recording's loadout and equipment do not belong to this battle.
            continue
        packets.append((kind, 0., b))
    if not added:
        raise ValueError('Missing vehicle creation slot')
    for t, frame in frames:
        if player_view:
            present = {pose[0] for pose in frame}
            for key in sorted(spawned - present):
                if key != player['id']:
                    packets.append((4,t-origin,U32.pack(slots[key])))
                    spawned.remove(key)
        for pose in frame:
            key, x,y,z,yaw,pitch,roll,hp = pose
            if key not in spawned:
                packets.append((5, t-origin, native_spawn(generic, slots[key], roster[key], pose, own_slot, avatar)))
                spawned.add(key)
            data = struct.pack('<II9fB', slots[key], 0, x,y,z, 0.,0.,0., yaw,pitch,roll, 1)
            packets.append((10, t-origin, data))
    append_extra(packets,extras,slots,frames,origin,player['id'],avatar)
    packets.sort(key=lambda packet: packet[1])
    metadata = [json.dumps(meta, separators=(',',':')).encode()]
    encoded = encode(metadata, packets)
    assert decode(encoded) == (metadata, [(k, struct.unpack('<f',struct.pack('<f',t))[0], b) for k,t,b in packets])
    temporary = destination.with_suffix('.%d.wotreplay.part' % os.getpid())
    temporary.write_bytes(encoded)
    os.replace(temporary, destination)
    return slots


@contextmanager
def last_battle_lock(folder):
    import msvcrt
    with (folder / '.last.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def export(track, seed):
    header = load_track(track)[0]
    mode = header['mode']
    if mode == 0:
        return
    if mode not in (1, 2):
        raise ValueError('Unknown battle recording mode %r' % mode)
    if header.get('aimStyles'):
        # Older fleet replays have no per-battle reticle settings.
        profile = seed.parent / 'aim-styles.json'
        temporary = profile.with_suffix('.%d.tmp' % os.getpid())
        try:
            temporary.write_text(json.dumps(header['aimStyles']), encoding='ascii')
            os.replace(temporary, profile)
        except OSError:
            temporary.unlink(missing_ok=True)
    root = track.parent.parent
    if mode != 1:
        return make(track, seed, root / ('OffHangar-' + track.stem + '.wotreplay'))
    with last_battle_lock(track.parent):
        last_track = track.parent / 'OffHangar-last.ohtrack'
        if last_track.exists() and last_track.stat().st_mtime_ns > track.stat().st_mtime_ns:
            track.unlink()
            return
        make(track, seed, root / 'OffHangar-last.wotreplay')
        os.replace(track, last_track)


def prepare_seed(source, destination):
    metadata, packets = decode(source.read_bytes())
    initial = [p for p in packets if p[1] == 0 and p[0] != 0xffffffff]
    meta = json.loads(metadata[0])
    own, avatar, generic, spawn, roster_packet = template_layout(meta, initial)
    # Exercise the same variable-length fields as the exporter before accepting a template.
    check = dict(name='Offline', cd='', outfit='', team=1, maxHealth=100)
    originals = {int(key): row for key, row in meta['vehicles'].items()}
    native_roster(roster_packet, originals, {own: own}, {own: check})
    native_spawn(spawn, own, check, (own, 0, 0, 0, 0, 0, 0, 100), own, avatar)
    avatar_base(next(b for k,t,b in initial if k == 0), check, 1)
    result = encode(metadata[:1], initial)
    temporary = destination.with_suffix('.%d.tmp' % os.getpid())
    temporary.write_bytes(result)
    os.replace(temporary, destination)


if __name__ == '__main__' and sys.argv[1:2] == ['--rebuild']:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    track, destination = map(Path, sys.argv[2:])
    if destination.suffix.lower() != '.wotreplay' or destination.exists():
        raise SystemExit('Choose a new .wotreplay filename.')
    make(track, Path(sys.executable).parent / 'bootstrap.dat', destination)
elif __name__ == '__main__' and sys.argv[1:2] == ['--prepare-seed']:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    destination = Path(sys.argv[3])
    try:
        prepare_seed(Path(sys.argv[2]), destination)
    except Exception as exc:
        destination.with_suffix('.error.txt').write_text(
            'Unsupported replay or unreadable file. '+type(exc).__name__+' '+str(exc), encoding='utf-8')
        raise SystemExit(1)
elif __name__ == '__main__' and len(sys.argv) > 1:
    track = Path(sys.argv[1])
    seed = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sys.executable).parent / 'bootstrap.dat'
    try:
        export(track, seed)
    except Exception:
        track.with_suffix('.export-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
