# coding=utf8
import asyncio
import fnmatch
import io
import logging
import shutil
import stat
import sys
import threading
from ipaddress import IPv4Address
from typing import Optional, List

import aiohttp
from aiohttp import WSMsgType, web, BasicAuth
from aiohttp.typedefs import LooseHeaders

import pyatv
import time
from pyatv import Protocol, FacadeAppleTV, Listeners
from pyatv.Listeners import atvComapanionStateProducer, atvCompanionSetup, atv_full_directory_remotes_config
from pyatv.const import InputAction, PairingRequirement, DeviceState, MediaType, ShuffleState, RepeatState
from pyatv.core import protocol
from pyatv.interface import BaseService, AppleTV, BaseConfig, Playing, ArtworkInfo
from pyatv.protocols.airplay.pairing import (
    AirPlayPairingHandler,
    get_preferred_auth_type,
)
from pyatv.protocols.airplay.auth import pair_setup
from pyatv.protocols.companion import pair, HidCommand, server_auth
import pyatv.protocols.airplay
from pyatv.support import http
from pyatv.support.http import ClientSessionManager, HttpConnection
from pyatv.protocols.companion.pairing import CompanionPairingHandler
import os
import json
from pyatv.support.state_producer import StateProducer, StateListener

version = 1.2
__author__ = 'Mustafa Abdel Azim'

PAGE = """
<script>
let socket = new WebSocket('ws://' + location.host + '/ws/DEVICE_ID');

socket.onopen = function(e) {
  document.getElementById('status').innerText = 'Connected!';
};

socket.onmessage = function(event) {
  document.getElementById('state').innerText = event.data;
};

socket.onclose = function(event) {
  if (event.wasClean) {
    document.getElementById('status').innerText = 'Connection closed cleanly!';
  } else {
    document.getElementById('status').innerText = 'Disconnected due to error!';
  }
  document.getElementById('state').innerText = "";
};

socket.onerror = function(error) {
  document.getElementById('status').innerText = 'Failed to connect!';
};
</script>
<div id="status">Connecting...</div>
<div id="state"></div>
"""

RouterPort = 11500
routes = web.RouteTableDef()

atvpairing = {

}
atvMeta = {
    "": AppleTV
}
atvKeyboard = {
    "": AppleTV
}
atvMetaKeepalive = {
    "": bool

}

Authcodes = {

}
atvCredentials = {
    "": pyatv.interface.BaseService
}
atvConfig = {
    "": pyatv.interface.BaseConfig
}
hidSemaphore = {
    "": ""
}
atvPairs = {
    "": pyatv.interface.BaseService
}
atvConfigAir = {
    "": pyatv.interface.BaseConfig
}
pairingAirplay = {
    "": AirPlayPairingHandler
}
atvCredentialsFile = {"": ""}  # device ID : Device Credentials
atvPairCredentialsFile = {'': ''}  # device ID : Device pair Credentials
ServicePortFile = {"": int()}  # "ServicePort" : Service Port
ServersFile = {  # Device_IP : Server_IP:Port
    "Device_IP": "ServerIP:Port"
}
ServerSession = {
    str: aiohttp.ClientSession
}
ThreadEvents = {  # events
    str: threading.Event
}
ExcompremoteipReturn = {  # devID: web.Response
}
atvApps = {  # [devID: {App_name:App_id}]

}


def MetaSender(meta: list, devip: str, devid: str, serverip: str,
               serverport: int):
    try:
        loop = asyncio.new_event_loop()
        future = loop.create_task(
            SendMetatoServer(meta=meta, devip=devip, devid=devid, serverip=serverip, serverport=serverport))
        loop.run_until_complete(future)
        loop.close()
        if future.result():
            print("Meta Sent!")
        else:
            print("Faild to send Meta!")
    except Exception as ex:
        print(f'Error MetaSender:{ex}')


async def SendMetatoServer(meta: list, devip: str, devid: str, serverip: str, serverport: int) -> bool:
    try:
        if ServerSession.__contains__(serverip):
            session = ServerSession[serverip]
        else:
            timeout = aiohttp.ClientTimeout(total=5)
            session = aiohttp.ClientSession(timeout=timeout, json_serialize=json.dumps)
            ServerSession.update({serverip: session})

        async with session as se:
            if se.closed:
                timeout = aiohttp.ClientTimeout(total=5)
                se = aiohttp.ClientSession(timeout=timeout, json_serialize=json.dumps)
                ServerSession.update({serverip: session})
            url = f'http://{serverip}:{str(serverport)}/meta/{devip}:{devid}'
            async with se.post(url=url, json=json.dumps(obj=meta, sort_keys=True, indent=2)) as ps:
                if ps.closed is False:
                    ps.close()
                if se.closed is False:
                    await se.close()
                if ps.status == 200:
                    data = await ps.read()
                    coding = ps.get_encoding()
                    _data = data.decode(coding)
                    if _data.__eq__('GotIt'):
                        return True
                    else:
                        return False
                else:
                    return False

    except Exception as ex:
        print(f'Error: SendMetatoServer: {ex}')


def ReestablishConnectionSender(id: str, devip: str, config: BaseConfig,
                                protocol: Protocol,
                                servip: str, servport: str, old_loop: str):
    try:
        ReestablishConnection(id=id, devip=devip, config=config, protocol=protocol,
                              servip=servip, servport=servport, old_loop=old_loop)

    except Exception as ex:
        print(f'Error  ReestablishConnectionSender {ex}')


def ReestablishConnection(id: str, devip: str, config, protocol: Protocol, servip: str, servport: str, old_loop):
    try:
        loop = asyncio.new_event_loop()
        cnt = 0
        time.sleep(50)

        while atvMeta.__contains__(id) is False and cnt < 50:
            time.sleep(3)
            cnt += 1
            future = loop.create_task(
                coro=Connectatv(id=id, config=config, protocol=protocol, loop=old_loop, object='meta'),
                name=f'ReestablishCon.Devi{id}')
            loop.run_until_complete(future)
            loop.close()
            print(f'Reconnecting devIP:{devip} devid:{id} attempt:{cnt}')
            time.sleep(10)
            if atvMeta.__contains__(id):
                atvlocal = atvMeta[id]
                print(f'Re-connected devIP:{devip} devid:{id} attempt:{cnt}')
                lis = DeviceListener(identifier=id, devip=devip, servip=servip, servport=servport, loop=old_loop)
                atvlocal.listener = lis
                atvlocal.push_updater.listener = lis
                atvlocal.push_updater.start()
                atvMeta.update({id: atvlocal})
                atvMetaListeners.update({id: lis})
                print(f" Listener:{str(lis.identifier)}")
            else:
                print(f'Faild to reconnect devIP:{devip} devid:{id} attempt:{cnt}')
                return

    except Exception as ex:
        print(f'Error ReestablishConnectionSender {ex}')


async def Connectatv(id, config, protocol, loop, object: str) -> AppleTV:
    try:
        # loop = asyncio.get_event_loop()
        if loop is None:
            atv_ = atvMeta[id]
            await atv_.connect()
        else:
            _atv = await pyatv.connect(config=config, loop=loop, protocol=protocol)
            # _atv.close()
            # await _atv.connect()
            if _atv is not None:
                if object.__eq__('meta'):
                    atvMeta.update({id: _atv})
                elif object.__eq__('keyboard'):
                    atvKeyboard.update({id: _atv})
            return _atv
    except Exception as ex:
        print(f'Error Connectatv: {str(ex)}')
        if atvMeta.__contains__(id):
            atvMeta.pop(id)
        raise ex


