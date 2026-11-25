"""Record the local player's loadout changes and incoming battle feedback."""
from bisect import bisect_right
from collections import OrderedDict
from functools import wraps
import json
import logging

import Avatar
import BattleReplay
import BigWorld
import Vehicle
from BattleFeedbackCommon import BATTLE_EVENT_TYPE
from account_helpers.settings_core.settings_constants import DAMAGE_LOG
from constants import ATTACK_REASON, ATTACK_REASON_INDICES, VEHICLE_SETTING
from gui.mods import mod_oh2_fleet as fleet
from gui.Scaleform.daapi.view.battle.shared.crosshair import plugins

LOG = logging.getLogger('oh2.fleet.status')
EVENTS = (BATTLE_EVENT_TYPE.RECEIVED_DAMAGE, BATTLE_EVENT_TYPE.TANKING, BATTLE_EVENT_TYPE.RECEIVED_CRIT,
          BATTLE_EVENT_TYPE.DAMAGE, BATTLE_EVENT_TYPE.RADIO_ASSIST, BATTLE_EVENT_TYPE.TRACK_ASSIST,
          BATTLE_EVENT_TYPE.STUN_ASSIST, BATTLE_EVENT_TYPE.DESTRUCTIBLE_DAMAGED)
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
        record(b'I', ['hit', hitDirYaw, visible_id(player, attackerID), damage, crits,
                      bool(isBlocked), bool(isShellHE), attackReasonID])


def events(player, events):
    rows = []
    for e in events:
        kind = e['eventType']
        if kind in EVENTS:
            target = e['targetID'] if kind == BATTLE_EVENT_TYPE.DESTRUCTIBLE_DAMAGED else visible_id(player, e['targetID'])
            rows.append([kind, target, e['count'], e['details']])
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
    if row[0] == 'hit' and not r.isTimeWarpInProgress:
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

    def snapshot(self, end):
        # Keep first appearance order for slots, then restore shell selection.
        items, choices = OrderedDict(), OrderedDict()
        for row in self.rows[:end]:
            if row[1] == 'ammo':
                items[row[2], row[8]] = row
            elif row[1] == 'selection':
                choices[row[2]] = row
        return list(items.values()) + list(choices.values())

    def apply(self, p, row, now):
        if row[1] == 'ammo':
            stamp, _, cd, count, stage, prev, remaining, total, index = row
            if remaining > 0:
                remaining = max(0., remaining - max(0., now-stamp))
            p.updateVehicleAmmo(p.playerVehicleID, cd, count, stage, prev, remaining, total, index)
        elif row[1] == 'selection' and row[2] in SELECTION:
            p.updateVehicleSetting(p.playerVehicleID, row[2], row[3])

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


def init():
    new_track = fleet.Track.__init__
    @wraps(new_track)
    def begin(track, *args, **kwargs):
        new_track(track, *args, **kwargs)
        from helpers import dependency
        from skeletons.account_helpers.settings_core import ISettingsCore
        settings = {}
        try:
            core = dependency.instance(ISettingsCore)
            settings = dict((key, core.getSetting(key)) for key in LOG_SETTINGS)
        except Exception:
            LOG.warning('Could not read damage log settings', exc_info=True)
        track.write(b'I', fleet._json_bytes([0., 'start', 'history', settings]))
    fleet.Track.__init__ = begin
    original_setting = BattleReplay.BattleReplay.getSetting
    @wraps(original_setting)
    def replay_setting(controller, key, default=None):
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
                          ('showOwnVehicleHitDirection', hit), ('onBattleEvents', events)):
        hook_capture(name, handler)
    BattleReplay.g_replayCtrl.setDataCallback('oh2Fleet.incoming', playback_incoming)
    health = Vehicle.Vehicle.onHealthChanged
    @wraps(health)
    def changed(vehicle, *args, **kwargs):
        try:
            legacy_hit(vehicle, *args, **kwargs)
        except Exception:
            LOG.exception('Restoring historic incoming damage')
        return health(vehicle, *args, **kwargs)
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
