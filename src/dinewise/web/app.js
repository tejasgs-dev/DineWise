/* DineWise web frontend: a thin client over the REST API (arch §10).
 *
 * The page is served by the API itself (same origin) or by a static host such as Vercel,
 * in which case config.js sets DINEWISE_API_BASE to the API's URL.
 * IDs in comments refer to docs/edge-case.md. Every value that comes from the API
 * is escaped before it is put into HTML (UI-14).
 */
"use strict";

const API_BASE = String(window.DINEWISE_API_BASE || "").replace(/\/+$/, "");
// Longer than the API's own LLM deadline (DINEWISE_LLM_DEADLINE_S, 20 s by default), so the
// page never gives up on a request the API is still allowed to finish (UI-16).
const REQUEST_TIMEOUT_MS = 60000;
const MAX_CUISINES = 5;
const MAX_CHIPS = 6; // cuisine chips per card before "+N more" (UI-12)
const MAX_OPTIONS = 60; // rows shown in a dropdown at once
// What the page calls the LLM ranker. "Namma" is Kannada for "our", as in Namma Bengaluru.
const AI_NAME = "Namma AI";

const QUICK_STARTS = [
  {
    icon: "🍕",
    title: "Italian and craft beer",
    detail: "Indiranagar · High budget",
    prefs: { location: "Indiranagar", cuisines: ["Italian"], budget: "high", min_rating: 4, extras: "craft beer, lively evening" },
  },
  {
    icon: "🍛",
    title: "Spicy Andhra biryani",
    detail: "Koramangala 5th Block · 4.0★ and up",
    prefs: { location: "Koramangala 5th Block", cuisines: ["Andhra", "Biryani"], budget: "medium", min_rating: 4, extras: "" },
  },
  {
    icon: "☕",
    title: "A quiet cafe to work from",
    detail: "HSR · Low budget",
    prefs: { location: "HSR", cuisines: ["Cafe"], budget: "low", min_rating: 3.5, extras: "quiet, good coffee" },
  },
  {
    icon: "🍨",
    title: "Late dessert run",
    detail: "Jayanagar · Desserts and ice cream",
    prefs: { location: "Jayanagar", cuisines: ["Desserts", "Ice Cream"], budget: null, min_rating: 4, extras: "" },
  },
  {
    icon: "👨‍👩‍👧",
    title: "Family dinner with a table booking",
    detail: "Whitefield · North Indian · Medium budget",
    prefs: { location: "Whitefield", cuisines: ["North Indian"], budget: "medium", min_rating: 3.5, extras: "family-friendly, table booking" },
    wide: true,
  },
];

const EMPTY_FORM = { location: "", cuisines: [], budget: null, min_rating: 0, extras: "" };

const state = {
  meta: null, // { city, locations, cuisines, budgets, restaurants }
  llm: null, // whether the API has an LLM ranker; null if unknown
  form: { ...EMPTY_FORM, cuisines: [] },
  pending: false, // a search is running; the button is disabled (UI-15)
  result: null, // last successful response; stays visible until the next one (UI-17)
  query: null, // the preferences that produced `result`
  error: null, // ApiError from the last search, if it failed
};

const $ = (id) => document.getElementById(id);

// --- Formatting ------------------------------------------------------------------------

const indian = new Intl.NumberFormat("en-IN"); // 1,20,000 grouping (D-09)

// The dataset and API say "Bangalore"; the page says "Bengaluru". Every API string goes
// through esc(), so renaming here covers addresses, summaries and messages alike.
const placeName = (text) => String(text ?? "").replace(/\bBangalore\b/g, "Bengaluru");

function esc(value) {
  return placeName(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const formatNumber = (n) => indian.format(Math.round(Number(n) || 0));
const formatInr = (n) => "₹" + formatNumber(n);
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

function costText(r) {
  return r.cost_for_two == null ? "Cost not available" : `${formatInr(r.cost_for_two)} for two`; // UI-10
}

function joinWords(items) {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function budgetCaption(band) {
  if (!band) return "No limit";
  const found = state.meta?.budgets.find((b) => b.band === band);
  return found ? cap(found.label) : cap(band);
}

// --- API -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status; // null when the API could not be reached
    this.code = extra.code ?? null;
    this.field = extra.field ?? null;
    this.value = extra.value ?? null;
    this.suggestions = extra.suggestions ?? [];
    this.requestId = extra.requestId ?? null;
  }
  get unreachable() {
    return this.status === null;
  }
}

async function api(path, { method = "GET", body, allow = [] } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(API_BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    const message =
      err.name === "AbortError"
        ? "The DineWise service took too long to answer."
        : "Can't reach the DineWise service.";
    throw new ApiError(null, message);
  } finally {
    clearTimeout(timer);
  }
  if (response.ok || allow.includes(response.status)) return response.json();
  throw await errorFromResponse(response);
}

async function errorFromResponse(response) {
  const status = response.status;
  const headerId = response.headers.get("x-request-id");
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }

  // Our own errors: {"error": {"code", "message", "field", "value", "suggestions"}}
  if (body && typeof body.error === "object" && body.error) {
    const e = body.error;
    return new ApiError(status, e.message || "Request failed", {
      code: e.code,
      field: e.field,
      value: e.value,
      suggestions: e.suggestions || [],
      requestId: body.request_id || headerId,
    });
  }
  // FastAPI request validation: {"detail": [{"loc": [...], "msg": ...}, ...]}
  if (body && Array.isArray(body.detail) && body.detail.length) {
    const first = body.detail[0];
    const name = (first.loc || []).filter((p) => p !== "body")[0];
    const msg = first.msg || "Invalid input";
    return new ApiError(status, name ? `${name}: ${msg}` : msg, {
      code: "validation_error",
      field: name,
      requestId: headerId,
    });
  }
  const defaults = {
    429: "Too many requests. Please try again in a minute.",
    503: "The DineWise service is temporarily unavailable.",
  };
  const message =
    defaults[status] ||
    (status >= 500 ? "The DineWise service hit an error." : `Request failed (${status}).`);
  return new ApiError(status, message, { requestId: headerId });
}

async function loadMeta() {
  const [health, locations, cuisines, budgets] = await Promise.all([
    api("/healthz", { allow: [503] }).catch(() => null),
    api("/api/v1/meta/locations"),
    api("/api/v1/meta/cuisines"),
    api("/api/v1/meta/budgets"),
  ]);
  state.llm = health && health.status === "ok" ? Boolean(health.llm_ranking) : null;
  state.meta = {
    city: placeName(locations.city), // sent back as-is: the API accepts "Bengaluru" too
    locations: locations.locations,
    cuisines: [...cuisines.cuisines].sort((a, b) => b.count - a.count),
    budgets: budgets.budgets,
    restaurants:
      health?.restaurants || locations.locations.reduce((sum, l) => sum + l.count, 0),
  };
}

