"""Animate local fleet replays from their recorded poses and gun angles."""
import logging
import math
from collections import deque
from bisect import bisect_right
from functools import wraps

import BattleReplay
import BigWorld
import Math
from gun_rotation_shared import calcGunPitchCorrection, decodeGunAngles
from vehicle_systems.tankStructure import TankPartNames, TankNodeNames

LOG = logging.getLogger('oh2.fleet.animation')
states = {}
arena = None
version = 0
pending = None
aim_pending = None
errors = set()


def wrap(value):
    return math.atan2(math.sin(value), math.cos(value))


class RecordedAngle(object):
    # Local poses arrive about every 100 ms. Keep one sample ahead of the model
    # so that intermediate frames can use both endpoints without extrapolating.
    delay = .13

    def __init__(self):
        self.samples = deque()
        self.last_time = None

    def at(self, now, angle):
        if self.last_time is None or now < self.last_time or now-self.last_time > .4:
            self.samples.clear()
        self.last_time = now
        sample = (now, angle)
        if not self.samples:
            self.samples.append(sample)
        elif self.samples[-1][1] != angle:
            stamp, previous = self.samples[-1]
            if now-stamp > self.delay:
                self.samples.append((now-self.delay,previous))
            self.samples.append(sample)
        when = now-self.delay
        while len(self.samples)>1 and self.samples[1][0] <= when:
            self.samples.popleft()
        start, a = self.samples[0]
        if len(self.samples)==1 or when <= start:
            return a
        end, b = self.samples[1]
        part = (when-start)/(end-start)
        return a + wrap(b-a)*part


class AnimatedVehicle(object):
    def __init__(self, vehicle, model):
        self.model = model
        self.angles = None
        self.last = None
        self.width = 1.5
        bbox = vehicle.typeDescriptor.chassis.hitTester.bbox
        if bbox:
            self.width = abs(bbox[1].x - bbox[0].x) * .5

    def aim(self, v, now):
        if self.angles is None:
            self.recorded_yaw = RecordedAngle()
            self.recorded_pitch = RecordedAngle()
            self.native_aim = False
            self.turretMatrix = Math.Matrix()
            self.gunMatrix = Math.Matrix()
            self.turret = self.model.node(TankPartNames.TURRET)
            self.gun = self.model.node(TankNodeNames.GUN_INCLINATION) or self.model.node(TankPartNames.GUN)
        yaw, pitch = decodeGunAngles(v.gunAnglesPacked, v.typeDescriptor.gun.pitchLimits['absolute'])
        replay = BattleReplay.g_replayCtrl
        native_yaw, native_pitch = replay.getTurretYaw(), replay.getGunPitch()
        if abs(native_yaw) <= math.pi+.001 and abs(native_pitch) <= math.pi:
            # Earlier fleet files have only packed vehicle angles. The native
            # accessors can also return zero before the first player samples.
            if native_yaw or native_pitch:
                self.native_aim = True
            if self.native_aim:
                yaw, pitch = native_yaw, native_pitch
        else:
            self.native_aim = False
        if not self.native_aim:
            yaw = self.recorded_yaw.at(now,yaw)
        pitch = self.recorded_pitch.at(now,pitch)
        self.angles = (yaw,pitch)
        desc = v.typeDescriptor
        correction = calcGunPitchCorrection(yaw,desc.hull.turretPitches[0],desc.turret.gunJointPitch)
        self.turretMatrix.setRotateYPR((yaw,0.,0.))
        self.gunMatrix.setRotateYPR((0.,pitch-correction,0.))
        v.appearance.setupGunMatrixTargets(self)
        if self.turret is not None:
            self.turret.localMatrix = v.appearance.turretMatrix
        if self.gun is not None:
            self.gun.localMatrix = v.appearance.gunMatrix

    def update(self, v, now, paused):
        from gui.mods.offhangar2 import turret
        pos = v.position
        current = (now, pos.x, pos.z, v.yaw)
        dt = now - self.last[0] if self.last else 0.
        speed = rate = 0.
        if not paused and 0 < dt < .6 and v.health > 0:
            _, x, z, yaw = self.last
            dx, dz = pos.x-x, pos.z-z
            turn = wrap(v.yaw-yaw)
            if dx*dx+dz*dz <= (3.+45.*dt)**2 and abs(turn) <= .2+4.*dt:
                heading = yaw+turn*.5
                speed = (dx*math.sin(heading)+dz*math.cos(heading))/dt
                rate = turn/dt
        self.last = current
        turret.trackScroll(v, speed, rate, self.width)


def aim_frame():
    global aim_pending
    aim_pending = None
    replay = BattleReplay.g_replayCtrl
    if version < 2 or not replay or not replay.isPlaying:
        return
    v = BigWorld.entity(replay.playerVehicleID)
    state = states.get(replay.playerVehicleID)
    if state is None or v is None or not v.isStarted or v.health <= 0:
        return
    app = v.appearance
    if not app or app.isTurretDetached or app.compoundModel is not state.model:
        return
    try:
        state.aim(v,replay.currentTime)
        aim_pending = BigWorld.callback(0.,aim_frame)
    except Exception:
        if 'aim' not in errors:
            errors.add('aim')
            LOG.exception('Updating replay gun')


def recorded_team(replay):
    if not replay or not replay.isPlaying:
        return
    info = replay.arenaInfo or {}
    fleet = info.get('oh2Fleet')
    if not fleet:
        return
    own = replay.playerVehicleID
    roster = info.get('vehicles', {})
    row = roster.get(str(own)) or roster.get(own)
    if row is None and info.get('playerName'):
        row = next((v for v in roster.itervalues() if v.get('name') == info['playerName']), None)
    if row:
        return row.get('team')


