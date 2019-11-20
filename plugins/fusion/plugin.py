"""
Base plugin (non-GUI)
"""
import weakref
import threading
import time
from collections import defaultdict

from electroncash.plugins import BasePlugin, hook, daemon_command
from electroncash.i18n import _, ngettext, pgettext
from electroncash.util import profiler, PrintError, InvalidPassword

from .fusion import Fusion, can_fuse_from, can_fuse_to, is_tor_port
from .server import FusionServer, Params
from .covert import limiter

import random # only used to select random coins

DEFAULT_TOR_HOST = 'localhost'
DEFAULT_TOR_PORT = 9050
TOR_PORTS = [9050, 9150]
server_list = [  # first one is the default
    ('89.40.7.97', 8787, False),
    ('server2.example.com', 3436, True),
    ]
NUM_SIMULTANEOUS_AUTO_FUSIONS = 2
AUTOFUSE_RECENT_TOR_LIMIT_LOWER = 40  # if more than <N> tor connections have been made recently (see covert.py) then don't start auto-fuses.
AUTOFUSE_RECENT_TOR_LIMIT_UPPER = 60  # if more than <N> tor connections have been made recently (see covert.py) then shut down auto-fuses that aren't yet started

pnp = None
def get_upnp():
    """ return an initialized UPnP singleton """
    global pnp
    if pnp is not None:
        return pnp
    try:
        import miniupnpc
    except ImportError:
        raise RuntimeError("python miniupnpc module not installed")
    u = miniupnpc.UPnP()
    if u.discover() < 1:
        raise RuntimeError("can't find UPnP server")
    try:
        u.selectigd()
    except Exception as e:
        raise RuntimeError("failed to connect to UPnP IGD")
    pnp = u
    return u

def select_random_coins(wallet, fraction, max_coins, keep_linked_probability = 0.1):
    """
    Grab wallet coins with a certain probability, while also paying attention
    to obvious linkages and possible linkages.
    Returns list of list of coins (bucketed by obvious linkage).
    """

    # TODO: include unconfirmed/unmatured coins here and exclude the entire
    # bucket if it contains these (better to wait for fusion).
    coins = wallet.get_utxos(domain=None, exclude_frozen=True, mature=True, confirmed_only=True, exclude_slp=True)
    if not coins:
        return ()

    # First, we want to bucket coins together when they have obvious linkage.
    # Coins that are linked together should be spent together.
    # Currently, just look at address.
    addr_coins = defaultdict(list)
    for c in coins:
        addr_coins[c['address']].append(c)
    addr_coins = list(addr_coins.items())
    random.shuffle(addr_coins)

    # While fusing we want to pay attention to semi-correlations among coins.
    # When we fuse semi-linked coins, it increases the linkage. So we try to
    # avoid doing that (but rarely, we just do it anyway :D).
    # Currently, we just look at all txids touched by the address.
    # (TODO this is a disruption vector: someone can spam multiple fusions'
    #  output addrs with massive dust transactions (2900 outputs in 100 kB)
    #  that make the plugin think that all those addresses are linked.)
    result_txids = set()

    result = []
    num_coins = 0
    for addr, acoins in addr_coins:
        if len(acoins) > 3:
            # skip addresses with too many coins, since they take up lots of 'space' for consolidation.
            # TODO: again there is possibility of disruption here. Need to deal
            # with 'dusty' addresses by ignoring / consolidating dusty coins.
            acoins.clear()
        if num_coins + len(acoins) > max_coins:
            # we don't keep trying other buckets even though others might put us at max_coins exactly
            break
        if random.random() > fraction:
            continue

        # Wemi-linkage check:
        # We consider all txids involving the address, historical and current.
        ctxids = {txid for txid, height in wallet.get_address_history(addr)}
        collisions = ctxids.intersection(result_txids)
        # Note each collision gives a separate chance of discarding this bucket.
        if random.random() > keep_linked_probability**len(collisions):
            continue
        # OK, no problems: let's include this bucket.
        num_coins += len(acoins)
        result.append(acoins)
        result_txids.update(ctxids)

    return result


