"""Record the offline player's HUD state and display it on fleet replays."""
from bisect import bisect_right
from functools import wraps
import logging
import json
import os
import Math
from AvatarInputHandler import aih_global_binding
from aih_constants import GUN_MARKER_FLAG
from constants import DEFAULT_GUN_INSTALLATION_INDEX
from VehicleGunRotator import GunMarkerInfo

import BattleReplay
import BigWorld
from gui.mods import mod_oh2_fleet as fleet
from gui.Scaleform.daapi.view.battle.shared.crosshair import plugins
from gui.battle_control.controllers.consumables.ammo_ctrl import ReloadingTimeSnapshot

LOG = logging.getLogger('oh2.fleet.hud')
CALLBACK = 'oh2Fleet.spotted'
session = None


def record(tag, values):
    track = fleet._track
    if track is not None:
        try:
            track.write(tag, fleet._json_bytes([BigWorld.time() - track.start] + values))
        except Exception:
            if tag not in track.errors:
                track.errors.add(tag)
                LOG.exception('Recording player HUD event %r', tag)


def observed(detection, visible=True):
    if visible:
        record(b'S', [int(detection)])


def play_warning(detection):
    replay = BattleReplay.g_replayCtrl
    if replay.isPlaying and replay.arenaInfo.get('oh2Fleet'):
        from constants import DIRECT_DETECTION_TYPE
        from PlayerEvents import g_playerEvents
        kind = DIRECT_DETECTION_TYPE.RAYTRACE if detection is None else detection
        g_playerEvents.onObservedByEnemy(kind, True)


class ReplayHUD(object):
    marker_flags = aih_global_binding.bindRO(aih_global_binding.BINDING_ID.GUN_MARKERS_FLAGS)

    def update_marker(self, replay):
        if not replay.isClientReady or not replay.isControllingCamera:
            return
        player = BigWorld.player()
        handler = getattr(player, 'inputHandler', None)
        vehicle = BigWorld.entity(replay.playerVehicleID)
        if (not handler or handler.ctrlModeName not in ('arcade', 'sniper', 'dualgun')
                or vehicle is None or not vehicle.isStarted or vehicle.health <= 0):
            return
        size, dual, point, direction = replay.getGunMarkerParams(Math.Vector3(), Math.Vector3())
        if point.lengthSquared == 0. and (not direction.lengthSquared or direction.y == 1.):
            point = handler.getDesiredShotPoint(False)
            if point is None:
                return
            direction = point - vehicle.position
            if not direction.lengthSquared:
                return
            direction.normalise()
        if not direction.lengthSquared:
            return
        index = getattr(getattr(player, 'gunRotator', None), '_VehicleGunRotator__gunIndex', 0)
        offset = getattr(getattr(vehicle.typeDescriptor.gun, 'twinGun', None), 'gunMarkerOffset', 0.)
        info = GunMarkerInfo(DEFAULT_GUN_INSTALLATION_INDEX, index, point, direction, size, dual, 2.*offset, None)
        handler.updateClientGunMarker(info, (), .08)
        if self.marker_flags & GUN_MARKER_FLAG.SERVER_MODE_ENABLED:
            handler.updateServerGunMarker(info, (), .08)
        if vehicle.typeDescriptor.hasDualAccuracy:
            handler.updateDualAccGunMarker(info, (), .08)

    def __init__(self, plugin, data, warnings):
        events = data.get('hudReload', ())
        self.warnings = warnings
        self.aim = data.get('sniperAim', ())
        self.aim_times = [row[0] for row in self.aim]
        self.animator = plugin._AmmoPlugin__reloadAnimator
        self.events = events
        self.times = [row[0] for row in events or ()]
        self.clip = data.get('clipReload', ())
        self.clip_times = [row[0] for row in self.clip]
        self.last_clip = None
        self.previous = None
        self.last_time = None
        self.cb = None
        self.marker_error = False

    def tick(self):
        self.cb = None
        replay = BattleReplay.g_replayCtrl
        if not replay.isPlaying or self is not session:
            return
        try:
            self.update_marker(replay)
        except Exception:
            if not self.marker_error:
                self.marker_error = True
                LOG.exception('Updating recorded gun marker')
        try:
            now = replay.currentTime
            seek = self.last_time is not None and (now < self.last_time or now-self.last_time > 1.)
            if self.warnings and self.last_time is not None and not seek and not replay.isTimeWarpInProgress:
                for i in xrange(bisect_right(self.warnings, self.last_time), bisect_right(self.warnings, now)):
                    play_warning(None)
            self.last_time = now
            i = bisect_right(self.times, now) - 1
            row = self.events[i] if i >= 0 else None
            if row is None and self.previous is not None:
                self.animator.setReloading(ReloadingTimeSnapshot(), False)
            if row is not None and (row is not self.previous or seek):
                stamp, remaining, base, blocked, available = row
                if remaining > 0 and not blocked:
                    remaining = max(0., remaining - (now - stamp))
                state = ReloadingTimeSnapshot(actualTime=remaining, baseTime=base, isReloadingBlocked=blocked)
                self.animator.setReloading(state, available)
            self.previous = row
            i = bisect_right(self.clip_times, now) - 1
            clip = self.clip[i] if i >= 0 else None
            if clip is not None and (clip is not self.last_clip or seek):
                stamp, left, base, critical, timer, red = clip
                self.animator.setClipAutoLoading(max(0., left - (now-stamp)), base, critical, timer, red)
            elif self.last_clip is not None and clip is None:
                self.animator.setClipAutoLoading(0., 0.)
            self.last_clip = clip
        except Exception:
            LOG.exception('Updating replay reload indicator')
            return
        self.cb = BigWorld.callback(.08, self.tick)

    def finish(self):
        if self.cb is not None:
            BigWorld.cancelCallback(self.cb)
            self.cb = None