// --- Form ------------------------------------------------------------------------------

function buildPreferences() {
  const f = state.form;
  const extras = f.extras.trim();
  return {
    location: f.location.trim(),
    cuisines: [...f.cuisines],
    budget: f.budget,
    min_rating: Number(f.min_rating),
    extras: extras || null,
  };
}

function setForm(values) {
  Object.assign(state.form, values);
  if (values.cuisines) state.form.cuisines = values.cuisines.slice(0, MAX_CUISINES);
  syncFormControls();
  updateStaleNotice();
}

function syncFormControls() {
  const f = state.form;
  if ($("loc").value !== f.location) $("loc").value = f.location;
  renderCuisineChips();

  for (const btn of document.querySelectorAll("#budget [data-band]")) {
    const selected = (btn.dataset.band || null) === f.budget;
    btn.setAttribute("aria-checked", String(selected));
    btn.tabIndex = selected ? 0 : -1;
    btn.className =
      "py-2 text-center rounded-md text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-container/60 " +
      (selected
        ? "bg-primary-container text-[#0f0f12] shadow-md shadow-primary-container/20"
        : "text-on-surface-variant hover:text-on-surface hover:bg-surface-container-high");
  }
  $("budget-caption").textContent = budgetCaption(f.budget);

  $("rating").value = String(f.min_rating);
  $("rating-value").innerHTML =
    Number(f.min_rating) > 0
      ? `<span class="material-symbols-outlined fill text-[14px]">star</span> ${Number(f.min_rating).toFixed(1)}+`
      : "Any";

  if ($("extras").value !== f.extras) $("extras").value = f.extras;
  $("extras-count").textContent = `${f.extras.length} / 300`;
}

function renderCuisineChips() {
  const chips = state.form.cuisines
    .map(
      (c, i) => `
      <span class="inline-flex items-center gap-1 pl-2.5 pr-1 py-0.5 rounded-full bg-primary-container/15 border border-primary-container/60 text-primary text-xs font-semibold">
        ${esc(c)}
        <button type="button" data-remove-cuisine="${i}" aria-label="Remove ${esc(c)}"
          class="flex items-center rounded-full p-0.5 hover:text-danger focus:outline-none focus-visible:ring-1 focus-visible:ring-primary">
          <span class="material-symbols-outlined text-[14px]">close</span>
        </button>
      </span>`
    )
    .join("");
  $("cuisine-chips").innerHTML = chips;
  const full = state.form.cuisines.length >= MAX_CUISINES;
  const input = $("cuisine-input");
  input.disabled = full;
  input.placeholder = full
    ? "Maximum of 5 reached"
    : state.form.cuisines.length
      ? "+ Add cuisine"
      : "Any cuisine (optional, up to 5)";
  $("cuisine-count").textContent = `${state.form.cuisines.length} / ${MAX_CUISINES} selected`;
}

function addCuisine(name) {
  const value = name.trim();
  if (!value || state.form.cuisines.length >= MAX_CUISINES) return;
  const exists = state.form.cuisines.some((c) => c.toLowerCase() === value.toLowerCase());
  if (!exists) setForm({ cuisines: [...state.form.cuisines, value] });
  $("cuisine-input").value = "";
}

function showFormWarning(message) {
  const el = $("form-warning");
  el.hidden = !message;
  el.lastElementChild.textContent = message || "";
}

/* A small accessible combobox: type to filter, arrows to move, Enter to pick. */
function combobox({ input, list, options, onPick, onEnterText, toggle }) {
  let items = [];
  let active = -1;

  // `showAll` lists every option (on focus or the toggle); typing filters them.
  function open(showAll = false) {
    items = options(input.value, showAll);
    active = items.length ? 0 : -1;
    if (!items.length) return close();
    list.innerHTML = items
      .map(
        (item, i) => `
        <div role="option" id="${list.id}-${i}" data-index="${i}" aria-selected="${item.selected ? "true" : "false"}"
          class="flex items-center justify-between gap-2 w-full px-2.5 py-1.5 rounded-md cursor-pointer text-sm ${item.selected ? "text-primary font-semibold" : "text-on-surface-variant"}">
          <span class="flex items-center gap-1.5 min-w-0 truncate">
            ${item.selected ? '<span class="material-symbols-outlined text-[16px]">check</span>' : ""}${esc(item.label)}
          </span>
          ${item.count != null ? `<span class="tnum shrink-0 text-[11px] px-1.5 py-0.5 rounded bg-surface-container-lowest text-on-surface-variant">${formatNumber(item.count)} places</span>` : ""}
        </div>`
      )
      .join("");
    if (items.header) list.insertAdjacentHTML("afterbegin", items.header);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    highlight();
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function highlight() {
    for (const el of list.querySelectorAll("[role=option]")) {
      const on = Number(el.dataset.index) === active;
      el.classList.toggle("bg-surface-variant", on);
      el.classList.toggle("text-on-surface", on);
      if (on) {
        input.setAttribute("aria-activedescendant", el.id);
        el.scrollIntoView({ block: "nearest" });
      }
    }
  }

  function pick(i) {
    const item = items[i];
    close();
    if (item) onPick(item);
  }

  input.addEventListener("input", () => open(false));
  input.addEventListener("focus", () => open(true));
  // Focus doesn't fire again while the field is focused, so a click reopens the list.
  input.addEventListener("click", () => list.hidden && open(true));
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) return open(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      active = (active + step + items.length) % items.length;
      highlight();
    } else if (e.key === "Enter") {
      if (!list.hidden && active >= 0) {
        e.preventDefault();
        pick(active);
      } else if (onEnterText) {
        e.preventDefault();
        onEnterText(input.value);
      }
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault();
      close();
    }
  });
  list.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the input
  list.addEventListener("click", (e) => {
    const el = e.target.closest("[role=option]");
    if (el) pick(Number(el.dataset.index));
  });
  list.addEventListener("mousemove", (e) => {
    const el = e.target.closest("[role=option]");
    if (el && Number(el.dataset.index) !== active) {
      active = Number(el.dataset.index);
      highlight();
    }
  });
  toggle?.addEventListener("click", () => {
    if (list.hidden) {
      input.focus();
      open(true);
    } else close();
  });
  return { open, close };
}

