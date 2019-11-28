"""
Magic parameters for the protocol that need to be followed uniformly by
participants either for functionality or for privacy reasons. Unlike
flexible server params these do need to be fixed and implicitly shared.

Any time the values are changed, the version should be bumped to avoid
having loss of function, or theoretical privacy loss.
"""

from . import pedersen

# this class doesn't get instantiated, it's just a bag of values.
class Protocol:
    VERSION = b'alpha11'
    PEDERSEN = pedersen.PedersenSetup(b'\x02CashFusion gives us fungibility.')

    # 4-byte 'lokad' identifier at start of OP_RETURN
    FUSE_ID = b'fuse'

    # The server only enforces dust limits, but clients should not make outputs
    # smaller than this.
    MIN_OUTPUT = 10000

    # Covert connection timescales
    # don't let connection attempts take longer than this, since they need to be finished early enough that a spare can be tried.
    COVERT_CONNECT_TIMEOUT = 15.0
    # What timespan to make connections over
    COVERT_CONNECT_WINDOW = 15.0
    # likewise for submitted data (which is quite small), we don't want it going too late.
    COVERT_SUBMIT_TIMEOUT = 3.0
    # What timespan to make covert submissions over.
    COVERT_SUBMIT_WINDOW = 5.0

    COVERT_CONNECT_SPARES = 6 # how many spare connections to make

    MAX_CLOCK_DISCREPANCY = 5.0 # how much the server's time is allowed to differ from client

    ### Critical timeline ###
    # (For early phases in a round)
    # For client privacy, it is critical that covert submissions happen within
    # very specific windows so that they know the server is not able to pull
    # off a strong timing partition.

    # Parameters for the 'warmup period' during which clients attempt Tor connections.
    # It is long since Tor circuits can take a while to establish.
    WARMUP_TIME = 30. # time interval between fusionbegin and first startround message.
    WARMUP_SLOP = 3.  # allowed discrepancy in warmup interval, and in clock sync.

    # T_* are client times measured from receipt of startround message.
    # TS_* are server times measured from send of startround message.

    # The server expects all commitments by this time, so it can start uploading them.
    TS_EXPECTING_COMMITMENTS = +3.0

    # when to start submitting covert components; the BlindSigResponses must have been received by this time.
    T_START_COMPS = +5.0
    # submission nominally stops at +10.0, but could be lagged if timeout and spares need to be used.

    # the server will reject all components received after this time.
    TS_EXPECTING_COVERT_COMPONENTS = +15.0

    # At this point the server needs to generate the tx template and calculate
    # all sighashes in order to prepare for receiving signatures, and then send
    # ShareCovertComponents (a large message, may need time for clients to download).

    # when to start submitting signatures; the ShareCovertComponents must be received by this time.
    T_START_SIGS = +20.0
    # submission nominally stops at +25.0, but could be lagged if timeout and spares need to be used.

    # the server will reject all signatures received after this time.
    TS_EXPECTING_COVERT_SIGNATURES = +30.0

    # At this point the server assembles the tx and tries to broadcast it.
    # It then informs clients of success or fail.

    # After submitting sigs, clients expect to hear back a result by this time.
    T_EXPECTING_CONCLUSION = 35.0

    # When to start closing covert connections if .stop() is called. It is
    # likely the server has already closed, but client needs to do this just
    # in case.
    T_START_CLOSE = +45.0 # before conclusion
    T_START_CLOSE_BLAME = +80.0 # after conclusion, during blame phase.

    ### (End critical timeline) ###


    # For non-critical messages like during blame phase, just regular relative timeouts are needed.
    # Note that when clients send a result and expect a 'gathered' response from server, they wait
    # twice this long to allow for other slow clients.
    STANDARD_TIMEOUT = 3.
    # How much extra time to allow for a peer to check blames (this may involve querying blockchain).
    BLAME_VERIFY_TIME = 5.


del pedersen
