"""Capture OffHangar battles with a selectable player-view or full-fleet scope."""
import atexit
import base64
import json
import logging
import os
import struct
import time
import uuid
from functools import wraps
from collections import OrderedDict

import BattleReplay
import BigWorld
import Math

LOG = logging.getLogger('oh2.fleet')
POSE = struct.Struct('<I6fi')
_track = None
_installed = False
FULL_FLEET_FLAG = os.path.join('mods', 'offhangar-fleet', 'full-fleet.enabled')


AIM_STYLE_KEYS = ('mixingType', 'mixing', 'gunTagType', 'gunTag', 'centralTagType',
                  'centralTag', 'netType', 'net')


def live_aim_styles():
    from helpers import dependency
    from skeletons.account_helpers.settings_core import ISettingsCore
    core = dependency.instance(ISettingsCore)
    styles = {}
    for mode in ('arcade', 'sniper'):
        values = core.getSetting(mode)
        styles[mode] = {key: values[key] for key in AIM_STYLE_KEYS}
    return styles


def _player_view():
    return not os.path.isfile(FULL_FLEET_FLAG)


def recording_mode(controller):
    from account_helpers.settings_core.settings_constants import GAME
    from helpers import dependency
    from skeletons.account_helpers.settings_core import ISettingsCore
    try:
        value = dependency.instance(ISettingsCore).getSetting(GAME.REPLAY_ENABLED)
        if value in (0, 1, 2):
            return int(value)
    except Exception:
        LOG.warning('Replay option unavailable, using active recorder mode', exc_info=True)
    return int(controller.isAutoRecordingEnabled)


def _toggle_view(event):
    import Keys
    if event.key != Keys.KEY_F9 or not event.isCtrlDown() or not event.isAltDown() or event.isRepeatedEvent():
        return
    from gui.mods.offhangar2 import login
    from gui import SystemMessages
    player = BigWorld.player()
    if not login.isOfflineSession() or not getattr(player,'isOffline',False) or getattr(player,'arena',None):
        return
    try:
        if _player_view():
            with open(FULL_FLEET_FLAG,'wb') as f:
                f.write('1\n')
        else:
            os.remove(FULL_FLEET_FLAG)
        msg = u'\ub2e4\uc74c \uc804\ud22c\ubd80\ud130 \ud50c\ub808\uc774\uc5b4 \uc2dc\uc810 \uc804\uc6a9 \ub179\ud654\uac00 \ucf1c\uc9d1\ub2c8\ub2e4.' if _player_view() else u'\ub2e4\uc74c \uc804\ud22c\ubd80\ud130 \uc804\uccb4 \uc804\ucc28 \ub179\ud654\uac00 \ucf1c\uc9d1\ub2c8\ub2e4.'
    except IOError:
        msg = u'\ub9ac\ud50c\ub808\uc774 \uc124\uc815\uc744 \uc800\uc7a5\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.'
    except OSError:
        msg = u'\ub9ac\ud50c\ub808\uc774 \uc124\uc815\uc744 \uc800\uc7a5\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.'
    SystemMessages.pushMessage(msg, SystemMessages.SM_TYPE.Information)


def _json_bytes(item):
    return json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii')