class DeviceListener(pyatv.interface.DeviceListener, pyatv.interface.PushListener):
    def __init__(self, identifier, devip, servip, servport, loop):

        self.identifier = identifier
        self.devip = devip
        self.servip = servip
        self.servport = servport
        self.loop = loop

    def connection_lost(self, exception: Exception) -> None:
        try:
            print(f"Connection Lost: ID:{self.identifier}")
            if atvMetaKeepalive[self.identifier]:

                print(f'Disposing DevListener: ID:{self.identifier}')
                if atvConfigAir.__contains__(self.identifier):
                    config = atvConfigAir[self.identifier]
                    atvConfigAir.pop(self.identifier)
                    atvMeta[self.identifier].close()
                    atvMeta.pop(self.identifier)
                    # for now, later will be added
                    # sent = threading.Thread(target=ReestablishConnectionSender,
                    #                         args=(self.identifier, self.devip, config,
                    #                               Protocol.AirPlay,
                    #                               self.servip, self.servport, self.loop))
                    # sent.setDaemon(daemonic=True)
                    # sent.start()
        except Exception as ex:
            print(f'Error: connection_lost:{ex}')
            if atvMeta.__contains__(self.identifier):
                atvMeta.pop(self.identifier)

    def connection_closed(self) -> None:
        try:
            print(f"Connection Closed: ID:{self.identifier}")
            if atvMetaKeepalive[self.identifier]:

                print(f'Disposing DevListener: ID:{self.identifier}')
                if atvConfigAir.__contains__(self.identifier):
                    config = atvConfigAir[self.identifier]
                    atvConfigAir.pop(self.identifier)
                    atvMeta[self.identifier].close()
                    atvMeta.pop(self.identifier)
                    # for now, later will be added
                    # sent = threading.Thread(target=ReestablishConnectionSender,
                    #                         args=(self.identifier, self.devip, config,
                    #                               Protocol.AirPlay,
                    #                               self.servip, self.servport, self.loop))
                    # sent.setDaemon(daemonic=True)
                    # sent.start()
        except Exception as ex:
            print(f'Error: connection_closed:{ex}')
            if atvMeta.__contains__(self.identifier):
                atvMeta.pop(self.identifier)

    def playstatus_update(self, updater, playstatus: pyatv.interface.Playing) -> None:
        try:
            _atv = atvMeta[self.identifier]
            if playstatus is not None:
                if _atv.metadata is not None:
                    app = _atv.metadata.app.__str__()
                else:
                    app = ''
                _status = processmeta(playstatus, app=app, artdir='', artworkid='')
                print(str(_status))
                name = f'{self.identifier}:MetaSender'
                for th in threading.enumerate():
                    if th.name.__eq__(name):
                        if th.is_alive():
                            print(f'playstatus_update:{th.getName()} still alive from previous update! check server')

                sent = threading.Thread(target=MetaSender, args=(_status, self.devip,
                                                                 self.identifier,
                                                                 self.servip, self.servport))
                sent.setName(f'{self.identifier}:MetaSender')
                sent.setDaemon(daemonic=True)
                sent.start()

                # sent.join(timeout=10)
        except Exception as ex:
            print(f'Error playstatus_update: {ex}')
            if atvMeta.__contains__(self.identifier):
                atvMeta.pop(self.identifier)

    def playstatus_error(self, updater, exception: Exception) -> None:
        pass


atvMetaListeners = {
    '': DeviceListener
}


def web_command(method):
    async def _handler(request):
        device_id = request.match_info["id"]
        atv = request.app["atv"].get(device_id)
        if not atv:
            return web.Response(text=f"Not connected to {device_id}", status=500)
        return await method(request, atv)

    return _handler


def add_credentials(config, query):
    for service in config.services:
        proto_name = service.protocol.name.lower()
        if proto_name in query:
            config.set_credentials(service.protocol, query[proto_name])


@routes.get("/test")
async def test(request):
    return web.Response(text=f"ComanionAPI V{version} is running!", status=200)


@routes.get("/state/{id}")
async def state(request):
    return web.Response(
        text=PAGE.replace("DEVICE_ID", request.match_info["id"]),
        content_type="text/html",
    )


@routes.get("/scan/{to}")
async def scan(request):
    try:
        tout = int(request.match_info["to"])
        results = await pyatv.scan(loop=asyncio.get_event_loop(), timeout=tout)
        output = "\n\n".join(str(result) for result in results)
        print(f"Scan Results:\n{output}")
        return web.Response(text=f"Scan results:\n{output}")
    except Exception as ex:
        return web.Response(text=f"Failed to scan devices: {ex}", status=500)


def HidCmdCorrector(cmd: str) -> str:
    _cmd = cmd.lower()
    if _cmd.__contains__("volumeup"):
        return "VolumeUp"
    elif _cmd.__contains__("volumedown"):
        return "VolumeDown"
    elif _cmd.__contains__("playpause"):
        return "PlayPause"
    elif _cmd.__contains__("channelincrement"):
        return "ChannelIncrement"
    elif _cmd.__contains__("channeldecrement"):
        return "ChannelDecrement"
    elif _cmd.__contains__("pageup"):
        return "PageUp"
    elif _cmd.__contains__("pagedown"):
        return "PageDown"
    else:
        return "none"


def Excompremoteip(devip, hid: Optional[str], press: Optional[str], cnt, app, update_applist: bool):
    try:
        loop = asyncio.new_event_loop()
        task = loop.create_task(coro=Fncompremoteip(devip=devip, hid=hid, press=press, cnt=cnt,
                                                    app=app, update_applist=update_applist), name=f'{devip}_task')
        loop.run_until_complete(future=task)
        res = task.result()
        task.done()
        task.cancel()
        loop.close()
        ExcompremoteipReturn.update({devip: res})
        if update_applist or app:
            ev = ThreadEvents.get(f'{devip}_apps')
            ev.set()
    except Exception as ex:
        wr = web.Response(text=f"Error Excompremoteip:{ex} ", status=500)
        ExcompremoteipReturn.update({devip: wr})


