"""Record the local player's loadout changes and incoming battle feedback."""
from bisect import bisect_right
from collections import OrderedDict, namedtuple
from functools import wraps
import json
import logging

import Avatar
import BattleReplay
import BigWorld
import Vehicle
from BattleFeedbackCommon import BATTLE_EVENT_TYPE
from account_helpers.settings_core.settings_constants import BATTLE_EVENTS, DAMAGE_LOG, DAMAGE_INDICATOR
from account_helpers.settings_core.SettingsCore import SettingsCore
from gui.Scaleform.daapi.view.battle.shared.ribbons_panel import BattleRibbonsPanel
from ReplayEvents import g_replayEvents
from constants import ATTACK_REASON, ATTACK_REASON_INDICES, VEHICLE_SETTING
from gui.mods import mod_oh2_fleet as fleet
from gui.Scaleform.daapi.view.battle.shared.crosshair import plugins
from gui.battle_control.battle_constants import VEHICLE_VIEW_STATE
from gui.battle_control.controllers.vehicle_state_ctrl import VehicleStateController

LOG = logging.getLogger('oh2.fleet.status')
EVENTS = BATTLE_EVENT_TYPE.ALL
RIBBON_SETTINGS = set(BattleRibbonsPanel.getBattleEventsSettingsToBattleEfficiencyTypes())
RIBBON_SETTINGS.update((BATTLE_EVENTS.SHOW_IN_BATTLE, BATTLE_EVENTS.EVENT_NAME, BATTLE_EVENTS.VEHICLE_INFO))
ribbon_panel = None
ShotResult = namedtuple('ShotResult', 'vehicleID hitFlags gunInstallationIndex')
DRIVER_STATES = (VEHICLE_VIEW_STATE.AUTO_ROTATION, VEHICLE_VIEW_STATE.CRUISE_MODE, VEHICLE_VIEW_STATE.FIRE)
LOG_SETTINGS = (DAMAGE_LOG.SHOW_DETAILS, DAMAGE_LOG.EVENT_POSITIONS, DAMAGE_LOG.SHOW_EVENT_TYPES,
                DAMAGE_LOG.TOTAL_DAMAGE, DAMAGE_LOG.BLOCKED_DAMAGE, DAMAGE_LOG.ASSIST_DAMAGE, DAMAGE_LOG.ASSIST_STUN)
SELECTION = (VEHICLE_SETTING.CURRENT_SHELLS, VEHICLE_SETTING.NEXT_SHELLS)
session = None


def record(tag, values):
    track = fleet._track
    if track is not None:
        track.write(tag, fleet._json_bytes([BigWorld.time()-track.start] + values))


def visible_id(player, key):
    track = fleet._track
    return key if not track.player_view or not key or track.visible(player, key) else 0


def ammo(player, vehicleID, compactDescr, quantity, quantityInClipOrNextStage,
         previousStage, timeRemaining, totalTime, index=0):
    if vehicleID == player.playerVehicleID:
        record(b'U', ['ammo', compactDescr, quantity, quantityInClipOrNextStage,
                     previousStage, timeRemaining, totalTime, index])


def setting(player, vehicleID, code, value):
    if vehicleID == player.playerVehicleID and code in SELECTION:
        record(b'U', ['selection', code, value])


def hit(player, hitDirYaw, attackerID, damage, crits, isBlocked, isShellHE, damagedID, attackReasonID):
    if damagedID == player.playerVehicleID:
        # The native direction indicator receives a public roster ID (zero for an unknown attacker).
        record(b'I', ['hit', hitDirYaw, attackerID, damage, crits,
                      bool(isBlocked), bool(isShellHE), attackReasonID])


def shot_results(player, results):
    rows = [[r.vehicleID, r.hitFlags, r.gunInstallationIndex] for r in results if visible_id(player, r.vehicleID)]
    if rows:
        record(b'I', ['shots', rows])


def events(player, events):
    rows = []
    for e in events:
        kind = e['eventType']
        if kind in EVENTS:
            target = e['targetID']
            rows.append([kind, target, e['count'], e['details'], target in player.arena.vehicles])
    if rows:
        record(b'I', ['events', rows])


def publish_history(player, rows, seeking):
    if not seeking:
        player.onBattleEvents(rows)
        return
    # Rebuild just the native detail list and totals when crossing earlier
    # events during a seek. Transient hit indicators belong to normal playback.
    from gui.battle_control.controllers.feedback_events import PlayerFeedbackEvent
    ctrl = player.guiSessionProvider.shared.personalEfficiencyCtrl
    if ctrl is not None:
        ctrl._onPlayerFeedbackReceived([PlayerFeedbackEvent.fromDict(row) for row in rows])