class Track(object):
    def __init__(self, map_name, arena_type, mode):
        folder = os.path.abspath(os.path.join('replays', 'offhangar'))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        stem = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.path = os.path.join(folder, stem + '.ohtrack')
        self.start = BigWorld.time()
        self.last_flush = self.start
        self.registered = set()
        self.player_view = _player_view()
        self.active = set()
        self.last_outline = None
        self.visible_shots = set()
        self.known_wrecks = set()
        self.pending_destruction = OrderedDict()
        self.recorded_destruction = set()
        self.impact_target = None
        self.cb = None
        self.frames = 0
        self.errors = set()
        self.angles = {}
        self.sticker_states = {}
        self.period = None
        self.outcome_recorded = False
        self.file = open(self.path + '.part', 'wb')
        self.file.write(b'OHF1')
        header = dict(map=map_name, arena=arena_type, mode=int(mode), build='2.3.1.2',
                      playerViewOnly=self.player_view, destructiblesRecorded=True)
        try:
            header['aimStyles'] = live_aim_styles()
        except Exception:
            LOG.warning('Could not read the current reticle settings', exc_info=True)
        self.write(b'H', _json_bytes(header))
        LOG.info('Recording local vehicles to %s', self.path)

    def write(self, tag, payload):
        self.file.write(struct.pack('<cI', tag, len(payload)) + payload)

    def visible(self, player, key):
        from gui.mods.offhangar2 import config, spotting
        own = getattr(player,'playerVehicleID',0)
        if own and key == own:
            return True
        arena = getattr(player,'arena',None)
        if not arena:
            return False
        team = arena.vehicles.get(own,{}).get('team')
        other = arena.vehicles.get(key,{}).get('team')
        v, viewer = BigWorld.entity(key), BigWorld.entity(own)
        if not team or not other or v is None or viewer is None or not v.isStarted:
            return False
        if team != other:
            if not spotting._state.get('running') or key in spotting._hidden:
                return False
            if not spotting.isSpottedFor(team,key) and key not in self.known_wrecks:
                return False
        radius = float(getattr(config,'SPOTTING_MAX_RENDER',0) or 0)
        return not radius or (v.position.x-viewer.position.x)**2+(v.position.z-viewer.position.z)**2 <= radius**2

    def event_args(self,name,entity,args):
        if not self.player_view:
            return args
        p = BigWorld.player()
        def shown(key):
            return key in self.active and self.visible(p,key)
        if name == 'showTracer':
            who, shot = args[:2]
            self.visible_shots.discard(shot)
            if shown(who):
                self.visible_shots.add(shot)
                return args
            return
        if name in ('stopTracer','explodeProjectile'):
            shot = args[0]
            if shot not in self.visible_shots:
                return
            self.visible_shots.discard(shot)
            if name == 'stopTracer':
                return args if shown(self.impact_target) else None
            if self.impact_target == -1:
                return
            # A client-side hit can be resolved behind scenery. Only keep an
            # impact that the recorded camera could actually see.
            pos = Math.Vector3(args[6])
            matrix = Math.Matrix(BigWorld.camera().invViewMatrix)
            origin = matrix.translation
            local = Math.Matrix(BigWorld.camera().matrix).applyPoint(pos)
            from gui.mods.offhangar2 import config
            import GUI, math
            w,h = GUI.screenResolution()
            limit = local.z * math.tan(BigWorld.projection().fov*.5)
            if local.z <= 0 or abs(local.y)>limit or abs(local.x)>limit*float(w)/max(1,h):
                return
            ray = pos-origin
            radius = float(getattr(config,'SPOTTING_MAX_RENDER',0) or 0)
            if radius and ray.length > radius:
                return
            if ray.length > 1.:
                ray.normalise()
                if BigWorld.wg_collideSegment(p.spaceID,origin,pos-ray*.5,128) is not None:
                    return
            return list(args[:-1])+[[]]
        if not shown(entity.id):
            return
        clean = list(args)
        if name == 'onHealthChanged':
            if clean[0] <= 0:
                self.known_wrecks.add(entity.id)
            if not shown(clean[2]):
                clean[2] = 0
        elif name in ('showDamageFromShot','showDamageFromExplosion') and not shown(clean[0]):
            if name == 'showDamageFromExplosion':
                return
            clean[0] = 0
        return clean

    def record_outline(self, player, now):
        try:
            target = getattr(player, '_PlayerAvatar__target', None)
            key = getattr(target, 'id', 0)
            highlighter = getattr(getattr(target, 'appearance', None), 'highlighter', None)
            if key not in self.active or key == player.playerVehicleID or not highlighter or not highlighter.isOn:
                key = 0
            if key != self.last_outline:
                self.write(b'O', _json_bytes([now - self.start, key]))
                self.last_outline = key
        except Exception:
            if 'outline' not in self.errors:
                self.errors.add('outline')
                LOG.exception('Recording player target outline')

    def remember_destruction(self, chunk, kind, data):
        if kind not in (0, 1, 2, 3) or len(data) != (5 if kind == 0 else 3):
            return
        index = data[0] << 8 | data[1]
        key = (chunk, index, data[2] >> 1 if kind == 3 else None)
        if key not in self.recorded_destruction:
            self.pending_destruction[key] = (BigWorld.time() - self.start, chunk, kind, tuple(data))

    def flush_destruction(self):
        if not self.pending_destruction:
            return
        from gui.mods.offhangar2 import destructibles
        destroyed = destructibles._destroyed
        objects = set((chunk, idx) for chunk, idx, mat in destroyed)
        for key, event in self.pending_destruction.iteritems():
            confirmed = key[:2] in objects if key[2] is None else key in destroyed
            if confirmed:
                self.write(b'D', _json_bytes(event))
                self.recorded_destruction.add(key)
        self.pending_destruction.clear()

    def record_stickers(self, vehicle, now, entering=False):
        marks = tuple((p['networkID'], p['segment'], p['params']) for p in vehicle.damageStickers)
        if entering or marks != self.sticker_states.get(vehicle.id, ()):
            # Match the frame timestamp so a newly visible vehicle exists before the update.
            stamp = struct.unpack('<f', struct.pack('<f', now - self.start))[0]
            self.write(b'K', _json_bytes([stamp, vehicle.id, marks]))
        self.sticker_states[vehicle.id] = marks

    def sample(self):
        self.flush_destruction()
        from gui.mods.offhangar2 import battle, bots, turret
        from gun_rotation_shared import encodeGunAngles
        player = BigWorld.player()
        arena = getattr(player, 'arena', None)
        if arena is None:
            return
        own = getattr(player, 'playerVehicleID', 0)
        if self.player_view and not self.frames and (not own or own not in arena.vehicles or BigWorld.entity(own) is None):
            return
        ids = set(arena.vehicles) | set(bots.spawned())
        if own:
            ids.add(own)
        rows = []
        angles = []
        active = set()
        now = BigWorld.time()
        for key in sorted(ids):
            v = BigWorld.entity(key)
            if v is None:
                continue
            try:
                if self.player_view and not self.visible(player,key):
                    continue
                if key not in self.registered:
                    info = arena.vehicles.get(key) or {}
                    desc = getattr(v, 'typeDescriptor', None) or info.get('vehicleType')
                    if desc is None or 'team' not in info:
                        continue
                    row = dict(id=key, team=int(info['team']), own=bool(own == key),
                               name=info.get('name', ''), tank=desc.name,
                               cd=base64.b64encode(desc.makeCompactDescr()),
                               outfit=base64.b64encode(info.get('outfitCD') or ''),
                               typeCD=desc.type.compactDescr, maxHealth=desc.maxHealth)
                    self.write(b'V', _json_bytes(row))
                    self.registered.add(key)
                p = v.position
                rows.append(POSE.pack(key, p.x, p.y, p.z, v.yaw, v.pitch, v.roll, v.health))
                active.add(key)
                if key not in self.active:
                    self.record_stickers(v, now, entering=True)
                aiming = player.gunRotator if key == own else turret._targets.get(key)
                packed = int(v.gunAnglesPacked)
                if aiming is not None:
                    yaw = aiming.turretYaw if key == own else aiming.yaw
                    pitch = aiming.gunPitch if key == own else aiming.pitch
                    packed = encodeGunAngles(yaw, pitch, v.typeDescriptor.gun.pitchLimits['absolute'])
                if self.angles.get(key) != packed or (self.player_view and key not in self.active):
                    self.angles[key] = packed
                    angles.append(struct.pack('<IH', key, packed & 65535))
            except Exception:
                if key not in self.errors:
                    self.errors.add(key)
                    LOG.exception('Sampling vehicle %s', key)
        self.active = active
        if rows or (self.player_view and self.frames):
            self.write(b'F', struct.pack('<f', now - self.start) + b''.join(rows))
            self.frames += 1
            self.record_outline(player, now)
            period = arena.period
            length, end = arena.periodLength, arena.periodEndTime
            # OffHangar uses PREBATTLE twice. Its first deadline only guards vehicle loading.
            if period == 2 and getattr(battle, '_countdownState', {}).get('armed') is False:
                period, length, end = 1, 0., 0.
            timing = (period, length, end)
            if not self.outcome_recorded and period in (1, 2, 3, 4) and timing != self.period:
                self.period = timing
                self.write(b'P', _json_bytes([now-self.start, period, length,
                                              max(0., end - BigWorld.serverTime())]))
            if angles:
                self.write(b'A', struct.pack('<f', now-self.start) + b''.join(angles))
            try:
                cam = BigWorld.camera()
                m = Math.Matrix(cam.invViewMatrix)
                axes = [list(m.applyVector(axis)) for axis in ((1,0,0),(0,1,0),(0,0,1))]
                origin = list(m.applyToOrigin())
                projection = BigWorld.projection()
                mode = str(player.inputHandler.ctrlModeName)
                gun_data = None
                rot = getattr(player, 'gunRotator', None)
                if rot is not None:
                    try:
                        aim = player.inputHandler.getDesiredShotPoint(False)
                        pos, direction, size = rot.markerInfo
                        if aim is not None:
                            gun_data = [list(aim),list(pos),list(direction),float(size),
                                        float(rot.turretYaw),float(rot.gunPitch)]
                    except Exception:
                        pass
                clip = None
                if mode == 'sniper':
                    sniper = getattr(player.inputHandler.ctrl, 'camera', None)
                    offset = getattr(sniper, '_SniperCamera__aimOffset', None)
                    if offset is not None:
                        clip = [float(offset.x), float(offset.y)]
                self.write(b'C', _json_bytes([now-self.start, mode, origin, axes,
                                              projection.fov, projection.nearPlane, gun_data, clip]))
            except Exception:
                if 'camera' not in self.errors:
                    self.errors.add('camera')
                    LOG.exception('Sampling player camera')
        if now - self.last_flush >= 1:
            self.file.flush()
            self.last_flush = now

    def tick(self):
        self.cb = None
        if self is not _track:
            return
        try:
            self.sample()
            self.cb = BigWorld.callback(0.1, self.tick)
        except Exception:
            LOG.exception('Local vehicle capture stopped')
            finish()

    def close(self):
        if self.cb is not None:
            BigWorld.cancelCallback(self.cb)
        try:
            self.flush_destruction()
            self.write(b'E', _json_bytes(dict(vehicles=len(self.registered), frames=self.frames)))
        finally:
            self.file.close()
        os.rename(self.path + '.part', self.path)
        LOG.warning('Saved %s vehicles, %s frames: %s', len(self.registered), self.frames, self.path)
        if self.frames:
            export = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(self.path))),
                                  'mods', 'offhangar-fleet', 'export.exe')
            if os.path.isfile(export):
                os.spawnl(os.P_NOWAIT, export, '"%s"' % export, '"%s"' % self.path)


