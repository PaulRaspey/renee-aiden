/* Renee mobile PWA client.
 *
 * Captures microphone as float32, resamples to 48000Hz if the device
 * AudioContext pins to a different rate (common on iOS), converts to
 * int16 little-endian, and streams the bytes over a WebSocket. Inbound
 * binary frames are int16 PCM at 48000 that we schedule back through
 * the same AudioContext; inbound text frames are transcript JSON.
 *
 * Lifecycle rules:
 *   1. AudioContext may not leave the tap-to-start overlay until its
 *      .state === "running" after a user gesture.
 *   2. Exactly one reconnect task at a time: neither a visibilitychange
 *      nor a ws.onclose may schedule a second if one is already pending.
 *   3. The screen wake-lock is acquired only inside unlockAndStart and
 *      released from stopSession / pagehide.
 */
(() => {
  "use strict";
  const SAMPLE_RATE = 48000;
  const WS_PATH = "/ws";

  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const overlay = document.getElementById("overlay");
  const overlayErr = document.getElementById("overlay-err");
  const startBtn = document.getElementById("start");
  const orb = document.getElementById("orb");
  const orbLabel = document.getElementById("orb-label");
  const youText = document.querySelector("#line-you .text");
  const reneeText = document.querySelector("#line-renee .text");
  const youLine = document.getElementById("line-you");
  const reneeLine = document.getElementById("line-renee");
  const ptt = document.getElementById("ptt");
  const certOverlay = document.getElementById("cert-overlay");
  const certRetryBtn = document.getElementById("cert-retry");

  // Threshold for showing the cert-trust overlay (#6). On HTTPS pages a
  // self-signed cert that wasn't installed at the device-trust level will
  // fail the WSS handshake silently — the WS just keeps closing without
  // ever opening. After this many close-without-open events we assume cert
  // trouble and surface the install walkthrough. HTTP pages skip this
  // entirely (no cert to install).
  const CERT_OVERLAY_FAILURE_THRESHOLD = 2;

  const session = {
    ws: null,
    audioCtx: null,
    micStream: null,
    micNode: null,
    captureMuted: false,
    pttEnabled: false,
    reconnectTimer: null,
    reconnectDelay: 1000,
    nextPlayTime: 0,
    lastAudioAt: 0,
    speakingTimer: null,
    wakeLock: null,
    sampleRate: 0,
    isStarted: false,
    // Cert-overlay state (#6): count consecutive WS closes that never reached open.
    // Reset to 0 the first time onopen fires.
    wsFailuresBeforeFirstOpen: 0,
    wsHasEverOpened: false,
  };

  // Cert overlay wiring (#6) — only meaningful on HTTPS pages
  if (certRetryBtn) {
    certRetryBtn.addEventListener("click", () => window.location.reload());
  }
  function maybeShowCertOverlay() {
    if (!certOverlay) return;
    if (window.location.protocol !== "https:") return; // HTTP has no cert to trust
    if (session.wsHasEverOpened) return;
    if (session.wsFailuresBeforeFirstOpen < CERT_OVERLAY_FAILURE_THRESHOLD) return;
    certOverlay.hidden = false;
  }
  function hideCertOverlay() {
    if (certOverlay && !certOverlay.hidden) certOverlay.hidden = true;
  }

  function setStatus(text, cls) {
    statusText.textContent = text;
    statusEl.className = cls || "";
  }

  function setOrb(mode) {
    orb.classList.remove("listening", "speaking");
    if (mode === "listening" || mode === "speaking") orb.classList.add(mode);
    orbLabel.textContent = mode || "idle";
  }

  function showLine(el, textEl, text) {
    textEl.textContent = text;
    el.classList.add("show");
  }

  async function unlockAndStart() {
    overlayErr.hidden = true;
    try {
      session.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: SAMPLE_RATE,
      });
    } catch (_) {
      session.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (session.audioCtx.state === "suspended") {
      try { await session.audioCtx.resume(); } catch (_) {}
    }
    if (session.audioCtx.state !== "running") {
      overlayErr.textContent =
        "AudioContext is " + session.audioCtx.state + "; tap again.";
      overlayErr.hidden = false;
      return;
    }
    session.sampleRate = session.audioCtx.sampleRate;
    session.nextPlayTime = session.audioCtx.currentTime;
    console.log("[renee] AudioContext sampleRate =", session.sampleRate,
                "(wire target:", SAMPLE_RATE + ")",
                session.sampleRate === SAMPLE_RATE
                  ? "no resample needed"
                  : "resampling to " + SAMPLE_RATE);

    try {
      session.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          sampleRate: SAMPLE_RATE,
        },
        video: false,
      });
    } catch (e) {
      overlayErr.textContent = "Microphone permission denied: " + e.message;
      overlayErr.hidden = false;
      return;
    }

    await setupWorklet();
    await requestWakeLock();
    session.isStarted = true;
    overlay.hidden = true;
    ptt.hidden = false;
    connectWS();
  }

  async function setupWorklet() {
    // Worklet pushes Float32 chunks back to the main thread; we resample
    // and convert to int16 here so audio_resample.js stays a single copy.
    const code = `
      class Capture extends AudioWorkletProcessor {
        constructor() { super(); this._muted = false;
          this.port.onmessage = (e) => {
            if (e.data && e.data.muted !== undefined) this._muted = e.data.muted;
          };
        }
        process(inputs) {
          const ch = inputs[0][0];
          if (!ch) return true;
          // Copy before posting; AudioWorklet reuses the buffer.
          const out = new Float32Array(ch.length);
          if (!this._muted) out.set(ch);
          this.port.postMessage(out.buffer, [out.buffer]);
          return true;
        }
      }
      registerProcessor('renee-capture', Capture);
    `;
    const url = URL.createObjectURL(new Blob([code], { type: "application/javascript" }));
    await session.audioCtx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    const src = session.audioCtx.createMediaStreamSource(session.micStream);
    session.micNode = new AudioWorkletNode(session.audioCtx, "renee-capture");

    // Streaming resampler state: when the device rate differs from 48000
    // we keep the previous-chunk tail so frame boundaries don't tick.
    const srcRate = session.sampleRate;
    const dstRate = SAMPLE_RATE;
    let tail = 0; // fractional source index carried across chunks
    let lastSample = 0; // last float sample from previous chunk

    session.micNode.port.onmessage = (e) => {
      if (!session.ws || session.ws.readyState !== WebSocket.OPEN) return;
      const chunk = new Float32Array(e.data);
      let resampled;
      if (srcRate === dstRate) {
        resampled = chunk;
      } else {
        const ratio = dstRate / srcRate;
        const approxLen = Math.floor(chunk.length * ratio + 1);
        const out = new Float32Array(approxLen);
        let wi = 0;
        let pos = tail;
        while (pos < chunk.length) {
          const i = Math.floor(pos);
          const frac = pos - i;
          const a = i === 0 ? lastSample : chunk[i - 1];
          const b = chunk[i];
          out[wi++] = a + (b - a) * frac;
          pos += srcRate / dstRate;
        }
        tail = pos - chunk.length;
        lastSample = chunk[chunk.length - 1];
        resampled = out.subarray(0, wi);
      }
      const int16 = new Int16Array(resampled.length);
      for (let i = 0; i < resampled.length; i++) {
        let s = resampled[i];
        if (s > 1) s = 1; else if (s < -1) s = -1;
        int16[i] = s < 0 ? s * 32768 : s * 32767;
      }
      session.ws.send(int16.buffer);
    };

    src.connect(session.micNode);
    const sink = session.audioCtx.createGain();
    sink.gain.value = 0;
    session.micNode.connect(sink).connect(session.audioCtx.destination);
  }

  function setMuted(m) {
    session.captureMuted = m;
    if (session.micNode) session.micNode.port.postMessage({ muted: m });
  }

  function connectWS() {
    if (session.reconnectTimer) {
      clearTimeout(session.reconnectTimer);
      session.reconnectTimer = null;
    }
    if (session.ws && (
      session.ws.readyState === WebSocket.OPEN ||
      session.ws.readyState === WebSocket.CONNECTING
    )) {
      return; // already connecting or connected: single-reconnect guard
    }

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + window.location.host + WS_PATH;
    setStatus("connecting", "");
    try {
      session.ws = new WebSocket(url);
      session.ws.binaryType = "arraybuffer";
    } catch (_) {
      setStatus("connection error", "error");
      scheduleReconnect();
      return;
    }
    session.ws.onopen = () => {
      setStatus("connected", "connected");
      session.reconnectDelay = 1000;
      session.wsHasEverOpened = true;
      session.wsFailuresBeforeFirstOpen = 0;
      hideCertOverlay();
      setOrb("listening");
      if (session.pttEnabled) setMuted(true);
    };
    session.ws.onclose = () => {
      setStatus("reconnecting", "error");
      setOrb("idle");
      if (!session.wsHasEverOpened) {
        session.wsFailuresBeforeFirstOpen += 1;
        maybeShowCertOverlay();
      }
      scheduleReconnect();
    };
    session.ws.onerror = () => setStatus("connection error", "error");
    session.ws.onmessage = (ev) => handleMessage(ev.data);
  }

  function scheduleReconnect() {
    if (session.reconnectTimer) return; // single-reconnect guard
    if (session.ws && session.ws.readyState === WebSocket.CONNECTING) return;
    session.reconnectTimer = setTimeout(() => {
      session.reconnectTimer = null;
      session.reconnectDelay = Math.min(session.reconnectDelay * 1.5, 10000);
      connectWS();
    }, session.reconnectDelay);
  }

  function handleMessage(data) {
    if (typeof data === "string") {
      try {
        const msg = JSON.parse(data);
        if (msg.type === "transcript") {
          showLine(youLine, youText, msg.text || "");
        } else if (msg.type === "response") {
          showLine(reneeLine, reneeText, msg.text || "");
        }
      } catch (_) {}
      return;
    }
    playPCM(new Int16Array(data));
    noteSpeaking();
  }

  function playPCM(int16) {
    if (!session.audioCtx) return;
    const n = int16.length;
    if (!n) return;
    const f32 = new Float32Array(n);
    for (let i = 0; i < n; i++) f32[i] = int16[i] / 32768;
    const buf = session.audioCtx.createBuffer(1, n, SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const src = session.audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(session.audioCtx.destination);
    const now = session.audioCtx.currentTime;
    if (session.nextPlayTime < now) session.nextPlayTime = now;
    src.start(session.nextPlayTime);
    session.nextPlayTime += n / SAMPLE_RATE;
  }

  function noteSpeaking() {
    session.lastAudioAt = performance.now();
    setOrb("speaking");
    if (session.speakingTimer) clearTimeout(session.speakingTimer);
    session.speakingTimer = setTimeout(() => {
      if (performance.now() - session.lastAudioAt >= 240) setOrb("listening");
    }, 260);
  }

  async function requestWakeLock() {
    if (!("wakeLock" in navigator)) return;
    try {
      session.wakeLock = await navigator.wakeLock.request("screen");
      session.wakeLock.addEventListener("release", () => {
        session.wakeLock = null;
      });
    } catch (_) {
      session.wakeLock = null;
    }
  }

  async function releaseWakeLock() {
    if (!session.wakeLock) return;
    try {
      await session.wakeLock.release();
    } catch (_) {}
    session.wakeLock = null;
  }

  function stopSession() {
    if (session.ws) {
      try { session.ws.close(1000, "client stop"); } catch (_) {}
    }
    if (session.reconnectTimer) {
      clearTimeout(session.reconnectTimer);
      session.reconnectTimer = null;
    }
    releaseWakeLock();
    session.isStarted = false;
  }

  // PTT
  function pttDown(e) {
    e.preventDefault();
    ptt.classList.add("active");
    ptt.setAttribute("aria-pressed", "true");
    setMuted(false);
  }
  function pttUp(e) {
    e.preventDefault();
    ptt.classList.remove("active");
    ptt.setAttribute("aria-pressed", "false");
    setMuted(true);
  }
  ptt.addEventListener("pointerdown", pttDown);
  ptt.addEventListener("pointerup", pttUp);
  ptt.addEventListener("pointercancel", pttUp);
  ptt.addEventListener("pointerleave", pttUp);

  let orbTapTimer = null;
  orb.addEventListener("click", () => {
    if (orbTapTimer) {
      clearTimeout(orbTapTimer);
      orbTapTimer = null;
      session.pttEnabled = !session.pttEnabled;
      ptt.hidden = !session.pttEnabled;
      setMuted(session.pttEnabled);
    } else {
      orbTapTimer = setTimeout(() => { orbTapTimer = null; }, 350);
    }
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

  startBtn.addEventListener("click", unlockAndStart);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (!session.isStarted) return;
    if (!session.ws || session.ws.readyState === WebSocket.CLOSED
        || session.ws.readyState === WebSocket.CLOSING) {
      connectWS();
    }
    if (session.audioCtx && session.audioCtx.state === "suspended") {
      session.audioCtx.resume().catch(() => {});
    }
    // Re-acquire the wake-lock; browsers drop it automatically on blur.
    if (!session.wakeLock && session.isStarted) requestWakeLock();
  });

  window.addEventListener("pagehide", stopSession);
  window.addEventListener("beforeunload", stopSession);
})();