async def Fncompremoteip(devip, hid: Optional[str], press: Optional[str], cnt, app,
                         update_applist: bool) -> web.Response:
    try:
        loop = asyncio.get_event_loop()
        count = 0
        config: BaseConfig
        found = False
        iddev = ""
        ipv4 = IPv4Address(devip)
        if atvConfig is not None:
            for con in atvConfig:
                if atvpairing.__contains__(con):
                    atvpairing.pop(con)
                    break
                if atvConfig[con].address == ipv4:
                    config = atvConfig[con]
                    found = True
                    for ide in config.all_identifiers:
                        if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                            id = str(ide).replace('.', '')
                            idc = str(id).__len__()
                            id1 = str(ide).replace(':', '')
                            id1c = str(id1).__len__()
                            if idc == 12:
                                iddev = id
                                break
                            elif id1c == 12:
                                iddev = id1
                                break
                            continue
                        else:
                            iddev = ide
                    break

        if found is False:
            results = await pyatv.scan(hosts=[str(devip)], loop=loop)
            if not results:
                return web.Response(text=f"Device:{devip} not found!", status=500)
            else:
                ident = results[0].all_identifiers
                for ide in ident:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            break
                        elif id1c == 12:
                            iddev = id1
                            break
                        continue
                    else:
                        iddev = ide

                atvConfig.update({iddev: results[0]})
                config = results[0]

        cr = ""
        if atvCredentialsFile.__contains__(iddev):
            cr = atvCredentialsFile.get(iddev)
        else:
            return web.Response(text=f"Device:{iddev} IPv4:{devip} has no Credentials stored!", status=500)
        config.set_credentials(protocol=Protocol.Companion, credentials=str(cr))

        _service = BaseService
        for ser in config.services:
            if ser.protocol == Protocol.Companion:
                _service = ser

        setupcompapi = pyatv.protocols.companion.CompanionAPI
        producer = StateProducer
        if atvCompanionSetup.__contains__(iddev) and atvComapanionStateProducer.__contains__(iddev):
            setupcompapi = atvCompanionSetup[iddev]
            producer = atvComapanionStateProducer[iddev]
            try:
                await setupcompapi.connect()
            except Exception:
                pass
        else:
            producer = StateProducer()
            setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop, service=_service,
                                                                  device_listener=producer)
            atvCompanionSetup.update({iddev: setupcompapi})
            atvComapanionStateProducer.update({iddev: producer})
            try:
                await setupcompapi.connect()
            except Exception:
                pass

        if hid is not None and press is not None:
            _cmd = HidCmdCorrector(hid)
            t = hid.title()
            cmd = HidCommand[t] if _cmd.__eq__('none') else HidCommand[_cmd]
            if press.__eq__("brief"):
                _cnt = int(cnt)
                count = _cnt
                if _cnt > 0:
                    while _cnt > 0:
                        setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop,
                                                                              service=_service,
                                                                              device_listener=producer)
                        await setupcompapi.hid_command(down=True, command=cmd)
                        await setupcompapi.hid_command(down=False, command=cmd)
                        await setupcompapi.disconnect()
                        _cnt -= 1
                else:
                    return web.Response(
                        text=f"Device:{iddev} IPv4:{devip} HID:{cmd} Press:{press.title()} Cnt Error:{count}")
            elif press.__eq__("down"):
                setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop,
                                                                      service=_service,
                                                                      device_listener=producer)
                await setupcompapi.hid_command(down=True, command=cmd)
                await setupcompapi.disconnect()
            elif press.__eq__("release"):
                setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop,
                                                                      service=_service,
                                                                      device_listener=producer)
                await setupcompapi.hid_command(down=False, command=cmd)
                await setupcompapi.disconnect()
            else:
                await setupcompapi.disconnect()
                return web.Response(
                    text=f"Device:{iddev} IPv4:{devip} HID:{cmd} Error Press:{press.title()} Cnt:{count}")
            return web.Response(text=f"Device:{iddev} IPv4:{devip} HID:{cmd} Press:{press.title()} Cnt:{count}")
        elif update_applist:
            _app_list = await pyatv.protocols.companion.CompanionApps(setupcompapi).app_list()
            _d = {}
            for _a in _app_list:
                _d.update({_a.identifier: _a.name})
            atvApps.update({f'{devip}_{iddev}': _d})
            j = json.dumps(obj=atvApps, sort_keys=True, indent=2)
            await setupcompapi.disconnect()
            return web.json_response(data=j)
        elif app:
            await setupcompapi.launch_app(app)
            await setupcompapi.disconnect()
            return web.Response(text=f"Device:{iddev} IPv4:{devip} App:{app}")
    except Exception as ex:
        return web.Response(text=f"Error Fncompremoteip:{ex} ", status=500)


@routes.get("/compremoteip/{ip}/{hid}/{press}/{cnt}")
async def compremoteip(request):
    try:
        device_ip = request.match_info["ip"]
        ipv4 = IPv4Address(device_ip)
        if ipv4 is None:
            return web.Response(text=f"Device:{ipv4} not correct!", status=500)
        hid = str(request.match_info["hid"]).lower()
        press = str(request.match_info["press"]).lower()
        cnt = str(request.match_info["cnt"]).lower()

        th = threading.Thread(target=Excompremoteip, args=(device_ip, hid, press, cnt, None, False),
                              name=f'{device_ip}_compremoteip')
        th.start()
        if ExcompremoteipReturn.__contains__(device_ip):
            resp = ExcompremoteipReturn[device_ip]
            ExcompremoteipReturn.pop(device_ip)
            return resp
        else:
            return web.Response(text=f"DeviceIPv4:{device_ip} HID:{hid} Press:{press.title()} Cnt:{cnt} previous sent!")

    except Exception as ex:
        return web.Response(text=f"Error compremoteip:{ex}", status=500)


@routes.get("/compremote/{id}/{hid}/{press}/{cnt}")
async def compremote(request):
    try:
        device_id = request.match_info["id"]
        hid = str(request.match_info["hid"]).lower()
        press = str(request.match_info["press"]).lower()
        cnt = str(request.match_info["cnt"]).lower()
        loop = asyncio.get_event_loop()
        count = 0
        config: BaseConfig
        if atvConfig.__contains__(device_id):
            config = atvConfig[device_id]
        else:
            results = await pyatv.scan(identifier=device_id, loop=loop)
            if not results:
                return web.Response(text=f"Device:{device_id} not found!", status=500)
            else:
                con = {device_id: results[0]}
                atvConfig.update(con)
            config = results[0]
            cred = {"": ""}

        cr = ""
        if atvCredentialsFile.__contains__(device_id):
            cr = atvCredentialsFile.get(device_id)
        else:
            return web.Response(text=f"Device:{device_id} has no Credentials stored!", status=500)
        config.set_credentials(protocol=Protocol.Companion, credentials=str(cr))
        setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop)

        _cmd = HidCmdCorrector(hid)

        cmd = HidCommand[hid.title()] if _cmd.__eq__("none") else HidCommand[_cmd]
        if press.__eq__("brief"):
            _cnt = int(cnt)
            count = _cnt
            if _cnt > 0:
                while _cnt > 0:
                    setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop)
                    await setupcompapi.hid_command(down=True, command=cmd)
                    await setupcompapi.hid_command(down=False, command=cmd)
                    await setupcompapi.disconnect()
                    _cnt -= 1
            else:
                return web
        elif press.__eq__("down"):
            await setupcompapi.hid_command(down=True, command=cmd)
            await setupcompapi.disconnect()
        elif press.__eq__("release"):
            await setupcompapi.hid_command(down=False, command=cmd)
            await setupcompapi.disconnect()
        else:
            return web.Response(text=f"Device:{device_id} HID:{cmd} Error Press:{press.title()} Cnt:{count}")
        return web.Response(text=f"Device:{device_id} HID:{cmd} Press:{press.title()} Cnt:{count}")
    except Exception as ex:
        return web.Response(text=f"Error compremote:{ex}", status=500)


@routes.get("/testhid/{id}")
async def testhid(request):
    try:
        device_id = request.match_info["id"]
        loop = asyncio.get_event_loop()
        pin: int
        results = await pyatv.scan(identifier=device_id, loop=loop)
        if not results:
            return web.Response(text=f"Device:{device_id} not found!", status=500)

        config = results[0]
        cred = {"": ""}
        if atvCredentialsFile.__contains__(device_id):
            cred = atvCredentialsFile.get(device_id)
        config.set_credentials(protocol=Protocol.Companion, credentials=str(cred))
        setupcompapi = pyatv.protocols.companion.CompanionAPI(config=config, loop=loop)

        while True:
            try:
                cmd = str(input())
                command: str
                if cmd.lower().__eq__("exit"):
                    break
                if cmd.__contains__(' '):
                    cmdsplit = cmd.split(' ')
                    command = cmdsplit[0]
                    hid = HidCommand[command.title()]
                    if cmdsplit[1].lower().__contains__("d"):
                        await setupcompapi.hid_command(down=True, command=hid)
                else:
                    hid = HidCommand[cmd.title()]
                    await setupcompapi.hid_command(down=True, command=hid)
                    await setupcompapi.hid_command(down=False, command=hid)
            except Exception as ex:
                print(f"Error:{ex}")

        return web.Response(text=f"Testing HID is Finished!")

    except Exception as ex:
        print()
        return web.Response(text=f"Faild to test6: {ex}", status=500)