function locationOptions(text, showAll) {
  const { city, locations, restaurants } = state.meta;
  const current = state.form.location.trim().toLowerCase();
  const filter = showAll ? "" : text.trim().toLowerCase();
  const all = [
    { label: city, value: city, count: restaurants, hint: "Whole city" },
    ...locations.map((l) => ({ label: l.name, value: l.name, count: l.count })),
  ];
  const items = all
    .filter((o) => !filter || o.label.toLowerCase().includes(filter))
    .slice(0, MAX_OPTIONS)
    .map((o) => ({ ...o, selected: o.value.toLowerCase() === current }));
  items.header = `<div class="px-2.5 py-1.5 text-[11px] font-semibold text-on-surface-variant/70 uppercase tracking-wider">${filter ? "Matching neighbourhoods" : `${esc(city)} neighbourhoods`}</div>`;
  return items;
}

function cuisineOptions(text) {
  const query = text.trim().toLowerCase();
  const chosen = new Set(state.form.cuisines.map((c) => c.toLowerCase()));
  const items = state.meta.cuisines
    .filter((c) => !chosen.has(c.name.toLowerCase()))
    .filter((c) => !query || c.name.toLowerCase().includes(query))
    .slice(0, MAX_OPTIONS)
    .map((c) => ({ label: c.name, value: c.name, count: c.count }));
  items.header = `<div class="px-2.5 py-1.5 text-[11px] font-semibold text-on-surface-variant/70 uppercase tracking-wider">${query ? "Matching cuisines" : "Popular cuisines"}</div>`;
  return items;
}

function bindForm() {
  combobox({
    input: $("loc"),
    list: $("loc-list"),
    toggle: $("loc-toggle"),
    options: locationOptions,
    onPick: (item) => {
      setForm({ location: item.value });
      showFormWarning(null);
    },
  });
  // Free text is allowed: the API corrects typos and suggests near matches (UI-03).
  $("loc").addEventListener("input", (e) => {
    state.form.location = e.target.value;
    showFormWarning(null);
    updateStaleNotice();
  });

  const cuisineBox = combobox({
    input: $("cuisine-input"),
    list: $("cuisine-list"),
    options: cuisineOptions,
    // Closes after each pick; clicking the field again opens it for another.
    onPick: (item) => addCuisine(item.value),
    onEnterText: (text) => addCuisine(text),
  });
  $("cuisine-input").addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && !e.target.value && state.form.cuisines.length) {
      setForm({ cuisines: state.form.cuisines.slice(0, -1) });
    }
  });
  $("cuisine-box").addEventListener("click", (e) => {
    const remove = e.target.closest("[data-remove-cuisine]");
    if (remove) {
      const i = Number(remove.dataset.removeCuisine);
      setForm({ cuisines: state.form.cuisines.filter((_, j) => j !== i) });
    } else if (e.target === e.currentTarget && !$("cuisine-input").disabled) {
      $("cuisine-input").focus();
      cuisineBox.open(true);
    }
  });

  const budgetGroup = $("budget");
  budgetGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-band]");
    if (btn) setForm({ budget: btn.dataset.band || null });
  });
  budgetGroup.addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const bands = [null, "low", "medium", "high"];
    const i = bands.indexOf(state.form.budget) + (e.key === "ArrowRight" ? 1 : -1);
    const next = bands[(i + bands.length) % bands.length];
    setForm({ budget: next });
    budgetGroup.querySelector(`[data-band="${next ?? ""}"]`).focus();
  });

  $("rating").addEventListener("input", (e) => setForm({ min_rating: Number(e.target.value) }));
  $("extras").addEventListener("input", (e) => setForm({ extras: e.target.value }));

  $("prefs").addEventListener("submit", (e) => {
    e.preventDefault();
    search();
  });
  $("reset").addEventListener("click", () => {
    setForm({ ...EMPTY_FORM, cuisines: [] });
    showFormWarning(null);
  });
}

// --- Search ----------------------------------------------------------------------------

