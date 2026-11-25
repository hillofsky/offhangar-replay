"""Record quick battle commands, including the offline-only local echoes."""
import json
import logging
import math
import struct
from functools import wraps

import BattleReplay
import BigWorld
from gui.mods import mod_oh2_fleet as fleet

LOG = logging.getLogger('oh2.fleet.radio')
CALLBACK = 'oh2Fleet.radio'
_recent = []
_ground_depth = 0


def capture(cmd, outgoing=False):
    track = fleet._track
    if track is None:
        return
    try:
        from messenger_common_chat2 import MESSENGER_ACTION_IDS
        definition = MESSENGER_ACTION_IDS.battleChatCommandFromActionID(cmd.getID())
        if not definition:
            return
        p = BigWorld.player()
        roster = p.arena.vehicles
        data = cmd.getProtoData()
        own = p.playerVehicleID
        sender = own if outgoing else data['int64Arg1']
        if sender not in roster or roster[sender]['team'] != roster[own]['team']:
            return
        target = int(data['int32Arg1'])
        vehicle_target = target in roster
        if track.player_view and vehicle_target and roster[target]['team'] != roster[own]['team'] and not track.visible(p,target):
            return
        if not outgoing:
            if any(previous is cmd for previous in _recent):
                return
            _recent.append(cmd)
            del _recent[:-16]
        text_arg = data['strArg1']
        if isinstance(text_arg,basestring):
            text_arg = text_arg.decode('utf8') if isinstance(text_arg,str) else text_arg
        else:
            text_arg = getattr(text_arg,'name',str(text_arg))
        point = list(struct.unpack('<fff',data['strArg2'])) if len(data['strArg2'])==12 else None
        item = dict(action=definition.name,sender=int(sender),target=target,vehicle=vehicle_target,
                    value=float(data['floatArg1']),text=text_arg,small=int(data['int8Arg1']),
                    point=point,outgoing=bool(outgoing),senderName=roster[sender].get('name',''),
                    silent=bool(not outgoing and cmd.isInSilentMode()))
        track.write(b'R',fleet._json_bytes([BigWorld.time()-track.start,item]))
    except Exception:
        LOG.exception('Recording a radio command')


def playback(encoded):
    replay = BattleReplay.g_replayCtrl
    if not replay.isPlaying or replay.isTimeWarpInProgress:
        return
    try:
        from chat_commands_consts import _COMMAND_NAME_TRANSFORM_MARKER_TYPE
        from messenger import MessengerEntry
        from messenger.proto.bw_chat2.battle_chat_cmd import BattleCommandFactory
        from messenger.proto.events import g_messengerEvents
        from messenger_common_chat2 import BATTLE_CHAT_COMMANDS_BY_NAMES, messageArgs
        from helpers import dependency
        from skeletons.gui.battle_session import IBattleSessionProvider
        item = json.loads(encoded)
        action = item['action']
        definition = BATTLE_CHAT_COMMANDS_BY_NAMES.get(action)
        if definition is None:
            return
        point = item['point']
        if point is not None and (len(point)!=3 or any(math.isnan(v) or math.isinf(v) for v in point)):
            return
        args = messageArgs(int64Arg1=item['sender'],int32Arg1=item['target'],floatArg1=item['value'],
                           strArg1=item['text'].encode('utf8'),int8Arg1=item['small'],
                           strArg2=struct.pack('<fff',*point) if point is not None else '')
        cmd = BattleCommandFactory.createByAction(definition.id,args)
        if item.get('silent'):
            cmd.setSilentMode(True)
        messenger = MessengerEntry.g_instance.gui
        if item['sender'] and messenger.channelsCtrl.getController(cmd.getClientID()) is not None:
            g_messengerEvents.channels.onCommandReceived(cmd)
            return
        session = dependency.instance(IBattleSessionProvider)
        chat = session.arenaVisitor.getComponentSystem().advancedChatComponent
        if chat is not None and item['sender']:
            if cmd.isReply():
                chat._handleReply(cmd)
            elif cmd.isCancelReply():
                chat._handleCancelReply(cmd)
            elif cmd.isClearChatCommand():
                chat._handleClearChatCommands(cmd)
            elif action in _COMMAND_NAME_TRANSFORM_MARKER_TYPE:
                chat._handleRegularCommand(cmd)
        if item['sender']:
            voice = getattr(BigWorld.player(),'_AvatarChatKeyHandling__onCommandReceived',None)
            if voice:
                voice(cmd)
        if not cmd.hasNoChatMessage():
            text = cmd.getCommandText()
            if text:
                from xml.sax.saxutils import escape
                sender_name = BigWorld.player().arena.vehicles.get(item['sender'],{}).get('name') or item.get('senderName')
                if sender_name:
                    if isinstance(sender_name,str):
                        sender_name = sender_name.decode('utf8','replace')
                    text = u'%s: %s' % (escape(sender_name),text)
                replay.onBattleChatMessage(text,item['sender']==replay.playerVehicleID)
    except Exception:
        LOG.exception('Playing a radio command')


def init():
    from gui.battle_control.controllers.chat_cmd_ctrl import ChatCommandsController
    from messenger.proto.events import g_messengerEvents
    method = '_ChatCommandsController__sendChatCommand'
    send = getattr(ChatCommandsController,method)
    @wraps(send)
    def sent(ctrl,cmd):
        result = send(ctrl,cmd)
        capture(cmd,True)
        return result
    setattr(ChatCommandsController,method,sent)
    g_messengerEvents.channels.onCommandReceived += capture
    BattleReplay.g_replayCtrl.setDataCallback(CALLBACK,playback)
    try:
        from gui.mods import mod_oh2_bot_orders as orders
    except ImportError:
        return
    from arena_components.advanced_chat_component import AdvancedChatComponent
    mark, regular, deliver = orders._markGround, AdvancedChatComponent._handleRegularCommand, orders._deliver
    @wraps(mark)
    def ground(*args,**kwargs):
        global _ground_depth
        _ground_depth += 1
        try:
            return mark(*args,**kwargs)
        finally:
            _ground_depth -= 1
    @wraps(regular)
    def marked(component,cmd):
        result = regular(component,cmd)
        if _ground_depth:
            capture(cmd)
        return result
    @wraps(deliver)
    def delivered(handler,cmd,vid):
        result = deliver(handler,cmd,vid)
        if result:
            capture(cmd)
        return result
    orders._markGround = ground
    AdvancedChatComponent._handleRegularCommand = marked
    orders._deliver = delivered