@routes.get("/authcodeip/{ip}/{authpin}")
async def authcodeip(request):
    # Tested
    try:
        device_ip = request.match_info["ip"]
        authpin = request.match_info["authpin"]
        ipv4 = IPv4Address(device_ip)
        if ipv4 is None:
            return web.Response(text=f"Device:{ipv4} not correct!", status=500)
        iddev = ""
        for con in atvConfig:
            if atvConfig[con].address == ipv4:
                for ide in atvConfig[con].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            break
                        elif id1c == 12:
                            iddev = id1
                            break
                        continue
                    else:
                        iddev = ide

        pin = int(authpin)
        _pairing = atvpairing[iddev]
        _pairing.pin(pin)
        await _pairing.finish()
        if _pairing.has_paired:
            await _pairing.close()
            d = {iddev: _pairing.service.credentials}
            atvCredentials.update(d)
            c = atvConfig[iddev]
            c.set_credentials(protocol=Protocol.Companion, credentials=_pairing.service.credentials)
            atvConfig.update({iddev: _pairing})
            atvCredentials.update({iddev: _pairing.service.credentials})
            crj = {str(iddev): str(_pairing.service.credentials)}
            atvCredentialsFile.update(crj)
            atvremotes_config(True)
            return web.Response(
                text=f"Paired with device:{ipv4} using pin:{pin} Credentials:{_pairing.service.credentials}")
        else:
            await _pairing.close()
            return web.Response(text=f"Did not pair with device:{ipv4} with pin:{pin} yet!")

    except Exception as ex:
        return web.Response(text=f"Failed to receive the authcode: {ex}", status=500)


@routes.get("/authcode/{id}/{authpin}")
async def authcode(request):
    # Tested
    try:
        device_id = request.match_info["id"]
        authpin = request.match_info["authpin"]
        Authcodes[device_id] = authpin
        v = Authcodes.popitem()
        pin = int(v[len(v) - 1])
        atvpairing.pin(pin)
        await atvpairing.finish()
        if atvpairing.has_paired:
            await atvpairing.close()
            d = {device_id: atvpairing.service.credentials}
            atvCredentials.update(d)

            c = atvConfig.get(device_id)
            c.set_credentials(protocol=Protocol.Companion, credentials=atvpairing.service.credentials)
            newc = {device_id: c}
            atvConfig.update(newc)
            cr = {device_id: atvpairing.service.credentials}
            atvCredentials.update(cr)
            crj = {str(device_id): str(atvpairing.service.credentials)}
            atvCredentialsFile.update(crj)
            atvremotes_config(True)
            return web.Response(
                text=f"Paired with device:{device_id} using pin:{pin} Credentials:{atvpairing.service.credentials}")
        else:
            await atvpairing.close()
            return web.Response(text=f"Did not pair with device:{device_id} with pin:{pin} yet!")

    except Exception as ex:
        return web.Response(text=f"Failed to receive the authcode: {ex}", status=500)


@routes.get("/compairip/{ip}/{passw}")
async def compairip(request):
    try:
        loop = asyncio.get_event_loop()
        device_ip = request.match_info["ip"]
        passw = request.match_info["passw"]
        ipv4 = IPv4Address(device_ip)
        if ipv4 is None:
            return web.Response(text=f"Device:{ipv4} not correct!", status=500)
        if ipv4 in request.app["atv"]:
            return web.Response(text=f"Already paired to {ipv4}")

        results = await pyatv.scan(hosts=[str(ipv4)], loop=loop)
        if not results:
            return web.Response(text=f"Device:{ipv4} not found!", status=500)
        iddev = ""
        for ide in results[0].all_identifiers:
            if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                id = str(ide).replace('.', '')
                idc = str(id).__len__()
                id1 = str(ide).replace(':', '')
                id1c = str(id1).__len__()
                if idc == 12:
                    iddev = id
                    break
                elif id1c == 12:
                    iddev = id1
                    break
                continue
            else:
                iddev = ide
        # check if password protected
        man = results[0].main_service(Protocol.AirPlay)
        if man.requires_password:
            if passw is not None:
                man.password = str(passw)
        c0 = results[0]
        c0.services.append(man)
        # add_credentials(results[0], request.query)
        c = {iddev: c0}
        atvConfig.update(c)

        pairing = await pyatv.pair(c0, Protocol.Companion, loop)

        await pairing.begin()

        if pairing.device_provides_pin:
            atvpairing.update({iddev: pairing})
            return web.Response(text=f"Device:{ipv4} indicated the pin is displayed!")
        else:
            return web.Response(text=f"Device:{ipv4} didn't provide pin!")

    except Exception as ex:
        return web.Response(text=f"Failed to receive the authcode: {ex}", status=500)


@routes.get("/compair/{id}/{passw}")
async def compair(request):
    try:
        loop = asyncio.get_event_loop()
        device_id = request.match_info["id"]
        passw = request.match_info["passw"]
        if device_id in request.app["atv"]:
            return web.Response(text=f"Already paired to {device_id}")

        results = await pyatv.scan(identifier=device_id, loop=loop)
        if not results:
            return web.Response(text=f"Device:{device_id} not found!", status=500)

        # check if password protected
        man = results[0].main_service(Protocol.AirPlay)
        if man.requires_password:
            if passw is not None:
                man.password = str(passw)
        c0 = results[0]
        c0.services.append(man)
        # add_credentials(results[0], request.query)
        c = {device_id: c0}
        atvConfig.update(c)

        pairing = await pyatv.pair(c0, Protocol.Companion, loop)
        await pairing.begin()

        if pairing.device_provides_pin:
            return web.Response(text=f"Device:{device_id} indicated the pin is displayed!")
        else:
            return web.Response(text=f"Device:{device_id} didn't provide pin!")

    except Exception as ex:
        return web.Response(text=f"Failed to receive the authcode: {ex}", status=500)


