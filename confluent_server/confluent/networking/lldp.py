# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2016, 2017 Lenovo
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This provides the implementation of locating MAC addresses on ethernet
# switches.  It is, essentially, a port of 'MacMap.pm' to confluent.
# However, there are enhancements.
# For one, each switch interrogation is handled in an eventlet 'thread'
# For another, MAC addresses are checked in the dictionary on every
# switch return, rather than waiting for all switches to check in
# (which makes it more responsive when there is a missing or bad switch)
# Also, we track the quantity, actual ifName value, and provide a mechanism
# to detect ambiguous result (e.g. if two matches are found, can log an error
# rather than doing the wrong one, complete with the detected ifName value).
# Further, the map shall be available to all facets of the codebase, not just
# the discovery process, so that the cached data maintenance will pay off
# for direct queries

# Provides support for viewing and processing lldp data for switches

if __name__ == '__main__':
    import sys
    import confluent.config.configmanager as cfm
import confluent.exceptions as exc
import confluent.log as log
import confluent.messages as msg
import confluent.snmputil as snmp
import confluent.networking.netutil as netutil
import confluent.util as util
import eventlet
from eventlet.greenpool import GreenPool
import eventlet.semaphore
import re

# The interesting OIDs are:
# 1.0.8802.1.1.2.1.3.7.1.4 - Lookup of LLDP index id to description
#                            Yet another fun fact, the LLDP port index frequent
#                            does *not* map to ifName, like a sane
#                            implementation would do.  Assume ifName equality
#                            but provide a way for 1.3.6.1.2.1.1 indicated
#                            ids to provide custom functions
#  (1.0.8802.1.1.2.1.3.7.1.2 - theoretically this process is only very useful
#                              if this is '5' meaning 'same as ifName per
#                              802.1AB-2005, however at *least* 7 has
#                              been observed to produce same results
#                              For now we'll optimistically assume
#                              equality to ifName
# 1.0.8802.1.1.2.1.4.1.1 - The information about the remote systems attached
#                            indexed by time index, local port, and an
#                            incrementing value
# 1.0.8802.1.1.2.1.4.1.1.5 - chassis id - in theory might have been useful, in
#                            practice limited as the potential to correlate
#                            to other contexts is limited.  As a result,
#                            our strategy will be to ignore this and focus
#                            instead on bridge-mib/qbridge-mib indicate data
#                            a potential exception would be pulling in things
#                            that are fundamentally network equipment,
#                            where significant ambiguity may exist.
#                            While in a 'host' scenario, there is ambiguity
#                            it is more controlled (virtual machines are given
#                            special treatment, and strategies exist for
#                            disambiguating shared management/data port, and
#                            other functions do not interact with our discovery
#                            framework
# # 1.0.8802.1.1.2.1.4.1.1.9 - SysName - could be handy hint in some scenarios
# # 1.0.8802.1.1.2.1.4.1.1.10 - SysDesc - good stuff


_neighdata = {}
_neighbypeerid = {}
_updatelocks = {}


def lenovoname(idx, desc):
    if desc.isdigit():
        return 'Ethernet' + str(idx)
    return desc

nameoverrides = [
    (re.compile('20301\..*'), lenovoname),
]


def _lldpdesc_to_ifname(switchid, idx, desc):
    for tform in nameoverrides:
        if tform[0].match(switchid):
            desc = tform[1](idx, desc)
    return desc.strip().strip('\x00')


def _dump_neighbordatum(info):
    return [msg.KeyValueData(info)]


def _extract_extended_desc(info, source, integritychecked):
    source = str(source)
    info['verified']  = bool(integritychecked)
    if source.startswith('Lenovo SMM;'):
        info['peerdescription'] = 'Lenovo SMM'
        if ';S2=' in source:
            info['peersha256fingerprint'] = source.replace('Lenovo SMM;S2=',
                                                           '')
    else:
        info['peerdescription'] = source

def sanitize(val):
    val = str(val)
    for x in val.strip('\x00'):
        if ord(x) < 32 or ord(x) > 128:
            val = ':'.join(['{0:02x}'.format(ord(x)) for x in str(val)])
            break
    return val


def _extract_neighbor_data_b(args):
    """Build LLDP data about elements connected to switch

    args are carried as a tuple, because of eventlet convenience
    """
    switch, password, user, force = args
    vintage = _neighdata.get(switch, {}).get('!!vintage', 0)
    now = util.monotonic_time()
    if vintage > (now - 60) and not force:
        return
    conn = snmp.Session(switch, password, user)
    sid = None
    lldpdata = {'!!vintage': now}
    for sysid in conn.walk('1.3.6.1.2.1.1.2'):
        sid = str(sysid[1][6:])
    idxtoifname = {}
    for oidindex in conn.walk('1.0.8802.1.1.2.1.3.7.1.4'):
        idx = oidindex[0][-1]
        idxtoifname[idx] = _lldpdesc_to_ifname(sid, idx, str(oidindex[1]))
    for remotedesc in conn.walk('1.0.8802.1.1.2.1.4.1.1.10'):
        iname = idxtoifname[remotedesc[0][-2]]
        lldpdata[iname] = {'port': iname}
        _extract_extended_desc(lldpdata[iname], remotedesc[1], user)
    for remotename in conn.walk('1.0.8802.1.1.2.1.4.1.1.9'):
        iname = idxtoifname[remotename[0][-2]]
        if iname not in lldpdata:
            lldpdata[iname] = {'port': iname}
        lldpdata[iname]['peername'] = str(remotename[1])
    for remotename in conn.walk('1.0.8802.1.1.2.1.4.1.1.7'):
        iname = idxtoifname[remotename[0][-2]]
        if iname not in lldpdata:
            lldpdata[iname] = {'port': iname}
        lldpdata[iname]['peerport'] = sanitize(remotename[1])
    for remoteid in conn.walk('1.0.8802.1.1.2.1.4.1.1.5'):
        iname = idxtoifname[remoteid[0][-2]]
        if iname not in lldpdata:
            lldpdata[iname] = {'port': iname}
        lldpdata[iname]['peerchassisid'] = sanitize(remoteid[1])
    for entry in lldpdata:
        if entry == '!!vintage':
            continue
        entry = lldpdata[entry]
        entry['switch'] = switch
        peerid = '{0}--{1}'.format(
            entry.get('peerchassisid', '').replace(':', '-'),
            entry.get('peerport', '').replace(':', '-'))
        entry['peerid'] = peerid
        _neighbypeerid[peerid] = entry
    _neighdata[switch] = lldpdata


