from __future__ import absolute_import

import logging
from functools import wraps

import BattleReplay
import BigWorld
from PlayerEvents import g_playerEvents
from constants_utils import getUsedInReplaysConfigKeys
from schema_manager import getSchemaManager


LOGGER = logging.getLogger('offhangar2.replay')
_installed = False


def _replay_settings(settings, schema_manager, keys, minimal_raw):
    keys = set(keys)
    snapshot = {key: settings[key] for key in keys if key in settings}
    missing = keys.difference(snapshot)
    for info in schema_manager.getSchemasInfo():
        key = info.schema.gpKey
        if key in missing:
            snapshot[key] = minimal_raw(info.schema)
    return snapshot


def init():
    global _installed
    if _installed:
        return

    from gui.mods.offhangar2 import battle, login, server_settings

    original_create = battle._createArenaAsBattleWorld
    original_finish = battle._finishLeave
    replay_class = BattleReplay.BattleReplay
    account_method = '_BattleReplay__onAccountBecomePlayer'
    original_account = getattr(replay_class, account_method)
    original_record = replay_class.record
    original_ready = replay_class.onClientReady

    @wraps(original_account)
    def account_ready(controller):
        player = BigWorld.player()
        if not login.isOfflineSession() or not getattr(player, 'isOffline', False):
            return original_account(controller)
        if controller.isPlaying:
            return original_account(controller)
        if not hasattr(player, 'initialServerSettings'):
            return
        try:
            settings = player.serverSettings
            snapshot = _replay_settings(
                settings, getSchemaManager(), getUsedInReplaysConfigKeys(),
                server_settings.minimalRaw)
            controller._BattleReplay__serverSettings = snapshot
            controller._BattleReplay__playerDatabaseID = player.databaseID
            LOGGER.warning('Offline replay metadata prepared: defaults=%s',
                           sorted(set(snapshot).difference(settings)))
        except Exception:
            LOGGER.exception('Could not prepare replay account settings')

    @wraps(original_record)
    def record(controller, filename=None):
        started = original_record(controller, filename)
        if login.isOfflineSession():
            LOGGER.warning('Native record result=%s, active=%s', started, controller.isRecording)
        return started

    @wraps(original_create)
    def create(*args, **kwargs):
        replay = BattleReplay.g_replayCtrl
        try:
            if replay and login.isOfflineSession() and not replay.isPlaying:
                enabled = replay.isAutoRecordingEnabled
                LOGGER.warning('Arena entry: enabled=%s, active=%s', enabled, replay.isRecording)
                if enabled and not replay.isRecording:
                    replay.record()
        except Exception:
            LOGGER.exception('Could not start offline replay')
        return original_create(*args, **kwargs)

    @wraps(original_ready)
    def ready(controller):
        result = original_ready(controller)
        if login.isOfflineSession() and not controller.isPlaying:
            LOGGER.warning('Battle ready: active=%s', controller.isRecording)
        return result

    @wraps(original_finish)
    def finish(*args, **kwargs):
        replay = BattleReplay.g_replayCtrl
        try:
            if replay and login.isOfflineSession() and not replay.isPlaying:
                LOGGER.warning('Battle exit: active=%s', replay.isRecording)
                if replay.isRecording:
                    LOGGER.warning('Replay finalized=%s', replay.stop())
        except Exception:
            LOGGER.exception('Could not finalize offline replay')
        return original_finish(*args, **kwargs)

    controller = BattleReplay.g_replayCtrl
    previous = getattr(controller, account_method, None)
    subscribers = g_playerEvents.onAccountBecomePlayer
    setattr(replay_class, account_method, account_ready)
    if previous is not None and previous in subscribers:
        subscribers[subscribers.index(previous)] = getattr(controller, account_method)
    replay_class.record = record
    replay_class.onClientReady = ready
    battle._createArenaAsBattleWorld = create
    battle._finishLeave = finish
    _installed = True
    LOGGER.warning('OffHangar replay compatibility loaded')