@routes.get('/paircode/{ip}/{pin}')
async def paircode(request):
    try:
        ip = request.match_info['ip']
        ipv4 = IPv4Address(ip)
        iddev = ''
        v = request.match_info['pin']
        pin = int(v)
        loop = asyncio.get_event_loop()
        config: BaseConfig
        for con in atvConfigAir:
            if atvConfigAir[con].address == ipv4:
                for ide in atvConfigAir[con].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            break
                        elif id1c == 12:
                            iddev = id1
                            break
                        continue
                    else:
                        iddev = ide
        if atvConfigAir.__contains__(iddev):
            config = atvConfigAir[iddev]

        else:
            results = await pyatv.scan(identifier=iddev, loop=loop)
            if not results:
                return web.Response(text=f"Device:{iddev} IPv4:{ipv4} not found!", status=500)
            else:
                con = {iddev: results[0]}
                atvConfigAir.update(con)
            config = results[0]

        if not pairingAirplay.__contains__(iddev):
            return web.Response(text=f"Device:{iddev} IPv4:{ipv4} not ready for pairing!", status=500)
        pairingAirplay[iddev].pin(pin)
        await pairingAirplay[iddev].finish()

        if pairingAirplay[iddev].has_paired:
            d = {iddev: pairingAirplay[iddev].service.credentials}
            atvPairs.update(d)
            config.set_credentials(protocol=Protocol.AirPlay,
                                   credentials=pairingAirplay[iddev].service.credentials)
            c = {iddev: config}
            atvConfigAir.update(c)

        await pairingAirplay[iddev].close()
        e = {iddev: pairingAirplay[iddev].service.credentials}
        atvPairCredentialsFile.update(e)
        atvremotes_config(True)
        return web.Response(
            text=f'Paired with device:{iddev} IPv4:{ipv4} using pin:{pin} Credentials:{pairingAirplay[iddev].service.credentials}')
    except Exception as ex:
        return web.Response(text=f"pair Error: {ex}")


def artprocess(path: str) -> bool:
    if path is None or path.__eq__(''):
        return False
    try:
        #     "/Users/mustafaabdelazim/Documents/CodeRepo/pyatv/Artwork/art.raw"
        _path_split = path.split('/')
        totallen = len(_path_split)
        cnt = 0
        _path = ''
        file = ''
        for pa in _path_split:
            if totallen - 1 == cnt:
                file = pa
                break
            if pa.__eq__(''):
                cnt += 1
                continue

            p = f'//{pa}'
            _path = f'{_path}{p}'
            cnt += 1
        if not file.__contains__('.raw'):
            return False
        os.chdir(path=_path)
        openerror = ''
        buf = ''
        # with open(mode='w+', file=file, errors=openerror, closefd=True) as _file:
        #     buf = _file.read()
        imstream = io.BytesIO(buf)

        return True
    except Exception as ex:
        raise f'Error artprocess:{ex}'


def artrawimage(art: Optional[ArtworkInfo]) -> str:
    if art is None:
        return 'None'
    try:
        artfile = "art.raw"
        folder = 'Artwork'
        # directory = '//Users//mustafaabdelazim//Documents'
        directory = os.path.abspath(__file__)
        directory_split = str(directory).split('/')
        totallen = len(directory_split)
        _directory = ""
        cnt = 0
        for di in directory_split:
            if di.__eq__(''):
                cnt += 1
                continue
            d = str(f'//{di}')
            _directory = f'{_directory}{d}'
            cnt += 1
            if totallen - 1 == cnt:
                break
        directoryfolder = _directory + '//' + folder
        os.chdir(path=_directory)
        os.makedirs(folder, exist_ok=True)
        os.chdir(directoryfolder)
        fulldirectory = os.getcwd()
        openerror = ''
        with open(mode='w+', file=artfile, errors=openerror, closefd=True) as file:
            file.write(str(art))
            file.close()

        # imbytes = io.BytesIO(art.bytes) # testing not yet done
        # img = Image.frombytes(mode='RGB', size=(art.height, art.width), data=art.bytes, decoder_name='raw')
        # with open(mode='w+', file='img.jpg') as o_file:
        #     o_file.write(img)
        #     o_file.close()

        print(f"Raw artwork saved:{fulldirectory}\n")
        return f'{fulldirectory}/{artfile}'
    except Exception as ex:
        print(f'Error artimage:{ex}')

    # img = Image..new('PNG',(512,512))
    # pixl = img.e


def processmeta(playing: Playing, app: str, artdir: str, artworkid: str) -> list:
    try:

        _d_status = {'title': str}
        _d_status.update({'album': str, 'genre': str, 'artist': str, 'device_state': str,
                          'episode_number': str,
                          'media_type': str,
                          'position': str, 'repeat': str,
                          'season_number': str, 'series_name': str,
                          'total_time': str, 'shuffle': str, 'artwork_id': artworkid,
                          'artwork': artdir, 'app': app})
        _k_status = []
        for k in _d_status.keys():
            _k_status.append(k)
        _status = ['MetaData']
        cnt = 0
        for it in playing.title, playing.album, \
                  playing.genre, playing.artist, \
                  playing.device_state, playing.episode_number, \
                  playing.media_type, playing.position, playing.repeat, \
                  playing.season_number, playing.series_name, \
                  playing.total_time, playing.shuffle:

            if type(it) == str or type(it) == int:
                _d_status.update({_k_status[cnt]: str(it)})
            elif isinstance(it, (DeviceState, MediaType, ShuffleState, RepeatState,)):
                _d_status.update({_k_status[cnt]: it.name})
            else:
                _d_status.update({_k_status[cnt]: ''})
            cnt += 1

        _status.append(_d_status)
        return _status
    except Exception as ex:
        raise ex


@routes.get('/setserver/{devip}/{servip}/{servport}')
async def setserver(request):
    try:
        server_ip = request.match_info["servip"]
        server_port = request.match_info["servport"]
        device_ip = request.match_info["devip"]

        if ServersFile.__contains__(device_ip):
            if str(ServersFile[device_ip]).__eq__(f'{server_ip}:{server_port}'):
                return web.Response(
                    text=f'ServerIP:{server_ip} Port:{server_port} Already set for DeviceIP:{device_ip}')

        ServersFile.update({device_ip: f'{server_ip}:{server_port}'})
        atvremotes_config(True)
        return web.Response(text=f'ServerIP:{server_ip} Port:{server_port} set for DeviceIP:{device_ip}')
    except Exception as ex:
        return web.Response(text=f'Error setserver:{ex}', status=500)


def thread_checker(thread) -> any:
    if thread.is_alive():
        return False
    else:
        return True


@routes.get('/apps/{ip}/{action}')
async def apps(request):
    try:
        action = request.match_info['action']
        device_ip = request.match_info["ip"]
        update_list = True if str(action).lower().__eq__('update') else False
        ev = threading.Event()
        ThreadEvents.update({f'{device_ip}_apps': ev})
        th = threading.Thread(target=Excompremoteip, args=(device_ip, None, None, None,
                                                           action if not update_list else None
                                                           , update_list),
                              name=f'{device_ip}_apps')
        th.start()
        ev.clear()
        ev.wait(5)
        ThreadEvents.pop(f'{device_ip}_apps')

        if ExcompremoteipReturn.__contains__(device_ip):
            resp = ExcompremoteipReturn[device_ip]
            ExcompremoteipReturn.pop(device_ip)
            return resp
        else:
            return web.Response(text=f"DeviceIPv4:{device_ip} No apps collected!")


    except Exception as ex:
        return web.Response(text=f'Error apps:{ex}', status=500)


