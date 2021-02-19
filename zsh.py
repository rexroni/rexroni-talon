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

from . import singletons
from . import speakify
from . import events

HERE = os.path.dirname(__file__)

ctx = Context()
mod = Module()

mod.apps.zsh = "title: /^zsh:/"
ctx.matches = r"""
app: zsh
"""

mod.list("zsh_completion", desc="zsh completions")
ctx.lists["user.zsh_completion"] = {}

mod.list("shell_command", desc="user shell commands")
ctx.lists["user.shell_command"] = {
    "less": "less",
    "ls": "ls",
    "cd": "cd",
    "vim": "vim",
    "git": "git",
    "make": "make",
    "ninja": "ninja",
    "sed": "sed",
    "g": "g",
    "grep": "grep",
    "excel": "xsel",
    "rm": "rm",
    "make dir": "mkdir",
    "remove dir": "rmdir",
    "move": "mv",
}

@mod.capture(rule="{user.shell_command}")
def shell_command(m) -> str:
    """Returns a shell command"""
    return m.shell_command

@mod.capture(rule="{user.zsh_completion}")
def zsh_completion(m) -> str:
    """Returns a zsh_completion"""
    if not m.zsh_completion.startswith("{"):
        # unambiguous result
        alt_sym = []
        return m.zsh_completion

    edit = speakify.Edit(**json.loads(m.zsh_completion))

    # Prefer FULL to NOPREFIX to SHORTHAND to SHORTHAND_NOPREFX.
    options = list(edit.results.keys())
    options.sort(key=lambda x: (edit.results[x][0], x != x.lower()))

    # Take the source text that generated the first option.
    kind, src = edit.results[options[0]]

    # Hope tab completion is helpful.
    return src[len(edit.prefix):] + "\t"


_typed_special = False
_typed_anything = False

@contextlib.contextmanager
def maybe_trigger_completions():
    """
    After running any command, retrigger the completion calculation,
    unless you typed enter at any point.
    """
    global _typed_special
    global _typed_anything
    # print('triggering?')
    _typed_special = False
    _typed_anything = False
    yield
    should_trigger = _typed_anything and not _typed_special
    if should_trigger:
        actions.key('ctrl-t')
    # print('triggered!' if should_triger else 'not triggered!')


@ctx.action_class("core")
class core_action:
    def run_phrase(phrase: core.Capture):
        with maybe_trigger_completions():
            core.CoreActions.run_phrase(phrase)


@ctx.action_class("main")
class main_action:
    def key(key: str):
        global _typed_special
        global _typed_anything
        if key == 'enter' or ('-' in key and key != '-'):
            # Don't trigger extra key presses on special keys.
            _typed_special = True
        _typed_anything = True

        # print('key:', key)

        # actions.next() is how you reference the action you are overriding.
        actions.next(key)


