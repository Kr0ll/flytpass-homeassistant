"""Standalone test of the Flyt client (no Home Assistant needed).

    pip install aiohttp
    python test_flyt.py

Asks for e-mail and password, logs in, and prints tags and the latest passings.
"""

import asyncio
import getpass
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# Load custom_components/flyt/api.py without running the HA __init__.py
_pkg = types.ModuleType("flyt")
_pkg.__path__ = [os.path.join(os.path.dirname(__file__), "custom_components", "flyt")]
sys.modules["flyt"] = _pkg

import aiohttp  # noqa: E402

from flyt.api import FlytClient  # noqa: E402
from flyt.locations import fetch_index, locate  # noqa: E402


async def main() -> None:
    email = input("E-post: ").strip()
    password = getpass.getpass("Passord: ")
    async with aiohttp.ClientSession() as session:
        client = FlytClient(session, email, password)
        await client.login()
        print("Innlogging OK\n")
        obes = []
        for customer in await client.get_customers():
            print(f"Kunde {customer.get('customerNumber')}: {customer.get('name')}")
            for agr in await client.get_agreements(customer["id"]):
                print(f"  Avtale {agr.get('agreementNumber')} ({agr.get('status')}), "
                      f"saldo {agr.get('accountBalance')} {agr.get('accountCurrency')}")
                for obe in agr.get("obes") or []:
                    v = obe.get("vehicle") or {}
                    print(f"    Brikke {obe['obeNumber']} {obe.get('obeStatus'):8} "
                          f"{v.get('registrationNumber') or '-':8} "
                          f"{v.get('brand') or ''} {v.get('model') or ''}")
                    obes.append(obe["obeNumber"])
        since = datetime.now(timezone.utc) - timedelta(days=30)
        passings = await client.get_passings(obes, date_from=since)
        index = await fetch_index(session)
        print(f"\n{len(passings)} passeringer siste 30 dager. Siste 15:")
        for p in passings[:15]:
            kind, loc = locate(index, p)
            pos = f"{loc.latitude:.4f},{loc.longitude:.4f}" if loc else "ingen posisjon"
            print(f"  {p.get('transactedOn')}  {p.get('licensePlateDetected') or '':8} "
                  f"{p.get('totalPrice'):>7} {p.get('currency')}  {kind:5}  "
                  f"{p.get('exitTollStationName')}  [{pos}]")


asyncio.run(main())
