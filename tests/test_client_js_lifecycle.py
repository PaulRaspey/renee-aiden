"""Behavioural checks for the PWA client lifecycle.

The renee client.js handles reconnect, visibilitychange, and wakeLock.
These are notoriously easy to get wrong (thundering-herd reconnect on
unlock, lost wake-lock, double-fire on ws.onclose + visibilitychange).
We exercise the real code under Node with a hand-rolled DOM shim and
assert the observable behaviour: exactly one reconnect attempt per
user-visible trigger, and a clean wake-lock request/release pair.

If Node isn't on PATH the tests skip; the CI story can add a node step.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


CLIENT_JS = (
    Path(__file__).resolve().parent.parent / "src" / "client" / "web" / "client.js"
)


def _has_node() -> bool:
    return shutil.which("node") is not None


def _run_node(scenario: str) -> dict:
    """Run a lifecycle scenario under a DOM shim. Returns the trace as a dict."""
    if not _has_node():
        pytest.skip("node not on PATH")
    client_src = CLIENT_JS.read_text(encoding="utf-8")

    # The shim exposes just enough of the browser surface for client.js
    # to run. `scenario` is a JS snippet that drives the shim after
    # client.js has loaded and returns a trace dict via console.log(json).
    harness_prelude = textwrap.dedent(
        """
        const trace = {
          wsOpens: 0, wsCloses: 0, connectWSCalls: 0,
          wakeLockAcquired: 0, wakeLockReleased: 0,
          reconnectTimers: 0, instances: [],
        };
        const realSetTimeout = setTimeout;
        function addListener(el, evt, fn) {
          (el._listeners ||= {})[evt] = (el._listeners[evt] || []).concat(fn);
        }
        function fire(el, evt, arg) {
          for (const fn of ((el._listeners||{})[evt] || [])) {
            try { fn(arg || {}); } catch (e) { console.log("handler err:", e.stack || e); }
          }
        }
        function stubEl() {
          const el = { hidden: false, classList: { add(){}, remove(){}, toggle(){} },
                       style: {}, textContent: "", value: "",
                       setAttribute(){}, removeAttribute(){} };
          el.addEventListener = (e, f) => addListener(el, e, f);
          return el;
        }
        const els = {};
        const doc = { hidden: false };
        doc.addEventListener = (e, f) => addListener(doc, e, f);
        doc.getElementById = (id) => (els[id] ||= stubEl());
        doc.querySelector = (q) => stubEl();
        Object.defineProperty(globalThis, 'document', { value: doc, writable: true, configurable: true });
        Object.defineProperty(globalThis, 'navigator', {
          value: {
            mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop(){} }] }) },
            wakeLock: {
              request: async () => {
                trace.wakeLockAcquired++;
                const lock = { released: false,
                  release: async () => { trace.wakeLockReleased++; lock.released = true; fire(lock, "release"); },
                  addEventListener(e,f){ addListener(lock, e, f); } };
                return lock;
              }
            },
            serviceWorker: { register: async () => ({}) },
          },
          writable: true, configurable: true,
        });
        class FakeWS {
          constructor(url) {
            this.url = url;
            this.readyState = 0;
            trace.wsOpens++;
            trace.instances.push(this);
            realSetTimeout(() => {
              if (this.readyState === 0) { this.readyState = 1; this.onopen && this.onopen(); }
            }, 0);
          }
          send(){}
          close(code, reason){
            if (this.readyState === 3) return;
            this.readyState = 3;
            trace.wsCloses++;
            this.onclose && this.onclose({ code, reason });
          }
        }
        FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
        global.WebSocket = FakeWS;
        const winObj = {
          location: { protocol: "http:", host: "127.0.0.1:8766" },
          AudioContext: class {
            constructor() { this.state = "running"; this.sampleRate = 48000; this.currentTime = 0; this.destination = {}; this.audioWorklet = { addModule: async () => {} }; }
            resume(){ return Promise.resolve(); }
            createMediaStreamSource(){ return { connect: (x) => x }; }
            createBufferSource(){ return { connect(){}, start(){} }; }
            createGain(){ return { gain: { value: 0 }, connect: (x) => x }; }
            createBuffer(){ return { copyToChannel(){} }; }
          },
          _handlers: {},
          addEventListener(e, f){ (winObj._handlers[e] ||= []).push(f); },
        };
        Object.defineProperty(globalThis, 'window', { value: winObj, writable: true, configurable: true });
        global.URL = { createObjectURL: () => "blob:", revokeObjectURL: () => null };
        global.Blob = class { constructor(){} };
        global.AudioWorkletNode = class {
          constructor(){ this.port = { onmessage: null, postMessage(){} }; this.connect = (x) => x; }
        };
        Object.defineProperty(globalThis, 'performance', { value: { now: () => Date.now() }, writable: true, configurable: true });
        global.setTimeout = (fn, ms) => {
          if (ms >= 500) trace.reconnectTimers++;
          return realSetTimeout(fn, 0);
        };
        process.on('unhandledRejection', e => console.log("UNHANDLED:", e?.stack || e));
        process.on('uncaughtException', e => console.log("UNCAUGHT:", e?.stack || e));
        function firePageHide(){ for (const f of (winObj._handlers['pagehide']||[])) f({}); }
        """
    )
    harness_coda = textwrap.dedent(
        """
        await new Promise(r => realSetTimeout(r, 100));
        console.log(JSON.stringify(trace));
        """
    )
    wrapped = (
        "(async () => {\n"
        + harness_prelude
        + "\n" + client_src + "\n"
        + scenario + "\n"
        + harness_coda
        + "\n})();"
    )

    result = subprocess.run(
        ["node", "-e", wrapped], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"node stderr:\n{result.stderr}\n---\n{result.stdout}"
    # The trace is the last JSON line printed.
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise AssertionError("no trace JSON printed by node harness")


START = (
    "fire(document.getElementById('start'), 'click');\n"
    "await new Promise(r => realSetTimeout(r, 30));\n"
)


def test_start_session_opens_ws_once_and_acquires_wake_lock():
    trace = _run_node(START)
    assert trace["wsOpens"] == 1, trace
    assert trace["wakeLockAcquired"] == 1, trace


def test_visibilitychange_with_live_ws_does_not_double_connect():
    trace = _run_node(
        START
        + "fire(document, 'visibilitychange');\n"
        + "await new Promise(r => realSetTimeout(r, 50));\n"
    )
    assert trace["wsOpens"] == 1, trace


def test_close_plus_visibilitychange_fires_exactly_one_reconnect():
    """OS-initiated ws close THEN a visibilitychange (user unlocked) must
    lead to exactly one new connection, not two (close path +
    visibilitychange path racing)."""
    trace = _run_node(
        START
        + "const ws = trace.instances[trace.instances.length - 1];\n"
        # Fire the close event as if the OS dropped the connection; client.js
        # treats onclose as a schedule-reconnect trigger.
        + "ws.readyState = 3;\n"
        + "ws.onclose && ws.onclose({ code: 1006, reason: 'abnormal' });\n"
        + "fire(document, 'visibilitychange');\n"
        + "await new Promise(r => realSetTimeout(r, 1500));\n"
    )
    assert trace["wsOpens"] == 2, trace


def test_pagehide_releases_wake_lock():
    trace = _run_node(
        START
        + "firePageHide();\n"
        + "await new Promise(r => realSetTimeout(r, 50));\n"
    )
    assert trace["wakeLockAcquired"] == 1, trace
    assert trace["wakeLockReleased"] == 1, trace