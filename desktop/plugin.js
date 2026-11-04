// usage-guard — a status-bar chip showing what the guard would decide right now.
// Plain ESM loaded uncompiled: jsx() calls, not JSX syntax. Imports restricted to
// the loader allowlist: @hermes/plugin-sdk, react, react/jsx-runtime.
//
// The chip reads GET /api/plugins/usage-guard/status, which answers from the
// guard's cache file only: polling it never spends a read of a provider's usage
// endpoint. The guard itself (the Python half) works with or without this chip.
import { jsx, jsxs } from "react/jsx-runtime";
import { useEffect, useState } from "react";
import { Codicon, Tip, host, useValue } from "@hermes/plugin-sdk";
import * as sdk from "@hermes/plugin-sdk";

const STATUSBAR_RIGHT = (sdk.STATUSBAR_AREAS && sdk.STATUSBAR_AREAS.right) || "statusBar.right";
const POLL_MS = 60000;
// Atom fallback for a Desktop without `host.state.profile`.
const NO_PROFILE = { get: () => "", subscribe: () => () => {}, listen: () => () => {} };

// Colours come from the host theme only, so the chip follows light and dark.
const TONE = {
  ok: "var(--ui-text-tertiary)",
  passthrough: "var(--ui-text-tertiary)",
  idle: "var(--ui-text-tertiary)",
  stale: "var(--ui-text-tertiary)",
  near: "var(--ui-yellow)",
  unverified: "var(--ui-orange)",
  blocked: "var(--ui-red)",
};
const ICON = { stale: "shield", idle: "shield", ok: "shield", passthrough: "shield", near: "warning", unverified: "warning", blocked: "debug-pause" };

const STATE_TEXT = {
  ok: "ok", near: "near the limit", blocked: "blocked", unverified: "allowed without a reading (fail_open)",
  passthrough: "not guarded (no usage surface)", idle: "not used yet",
  stale: "reading is old, refreshed before the next request",
};

// Pure: the chip's text and tooltip from a /status payload. Exported for tests.
function chipModel(status) {
  if (!status || !Array.isArray(status.providers)) {
    return { state: "unknown", text: "guard", tip: "usage-guard: status unavailable" };
  }
  const state = status.state || "ok";
  const peaks = status.providers.map((p) => p.peak_percent).filter((v) => typeof v === "number");
  const peak = peaks.length ? Math.max(...peaks) : null;
  const text = state === "blocked" ? "blocked" : peak === null ? "guard" : `${Math.round(peak)}%`;
  const lines = [`usage-guard refuses at ${Math.round(status.threshold_percent)}% used`];
  if (status.fail_open) lines.push("fail_open is ON");
  for (const p of status.providers) {
    const windows = (p.windows || []).map((w) => `${w.label} ${Math.round(w.used_percent)}% (resets ${w.resets})`);
    const detail = windows.length ? ` · ${windows.join(" · ")}` : p.state === "idle" ? "" : " · no reading";
    lines.push(`${p.provider}: ${STATE_TEXT[p.state] || p.state}${detail}`);
  }
  return { state, text, tip: lines.join("\n") };
}

function GuardChip({ ctx }) {
  const [status, setStatus] = useState(null);
  // ctx.rest is scoped to the active profile. A profile without usage-guard
  // enabled answers 404 and the chip hides; re-read at once when the profile
  // changes instead of waiting for the next minute's poll.
  const profile = useValue((host.state && host.state.profile) || NO_PROFILE);
  useEffect(() => {
    setStatus(null);
    let gone = false;
    let timer;
    const load = async () => {
      try {
        const r = await ctx.rest("/status", { timeoutMs: 10000 });
        if (!gone) setStatus(r);
      } catch {
        if (!gone) setStatus(null);
      }
      if (!gone) timer = setTimeout(load, POLL_MS);
    };
    load();
    // A finished turn is when a fresh reading is most likely: refresh then too.
    const off = typeof host.onEvent === "function" ? host.onEvent("message.complete", () => { clearTimeout(timer); load(); }) : null;
    return () => { gone = true; clearTimeout(timer); if (typeof off === "function") off(); };
  }, [ctx, profile]);
  const model = chipModel(status);
  if (model.state === "unknown") return null;
  const color = TONE[model.state] || TONE.ok;
  return jsx(Tip, {
    label: model.tip,
    children: jsxs("span", {
      role: "status",
      "aria-label": model.tip,
      "data-state": model.state,
      style: { display: "inline-flex", alignItems: "center", gap: 4, height: "100%", padding: "0 6px",
        color, fontSize: "0.6875rem", fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" },
      children: [jsx(Codicon, { name: ICON[model.state] || "shield", size: "0.7rem" }), jsx("span", { children: model.text })],
    }),
  });
}

const plugin_default = {
  id: "usage-guard",
  name: "usage-guard",
  defaultEnabled: true,
  register(ctx) {
    ctx.register({ id: "status", area: STATUSBAR_RIGHT, order: 78, render: () => jsx(GuardChip, { ctx }) });
  },
};

export { chipModel, GuardChip, plugin_default as default };