# http://127.0.0.1:11500/keyboard/192.168.31.21/0/single/?char="a"&fn=0x1e
@routes.get('/keyboard/{ip}/{passw}/{action}/')
async def keyboard(request: web.Request):
    try:
        device_ip = request.match_info['ip']
        ipv4 = IPv4Address(device_ip)
        v = request.match_info['passw']
        passw = str(v)
        iddev = ""
        data = request.rel_url.query.get('data')
        if data.__eq__('" "'):
            data = " "
        else:
            data = str(data).strip('"')
        _fn = request.rel_url.query.get('fn')
        fn = int(_fn, base=16)
        if fn > 0xff:
            return web.Response(text=f'Error fn value is bigger than 0xff', status=500)
        action: str = request.match_info['action']
        config: BaseConfig
        found = False
        loop = asyncio.get_event_loop()
        for con in atvConfigAir:
            if not (con.lower().__eq__('')):
                for ide in atvConfigAir[con].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            found = True
                            break
                        elif id1c == 12:
                            iddev = id1
                            found = True
                            break
                        continue
                    elif atvConfigAir[con].address == ipv4:
                        iddev = ide
                        if not str(iddev).__eq__(''):
                            found = True
                            break
                        else:
                            found = False
            else:
                continue
            if found:
                config = atvConfigAir[iddev]
                c = atvPairCredentialsFile[iddev]
                config.set_credentials(protocol=Protocol.AirPlay, credentials=c)
                break

        if not found:
            results = await pyatv.scan(hosts=[str(ipv4)], loop=loop)
            if not results:
                return web.Response(text=f"Device:{device_ip} not found!", status=500)
            else:
                for ide in results[0].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            break
                        elif id1c == 12:
                            iddev = id1
                            break
                        continue
                    else:
                        iddev = ide
                man = results[0]
                passneeded = False
                if passw is not None:
                    cnt = 0
                    for ser in man.services:
                        if ser.protocol == Protocol.AirPlay:
                            if ser.requires_password:  # Password on airplay still buggy, I will debug it later
                                passneeded = True
                                break
                        cnt += 1
                    if passneeded:
                        man.services[cnt].password = str(passw)
                atvConfigAir.update({iddev: man})
                config = man
                c = atvPairCredentialsFile[iddev]
                config.set_credentials(protocol=Protocol.AirPlay, credentials=c)

        cnt = 5
        while cnt > 0:
            try:
                atvlocal: AppleTV
                if atvKeyboard.__contains__(iddev):
                    atvlocal = atvKeyboard[iddev]
                    try:
                        atvlocal = await Connectatv(id=iddev, config=config, protocol=Protocol.AirPlay, loop=loop,
                                                    object='keyboard')
                    except Exception as ex:
                        if str(ex).lower().__contains__('already connected') is False:
                            if atvKeyboard.__contains__(iddev) is False:
                                print(f"Error reconnecting keyboard DevIP: {device_ip}, trying: {cnt}")
                                atvKeyboard.pop(iddev)
                                cnt -= 1
                                continue

                else:
                    try:
                        atvlocal = await Connectatv(id=iddev, config=config, protocol=Protocol.AirPlay, loop=loop,
                                                    object='keyboard')
                        if atvKeyboard.__contains__(iddev) is False:
                            print(f"Couldn't connect keyboard DevIP: {device_ip}, trying: {cnt}")
                            atvKeyboard.pop(iddev)
                            cnt -= 1
                            continue
                    except Exception as ex:
                        if str(ex).lower().__contains__('already connected') is False:
                            print(f"Error reconnecting keyboard DevIP: {device_ip}, trying: {cnt}")
                            cnt -= 1
                            continue

                _action: InputAction
                if action.lower().__eq__('single'):
                    _action = InputAction.SingleTap
                elif action.lower().__eq__('double'):
                    _action = InputAction.DoubleTap
                elif action.lower().__eq__('hold'):
                    _action = InputAction.Hold
                elif action.lower().__eq__('release'):
                    _action = InputAction.Release
                else:
                    _action = InputAction.SingleTap

                complete = await atvlocal.remote_control.set_custom(keyboard=data, action=_action, fn=fn, devid=iddev)
                # atvlocal.close()
                if cnt == 0:
                    if atvConfigAir.__contains__(iddev):
                        atvConfigAir.pop(iddev)
                    if atvKeyboard.__contains__(iddev):
                        atvKeyboard.pop(iddev)

                return web.Response(
                    text=f"{'' if complete else 'Error'}    Device:{device_ip} Keyboard data:{data} , fn={fn},"
                         f" action:{action}",
                    status=200)
            except Exception as ex:
                if str(ex).__eq__('STOPPED'):
                    if atvKeyboard.__contains__(iddev):
                        atvKeyboard.pop(iddev)
                cnt -= 1

        return web.Response(
            text=f"Error keyboard attempt:{5 - cnt}  Device:{device_ip} Keyboard data:{data} , fn={fn}, action:{action}",
            status=500)
    except Exception as ex:
        return web.Response(text=f'Error keyboard:{ex}', status=500)