async function search() {
  if (state.pending) return; // one request at a time (UI-15)
  const prefs = buildPreferences();
  if (!prefs.location) {
    showFormWarning("Choose a location first.");
    $("loc").focus();
    return;
  }
  showFormWarning(null);
  state.pending = true;
  state.error = null;
  setFindButton();
  renderResults();
  try {
    const result = await api("/api/v1/recommendations", { method: "POST", body: prefs });
    state.result = result;
    state.query = prefs;
  } catch (err) {
    state.error = err; // the form keeps its values (UI-02)
    if (err.status === 429) toast("Too many requests. Please try again in a minute."); // UI-04
  } finally {
    state.pending = false;
    setFindButton();
    renderResults();
    if (window.matchMedia("(max-width: 1023px)").matches) {
      $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
}

function setFindButton() {
  const btn = $("find");
  btn.disabled = state.pending;
  btn.setAttribute("aria-busy", String(state.pending));
  btn.innerHTML = state.pending
    ? `<span class="material-symbols-outlined text-[20px] animate-spin">progress_activity</span>
       <span>${state.llm ? `${AI_NAME} is picking the best matches…` : "Finding restaurants…"}</span>`
    : `<span class="material-symbols-outlined text-[20px]">search</span><span>Find restaurants</span>`;
  for (const el of $("prefs").querySelectorAll("input, textarea, [data-band]")) {
    el.disabled = state.pending || (el.id === "cuisine-input" && state.form.cuisines.length >= MAX_CUISINES);
  }
}

function applySuggestion(field, wrong, suggestion) {
  // Swap the value the API didn't recognise for the one the user picked, and search (UI-03).
  if (field === "location") {
    setForm({ location: suggestion });
  } else if (field === "cuisines") {
    const wrongKey = (wrong || "").toLowerCase();
    let replaced = state.form.cuisines.map((c) => (c.toLowerCase() === wrongKey ? suggestion : c));
    if (!replaced.includes(suggestion)) replaced.push(suggestion);
    replaced = [...new Set(replaced)];
    setForm({ cuisines: replaced });
  }
  search();
}

const TIP_ACTIONS = {
  rating: { label: "Lower the minimum rating", apply: () => ({ min_rating: 0 }) },
  budget: { label: "Set the budget to Any", apply: () => ({ budget: null }) },
  cuisines: { label: "Remove the cuisines", apply: () => ({ cuisines: [] }) },
  extras: { label: "Clear the extra preferences", apply: () => ({ extras: "" }) },
  city: { label: "Search the whole city", apply: () => ({ location: state.meta.city }) },
};

function emptyStateTips(query, cityWide) {
  // Suggestions for an empty result, based on what was searched (UI-07).
  const tips = [];
  if (query.min_rating) tips.push("rating");
  if (query.budget) tips.push("budget");
  if (query.cuisines?.length) tips.push("cuisines");
  if (query.extras) tips.push("extras");
  if (!cityWide) tips.push("city");
  return tips;
}

function updateStaleNotice() {
  // Old results stay visible but are marked out of date once the form changes (UI-17).
  const el = $("stale-notice");
  if (!el || !state.query) return;
  const same = JSON.stringify(buildPreferences()) === JSON.stringify(state.query);
  el.hidden = same;
}

// --- Rendering -------------------------------------------------------------------------

function renderAiStatus() {
  const el = $("ai-status");
  if (state.llm === null) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.innerHTML = state.llm
    ? `<span class="relative flex h-2 w-2"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-violet opacity-75"></span><span class="relative inline-flex rounded-full h-2 w-2 bg-violet"></span></span>
       <span class="text-secondary">${AI_NAME}: on</span>`
    : `<span class="inline-flex rounded-full h-2 w-2 bg-muted"></span>
       <span class="text-on-surface-variant">${AI_NAME}: off</span>
       <span class="hidden sm:inline text-on-surface-variant/60 font-normal">· basic ranking</span>`;
}

function renderResults() {
  const parts = [];
  if (state.error && !state.pending) parts.push(errorHtml(state.error));
  if (state.pending) parts.push(loadingHtml());
  else if (state.result) parts.push(resultHtml(state.result, state.query || {}));
  else if (!state.error) parts.push(welcomeHtml());
  $("results").innerHTML = parts.join("");
  updateStaleNotice();
}

function welcomeHtml() {
  const { locations, cuisines } = state.meta;
  const prompts = QUICK_STARTS.map(
    (q, i) => `
    <button type="button" data-quick="${i}"
      class="${q.wide ? "md:col-span-2 " : ""}group text-left flex items-center gap-3 p-4 rounded-2xl bg-surface-container-low border border-transparent hover:border-primary-container/50 hover:bg-surface-container transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-container/60">
      <span class="w-11 h-11 shrink-0 rounded-xl bg-surface-container-high flex items-center justify-center text-xl" aria-hidden="true">${q.icon}</span>
      <span class="min-w-0">
        <span class="block font-semibold text-on-surface group-hover:text-primary transition-colors truncate">${esc(q.title)}</span>
        <span class="block text-xs text-on-surface-variant truncate">${esc(q.detail)}</span>
      </span>
      <span class="material-symbols-outlined ml-auto text-[18px] text-on-surface-variant group-hover:text-primary transition-colors">arrow_forward</span>
    </button>`
  ).join("");

  return `
  <div class="fade-in flex flex-col gap-6">
    <div class="relative overflow-hidden rounded-3xl bg-surface-container-low px-6 py-12 sm:px-12 text-center shadow-xl shadow-black/30">
      <div aria-hidden="true" class="absolute -top-24 left-1/2 -translate-x-1/2 w-[28rem] h-[28rem] rounded-full bg-secondary-container/15 blur-3xl"></div>
      <div class="relative flex flex-col items-center gap-5">
        <span class="w-24 h-24 rounded-full bg-surface-container ring-1 ring-primary-container/20 flex items-center justify-center text-primary-container text-[44px] shadow-[0_0_48px_-8px_rgba(255,122,26,0.35)]">
          <span class="material-symbols-outlined">room_service</span>
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-violet/10 border border-violet/30 text-secondary text-xs font-semibold">
          <span class="material-symbols-outlined fill text-[14px]">auto_awesome</span>
          ${formatNumber(state.meta.restaurants)} real Bengaluru restaurants
        </span>
        <h1 class="font-display text-display-hero-mobile sm:text-display-hero text-on-surface max-w-2xl">Discover Bengaluru's best dining, curated by ${AI_NAME}</h1>
        <p class="text-base text-on-surface-variant max-w-xl leading-relaxed">
          Choose a location, add any preferences, and press <span class="text-primary-container font-semibold">Find restaurants</span>.
          DineWise filters real restaurants, then ${state.llm === false ? "ranks" : `${AI_NAME} ranks`} the top 5 and explains why each one fits.
        </p>
        <div class="flex flex-wrap justify-center gap-x-5 gap-y-2 px-5 py-2.5 rounded-full bg-surface-container-lowest/60 text-xs font-semibold">
          <span class="flex items-center gap-1.5 text-tertiary"><span class="material-symbols-outlined text-[16px]">location_city</span>${formatNumber(locations.length)} neighbourhoods</span>
          <span class="flex items-center gap-1.5 text-secondary"><span class="material-symbols-outlined text-[16px]">ramen_dining</span>${formatNumber(cuisines.length)} cuisines</span>
          <span class="flex items-center gap-1.5 text-on-surface"><span class="material-symbols-outlined text-[16px]">task_alt</span>Typos auto-corrected</span>
        </div>
      </div>
    </div>

    <div class="flex flex-col gap-3">
      <div class="flex items-center justify-between">
        <h2 class="flex items-center gap-2 font-semibold text-on-surface">
          <span class="material-symbols-outlined text-primary-container text-[20px]">bolt</span>
          Or try one of these:
        </h2>
        <span class="text-xs text-secondary">Tap to search</span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">${prompts}</div>
    </div>
  </div>`;
}

function loadingHtml() {
  const prefs = buildPreferences();
  const where = esc(prefs.location);
  const header = state.llm
    ? `<div class="relative overflow-hidden p-6 rounded-3xl bg-secondary-container/15 shadow-[0_0_24px_-4px_rgba(155,123,255,0.15)]">
         <div aria-hidden="true" class="absolute -right-10 -top-10 w-40 h-40 bg-violet/15 rounded-full blur-2xl"></div>
         <div class="relative flex items-start gap-3">
           <span class="w-10 h-10 shrink-0 rounded-xl bg-secondary-container flex items-center justify-center text-secondary text-[22px]"><span class="material-symbols-outlined fill">auto_awesome</span></span>
           <div class="min-w-0 flex-1">
             <p class="font-display text-headline-sm text-on-surface">${AI_NAME} is picking the best matches…</p>
             <p class="text-xs text-secondary mt-0.5">Shortlisting real restaurants in ${where}, then ranking and explaining the top picks. This can take a few seconds.</p>
             <div class="indeterminate mt-4 h-1.5 rounded-full bg-surface-container-high"></div>
           </div>
         </div>
       </div>`
    : `<div class="p-5 rounded-2xl bg-surface-container-low flex items-center gap-3">
         <span class="material-symbols-outlined animate-spin text-primary-container text-[22px]">progress_activity</span>
         <p class="text-sm text-on-surface">Finding restaurants in ${where}…</p>
       </div>`;

  const skeleton = () => `
    <div class="p-5 rounded-2xl bg-surface-container-low flex flex-col md:flex-row gap-5" aria-hidden="true">
      <div class="shimmer w-full md:w-48 h-32 md:h-44 rounded-xl"></div>
      <div class="flex-1 flex flex-col gap-3">
        <div class="flex justify-between"><div class="shimmer h-5 w-12 rounded-full"></div><div class="shimmer h-5 w-24 rounded-full"></div></div>
        <div class="shimmer h-6 w-2/3 rounded-lg"></div>
        <div class="shimmer h-4 w-1/2 rounded"></div>
        <div class="flex gap-2"><div class="shimmer h-5 w-16 rounded"></div><div class="shimmer h-5 w-20 rounded"></div><div class="shimmer h-5 w-14 rounded"></div></div>
        <div class="shimmer h-4 w-full rounded"></div>
        <div class="shimmer h-4 w-4/5 rounded"></div>
      </div>
    </div>`;
  return `<div class="flex flex-col gap-4" role="status" aria-label="Loading recommendations">${header}${skeleton()}${skeleton()}${skeleton()}</div>`;
}

function errorHtml(err) {
  const requestId = err.requestId
    ? `<p class="tnum text-[11px] text-on-surface-variant/60 mt-3">Request ID: ${esc(err.requestId)}</p>`
    : "";
  if (err.status === 429) {
    return `
    <div class="fade-in flex items-center gap-3 p-4 rounded-xl bg-tertiary/10 border border-tertiary/30 text-tertiary text-sm">
      <span class="material-symbols-outlined text-[20px]">hourglass_top</span>
      <span>Too many requests. Please try again in a minute.</span>
    </div>`;
  }
  if (err.status === 422) {
    const suggestions = err.suggestions.length
      ? `<p class="text-sm text-on-surface mt-4 mb-2">Did you mean:</p>
         <div class="flex flex-wrap gap-2">
           ${err.suggestions
             .map(
               (s, i) => `<button type="button" data-suggestion="${i}"
                 class="px-4 py-1.5 rounded-full bg-surface-container-high border border-outline-variant text-on-surface text-sm font-semibold hover:border-primary-container hover:text-primary transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-container/60">${esc(s)}</button>`
             )
             .join("")}
         </div>`
      : "";
    return `
    <div class="fade-in p-5 rounded-2xl bg-error-container/20 border border-danger/30">
      <div class="flex items-start gap-3">
        <span class="material-symbols-outlined text-danger text-[22px]">search_off</span>
        <div class="min-w-0">
          <p class="font-semibold text-on-surface">${esc(err.message)}</p>
          ${suggestions}
          ${requestId}
        </div>
      </div>
    </div>`;
  }
  const title = err.unreachable ? "Can't reach DineWise" : "Something went wrong on our side";
  const body = err.unreachable
    ? `${esc(err.message)} Make sure the API is running, then try again.`
    : "Please try again in a moment.";
  return `
  <div class="fade-in p-5 rounded-2xl bg-error-container/20 border border-danger/30">
    <div class="flex items-start gap-3">
      <span class="material-symbols-outlined text-danger text-[22px]">${err.unreachable ? "cloud_off" : "error"}</span>
      <div class="min-w-0 flex-1">
        <p class="font-semibold text-on-surface">${title}</p>
        <p class="text-sm text-on-surface-variant mt-1">${body}</p>
        ${requestId}
      </div>
      <button type="button" data-action="retry" class="shrink-0 px-4 py-1.5 rounded-full bg-surface-container-high border border-outline-variant text-sm font-semibold hover:border-primary-container transition-colors">Try again</button>
    </div>
  </div>`;
}

function resultHtml(result, query) {
  const recs = result.recommendations || [];
  const normalized = result.normalized_preferences || {};
  const parts = [];

  parts.push(`
    <div id="stale-notice" hidden class="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-surface-container-low border border-tertiary/30 text-xs text-tertiary">
      <span class="material-symbols-outlined text-[18px]">history</span>
      <span>You've changed your preferences. These results are for your previous search; press <strong>Find restaurants</strong> to update.</span>
    </div>`);

  const corrections = normalized.corrections || [];
  if (corrections.length) {
    // "Showing results for Koramangala (you typed "koramangla")." (UI-18)
    const pieces = corrections.map(
      (c) =>
        `<span class="font-semibold text-on-surface">${esc(c.value)}</span> <span class="text-on-surface-variant/60">(you typed “${esc(c.input)}”)</span>`
    );
    parts.push(`
      <div class="flex items-start gap-2 px-4 py-2.5 rounded-xl bg-surface-container-low text-on-surface-variant text-xs">
        <span class="material-symbols-outlined text-[18px] text-secondary shrink-0">info</span>
        <p>Showing results for ${joinWords(pieces)}.</p>
      </div>`);
  }

  if (result.degraded) {
    // UI-05
    parts.push(`
      <div class="flex items-start gap-4 p-5 rounded-2xl bg-surface-container-low border border-tertiary/25">
        <span class="w-10 h-10 shrink-0 rounded-full bg-tertiary/15 flex items-center justify-center text-tertiary text-[22px]"><span class="material-symbols-outlined">warning</span></span>
        <div>
          <p class="font-display text-headline-sm text-tertiary">Basic ranking (${AI_NAME} unavailable)</p>
          <p class="text-sm text-on-surface mt-1">These picks are ranked by rating and popularity, and the explanations are generated from the data.</p>
        </div>
      </div>`);
  }

  if (!recs.length) {
    const tips = emptyStateTips(query, normalized.location_kind === "city");
    parts.push(`
      <div class="flex flex-col items-center text-center gap-4 px-6 py-12 rounded-3xl bg-surface-container-low">
        <span class="w-20 h-20 rounded-full bg-surface-container flex items-center justify-center text-[40px] text-on-surface-variant"><span class="material-symbols-outlined">no_meals</span></span>
        <p class="font-display text-headline-md text-on-surface">No restaurants matched</p>
        <p class="text-sm text-on-surface-variant max-w-md">${esc(result.summary || "Nothing matched these preferences.")}</p>
        ${
          tips.length
            ? `<div class="flex flex-col items-center gap-2 mt-2">
                 <p class="text-xs font-semibold text-on-surface uppercase tracking-wider">Try this</p>
                 <div class="flex flex-wrap justify-center gap-2">
                   ${tips
                     .map(
                       (t) => `<button type="button" data-tip="${t}" class="px-4 py-1.5 rounded-full bg-surface-container-high border border-outline-variant text-sm text-on-surface hover:border-primary-container hover:text-primary transition-colors">${esc(TIP_ACTIONS[t].label)}</button>`
                     )
                     .join("")}
                 </div>
               </div>`
            : ""
        }
      </div>`);
    return `<div class="fade-in flex flex-col gap-4">${parts.join("")}</div>`;
  }

  parts.push(
    result.degraded
      ? `<div class="p-5 rounded-2xl bg-surface-container-low">
           <p class="text-xs font-semibold text-on-surface-variant uppercase tracking-wider mb-1.5">Summary</p>
           <p class="text-sm text-on-surface leading-relaxed">${esc(result.summary)}</p>
         </div>`
      : `<div class="relative overflow-hidden p-6 rounded-3xl bg-secondary-container/15 shadow-lg shadow-secondary-container/5">
           <div aria-hidden="true" class="absolute -right-8 -top-8 w-32 h-32 bg-violet/15 rounded-full blur-2xl"></div>
           <div class="relative flex flex-col gap-2">
             <span class="self-start inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-secondary-container text-secondary text-xs font-semibold">
               <span class="material-symbols-outlined fill text-[15px]">auto_awesome</span> ${AI_NAME}'s take
             </span>
             <p class="text-sm sm:text-[15px] text-on-surface leading-relaxed pt-1">${esc(result.summary)}</p>
           </div>
         </div>`
  );

  if (result.relaxations?.length) {
    parts.push(`
      <details open class="group rounded-xl bg-surface-container-low p-4">
        <summary class="flex items-center justify-between cursor-pointer list-none select-none text-tertiary">
          <span class="flex items-center gap-2 text-xs font-semibold">
            <span class="material-symbols-outlined text-[18px]">tune</span>
            We relaxed some filters to find enough matches:
          </span>
          <span class="material-symbols-outlined text-[20px] text-on-surface-variant transition-transform duration-200 group-open:rotate-180">expand_more</span>
        </summary>
        <ul class="mt-3 flex flex-col gap-1.5 pl-6 list-disc text-on-surface-variant text-xs">
          ${result.relaxations.map((r) => `<li>${esc(r)}</li>`).join("")}
        </ul>
      </details>`);
  }

  parts.push(`
    <div class="flex items-center justify-between gap-2 mt-1">
      <h2 class="font-semibold text-on-surface flex items-center gap-2">
        Top picks in ${esc(normalized.location || query.location)}
        <span class="tnum px-2 py-0.5 rounded-full bg-surface-container-high text-xs text-on-surface-variant font-medium">${recs.length} ${recs.length === 1 ? "result" : "results"}</span>
      </h2>
    </div>`);

  // Only what was found; no placeholder cards (UI-08)
  const visuals = assignImages(recs);
  parts.push(`<div class="flex flex-col gap-4">${recs.map((rec, i) => cardHtml(rec, visuals[i])).join("")}</div>`);

  const meta = result.meta || {};
  const details = [`${formatNumber(meta.candidates_considered ?? recs.length)} candidates considered`];
  if (meta.total_matches != null) details.push(`${formatNumber(meta.total_matches)} matches`);
  details.push(
    meta.model
      ? `ranked by ${AI_NAME} <span class="text-on-surface-variant/50">(${esc(meta.model)})</span>`
      : "rule-based ranking"
  );
  if (meta.latency_ms != null) details.push(`<span class="tnum">${formatNumber(meta.latency_ms)} ms</span>`);
  parts.push(`
    <div class="pt-4 flex flex-wrap items-center justify-between gap-3 text-on-surface-variant/70 text-xs border-t border-surface-container-high/60">
      <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span class="w-1.5 h-1.5 rounded-full ${result.degraded ? "bg-muted" : "bg-violet"}"></span>
        ${details.join('<span aria-hidden="true">·</span>')}
      </div>
      <button type="button" data-action="rerun" class="flex items-center gap-1 hover:text-on-surface transition-colors">
        <span class="material-symbols-outlined text-[16px]">refresh</span> Search again
      </button>
    </div>`);

  return `<div class="fade-in flex flex-col gap-4">${parts.join("")}</div>`;
}

// Cuisine -> emoji and AI illustration category. The first cuisine that matches wins.
const DISH_STYLES = [
  [/biryani/i, "🍛", "biryani"],
  [/pizza|italian/i, "🍕", "italian"],
  [/burger|fast food|american/i, "🍔", "burger"],
  [/ice cream|dessert|mithai/i, "🍨", "desserts"],
  [/bakery|cake/i, "🥐", "bakery"],
  [/cafe|coffee|tea/i, "☕", "cafe"],
  [/chinese|momos|tibetan/i, "🥡", "chinese"],
  [/asian|thai|japanese|sushi|korean/i, "🍣", "asian"],
  [/seafood|kerala|mangalorean|goan/i, "🦐", "seafood"],
  [/south indian|udupi|chettinad/i, "🥞", "south-indian"],
  [/kebab|bbq|grill|mughlai|arabian|charcoal/i, "🍢", "grill"],
  [/north indian|andhra|indian|bengali|hyderabadi/i, "🍲", "north-indian"],
  [/salad|healthy/i, "🥗", "healthy"],
  [/juice|beverages/i, "🥤", "beverages"],
];

// Illustrations in /static/images/<category>-<n>.jpg, made by scripts/generate_card_images.py.
// The dataset has no photos, so these show the kind of food, not the restaurant itself.
const CARD_IMAGES = {
  brewery: 2, biryani: 2, "north-indian": 2, "south-indian": 2, chinese: 2, italian: 2,
  burger: 1, cafe: 2, desserts: 2, bakery: 1, seafood: 1, grill: 1, asian: 1,
  healthy: 1, beverages: 1, dining: 2,
};

function dishStyle(r) {
  const types = (r.rest_types || []).join(" ");
  if (/brewery|pub|bar|lounge/i.test(types)) return { icon: "🍺", category: "brewery" };
  for (const cuisine of r.cuisines || []) {
    for (const [pattern, icon, category] of DISH_STYLES) {
      if (pattern.test(cuisine)) return { icon, category };
    }
  }
  return { icon: "🍽️", category: "dining" };
}

function hashString(text) {
  let h = 0;
  for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h;
}

// Where to look when a category's own images are missing (not generated yet).
const RELATED_IMAGES = {
  brewery: ["italian", "grill", "burger"],
  cafe: ["bakery", "desserts"],
  desserts: ["bakery", "cafe"],
  bakery: ["desserts", "cafe"],
  burger: ["italian"],
  asian: ["chinese"],
  chinese: ["asian"],
  seafood: ["south-indian"],
  grill: ["north-indian", "biryani"],
  biryani: ["north-indian"],
  beverages: ["cafe"],
};
// Last resort before the emoji: any dish, so a card always shows food when it can.
const FOOD_POOL = ["north-indian", "biryani", "south-indian", "chinese", "italian", "grill", "burger", "seafood", "asian", "dining"];

const imagePaths = (category) =>
  Array.from({ length: CARD_IMAGES[category] || 0 }, (_, i) => `/static/images/${category}-${i + 1}.jpg`);

function rotate(list, by) {
  if (!list.length) return list;
  const k = by % list.length;
  return [...list.slice(k), ...list.slice(0, k)];
}

/* Image candidates per card, best first: its own category, related ones, then the food
   pool. The browser moves down the list while files are missing (see nextCardImage).
   Rotations differ per card, so neighbours don't land on the same picture. */
function assignImages(recs) {
  const used = {};
  return recs.map((rec, index) => {
    const style = dishStyle(rec.restaurant);
    const nth = used[style.category] ?? hashString(rec.restaurant.id);
    used[style.category] = nth + 1;
    const own = rotate(imagePaths(style.category), nth);
    const related = (RELATED_IMAGES[style.category] || []).flatMap(imagePaths);
    const pool = rotate(FOOD_POOL.flatMap(imagePaths), index * 3 + nth);
    const images = [...new Set([...own, ...rotate(related, index), ...pool])];
    return { ...style, images };
  });
}

// onload handler: skip a picture another card already shows, while alternatives remain.
window.cardImageLoaded = (img) => {
  const src = img.getAttribute("src");
  const taken = [...document.querySelectorAll("#results img[data-shown]")].some(
    (other) => other !== img && other.getAttribute("src") === src
  );
  if (taken && img.dataset.fallbacks) return nextCardImage(img);
  img.dataset.shown = "1";
  img.classList.remove("opacity-0");
};

// onerror handler for card images: try the next candidate, or fall back to the emoji.
window.nextCardImage = (img) => {
  const rest = (img.dataset.fallbacks || "").split("|").filter(Boolean);
  if (!rest.length) {
    img.remove();
    return;
  }
  img.dataset.fallbacks = rest.slice(1).join("|");
  img.src = rest[0];
};

const TAG_LABELS = {
  family_friendly: "Family-friendly",
  quick_service: "Quick service",
  buffet: "Buffet",
  bar: "Bar",
};

function featureTags(r) {
  const tags = [...(r.rest_types || []).slice(0, 2)];
  if (r.book_table) tags.push("Table booking");
  if (r.online_order) tags.push("Online order");
  for (const t of r.tags || []) {
    const label = TAG_LABELS[t];
    if (label) tags.push(label);
  }
  // Skip tags that repeat a cuisine chip (e.g. "Cafe" as both cuisine and type).
  const seen = new Set((r.cuisines || []).map((c) => c.toLowerCase().replace(/[^a-z]/g, "")));
  return tags.filter((t) => {
    const key = t.toLowerCase().replace(/[^a-z]/g, "");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function cardHtml(rec, visual) {
  const r = rec.restaurant;
  const top = rec.rank === 1;
  const ai = rec.source === "llm";
  const cuisines = r.cuisines || [];
  const shown = cuisines.slice(0, MAX_CHIPS);
  const more = cuisines.length - shown.length;

  const rankBadge = top
    ? `<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-primary-container text-[#0f0f12] text-xs font-bold shadow-sm shadow-primary-container/30">
         <span class="material-symbols-outlined fill text-[14px]">star</span> #1 Top pick
       </span>`
    : `<span class="tnum inline-flex items-center px-2.5 py-0.5 rounded-full bg-surface-container-highest text-primary text-xs font-semibold">#${Number(rec.rank)}</span>`;

  // AI picks are the norm, so only rule-based ones are labelled; one response can mix both (UI-06).
  const sourcePill = ai
    ? ""
    : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border border-outline-variant text-on-surface-variant text-[11px] font-semibold"><span class="material-symbols-outlined text-[12px]">filter_list</span> Basic ranking</span>`;

  const rating =
    r.rating == null
      ? `<span class="px-2 py-0.5 rounded-full bg-success/10 border border-success/30 text-success text-[11px] font-semibold">New / not rated</span>` // UI-09
      : `<span class="tnum inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-surface-container font-semibold ${r.rating >= 4 ? "text-success" : "text-tertiary"}">
           ★ ${Number(r.rating).toFixed(1)}
           <span class="font-normal text-on-surface-variant text-[11px]">(${formatNumber(r.votes)} ${r.votes === 1 ? "vote" : "votes"})</span>
         </span>`;

  const cuisineChips = shown
    .map((c) => `<span class="px-2 py-0.5 rounded bg-surface-container text-xs text-on-surface-variant">${esc(c)}</span>`)
    .join("");
  const moreChip = more
    ? `<span title="${esc(cuisines.slice(MAX_CHIPS).join(", "))}" class="px-2 py-0.5 rounded border border-dashed border-outline-variant text-xs text-on-surface-variant">+${more} more</span>`
    : "";
  const features = featureTags(r)
    .map((t) => `<span class="px-2 py-0.5 rounded bg-surface-variant text-[11px] font-semibold text-primary">${esc(t)}</span>`)
    .join("");

  const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${r.name}, ${r.address || r.locality}`)}`;
  const caveat = rec.caveats
    ? `<div class="flex items-start gap-1.5 text-tertiary text-xs italic">
         <span class="material-symbols-outlined text-[16px] shrink-0 not-italic">warning</span><span>${esc(rec.caveats)}</span>
       </div>`
    : `<span></span>`;

  return `
  <article class="group bg-surface-container-low rounded-2xl p-5 flex flex-col md:flex-row gap-5 transition-all border ${
    top ? "border-primary-container/30 shadow-xl shadow-primary-container/5" : "border-transparent shadow-lg"
  } hover:-translate-y-0.5 hover:border-primary-container/50 hover:shadow-[0_8px_32px_-8px_rgba(255,122,26,0.25)]">
    <div class="relative w-full md:w-56 h-44 md:h-auto md:min-h-[12rem] shrink-0 rounded-xl overflow-hidden bg-surface-container flex items-center justify-center">
      <div aria-hidden="true" class="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgba(255,122,26,0.28),transparent_60%),radial-gradient(circle_at_80%_90%,rgba(155,123,255,0.22),transparent_55%)]"></div>
      <span aria-hidden="true" class="relative text-5xl md:text-6xl drop-shadow-[0_6px_16px_rgba(0,0,0,0.6)]">${visual.icon}</span>
      ${
        visual.images.length
          ? `<img src="${visual.images[0]}" data-fallbacks="${visual.images.slice(1).join("|")}" alt="" decoding="async"
               onload="cardImageLoaded(this)" onerror="nextCardImage(this)"
               class="absolute inset-0 w-full h-full object-cover opacity-0 transition-[opacity,transform] duration-500 group-hover:scale-105" />`
          : ""
      }
      <div aria-hidden="true" class="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-surface-container-lowest/90 to-transparent pointer-events-none"></div>
      <span class="absolute bottom-2 left-2 max-w-[calc(100%-1rem)] truncate px-2 py-0.5 rounded bg-surface-container-lowest/80 backdrop-blur text-primary text-[11px] font-semibold">${esc(r.locality)}</span>
    </div>

    <div class="flex-1 min-w-0 flex flex-col gap-3">
      <div>
        <div class="flex items-center justify-between gap-2 flex-wrap mb-1.5">${rankBadge}${sourcePill}</div>
        <div class="flex items-baseline justify-between gap-x-3 gap-y-1 flex-wrap">
          <h3 class="font-display text-headline-md text-on-surface group-hover:text-primary transition-colors break-words min-w-0">${esc(r.name)}</h3>
          <span class="flex items-center gap-1 text-xs text-on-surface-variant shrink-0">
            <span class="material-symbols-outlined text-[16px] text-primary">pin_drop</span>${esc(r.locality)}
          </span>
        </div>
        ${r.address ? `<p class="text-xs text-on-surface-variant/60 mt-0.5 truncate" title="${esc(r.address)}">${esc(r.address)}</p>` : ""}

        <div class="flex items-center gap-x-3 gap-y-1.5 mt-2 flex-wrap text-xs">
          ${rating}
          <span class="text-on-surface-variant" aria-hidden="true">•</span>
          <span class="tnum text-on-surface font-medium">${esc(costText(r))}</span>
        </div>

        <div class="flex flex-wrap gap-1.5 mt-3">${cuisineChips}${moreChip}${features}</div>

        <p class="text-sm text-on-surface-variant mt-3 leading-relaxed">${esc(rec.explanation)}</p>
      </div>

      <div class="mt-auto flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 ${rec.caveats ? "bg-surface-container/40 p-2.5 rounded-xl" : ""}">
        ${caveat}
        <a href="${esc(mapsUrl)}" target="_blank" rel="noopener noreferrer"
          class="shrink-0 inline-flex items-center gap-1 px-3.5 py-1.5 rounded-full text-xs font-semibold transition-all ${
            top
              ? "bg-primary-container text-[#0f0f12] hover:brightness-110"
              : "bg-surface-container-high text-on-surface hover:bg-surface-variant"
          }">
          <span class="material-symbols-outlined text-[16px]">map</span> Open in Maps
        </a>
      </div>
    </div>
  </article>`;
}

function renderUnavailable(err) {
  $("app").hidden = true;
  const el = $("unavailable");
  el.hidden = false;
  el.innerHTML = `
    <div class="fade-in flex flex-col items-center text-center gap-4 p-10 rounded-3xl bg-surface-container-low shadow-xl shadow-black/40">
      <span class="w-20 h-20 rounded-full bg-error-container/30 flex items-center justify-center text-danger text-[40px]"><span class="material-symbols-outlined">cloud_off</span></span>
      <h1 class="font-display text-headline-md text-on-surface">Service unavailable</h1>
      <p class="text-sm text-on-surface-variant">${esc(err.message)}</p>
      <button type="button" id="retry-meta" class="mt-2 h-11 px-6 rounded-full bg-primary-container text-[#0f0f12] font-semibold text-sm flex items-center gap-2 hover:brightness-110 transition-all">
        <span class="material-symbols-outlined text-[18px]">refresh</span> Retry
      </button>
      ${err.requestId ? `<p class="tnum text-[11px] text-on-surface-variant/60">Request ID: ${esc(err.requestId)}</p>` : ""}
    </div>`;
  $("retry-meta").addEventListener("click", start);
}

function toast(message) {
  const el = document.createElement("div");
  el.className =
    "fade-in flex items-center gap-2 px-4 py-3 rounded-xl bg-surface-container-high border border-tertiary/30 text-tertiary text-sm shadow-[0_12px_32px_-4px_rgba(0,0,0,0.7)]";
  el.innerHTML = `<span class="material-symbols-outlined text-[20px]">hourglass_top</span><span></span>`;
  el.lastElementChild.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// --- Result-area actions (event delegation) --------------------------------------------

function bindResults() {
  $("results").addEventListener("click", (e) => {
    const target = e.target.closest("button");
    if (!target || state.pending) return;
    if (target.dataset.quick != null) {
      const q = QUICK_STARTS[Number(target.dataset.quick)];
      setForm({ ...q.prefs, cuisines: [...q.prefs.cuisines] });
      search();
    } else if (target.dataset.suggestion != null) {
      const err = state.error;
      applySuggestion(err.field, err.value, err.suggestions[Number(target.dataset.suggestion)]);
    } else if (target.dataset.tip) {
      setForm(TIP_ACTIONS[target.dataset.tip].apply());
      search();
    } else if (target.dataset.action === "retry") {
      search();
    } else if (target.dataset.action === "rerun" && state.query) {
      setForm({ ...state.query, extras: state.query.extras || "" });
      search();
    }
  });
}

// --- Start -----------------------------------------------------------------------------

let bound = false;

async function start() {
  try {
    await loadMeta();
  } catch (err) {
    renderUnavailable(err); // UI-01
    return;
  }
  $("unavailable").hidden = true;
  $("app").hidden = false;
  if (!bound) {
    bindForm();
    bindResults();
    bound = true;
  }
  renderAiStatus();
  syncFormControls();
  setFindButton();
  renderResults();
}

start();