class Zsh:
    def __init__(self, pid, ctx):
        self.pid = pid
        self.ctx = ctx
        self.sock = socket.socket(family=socket.AF_UNIX)
        self.recvd = []
        self.recvd_buf = b''
        self.send_buf = b''
        self.completions = {}
        self.closed = False

        # print(f'new Zsh({pid})!')

        # There is a race condition where this hangs, unfortunately.
        # If the zsh line editor is not active, the zsh completion server
        # cannot accept connections and incoming connections can hang.
        # The good news is that it seems the first two connections do not hang
        # and we should only ever make one connection to each zsh server.
        try:
            self.sock.connect(f"{HERE}/zsh-completion-server/sock/{pid}.sock")
        except Exception as e:
            self.sock.close()
            logging.warning('connection failed:', e)
            raise

        self.sock.setblocking(False)
        self.ctx.register(self.sock, self._event_mask(), self)

    def _event_mask(self):
        if self.send_buf:
            return selectors.EVENT_READ | selectors.EVENT_WRITE
        return selectors.EVENT_READ

    def queue_send(self, msg):
        # Only safe to call inside the event loop.
        self.send_buf += msg
        self.ctx.modify(self.sock, self._event_mask(), self)

    def event(self, readable, writable):
        if readable:
            msg = self.sock.recv(4096)
            if not msg:
                # Broken connection.
                self.close()
                return
            self.recvd_buf += msg
            last_newline = self.recvd_buf.rfind(b'\n')
            if last_newline != -1:
                # commit complete lines to recvd
                self.recvd.extend(self.recvd_buf[:last_newline].split(b'\n'))
                self.recvd_buf = self.recvd_buf[last_newline + 1:]
                self.check_cmd()

        if writable:
            written = self.sock.send(self.send_buf)
            if written == 0:
                # Broken connection.
                self.close()
                return
            self.send_buf = self.send_buf[written:]
            self.ctx.modify(self.sock, self._event_mask(), self)

    def check_cmd(self):
        if not self.recvd:
            return
        # cmd will be like 'CMD:ARG:ARG'
        cmd = self.recvd[0].split(b':')
        # Right now we only allow one command.
        assert cmd[0] == b'completions', f'command {cmd} not valid'
        try:
            end = self.recvd.index(b"::done::")
        except ValueError:
            return
        assert len(self.recvd) >= 4, "not enough lines in response"
        context = self.recvd[1]
        prefix = self.recvd[2]
        raw_completions = self.recvd[3:end]
        self.recvd = self.recvd[end+1:]
        self.handle_completions(context, prefix, raw_completions)

    def handle_completions(self, context, prefix, raw_completions):
        # For blank commands, we use a custom command list for completion and
        # ignore the huge list from zsh.

        if prefix == b'' and context in [
            b":complete:-command-:",
            b":complete:-sudo-:",
            b":complete:-env-:",
        ]:
            raw_completions = []

        speakifier = speakify.Speakifier(prefix.decode("utf8"))
        for symbol in raw_completions:
            speakifier.add_symbol(symbol.decode("utf8"))
        completions = speakifier.get_talon_list()

        # cache these completions for later
        self.completions = completions
        # if we are active, update the list of zsh_completions
        if self.is_active_window():
            logging.debug(completions)
            ctx.lists["user.zsh_completion"] = completions

    def is_active_window(self):
        window = ui.active_window()
        if not window.title.startswith("zsh:"):
            return False
        active_pid = int(window.title.split(':')[1])
        return active_pid == self.pid

    def close(self):
        if not self.closed:
            self.closed = True
            self.ctx.unregister(self.sock)
            self.sock.close()


class ZshPool(events.EventConsumer):
    def __init__(self):
        # shells maps pids to Zsh objects.
        self.shells = {}

        self.ctx = None

    def startup(self, ctx):
        self.ctx = ctx

    def shutdown(self):
        # unregister and close all Zsh objects
        for zsh in self.shells.values():
            zsh.close()

    def event(self, key, mask):
        readable = mask & selectors.EVENT_READ
        writable = mask & selectors.EVENT_WRITE
        zsh = key.data
        zsh.event(readable, writable)
        if zsh.closed:
            del self.shells[zsh.pid]

    def notify(self, msg):
        # we only have one type of message
        self._trigger_pid(msg)

    def _trigger_pid(self, pid):
        if pid not in self.shells:
            # detected new shell
            try:
                zsh = Zsh(pid, self.ctx)
            except:
                return
            self.shells[pid] = zsh
        zsh = self.shells[pid]
        zsh.queue_send(b'trigger\n')

    def trigger(self, pid):
        """trigger() may be called from off-thread"""
        if self.ctx is not None:
            self.ctx.notify_me(pid)


@events.singleton
def zsh_pool():
    return ZshPool()


class ZshTriggerWatch:
    """
    Whenever a new zsh window is selected, reach out to the zsh completion
    server and have it tell us what its completion list is.
    """
    def win_focus(window):
        if window.title.startswith("zsh:"):
            pid = int(window.title.split(':')[1])
            zsh_pool.trigger(pid)

    def win_title(window):
        if window == ui.active_window():
            if window.title.startswith("zsh:"):
                pid = int(window.title.split(':')[1])
                zsh_pool.trigger(pid)

    ui.register('win_focus', win_focus)
    ui.register('win_title', win_title)