def install_aim_settings():
    original = BattleReplay.BattleReplay.getSetting
    cache = {}

    @wraps(original)
    def settings(replay, key, default=None):
        base = original(replay, key, default)
        if key in ('ally', 'enemy') and replay.isPlaying and getattr(replay, '_BattleReplay__fileName', None):
            try:
                is_fleet = (replay.arenaInfo or {}).get('oh2Fleet')
            except (TypeError, ValueError):
                is_fleet = False
            if is_fleet:
                marker = dict(base or {})
                marker.setdefault('markerBaseLevel', True)
                return marker
        if key not in ('arcade', 'sniper') or not replay.isPlaying:
            return base
        filename = getattr(replay, '_BattleReplay__fileName', None)
        if not filename:
            return base
        if filename != cache.get('filename'):
            try:
                info = replay.arenaInfo
            except (TypeError, ValueError):
                return base
            if not info:
                return base
            data = info.get('oh2Fleet')
            styles = data.get('aimStyles') if data else None
            if data and not styles:
                path = os.path.join('mods', 'offhangar-fleet', 'aim-styles.json')
                try:
                    with open(path, 'rb') as stream:
                        styles = json.load(stream)
                except (IOError, ValueError):
                    styles = None
            cache.update(filename=filename, styles=styles)
        styles = cache.get('styles')
        profile = styles.get(key) if isinstance(styles, dict) else None
        if not isinstance(profile, dict):
            return base
        result = dict(base or {})
        for field in fleet.AIM_STYLE_KEYS:
            if field in profile:
                result[field] = profile[field]
        return result

    BattleReplay.BattleReplay.getSetting = settings