def hook_capture(name, capture):
    method = getattr(Avatar.PlayerAvatar, name)
    @wraps(method)
    def wrapped(player, *args, **kwargs):
        result = method(player, *args, **kwargs)
        track = fleet._track
        if track is not None:
            try:
                capture(player, *args, **kwargs)
            except Exception:
                if name not in track.errors:
                    track.errors.add(name)
                    LOG.exception('Recording %s', name)
        return result
    setattr(Avatar.PlayerAvatar, name, wrapped)


def playback_incoming(encoded):
    r = BattleReplay.g_replayCtrl
    if not r.isPlaying or not (r.arenaInfo or {}).get('oh2Fleet'):
        return
    p = BigWorld.player()
    row = json.loads(encoded)
    if row[0] == 'shots' and not r.isTimeWarpInProgress:
        results = [ShotResult(*item) for item in row[1] if item[0] in p.arena.vehicles]
        if results:
            p.showShotResults(results)
    elif row[0] == 'hit' and not r.isTimeWarpInProgress:
        _, yaw, attacker, damage, crits, blocked, he, reason = row
        p.showOwnVehicleHitDirection(yaw, attacker, damage, crits, blocked, he, p.playerVehicleID, reason)
    elif row[0] == 'events':
        publish_history(p, [dict(eventType=kind, targetID=key, count=count, details=details)
                            for kind, key, count, details in row[1] if kind in EVENTS], r.isTimeWarpInProgress)


def legacy_hit(vehicle, newHealth, oldHealth, attackerID, attackReasonID, attackReasonExtID):
    r = BattleReplay.g_replayCtrl
    if not r.isPlaying or r.playerVehicleID not in (attackerID, vehicle.id):
        return
    data = (r.arenaInfo or {}).get('oh2Fleet')
    amount = oldHealth - max(newHealth, 0)
    if not data or amount <= 0:
        return
    p = BigWorld.player()
    received = vehicle.id == r.playerVehicleID
    if (not received and not data.get('shotResults') and not r.isTimeWarpInProgress and newHealth > 0
            and attackReasonID == ATTACK_REASON_INDICES[ATTACK_REASON.SHOT]
            and p.arena.vehicles.get(vehicle.id, {}).get('team') in (1, 2)
            and p.arena.vehicles[vehicle.id]['team'] != p.team):
        p.soundNotifications.play('enemy_hp_damaged_by_projectile_by_player', boundVehicleID=vehicle.id)
    if received and data.get('incomingFeedback') or not received and data.get('battleHistory'):
        return
    if not received:
        target_team = p.arena.vehicles.get(vehicle.id, {}).get('team')
        if not target_team or target_team == p.team:
            return
    details = BATTLE_EVENT_TYPE.packDamage(amount, attackReasonID)
    kind = BATTLE_EVENT_TYPE.RECEIVED_DAMAGE if received else BATTLE_EVENT_TYPE.DAMAGE
    publish_history(p, [dict(eventType=kind, targetID=attackerID if received else vehicle.id,
                             count=amount, details=details)], r.isTimeWarpInProgress)
    if not received or r.isTimeWarpInProgress:
        return
    attacker = BigWorld.entity(attackerID)
    if attacker is not None and attackerID != vehicle.id and attackReasonID == ATTACK_REASON_INDICES[ATTACK_REASON.SHOT]:
        direction = vehicle.position - attacker.position
        if direction.lengthSquared:
            p.showOwnVehicleHitDirection(direction.yaw, attackerID, amount, 0, False, False, vehicle.id, attackReasonID)