#   http://127.0.0.1:11500/meta/10.10.99.142/1893/?art=true&keepalive=true
@routes.get('/meta/{ip}/{passw}/')
async def meta(request: web.Request):
    try:
        device_ip = request.match_info["ip"]
        ipv4 = IPv4Address(device_ip)
        v = request.match_info['passw']
        passw = str(v)
        art = True if str(request.rel_url.query.get('art', '')).lower().__eq__('true') else False
        keepalive = True if str(request.rel_url.query.get('keepalive', '')).lower().__eq__('true') else False
        if ipv4 is None:
            return web.Response(text=f"Device:{ipv4} not correct!", status=500)
        session = Optional[aiohttp.ClientSession]
        loop = asyncio.get_event_loop()
        needpair = False
        config: BaseConfig
        iddev = ""
        found = False

        for con in atvConfigAir:
            if not (con.lower().__eq__('')):
                for ide in atvConfigAir[con].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            found = True
                            break
                        elif id1c == 12:
                            iddev = id1
                            found = True
                            break
                        continue
                    elif atvConfigAir[con].address == ipv4:
                        iddev = ide
                        if not str(iddev).__eq__(''):
                            found = True
                            break
                        else:
                            found = False
            else:
                continue
            if found:
                config = atvConfigAir[iddev]
                needpair = True if not atvPairCredentialsFile.__contains__(iddev) else False
                break
        if not found:
            results = await pyatv.scan(hosts=[str(ipv4)], loop=loop)
            if not results:
                return web.Response(text=f"Device:{device_ip} not found!", status=500)
            else:
                for ide in results[0].all_identifiers:
                    if ide.__contains__('.') or ide.__contains__(':'):  # F0B3EC5DBF20
                        id = str(ide).replace('.', '')
                        idc = str(id).__len__()
                        id1 = str(ide).replace(':', '')
                        id1c = str(id1).__len__()
                        if idc == 12:
                            iddev = id
                            break
                        elif id1c == 12:
                            iddev = id1
                            break
                        continue
                    else:
                        iddev = ide
                man = results[0]
                passneeded = False
                if passw is not None:
                    cnt = 0
                    for ser in man.services:
                        if ser.protocol == Protocol.AirPlay:
                            if ser.requires_password:  # Password on airplay still buggy, I will debug it later
                                passneeded = True
                                break
                        cnt += 1
                    if passneeded:
                        man.services[cnt].password = str(passw)
                atvConfigAir.update({iddev: man})
                needpair = True if not atvPairCredentialsFile.__contains__(iddev) else False

        config = atvConfigAir[iddev]
        if needpair:
            session_manager = await http.create_session(session)
            loop1 = asyncio.get_event_loop()
            servrair = BaseService
            for service in config.services:
                if service.protocol == Protocol.AirPlay:
                    servrair = service
                    break
            pair1 = pyatv.protocols.airplay.get_pairing_requirement(servrair)
            if pair1 is PairingRequirement.Mandatory:
                pair2 = pyatv.protocols.airplay.pair(config, servrair, session_manager, loop1)
                await pair2.begin()
                pairingAirplay.update({iddev: pair2})
                if pair2.device_provides_pin:
                    print(f"Pairing Pin is needed for device:{iddev} IPv4:{config.address}")
                    return web.Response(text=f'Pairing Pin is needed for device:{iddev} IPv4:{config.address}',
                                        status=200)
        else:
            c = atvPairCredentialsFile[iddev]
            config.set_credentials(protocol=Protocol.AirPlay, credentials=c)

            atvlocal: AppleTV
            if atvMeta.__contains__(iddev):
                atvlocal = atvMeta[iddev]
                if not keepalive:
                    atvMeta[iddev].close()
                    atvMeta.pop(iddev)
                    atvMetaKeepalive.update({iddev: keepalive})
            else:
                # atvlocal = await pyatv.connect(config=config, loop=loop2, protocol=Protocol.AirPlay)

                loop3 = asyncio.get_event_loop()
                # future3 = loop3.create_future()
                # future3.set_result(AppleTV)
                # _atvlocal = loop3.create_task(
                #     coro=Connectatv(id=iddev, config=config, protocol=Protocol.AirPlay, loop=loop3))
                # _atvlocal = loop3.run_until_complete()
                # _atvlocal.set_result(AppleTV)
                # atvlocal = _atvlocal.result()
                # atvlocal = loop3.run_until_complete(future=_atvlocal)
                # loop3.close()
                atvlocal = await Connectatv(id=iddev, config=config, protocol=Protocol.AirPlay, loop=loop3,
                                            object='meta')
                if atvMeta.__contains__(iddev) is False:
                    return web.Response(text=f"Couldn't connect DevIP: {device_ip}")

                # if atvMeta.__contains__(iddev):
                #     atvlocal = atvMeta[iddev]

            if keepalive:
                if ServersFile.__contains__(device_ip):
                    _serv = ServersFile[device_ip]
                    _servsplit = str(_serv).split(':')
                    if len(_servsplit) < 2:
                        return web.Response(text=f'ServerIP:Port parsing failed for DeviceIP{device_ip}',
                                            status=500)
                    else:
                        serverip = _servsplit[0]
                        serverport = _servsplit[1]
                else:
                    return web.Response(
                        text=f'Keepalive={keepalive} but no given serverIP:Port for DeviceIP{device_ip}',
                        status=500)
                # _app = request.app
                _ip = str(config.address)
                atvMeta.update({iddev: atvlocal})
                loop2 = asyncio.get_event_loop()
                lis = DeviceListener(identifier=iddev, devip=_ip,
                                     servip=serverip, servport=serverport, loop=loop2)
                atvMetaListeners.update({iddev: lis})
                atvlocal.listener = lis
                atvlocal.push_updater.listener = lis
                atvlocal.push_updater.start()
                # request.app["listeners"].append(lis)
                # request.app["atv"][iddev] = atvlocal
                atvMetaKeepalive.update({iddev: keepalive})

            if atvlocal is not None:
                status = await atvlocal.metadata.playing()
                app = atvlocal.metadata.app.__str__()
                artdir = ''
                artwrodid = ''
                if art:
                    artwork = await atvlocal.metadata.artwork()
                    artwrodid = atvlocal.metadata.artwork_id
                    artdir = artrawimage(artwork)
                    # artprocess(artdir)

                if not keepalive:
                    atvlocal.close()
                    atvMetaKeepalive.update({iddev: keepalive})
                # else:
                #     atvMeta.update({iddev: atvlocal})
                _status = processmeta(status, app, artdir=artdir, artworkid=artwrodid)
                res = json.dumps(obj=_status, sort_keys=True, indent=2)
                # await atvlocal.remote_control.stop()    #this works on Airplay protocol unlike Companion
                return web.json_response(res)
                # return web.Response(text=f'ATVLocal:{atvlocal} status:{status}\n'
                #                          f'art:{artwork}\n'
                #                          f'app:{app}')
            # return web.json_response(data=status, status=200)
    except Exception as ex:
        return web.Response(text=f"Error Meta: {ex}")


@routes.get("/playing/{id}/{passw}")  # Don't use it yet, not ready!
async def playing(request):
    try:
        device_id = request.match_info["id"]
        passw = request.match_info["passw"]
        loop = asyncio.get_event_loop()
        config: BaseConfig
        if atvConfig.__contains__(device_id):
            config = atvConfig[device_id]
            cr = atvPairs[device_id]
            # config.set_credentials(protocol=Protocol.Companion, credentials=str(cr))

        else:
            results = await pyatv.scan(identifier=device_id, loop=loop)
            if not results:
                return web.Response(text=f"Device:{device_id} not found!", status=500)
            else:
                con = {device_id: results[0]}
                atvConfig.update(con)
            config = results[0]

        status = Playing
        fetched = False
        if not (atvPairs.__contains__(device_id)):

            loop1 = asyncio.get_event_loop()
            pair1 = await pyatv.pair(config=config, protocol=Protocol.Companion, loop=loop1)
            await pair1.begin()
            pin = int(input("Enter PIN: "))
            pair1.pin(pin)
            await pair1.finish()

            if pair1.has_paired:
                d = {device_id: pair1.service.credentials}
                atvPairs.update(d)
                print(f"AirPairing Device:{device_id} Credentials:{pair1.service.credentials}")
                config.set_credentials(protocol=Protocol.Companion, credentials=pair1.service.credentials)

                c = {device_id: config}
                atvConfig.update(c)
            await pair1.close()
        else:
            con = atvConfig[device_id]
            loop2 = asyncio.get_event_loop()
            atvlocal = await pyatv.connect(config=con, loop=loop2, protocol=Protocol.Companion)
            connection: Optional[HttpConnection] = None
            if atvlocal is not None:
                listener = DeviceListener(request.app, device_id)
                atvlocal.listener = listener
                atvlocal.push_updater.listener = listener
                atvlocal.push_updater.start()
                request.app["listeners"].append(listener)
                request.app["atv"][device_id] = atvlocal
                status = await atvlocal.metadata.playing()
                fetched = True


    except Exception as ex:
        return web.Response(text=f"Remote control command failed: {ex}")
    if fetched:
        return web.Response(text=str(status))
    else:
        return web.Response(text=f"Failed to fetch playing data!")


@routes.get("/ws/{id}")
@web_command
async def websocket_handler(request, pyatv):
    device_id = request.match_info["id"]
    try:

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # here tomorrow
        request.app["clients"].setdefault(device_id, []).append(ws)

        playstatus = await pyatv.metadata.playing()
        await ws.send_str(str(playstatus))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Handle custom commands from client here
                if msg.data == "close":
                    await ws.close()
            elif msg.type == WSMsgType.ERROR:
                print(f"Connection closed with exception: {ws.exception()}")

        request.app["clients"][device_id].remove(ws)

        return ws
    except Exception as ex:
        return web.Response(text=f'Error websocket_handler:{ex}')


@routes.get('/readjsonfile')
async def readjsonfile(request: web.Request):
    try:
        settingsfile = "ATVsettings.json"
        os.chdir(Listeners.atv_full_directory_remotes_config)
        fulldirectory = os.getcwd()
        if fulldirectory == Listeners.atv_full_directory_remotes_config:
            try:
                with open(file=settingsfile, mode='r+', closefd=True) as file:
                    jfile = json.load(fp=file)
                    file.close()
                return web.json_response(jfile)
            except Exception as ex:
                file.close()
        else:
            print(f'Error readjsonfile path unreadable!')
            return web.Response(text=f'Error readjsonfile path unreadable!',status=500)
    except Exception as ex:
        print(f'Error readjsonfile {ex}')
        return web.Response(text=f'Error readjsonfile {ex}', status=500)