def finish():
    global _track
    track, _track = _track, None
    if track is not None:
        try:
            track.close()
        except Exception:
            LOG.exception('Could not finalize %s', track.path)


def _plain(x):
    if isinstance(x, (int, long, float, bool)):
        return x
    if isinstance(x, dict) or hasattr(x, 'keys'):
        return {k: _plain(x[k]) for k in x.keys()}
    return [_plain(v) for v in x]


def _combat_hook(cls, name):
    original = getattr(cls, name)
    @wraps(original)
    def call(entity, *args, **kwargs):
        if _track is not None:
            try:
                captured = _track.event_args(name,entity,args)
                if captured is not None:
                    _track.write(b'X', _json_bytes([BigWorld.time()-_track.start, name, entity.id,
                                                    _plain(captured)]))
            except Exception:
                if name not in _track.errors:
                    _track.errors.add(name)
                    LOG.exception('Recording %s', name)
        result = original(entity, *args, **kwargs)
        if name == 'onHealthChanged' and args and args[0] <= 0 and _track is None:
            try:
                replay = BattleReplay.g_replayCtrl
                if replay and replay.isPlaying and replay.arenaInfo.get('oh2Fleet', {}).get('version',0) >= 2:
                    avatar = BigWorld.player()
                    arena = avatar.arena
                    row = arena.vehicles.get(entity.id)
                    if row and row.get('isAlive'):
                        row['isAlive'] = False
                        row['deathInfo'] = dict(victimID=entity.id, killerID=args[2], equipmentID=0,
                                                reasonID=args[3], numVehiclesAffected=0)
                        arena.updateVehicleIsAlive(entity.id, None, entity.id == avatar.playerVehicleID)
                        killer = arena.vehicles.get(args[2])
                        if killer and killer['team'] != row['team']:
                            stats = arena.statistics.get(args[2], {})
                            arena.updateVehiclesFrags(args[2], int(stats.get('frags',0)) + 1)
            except Exception:
                LOG.exception('Applying recorded arena death')
        return result
    setattr(cls, name, call)