class ReplayOutline(object):
    def __init__(self, events):
        self.events = events
        self.times = [stamp for stamp, key in events or ()]
        self.previous = None

    def update(self, player, replay):
        from gui.mods.offhangar2 import turret
        target = None
        handler = getattr(player, 'inputHandler', None)
        if handler and handler.isGuiVisible:
            if replay.isControllingCamera and self.events is not None:
                i = bisect_right(self.times, replay.currentTime) - 1
                if i >= 0 and self.events[i][1]:
                    target = BigWorld.entity(self.events[i][1])
            elif handler.ctrlModeName in ('arcade', 'sniper', 'dualgun'):
                target = turret._vehicleUnderCrosshair(player)
        app = getattr(target, 'appearance', None)
        if (not app or not getattr(target, 'isStarted', False) or target.health <= 0
                or target.id == player.playerVehicleID or not app.isCompositionReady
                or not getattr(app.compoundModel, 'visible', True)):
            target = None
        if replay.isControllingCamera and self.events is not None:
            focused = getattr(player, '_PlayerAvatar__target', None)
            if (focused is not None and focused is not target and focused.id != player.playerVehicleID
                    and getattr(getattr(focused, 'appearance', None), 'highlighter', None)
                    and focused.appearance.highlighter.isOn):
                focused.removeEdge()
        if self.previous is not None and self.previous is not target:
            old = self.previous
            self.previous = None
            if not old.isDestroyed and old.isStarted:
                old.removeEdge()
        if target is not None and app.highlighter and not app.highlighter.isOn:
            target.drawEdge()
            self.previous = target


outline = None
team = None


def restore_initial_team(method):
    @wraps(method)
    def start(player, *args, **kwargs):
        try:
            expected = recorded_team(BattleReplay.g_replayCtrl)
            if expected and getattr(player, 'team', None) != expected:
                player.team = expected
        except Exception:
            LOG.debug('Avatar team not available before world entry', exc_info=True)
        return method(player, *args, **kwargs)
    return start


def tick():
    global arena, version, pending, outline, team
    delay = 1.
    try:
        replay = BattleReplay.g_replayCtrl
        active = getattr(BigWorld.player(),'arena',None) if replay and replay.isPlaying else None
        if active is not arena:
            states.clear()
            errors.clear()
            version = 0
            outline = team = None
            if active is not None:
                fleet = replay.arenaInfo.get('oh2Fleet') or {}
                version = fleet.get('version', 0)
                if version:
                    outline = ReplayOutline(fleet.get('outlineTargets'))
                    team = recorded_team(replay)
            arena = active
        if not version:
            return
        delay = .04
        now = replay.currentTime
        player = BigWorld.player()
        try:
            if team and replay.isClientReady:
                if player.team != team:
                    player.team = team
                dp = player.guiSessionProvider.getArenaDP()
                if dp and not dp.isAllyTeam(team):
                    dp.switchCurrentTeam(team)
            if replay.isClientReady:
                outline.update(player, replay)
        except Exception:
            if 'presentation' not in errors:
                errors.add('presentation')
                LOG.exception('Updating replay team and target outline')
        seen = set()
        for v in BigWorld.entities.valuesOfType('Vehicle'):
            model = getattr(v.appearance,'compoundModel',None)
            if not model or not v.isStarted or not v.appearance.isCompositionReady:
                continue
            seen.add(v.id)
            try:
                state = states.get(v.id)
                if state is None or state.model is not model:
                    state = states[v.id] = AnimatedVehicle(v, model)
                state.update(v, now, replay.isPaused)
            except Exception:
                if v.id not in errors:
                    errors.add(v.id)
                    LOG.exception('Animating replay vehicle %s',v.id)
        for key in set(states)-seen:
            del states[key]
        if aim_pending is None and version >= 2:
            aim_frame()
    except Exception:
        if 'tick' not in errors:
            errors.add('tick')
            LOG.exception('Updating replay models')
    finally:
        pending = BigWorld.callback(delay,tick)


def init():
    global pending
    import Avatar
    from gui.Scaleform.daapi.view.battle.shared.stats_exchange.stats_ctrl import BattleStatisticsDataController
    refresh = BattleStatisticsDataController.invalidateVehiclesInfo
    @wraps(refresh)
    def refresh_roster(view, arenaDP):
        expected = recorded_team(BattleReplay.g_replayCtrl)
        if expected in (1, 2):
            if not arenaDP.isAllyTeam(expected):
                arenaDP.switchCurrentTeam(expected)
            view._BattleStatisticsDataController__avatarTeam = expected
            cached = view._BattleStatisticsDataController__reusable
            if any(enemy != (side != expected) for side, (enemy, _) in cached.iteritems()):
                view._BattleStatisticsDataController__clearTeamOverrides()
        return refresh(view, arenaDP)
    BattleStatisticsDataController.invalidateVehiclesInfo = refresh_roster
    Avatar.PlayerAvatar.onEnterWorld = restore_initial_team(Avatar.PlayerAvatar.onEnterWorld)
    Avatar.PlayerAvatar.onBecomePlayer = restore_initial_team(Avatar.PlayerAvatar.onBecomePlayer)
    pending = BigWorld.callback(1.,tick)


def fini():
    if pending is not None:
        BigWorld.cancelCallback(pending)
    if aim_pending is not None:
        BigWorld.cancelCallback(aim_pending)
    states.clear()