class Loadout(object):
    def __init__(self, plugin, rows):
        self.plugin = plugin
        self.rows = rows
        self.times = [row[0] for row in rows]
        self.cursor = None
        self.previous = None
        self.timer = None
        self.errors = set()
        self.shown_devices = set()
        self.on_fire = False

    def snapshot(self, end):
        # Keep first appearance order for slots, then restore shell selection.
        items, choices = OrderedDict(), OrderedDict()
        devices, repairs = OrderedDict(), OrderedDict()
        for row in self.rows[:end]:
            if row[1] == 'ammo':
                items[row[2], row[8]] = row
            elif row[1] == 'selection':
                choices[row[2]] = row
            elif row[1] == 'driverState':
                choices['driverState', row[2]] = row
            elif row[1] == 'device':
                name, actual = row[2], row[4]
                devices[name] = [row[0], 'device', name, actual, actual]
                if actual != 'destroyed':
                    repairs.pop(name, None)
            elif row[1] == 'repair':
                repairs[row[2]] = row
        # Rewinding before a hit must clear the icon from the later timeline.
        for name in sorted(self.shown_devices.difference(devices)):
            devices[name] = [0., 'device', name, 'normal', 'normal']
        if self.on_fire:
            choices.setdefault(('driverState', VEHICLE_VIEW_STATE.FIRE), [0., 'driverState', VEHICLE_VIEW_STATE.FIRE, False])
        return list(items.values()) + list(choices.values()) + list(devices.values()) + list(repairs.values())

    def apply(self, p, row, now):
        if row[1] == 'ammo':
            stamp, _, cd, count, stage, prev, remaining, total, index = row
            if remaining > 0:
                remaining = max(0., remaining - max(0., now-stamp))
            p.updateVehicleAmmo(p.playerVehicleID, cd, count, stage, prev, remaining, total, index)
        elif row[1] == 'selection' and row[2] in SELECTION:
            p.updateVehicleSetting(p.playerVehicleID, row[2], row[3])
        elif row[1] == 'driverState' and row[2] == VEHICLE_VIEW_STATE.FIRE:
            p.guiSessionProvider.invalidateVehicleState(row[2], row[3], p.playerVehicleID)
            self.on_fire = bool(row[3])
        elif row[1] == 'device':
            name, display, actual = row[2:]
            p.guiSessionProvider.invalidateVehicleState(
                VEHICLE_VIEW_STATE.DEVICES, (name, display, actual), p.playerVehicleID)
            if actual == 'normal':
                self.shown_devices.discard(name)
            else:
                self.shown_devices.add(name)
        elif row[1] == 'repair':
            stamp, _, name, progress, seconds, mode = row
            elapsed = max(0., now-stamp)
            if seconds > elapsed:
                progress += (1.-progress) * elapsed / seconds
                p.guiSessionProvider.invalidateVehicleState(
                    VEHICLE_VIEW_STATE.REPAIRING, (name, progress, seconds-elapsed, mode), p.playerVehicleID)

    def tick(self):
        self.timer = None
        r = BattleReplay.g_replayCtrl
        if self is not session or not r.isPlaying:
            return
        try:
            p = BigWorld.player()
            v = p.getVehicleAttached() if r.isClientReady else None
            if (r.isTimeWarpInProgress or v is None or not v.isStarted or v.id != p.playerVehicleID
                    or p.guiSessionProvider.shared.prebattleSetups.getPrebattleVehicleID() == v.id):
                self.cursor = None
            else:
                now = r.currentTime
                end = bisect_right(self.times, now)
                restore = self.cursor is None or now < self.previous or now-self.previous > 1.
                rows = self.snapshot(end) if restore else self.rows[self.cursor:end]
                for row in rows:
                    self.apply(p, row, now)
                self.previous, self.cursor = now, end
        except Exception as e:
            key = (type(e), str(e))
            if key not in self.errors:
                self.errors.add(key)
                LOG.exception('Restoring recorded loadout')
            self.cursor = None
        self.timer = BigWorld.callback(.08, self.tick)

    def stop(self):
        if self.timer is not None:
            BigWorld.cancelCallback(self.timer)
            self.timer = None


class ReplayRibbons(object):
    # Flash animates in real time and has its own queue. On a timeline jump, only
    # reintroduce recent ribbons that fit in the native three-row display.
    def __init__(self, panel, methods, rows):
        self.panel = panel
        self.methods = methods
        self.rows = rows
        self.times = [row[0] for row in rows]
        self.recent = OrderedDict()
        self.active = set()
        self.pending = {}
        self.speed = BattleReplay.g_replayCtrl.playbackSpeed

    def dispatch(self, index, args, now):
        key = args[1]
        update = index and key in self.active
        if not update:
            self.pending[key] = now
        self.active.add(key)
        self.methods[1 if update else 0](self.panel, *args)

    def play(self, index, args):
        replay = BattleReplay.g_replayCtrl
        if replay.isTimeWarpInProgress:
            return
        now = replay.currentTime
        key = args[1]
        self.recent.pop(key, None)
        self.recent[key] = (now, args)
        while len(self.recent) > 3:
            self.recent.popitem(last=False)
        if replay.playbackSpeed > 1 and self.pending and min(self.pending.values()) < now - 3.:
            self.restore()
        else:
            self.dispatch(index, args, now)

    def reset(self):
        self.panel.as_resetS()
        self.active.clear()
        self.pending.clear()

    def seek_start(self):
        self.recent.clear()
        self.reset()

    def restore(self):
        now = BattleReplay.g_replayCtrl.currentTime
        self.reset()
        if self.rows:
            left, right = bisect_right(self.times, now - 3.), bisect_right(self.times, now + .0001)
            for stamp, args in self.rows[left:right]:
                self.recent.pop(args[1], None)
                self.recent[args[1]] = (stamp, args)
        current = [(t, args) for t, args in self.recent.values() if now-3. < t <= now+.0001]
        self.recent.clear()
        for t, args in current[-3:]:
            self.recent[args[1]] = (t, args)
            self.dispatch(0, args, now)

    def change_speed(self, speed):
        if speed and speed != self.speed:
            self.speed = speed
            if not BattleReplay.g_replayCtrl.isTimeWarpInProgress:
                self.restore()