@routes.post('/updatejsonfile')
async def updatejsonfile(request: web.Request):
    try:
        if request.can_read_body:
            jfile = await request.json()
            print(f'==============> Received json update request!<==============\n{jfile}\n'
                  f'==============> end <==============')
            di = jfile['Credentials']
            di1 = jfile['Service']
            di2 = jfile['PairCredentials']
            di3 = jfile['Servers']
            for dd in di:
                atvCredentialsFile.update(dd)
            for dd1 in di1:
                ServicePortFile.update(dd1)
            for dd2 in di2:
                atvPairCredentialsFile.update(dd2)
            for dd3 in di3:
                ServersFile.update(dd3)
            atvremotes_config(True)
            print(f'==============> Json updated! <==============')
            return web.Response(text='Json updated!', status=200)

        else:
            print(f'Update json file request has no body, json\'sn\'t updated!')
            return web.Response(text='Update json file request has no body, json\'sn\'t updated!', status=500)

    except Exception as ex:
        print(f'Error updatejsonfile {ex}')
        return web.Response(text=f'Error updatejsonfile {ex}', status=500)


def load_save_atvremotes_config(loadfilepath: str, saveonly: bool = False) -> bool:
    try:
        directory_split = str(loadfilepath).split('/')
        totallen = len(directory_split)
        _directory = ""
        cnt = 0
        for di in directory_split:
            d = str(f'//{di}')
            _directory = f'{_directory}{d}'
            cnt += 1
            if totallen - 1 == cnt:
                break

        if saveonly is False:
            shutil.copyfile(_directory, atv_full_directory_remotes_config)
        else:
            shutil.copyfile(atv_full_directory_remotes_config, _directory)

        return True
    except Exception as ex:
        print(f'Error load_save_atvremotes_config {ex}')
        return False


def atvremotes_config(updateJsononly: bool) -> None:
    # updateonly if true then data will be saved to json without creating new file if it's corrupt
    try:
        settingsfile = "ATVsettings.json"
        folder = 'ATVremotes'
        # directory = '//Users//mustafaabdelazim//Documents'
        directory = os.path.abspath(__file__)
        directory_split = str(directory).split('/')
        totallen = len(directory_split)
        _directory = ""
        cnt = 0
        for di in directory_split:
            d = str(f'//{di}')
            _directory = f'{_directory}{d}'
            cnt += 1
            if totallen - 1 == cnt:
                break
        directoryfolder = _directory + '//' + folder
        os.chdir(path=_directory)
        os.makedirs(folder, exist_ok=True)
        os.chdir(directoryfolder)
        fulldirectory = os.getcwd()
        Listeners.atv_full_directory_remotes_config = fulldirectory
        print(f"The configuration directory for ATV remotes credentials:{fulldirectory}\n")
        openerror = ""
        createnewfile = False
        if updateJsononly is False:
            try:
                with open(mode='r', file=settingsfile, errors=openerror, closefd=True) as file:
                    buf = json.load(file)
                    di = buf['Credentials']
                    di1 = buf['Service']
                    di2 = buf['PairCredentials']
                    di3 = buf['Servers']
                    for dd in di:
                        atvCredentialsFile.update(dd)
                    for dd1 in di1:
                        ServicePortFile.update(dd1)
                    for dd2 in di2:
                        atvPairCredentialsFile.update(dd2)
                    for dd3 in di3:
                        ServersFile.update(dd3)
                    print(f"ATVsettings.json exists and loaded:\n"
                          f"<><><><><><><[Authentication]><><><><><><><>")
                    print(f"Stored Credentials:{len(atvCredentialsFile) - 2}")
                    for cre in atvCredentialsFile:
                        print(f"{cre}")
                    print(f"<><><><><><><><><><><><><><><><><><><><><><>")
                    print(f"<><><><><><><><><><[Pairs]><><><><><><><><><>")
                    print(f"Stored Pairs Credentials:{len(atvPairCredentialsFile) - 2}")
                    for cre in atvPairCredentialsFile:
                        print(f"{cre}")
                    print(f"<><><><><><><><><><><><><><><><><><><><><><>")
                    print(f"<><><><><><><><><><[Servers]><><><><><><><><>")
                    print(f"Stored Devices and Servers:{len(ServersFile) - 2}")
                    for ser in ServersFile:
                        print(f"{ser} >>> {ServersFile[ser]}")
                    file.close()
                    return
            except Exception as ex:
                print(print(f"Error:atvremotes_config:open1: {ex}"))
                err = ex.args[0]
                if err == 2:
                    createnewfile = True
        else:
            try:

                di = atvCredentialsFile
                di1 = {"ServicePort": RouterPort}
                di2 = atvPairCredentialsFile
                di3 = ServersFile
                dfile = {}
                dfile["Credentials"] = []
                dfile["Credentials"].append(di)
                dfile["Service"] = []
                dfile["Service"].append(di1)
                dfile["PairCredentials"] = []
                dfile["PairCredentials"].append(di2)
                dfile['Servers'] = []
                dfile['Servers'].append(di3)
                with open(mode='w+', file=settingsfile, errors=openerror, closefd=True) as file:
                    json.dump(obj=dfile, fp=file, sort_keys=True, indent=1)
                    print(f"ATVsettings.json Updated:{file}")
                    file.close()
                    return
            except Exception as ex:
                print(print(f"Error:atvremotes_config:Updateonly: {ex}"))

        if createnewfile and updateJsononly is not True:
            try:
                di = {"ID": "Cred."}
                di1 = {"ServicePort": RouterPort}
                ServicePortFile.update(di1)
                di2 = {"ID": "PairCred."}
                di3 = {"DeviceIP": "ServerIP:Port"}
                dfile = {}
                dfile["Credentials"] = []
                dfile["Credentials"].append(di)
                dfile["Service"] = []
                dfile["Service"].append(di1)
                dfile["PairCredentials"] = []
                dfile["PairCredentials"].append(di2)
                dfile['Servers'] = []
                dfile['Servers'].append(di3)
                with open(mode='w+', file=settingsfile, errors=openerror, closefd=True) as file:
                    json.dump(obj=dfile, fp=file, sort_keys=True, indent=1)
                    print(f"ATVsettings.json isn't found Created new empty file with default service port{RouterPort}")
                    file.close()
            except Exception as ex:
                print(print(f"Error:atvremotes_config:open2: {ex}"))

    except Exception as ex:
        print(f"Error:atvremotes_config: {ex}")


async def on_shutdown(app: web.Application) -> None:
    for atv in app["atv"].values():
        atv.close()


def fetchport() -> int:
    if ServicePortFile.__contains__("ServicePort"):
        return int(ServicePortFile["ServicePort"])
    else:
        pr = {"ServicePort": RouterPort}
        ServicePortFile.update(pr)
        atvremotes_config(True)
        print(f"Serviceport not found in ATVsettings.json. Using default service port:{RouterPort} and updating json\n")
        return RouterPort


def main():
    # logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    atvremotes_config(False)
    RouterPort = fetchport()
    app = web.Application()
    app["atv"] = {}
    app["listeners"] = []
    app["clients"] = {}
    app.add_routes(routes)
    app.on_shutdown.append(on_shutdown)
    print(f'============>>> CompanionApi {version} <<<=================')
    web.run_app(app, port=RouterPort)


if __name__ == "__main__":
    main()