class FusionPlugin(BasePlugin):
    testserver = None
    active = True
    _run_iter = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs) # gives us self.config
        self.fusions = weakref.WeakKeyDictionary()
        # Do an initial check on the tor port
        t = threading.Thread(name = 'Fusion-scan_torport_initial', target = self.scan_torport)
        t.start()
        self.scan_torport_thread = weakref.ref(t)
        self.autofusing_wallets = weakref.WeakKeyDictionary()  # wallet -> password
        self.lock = threading.RLock() # always order: plugin.lock -> wallet.lock -> fusion.lock

    def on_close(self,):
        self.stop_testserver()

    def fullname(self):
        return 'CashFusion'

    def description(self):
        return _("CashFusion Protocol")

    def get_server(self, ):
        return tuple(self.config.get('cashfusion_server', server_list[0]))

    def set_server(self, host, port, ssl):
        assert isinstance(host, str)
        assert isinstance(port, int)
        assert isinstance(ssl, bool)
        self.config.set_key('cashfusion_server', (host, port, ssl))

    def get_torhost(self):
        if self.has_auto_torport():
            return DEFAULT_TOR_HOST
        else:
            return self.config.get('cashfusion_tor_host', DEFAULT_TOR_HOST)

    def set_torhost(self, host):
        # host should be a valid hostname
        self.config.set_key('cashfusion_tor_host', host)

    def has_auto_torport(self, ):
        return self.config.get('cashfusion_tor_port_auto', True)

    def get_torport(self, ):
        ''' Retreive either manual port or autodetected port; may return None
        if 'auto' mode and no Tor port has been autodetected. (this is non-blocking) '''
        if self.has_auto_torport():
            return self.tor_port_good
        else:
            return self.config.get('cashfusion_tor_port_manual', DEFAULT_TOR_PORT)

    def set_torport(self, port):
        # port may be 'auto' or 'manual' or an int
        if port == 'auto':
            self.config.set_key('cashfusion_tor_port_auto', True)
            return
        else:
            self.config.set_key('cashfusion_tor_port_auto', False)
        if port == 'manual':
            return # we're simply going to use whatever manual port was already set
        assert isinstance(port, int)
        self.config.set_key('cashfusion_tor_port_manual', port)

    def scan_torport(self, ):
        ''' Scan for Tor proxy on either the manual port or on a series of
        automatic ports. This is blocking. Returns port if it's up, or None if
        down / can't find. '''
        host = self.get_torhost()

        if self.has_auto_torport():
            portlist = TOR_PORTS
        else:
            portlist = [self.config.get('cashfusion_tor_port_manual', DEFAULT_TOR_PORT)]

        for port in portlist:
            if is_tor_port(host, port):
                self.tor_port_good = port
                break
        else:
            self.tor_port_good = None
        return self.tor_port_good

    def disable_autofusing(self, wallet):
        self.autofusing_wallets.pop(wallet, None)
        wallet.storage.put('cashfusion_autofuse', False)
        running = []
        for f in wallet._fusions_auto:
            f.stop('Autofusing disabled', not_if_running = True)
            if f.status[0] == 'running':
                running.append(f)
        return running

    def enable_autofusing(self, wallet, password):
        if password is None and wallet.has_password():
            raise InvalidPassword
        else:
            wallet.check_password(password)
        self.autofusing_wallets[wallet] = password
        wallet.storage.put('cashfusion_autofuse', True)

    def is_autofusing(self, wallet):
        return (wallet in self.autofusing_wallets)

    def add_wallet(self, wallet, password=None):
        ''' Attach the given wallet to fusion plugin, allowing it to be used in
        fusions with clean shutdown. Also start auto-fusions for wallets that want
        it (if no password).
        '''
        # all fusions relating to this wallet, in particular the fuse-from type (which have frozen coins!)
        wallet._fusions = weakref.WeakSet()
        # fusions that were auto-started.
        wallet._fusions_auto = weakref.WeakSet()

        if wallet.storage.get('cashfusion_autofuse', False):
            try:
                self.enable_autofusing(wallet, password)
            except InvalidPassword:
                self.disable_autofusing(wallet)

    def remove_wallet(self, wallet):
        ''' Detach the provided wallet; returns list of active fusions. '''
        with self.lock:
            self.autofusing_wallets.pop(wallet, None)
        with wallet.lock:
            fusions = tuple(getattr(wallet, '_fusions', ()))
            try:
                del wallet._fusions
            except AttributeError:
                pass
        return [f for f in fusions if f.status[0] not in ('complete', 'failed')]


    def start_fusion(self, source_wallet, password, coins, target_wallet = None):
        # Should be called with plugin.lock and wallet.lock
        if target_wallet is None:
            target_wallet = source_wallet # self-fuse
        assert can_fuse_from(source_wallet)
        assert can_fuse_to(target_wallet)
        host, port, ssl = self.get_server()
        if host == 'localhost':
            # as a special exemption for the local test server, we don't use Tor.
            torhost = None
            torport = None
        else:
            torhost = self.get_torhost()
            torport = self.get_torport()
            if torport is None:
                torport = self.scan_torport() # may block for a very short time ...
            if torport is None:
                raise RuntimeError("can't find tor port")
        fusion = Fusion(target_wallet, host, port, ssl, torhost, torport)
        target_wallet._fusions.add(fusion)
        source_wallet._fusions.add(fusion)
        fusion.add_coins_from_wallet(source_wallet, password, coins)
        fusion.start()
        self.fusions[fusion] = time.time()
        return fusion


    def thread_jobs(self, ):
        return [self]
    def run(self, ):
        # this gets called roughly every 0.1 s in the Plugins thread; downclock it to 1 s.
        run_iter = self._run_iter + 1
        if run_iter < 10:
            self._run_iter = run_iter
            return
        else:
            self._run_iter = 0

        with self.lock:
            if not self.active:
                return
            torcount = limiter.count
            if torcount > AUTOFUSE_RECENT_TOR_LIMIT_UPPER:
                # need tor cooldown, stop the waiting fusions
                for wallet, password in tuple(self.autofusing_wallets.items()):
                    with wallet.lock:
                        autofusions = set(wallet._fusions_auto)
                        for f in autofusions:
                            if f.status[0] in ('complete', 'failed'):
                                wallet._fusions_auto.discard(f)
                                continue
                            if not f.stopping:
                                f.stop('Tor cooldown', not_if_running = True)

            if torcount > AUTOFUSE_RECENT_TOR_LIMIT_LOWER:
                return
            for wallet, password in tuple(self.autofusing_wallets.items()):
                num_auto = 0
                with wallet.lock:
                    autofusions = set(wallet._fusions_auto)
                    for f in autofusions:
                        if f.status[0] in ('complete', 'failed'):
                            wallet._fusions_auto.discard(f)
                        else:
                            num_auto += 1
                    if num_auto < NUM_SIMULTANEOUS_AUTO_FUSIONS:
                        # we don't have enough auto-fusions running, so start one
                        coins = [c for l in select_random_coins(wallet, 0.1, 20) for c in l]
                        if not coins:
                            self.print_error("auto-fusion skipped due to lack of coins")
                            continue
                        try:
                            f = self.start_fusion(wallet, password, coins)
                            self.print_error("started auto-fusion")
                        except RuntimeError as e:
                            self.print_error(f"auto-fusion skipped due to error: {e}")
                            return
                        wallet._fusions_auto.add(f)

    def start_testserver(self, network, bindhost, port, upnp = None):
        if self.testserver:
            raise RuntimeError("server already running")
        self.testserver = FusionServer(self.config, network, bindhost, port, upnp = upnp)
        self.testserver.start()
        return self.testserver.host, self.testserver.port

    def stop_testserver(self):
        try:
            self.testserver.stop('server stopped by operator')
            self.testserver = None
        except Exception:
            pass

    @daemon_command
    def fusion_test_server_start(self, daemon, config):
        # Usage:
        #   ./electron-cash daemon fusion_test_server_start <bindhost> <port>
        #   ./electron-cash daemon fusion_test_server_start <bindhost> <port> upnp
        network = daemon.network
        if not network:
            return "error: cannot run test server without electrumx connection"
        def invoke(bindhost = '0.0.0.0', sport='8787', upnp_str = None):
            port = int(sport)
            pnp = get_upnp() if upnp_str == 'upnp' else None
            return self.start_testserver(network, bindhost, port, upnp = pnp)

        try:
            host, port = invoke(*config.get('subargs', ()))
        except Exception as e:
            import traceback, sys;  traceback.print_exc(file=sys.stderr)
            return f'error: {str(e)}'
        return (host, port)

    @daemon_command
    def fusion_test_server_stop(self, daemon, config):
        self.stop_testserver()
        return 'ok'

    @daemon_command
    def fusion_test_server_status(self, daemon, config):
        if not self.testserver:
            return "test server not running"
        return dict(poolsizes = {t: len(pool.pool) for t,pool in self.testserver.waiting_pools.items()})

    @daemon_command
    def fusion_test_server_fuse(self, daemon, config):
        if self.testserver is None:
            return
        subargs = config.get('subargs', ())
        if len(subargs) != 1:
            return "expecting tier"
        tier = int(subargs[0])
        num_clients = self.testserver.start_fuse(tier)
        return num_clients