def install_ribbons():
    original_setting = SettingsCore.getSetting
    @wraps(original_setting)
    def ribbon_setting(core, key):
        replay = BattleReplay.g_replayCtrl
        if key in RIBBON_SETTINGS and replay and replay.isPlaying:
            data = (replay.arenaInfo or {}).get('oh2Fleet')
            if data:
                return data.get('ribbonSettings', {}).get(key, True)
        return original_setting(core, key)
    SettingsCore.getSetting = ribbon_setting

    # Some native ribbons originate in vehicle state or perk callbacks instead of onBattleEvents.
    # Keep the formatted calls that the player actually saw, including aggregated updates.
    methods = (BattleRibbonsPanel.as_addBattleEfficiencyEventS, BattleRibbonsPanel.as_updateBattleEfficiencyEventS)
    def observe(index):
        native = methods[index]
        @wraps(native)
        def show(panel, *args):
            replay = BattleReplay.g_replayCtrl
            if replay.isPlaying and (replay.arenaInfo or {}).get('oh2Fleet', {}).get('recordedRibbons'):
                return
            result = native(panel, *args)
            if fleet._track is not None and panel._isDAAPIInited():
                record(b'I', ['ribbon', index, list(args)])
            return result
        return show
    BattleRibbonsPanel.as_addBattleEfficiencyEventS = observe(0)
    BattleRibbonsPanel.as_updateBattleEfficiencyEventS = observe(1)
    populate, dispose = BattleRibbonsPanel._populate, BattleRibbonsPanel._dispose
    show, hide = BattleRibbonsPanel.onShow, BattleRibbonsPanel.onHide
    @wraps(populate)
    def opened(panel):
        global ribbon_panel
        result = populate(panel)
        replay = BattleReplay.g_replayCtrl
        data = (replay.arenaInfo or {}).get('oh2Fleet') if replay.isPlaying else None
        if data and data.get('recordedRibbons'):
            ribbon_panel = ReplayRibbons(panel, methods, data.get('ribbonTimeline', []))
            g_replayEvents.onTimeWarpStart += ribbon_panel.seek_start
            g_replayEvents.onTimeWarpFinish += ribbon_panel.restore
            g_replayEvents.onPlaybackSpeedChanged += ribbon_panel.change_speed
        return result
    @wraps(dispose)
    def closed(panel):
        global ribbon_panel
        if ribbon_panel and ribbon_panel.panel is panel:
            g_replayEvents.onTimeWarpStart -= ribbon_panel.seek_start
            g_replayEvents.onTimeWarpFinish -= ribbon_panel.restore
            g_replayEvents.onPlaybackSpeedChanged -= ribbon_panel.change_speed
            ribbon_panel = None
        return dispose(panel)
    @wraps(show)
    def shown(panel, key):
        if ribbon_panel and ribbon_panel.panel is panel:
            ribbon_panel.pending.pop(key, None)
        return show(panel, key)
    @wraps(hide)
    def hidden(panel, key):
        if ribbon_panel and ribbon_panel.panel is panel:
            ribbon_panel.active.discard(key)
            ribbon_panel.pending.pop(key, None)
        return hide(panel, key)
    BattleRibbonsPanel._populate, BattleRibbonsPanel._dispose = opened, closed
    BattleRibbonsPanel.onShow, BattleRibbonsPanel.onHide = shown, hidden
    def play(index, args):
        if ribbon_panel:
            ribbon_panel.play(index, args)
    BattleReplay.g_replayCtrl.setDataCallback('oh2Fleet.ribbon', play)