def init():
    global _installed
    if _installed:
        return
    from gui.mods.offhangar2 import battle, login
    from gui.mods.offhangar2 import shooting
    from gui import InputHandler
    import Avatar, Vehicle
    sticker_setter = Vehicle.Vehicle.set_damageStickers

    @wraps(sticker_setter)
    def stickers_changed(vehicle, *args, **kwargs):
        result = sticker_setter(vehicle, *args, **kwargs)
        track = _track
        if track is not None and vehicle.id in track.active:
            try:
                if not track.player_view or track.visible(BigWorld.player(), vehicle.id):
                    track.record_stickers(vehicle, BigWorld.time())
            except Exception:
                if 'damageStickers' not in track.errors:
                    track.errors.add('damageStickers')
                    LOG.exception('Recording vehicle damage stickers')
        return result

    Vehicle.Vehicle.set_damageStickers = stickers_changed
    from AreaDestructibles import DestructiblesManager
    native_destroy = DestructiblesManager.orderDestructibleDestroy

    @wraps(native_destroy)
    def destroy(self, chunkID, dmgType, destrData, isNeedAnimation, syncWithProjectile=False, forceRemove=False):
        if _track is not None:
            _track.remember_destruction(chunkID, dmgType, destrData)
        return native_destroy(self, chunkID, dmgType, destrData, isNeedAnimation, syncWithProjectile, forceRemove)

    DestructiblesManager.orderDestructibleDestroy = destroy
    InputHandler.g_instance.onKeyDown += _toggle_view
    def watch_focus(method):
        @wraps(method)
        def focus(player, *args, **kwargs):
            result = method(player, *args, **kwargs)
            if _track is not None and _track.frames:
                _track.record_outline(player, BigWorld.time())
            return result
        return focus
    for name in ('targetFocus', 'targetBlur'):
        setattr(Avatar.PlayerAvatar, name, watch_focus(getattr(Avatar.PlayerAvatar, name)))
    native_record = BattleReplay.BattleReplay.record
    @wraps(native_record)
    def native_start(controller, *args, **kwargs):
        if (not controller.isPlaying and login.isOfflineSession()
                and (_player_view() or not recording_mode(controller))):
            return False
        return native_record(controller,*args,**kwargs)
    BattleReplay.BattleReplay.record = native_start
    stop_at, explode = shooting._stopTracerAt, shooting._explode

    def impact_context(fn, key, args, kwargs):
        track = _track
        if track is None or not track.player_view:
            return fn(*args,**kwargs)
        previous = track.impact_target
        track.impact_target = key
        try:
            return fn(*args,**kwargs)
        finally:
            track.impact_target = previous

    @wraps(stop_at)
    def stop_at_vehicle(avatar, shot, target, *args, **kwargs):
        return impact_context(stop_at,target,(avatar,shot,target)+args,kwargs)

    @wraps(explode)
    def on_explosion(*args, **kwargs):
        on_vehicle = kwargs.get('onVehicle', args[5] if len(args)>5 else False)
        return impact_context(explode,-1 if on_vehicle else None,args,kwargs)

    shooting._stopTracerAt = stop_at_vehicle
    shooting._explode = on_explosion
    for name in ('showTracer', 'stopTracer', 'explodeProjectile'):
        _combat_hook(Avatar.PlayerAvatar, name)
    for name in ('showShooting', 'onHealthChanged', 'showDamageFromShot','showDamageFromExplosion'):
        _combat_hook(Vehicle.Vehicle, name)
    create = battle._createArenaAsBattleWorld
    leave = battle.leave
    end_battle = battle._endBattle

    @wraps(end_battle)
    def outcome(reason, winnerTeam=0, finishReason=0):
        result = end_battle(reason, winnerTeam, finishReason)
        track = _track
        if track is not None and not track.outcome_recorded:
            try:
                from gui.mods.offhangar2 import config
                delay = max(0., float(config.BATTLE_AUTO_RETURN_DELAY))
                track.write(b'P', _json_bytes([BigWorld.time()-track.start, 4, delay, delay,
                                               [int(winnerTeam), int(finishReason)]]))
                track.outcome_recorded = True
            except Exception:
                LOG.exception('Recording battle outcome')
        return result

    @wraps(create)
    def begin(map_name, arena_type):
        global _track
        replay = BattleReplay.g_replayCtrl
        mode = recording_mode(replay) if login.isOfflineSession() and not replay.isPlaying else 0
        result = create(map_name, arena_type)
        try:
            if result and mode:
                finish()
                _track = Track(map_name, arena_type, mode)
                _track.tick()
        except Exception:
            LOG.exception('Could not start local vehicle capture')
        return result

    @wraps(leave)
    def end(*args, **kwargs):
        finish()
        return leave(*args, **kwargs)

    battle._createArenaAsBattleWorld = begin
    battle._endBattle = outcome
    battle.leave = end
    atexit.register(finish)
    _installed = True
    LOG.warning('Local vehicle recorder ready')
