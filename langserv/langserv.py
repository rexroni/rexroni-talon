"""
langserv keeps the following lists up-to-date, which can be referenced from
talon scripts.
"""

from talon import Module, Context, ui, actions, linux, actions, ui
from talon.scripting import core

import os
import typing
import json
import socket
import contextlib
import selectors
import threading
import traceback
import logging

from .. import singletons
from .. import events
from .. import speakify
from . import util

HERE = os.path.dirname(__file__)

ctx = Context()
mod = Module()

mod.list("langserv_docsym", desc="the symbols present in the open document")
ctx.lists["user.langserv_docsym"] = {}

@mod.capture(rule="{user.langserv_docsym}")
def langserv_docsym(m) -> str:
    """Returns a langserv_docsym"""
    return m.langserv_docsym


class LangServ:
    def __init__(self, conn, ctx):
        self.conn = conn
        self.ctx = ctx
        self.closed = False

        self.send_buf = b''

        self.conn.setblocking(False)
        self.ctx.register(self.conn, self._event_mask(), self)

        self.parser = util.Parser(self.handle_complete_msg)

    def _event_mask(self):
        if self.send_buf:
            return selectors.EVENT_READ | selectors.EVENT_WRITE
        return selectors.EVENT_READ

    def event(self, key, mask):
        if mask & selectors.EVENT_READ:
            msg = self.conn.recv(4096)
            if not msg:
                # Broken connection.
                self.close()
                return
            self.parser.feed(msg)

        if mask & selectors.EVENT_WRITE:
            written = self.conn.send(self.send_buf)
            if written == 0:
                # Broken connection.
                self.close()
                return
            self.send_buf = self.send_buf[written:]
            self.ctx.modify(self.conn, self._event_mask(), self)

    def queue_send(self, msg):
        # Only safe to call inside the event loop.
        self.send_buf += msg
        self.ctx.modify(self.conn, self._event_mask(), self)

    def close(self):
        if not self.closed:
            self.closed = True
            self.ctx.unregister(self.conn)
            self.conn.close()

    def handle_complete_msg(self, content, body, headers):
        parsed = json.loads(body)
        typ = headers.get("Type")
        if typ == "documentSymbol":
            syms = {}
            for item in parsed["result"]:
                sym = item["name"]
                kind = util.SymbolKind(item["kind"])
                syms.update(
                    speakify.get_pronunciations(sym)
                )
            logging.debug(sorted(set(syms.values())))
            ctx.lists["user.langserv_docsym"] = syms


class LangServPool(events.EventConsumer):
    def __init__(self):
        # open a socket in a well-known location.
        sockpath = os.path.join(HERE, "langserv.sock")
        if os.path.exists(sockpath):
            os.remove(sockpath)
        self.listener = socket.socket(family=socket.AF_UNIX)
        self.listener.bind(sockpath)
        self.listener.listen(5)
        self.listener.setblocking(False)

        # lang_servs maps connections to the LangServ that handles them
        self.lang_servs = {}

        # We get a LoopContext in started(), when we are on-thread.
        self.ctx = None

    def startup(self, ctx):
        self.ctx = ctx
        self.ctx.register(self.listener, selectors.EVENT_READ)

    def shutdown(self):
        self.ctx.unregister(self.listener)
        self.listener.close()

        # unregister and close all Zsh objects
        for ls in self.lang_servs.values():
            ls.close()

    def event(self, key, mask):
        if key.fileobj == self.listener:
            self.handle_conn()
        else:
            ls = self.lang_servs[key.fileobj]
            ls.event(key, mask)
            if ls.closed:
                del self.lang_servs[key.fileobj]

    def handle_conn(self):
        conn, _ = self.listener.accept()
        self.lang_servs[conn] = LangServ(conn, self.ctx)


@events.singleton
def lang_serv_pool():
    return LangServPool()