def update_switch_data(switch, configmanager, force=False):
    switchcreds = netutil.get_switchcreds(configmanager, (switch,))[0]
    _extract_neighbor_data(switchcreds + (force,))
    return _neighdata.get(switch, {})


def update_neighbors(configmanager, force=False):
    return _update_neighbors_backend(configmanager, force)


def _update_neighbors_backend(configmanager, force):
    global _neighdata
    global _neighbypeerid
    vintage = _neighdata.get('!!vintage', 0)
    now = util.monotonic_time()
    if vintage > (now - 60) and not force:
        return
    _neighdata = {'!!vintage': now}
    _neighbypeerid = {'!!vintage': now}
    switches = list_switches(configmanager)
    switchcreds = netutil.get_switchcreds(configmanager, switches) + (force,)
    pool = GreenPool(64)
    for ans in pool.imap(_extract_neighbor_data, switchcreds):
        yield ans


def _extract_neighbor_data(args):
    # single switch neighbor data update
    switch = args[0]
    if switch not in _updatelocks:
        _updatelocks[switch] = eventlet.semaphore.Semaphore()
    if _updatelocks[switch].locked():
        while _updatelocks[switch].locked():
            eventlet.sleep(1)
        return
    try:
        with _updatelocks[switch]:
            _extract_neighbor_data_b(args)
    except Exception:
        log.logtrace()

if __name__ == '__main__':
    # a quick one-shot test, args are switch and snmpv1 string for now
    # (should do three argument form for snmpv3 test
    import sys
    _extract_neighbor_data((sys.argv[1], sys.argv[2], None, True))
    print(repr(_neighdata))


multi_selectors = set(['by-switch', 'by-peername', 'by-peerport',
                        'by-peerchassisid', 'by-port'])
single_selectors = set(['by-peerid'])

def _parameterize_path(pathcomponents):
    listrequested = False
    childcoll = True
    if len(pathcomponents) % 2 == 1:
        listrequested = pathcomponents[-1]
        pathcomponents = pathcomponents[:-1]
    pathit = iter(pathcomponents)
    keyparams = {}
    validselectors = multi_selectors | single_selectors
    for key, val in zip(pathit, pathit):
        if key not in validselectors:
            raise exc.NotFoundException('{0} is not valid here'.format(key))
        keyparams[key] = val
        validselectors.discard(key)
        if key == 'by-switch':
            validselectors.add('by-port')
        if key in single_selectors:
            childcoll = False
            validselectors = set([])
    return validselectors, keyparams, listrequested, childcoll

def list_info(parms, requestedparameter):
    #{u'by-switch': u'r8e1', u'by-port': u'e'}
    #by-peerport
    suffix = '/' if requestedparameter in multi_selectors else ''
    results = set([])
    requestedparameter = requestedparameter.replace('by-', '')
    for info in _neighbypeerid:
        info = _neighbypeerid[info]
        for mk in parms:
            mk = mk.replace('by-', '')
            if mk not in info:
                continue
            if parms['by-' + mk] != info[mk] or requestedparameter not in info:
                break
        else:
            results.add(info[requestedparameter])
    return [msg.ChildCollection(x + suffix) for x in util.natural_sort(results)]

def _handle_neighbor_query(pathcomponents, configmanager, list_switches):
    choices, parms, listrequested, childcoll = _parameterize_path(
        pathcomponents)
    if not childcoll:  # this means it's a single entry with by-peerid
        # guaranteed
        return _dump_neighbordatum(_neighbypeerid[parms['by-peerid']])
    if not listrequested:  # the query is for currently valid choices
        return [msg.ChildCollection(x + '/') for x in sorted(list(choices))]
    if listrequested not in multi_selectors | single_selectors:
        raise exc.NotFoundException('{0} is not found'.format(listrequested))
    if 'by-switch' in parms:
        update_switch_data(parms['by-switch'], configmanager)
    else:
        update_neighbors(configmanager)
    return list_info(parms, listrequested)


def _list_interfaces(switchname, configmanager):
    switchcreds = get_switchcreds(configmanager, (switchname,))
    switchcreds = switchcreds[0]
    conn = snmp.Session(*switchcreds)
    ifnames = netutil.get_portnamemap(conn)
    return util.natural_sort(ifnames.values())