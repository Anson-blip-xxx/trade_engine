"""V2 tests can run independently of legacy secret-file-dependent fixtures.

Keep the network boundary even with --confcutdir=tests/v2_core. Isolated QA
databases use dedicated Unix sockets; no test may contact a venue or live TCP DB.
"""

import socket
import sys


def _reject_external_network(event, args):
    if event in {"socket.connect", "socket.sendto"}:
        if args[0].family in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeError("external network disabled during V2 tests")


sys.addaudithook(_reject_external_network)