def init():
    install_ribbons()
    new_track = fleet.Track.__init__
    @wraps(new_track)
    def begin(track, *args, **kwargs):
        new_track(track, *args, **kwargs)
        track.device_states = {}
        from helpers import dependency
        from skeletons.account_helpers.settings_core import ISettingsCore
        settings = {}
        try:
            core = dependency.instance(ISettingsCore)
            settings = dict((key, core.getSetting(key)) for key in LOG_SETTINGS)
            ribbon_settings = dict((key, core.getSetting(key)) for key in RIBBON_SETTINGS)
            track.write(b'I', fleet._json_bytes([0., 'start', 'ribbons', ribbon_settings]))
        except Exception:
            LOG.warning('Could not read damage log settings', exc_info=True)
        track.write(b'I', fleet._json_bytes([0., 'start', 'history', settings]))
        track.write(b'I', fleet._json_bytes([0., 'start', 'shotResults']))
    fleet.Track.__init__ = begin
    notify = VehicleStateController.notifyStateChanged
    @wraps(notify)
    def state_changed(ctrl, stateID, value):
        result = notify(ctrl, stateID, value)
        track = fleet._track
        if track is not None and stateID in (VEHICLE_VIEW_STATE.DEVICES, VEHICLE_VIEW_STATE.REPAIRING) + DRIVER_STATES:
            try:
                if ctrl.getControllingVehicleID() == BigWorld.player().playerVehicleID:
                    if stateID in DRIVER_STATES:
                        if track.device_states.get(stateID) != value:
                            record(b'U', ['driverState', stateID, value])
                            track.device_states[stateID] = value
                    elif stateID == VEHICLE_VIEW_STATE.REPAIRING:
                        record(b'U', ['repair'] + list(value))
                    elif track.device_states.get(value[0]) != tuple(value):
                        record(b'U', ['device'] + list(value))
                        track.device_states[value[0]] = tuple(value)
            except Exception:
                if 'devices' not in track.errors:
                    track.errors.add('devices')
                    LOG.exception('Recording player vehicle damage panel')
        return result
    VehicleStateController.notifyStateChanged = state_changed
    original_setting = BattleReplay.BattleReplay.getSetting
    @wraps(original_setting)
    def replay_setting(controller, key, default=None):
        if (controller.isPlaying and key in (DAMAGE_INDICATOR.VEHICLE_INFO, DAMAGE_INDICATOR.DAMAGE_VALUE,
                                             DAMAGE_INDICATOR.TYPE) and (controller.arenaInfo or {}).get('oh2Fleet')):
            return 1
        if controller.isPlaying and key in LOG_SETTINGS:
            try:
                settings = (controller.arenaInfo or {}).get('oh2Fleet', {}).get('battleLogSettings', {})
                if key in settings:
                    return settings[key]
            except (TypeError, ValueError):
                pass
        return original_setting(controller, key, default)
    BattleReplay.BattleReplay.getSetting = replay_setting
    for name, handler in (('updateVehicleAmmo', ammo), ('updateVehicleSetting', setting),
                          ('showOwnVehicleHitDirection', hit), ('onBattleEvents', events),
                          ('showShotResults', shot_results)):
        hook_capture(name, handler)
    BattleReplay.g_replayCtrl.setDataCallback('oh2Fleet.incoming', playback_incoming)
    health = Vehicle.Vehicle.onHealthChanged
    @wraps(health)
    def changed(vehicle, newHealth, *args, **kwargs):
        try:
            legacy_hit(vehicle, newHealth, *args, **kwargs)
        except Exception:
            LOG.exception('Restoring historic incoming damage')
        result = health(vehicle, newHealth, *args, **kwargs)
        replay = BattleReplay.g_replayCtrl
        if replay.isPlaying and (replay.arenaInfo or {}).get('oh2Fleet'):
            player = BigWorld.player()
            if vehicle.id == player.playerVehicleID:
                # The vehicle packet updates the entity. The driver's damage panel
                # normally receives a separate health notification from the avatar.
                player.guiSessionProvider.invalidateVehicleState(
                    VEHICLE_VIEW_STATE.HEALTH, max(0, newHealth), vehicle.id)
        return result
    Vehicle.Vehicle.onHealthChanged = changed
    start, stop = plugins.AmmoPlugin.start, plugins.AmmoPlugin.stop
    @wraps(start)
    def ready(plugin):
        global session
        result = start(plugin)
        r = BattleReplay.g_replayCtrl
        data = (r.arenaInfo or {}).get('oh2Fleet') if r.isPlaying else None
        if data and data.get('loadout'):
            if session is not None:
                session.stop()
            session = Loadout(plugin, data['loadout'])
            session.tick()
        return result
    @wraps(stop)
    def end(plugin):
        global session
        if session is not None and session.plugin is plugin:
            session.stop()
            session = None
        return stop(plugin)
    plugins.AmmoPlugin.start, plugins.AmmoPlugin.stop = ready, end
