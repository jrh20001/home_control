#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""x10_mochd_relay_asyncio2.py

monitor X10 CM15 USB receiver for received commands using mochad socket.
for somespecific received commands, transmit a new X10 CM15 command to
different house address

specifically: for Bert alert, translate C5 to A
"""

import asyncio, socket

host = 'hcontrol.local'
port = 1099

#trig = 'C1'
#mess = b'rf c1 on\n\n'  # c1 is bert alert

trig = 'A13'
mess = b'rf a5 off\n\n'

async def recv_data(loop, soc):
    while True:
        resp = (await loop.sock_recv(soc, 2048)).decode()
        mdir = resp[15:17]
        mtyp = resp[32:35]
        if (mdir.lower() =='rx')  and (mtyp.lower() == trig):
            await loop.create_task(send_data(loop, soc))
        print(mdir, resp.split('\n')[0])

async def send_data(loop, soc):
    await loop.sock_sendall(soc, mess)

async def main():
    soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    soc.connect((host, port))
    soc.setblocking(False)
    loop = asyncio.get_event_loop()
    task = loop.create_task(recv_data(loop, soc))
    await asyncio.gather(task)

asyncio.run(main())