def install_countdown():
    from gui.Scaleform.daapi.view.battle.shared.prebattle_timers.timer_base import PreBattleTimerBase
    from gui.battle_control.battle_constants import COUNTDOWN_STATE
    from gui.shared.utils.functions import getArenaSubTypeName
    original = PreBattleTimerBase.setCountdown
    @wraps(original)
    def countdown(view, state, timeLeft):
        replay = BattleReplay.g_replayCtrl
        fleet_start = (state == COUNTDOWN_STATE.START and replay.isPlaying
                       and (replay.arenaInfo or {}).get('oh2Fleet'))
        if fleet_start and (not view._battleTypeStr or view._battleTypeStr.strip().lower() == 'none'):
            arena_id = view.sessionProvider.arenaVisitor.type.getID()
            view._battleTypeStr = '#arenas:type/%s/name' % getArenaSubTypeName(arena_id)
        result = original(view, state, timeLeft)
        if fleet_start and timeLeft is not None and timeLeft >= 0:
            label = view._getMessage()
            digits = str(int(timeLeft))
            if label and label.strip().lower() not in ('', 'none'):
                digits = label + '\n' + digits
            view.as_setMessageS(digits)
        return result
    PreBattleTimerBase.setCountdown = countdown


def init():
    global session
    from PlayerEvents import g_playerEvents
    install_aim_settings()
    install_countdown()
    g_playerEvents.onObservedByEnemy += observed
    BattleReplay.g_replayCtrl.setDataCallback(CALLBACK, play_warning)
    live = plugins._ASAutoReloadProxy.setReloading
    @wraps(live)
    def capture(proxy, state, isShotAvailable):
        result = live(proxy, state, isShotAvailable)
        if fleet._track is not None:
            blocked = state.isReloadingBlocked()
            remaining = state.getActualValue() if blocked else state.getTimeLeft()
            record(b'L', [remaining, state.getBaseValue(), bool(blocked), bool(isShotAvailable)])
        return result
    plugins._ASAutoReloadProxy.setReloading = capture
    live_clip = plugins._ASAutoReloadProxy.setClipAutoLoading
    @wraps(live_clip)
    def capture_clip(proxy, timeLeft, baseTime, isCritical=False, isTimerOn=False, isRedText=False):
        result = live_clip(proxy, timeLeft, baseTime, isCritical, isTimerOn, isRedText)
        if fleet._track is not None:
            record(b'T', [timeLeft, baseTime, bool(isCritical), bool(isTimerOn), bool(isRedText)])
        return result
    plugins._ASAutoReloadProxy.setClipAutoLoading = capture_clip
    start, stop = plugins.AmmoPlugin.start, plugins.AmmoPlugin.stop
    @wraps(start)
    def ready(plugin):
        global session
        result = start(plugin)
        replay = BattleReplay.g_replayCtrl
        if replay.isPlaying and replay.arenaInfo.get('oh2Fleet'):
            if session is not None:
                session.finish()
            data = replay.arenaInfo['oh2Fleet']
            warnings = []
            filename = getattr(replay, '_BattleReplay__fileName', None)
            if filename and 'hudReload' not in data:
                sidecar = filename + '.hud.json'
                if os.path.isfile(sidecar):
                    try:
                        with open(sidecar, 'rb') as stream:
                            history = json.load(stream)
                        if history.get('size') == os.path.getsize(filename):
                            data['hudReload'] = history['hudReload']
                            warnings = history['warnings']
                    except (IOError, ValueError, KeyError):
                        LOG.warning('Could not read HUD history for %s', filename)
            session = ReplayHUD(plugin, data, warnings)
            session.tick()
        return result
    @wraps(stop)
    def end(plugin):
        global session
        if session is not None and session.animator is plugin._AmmoPlugin__reloadAnimator:
            session.finish()
            session = None
        return stop(plugin)
    plugins.AmmoPlugin.start, plugins.AmmoPlugin.stop = ready, end

    native_clip = BattleReplay.BattleReplay.getAimClipPosition
    @wraps(native_clip)
    def aim_clip(replay):
        if (session is not None and replay.isPlaying and replay.isControllingCamera
                and replay.getControlMode() == 'sniper'):
            index = bisect_right(session.aim_times, replay.currentTime) - 1
            if index >= 0:
                return Math.Vector2(*session.aim[index][1:])
            return Math.Vector2(0., 0.)
        return native_clip(replay)
    BattleReplay.BattleReplay.getAimClipPosition = aim_clip
