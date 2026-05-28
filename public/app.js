// Имоти Ловеч — клиентска логика: филтри + NEW badge + localStorage state
// Конфигурация per-страница от <body data-data="..." data-storage="...">

const EUR_TO_BGN = 1.95583;
const DATA_FILE = document.body.dataset.data || "data.json";
const STORAGE_KEY = document.body.dataset.storage || "imot-lovech-state-v1";
const APARTMENT_TYPES = new Set([
  "Едностаен", "Двустаен", "Тристаен", "Четиристаен",
  "Многостаен", "Мезонет", "Ателие", "Гарсониера", "Стая",
]);

// ----- State -----
let DATA = { listings: [], generated_at: null };
let STATE = loadState();
const FILTERS = {
  types: new Set(),
  regions: new Set(),
  priceMin: null, priceMax: null,
  areaMin: null, areaMax: null,
  ppmMin: null, ppmMax: null,
  sort: "new",
  onlyNew: false,
};

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const s = JSON.parse(raw);
      return {
        installed_at: s.installed_at,
        seen_ids: new Set(s.seen_ids || []),
      };
    }
  } catch (e) { /* fallthrough */ }
  // първо посещение
  const s = { installed_at: new Date().toISOString(), seen_ids: new Set() };
  saveState(s);
  return s;
}

function saveState(s = STATE) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    installed_at: s.installed_at,
    seen_ids: Array.from(s.seen_ids),
  }));
}

// ----- Helpers -----
const $ = (sel) => document.querySelector(sel);

function fmtPrice(eur) {
  if (eur == null) return "—";
  return new Intl.NumberFormat("bg-BG").format(Math.round(eur));
}

function fmtRelTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "току що";
  if (diff < 3600) return `преди ${Math.round(diff / 60)} мин`;
  if (diff < 86400) return `преди ${Math.round(diff / 3600)} ч`;
  if (diff < 86400 * 7) return `преди ${Math.round(diff / 86400)} дни`;
  return d.toLocaleDateString("bg-BG");
}

function isNew(listing) {
  if (!STATE.installed_at) return false;
  if (STATE.seen_ids.has(listing.id)) return false;
  return listing.first_seen && listing.first_seen > STATE.installed_at;
}

// Предикат с опция да игнорира даден фасет (за faceted броеве).
// ignore: "types" | "regions" | null
function passesFilters(r, ignore) {
  if (!r.still_active) return false;
  if (FILTERS.onlyNew && !isNew(r)) return false;
  if (ignore !== "types" && FILTERS.types.size && !FILTERS.types.has(r.type)) return false;
  if (ignore !== "regions" && FILTERS.regions.size && !FILTERS.regions.has(r.region)) return false;
  if (FILTERS.priceMin != null && (r.price_eur == null || r.price_eur < FILTERS.priceMin)) return false;
  if (FILTERS.priceMax != null && (r.price_eur == null || r.price_eur > FILTERS.priceMax)) return false;
  if (FILTERS.areaMin != null && (r.area_m2 == null || r.area_m2 < FILTERS.areaMin)) return false;
  if (FILTERS.areaMax != null && (r.area_m2 == null || r.area_m2 > FILTERS.areaMax)) return false;
  if (FILTERS.ppmMin != null && (r.price_per_m2_eur == null || r.price_per_m2_eur < FILTERS.ppmMin)) return false;
  if (FILTERS.ppmMax != null && (r.price_per_m2_eur == null || r.price_per_m2_eur > FILTERS.ppmMax)) return false;
  return true;
}

function applyFilters(listings) {
  return listings.filter((r) => passesFilters(r, null));
}

// Брои наличните имоти по тип/район, съобразено с другите активни филтри
function facetCount(key) {
  // key: "type" -> игнорира type филтъра; "region" -> игнорира region филтъра
  const ignore = key === "type" ? "types" : "regions";
  const counts = new Map();
  for (const r of DATA.listings) {
    if (!passesFilters(r, ignore)) continue;
    const v = r[key];
    if (v == null) continue;
    counts.set(v, (counts.get(v) || 0) + 1);
  }
  return counts;
}

function sortListings(listings) {
  const sortFn = {
    new: (a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""),
    price_asc: (a, b) => (a.price_eur ?? Infinity) - (b.price_eur ?? Infinity),
    price_desc: (a, b) => (b.price_eur ?? -1) - (a.price_eur ?? -1),
    area_desc: (a, b) => (b.area_m2 ?? -1) - (a.area_m2 ?? -1),
    ppm_asc: (a, b) => (a.price_per_m2_eur ?? Infinity) - (b.price_per_m2_eur ?? Infinity),
    ppm_desc: (a, b) => (b.price_per_m2_eur ?? -1) - (a.price_per_m2_eur ?? -1),
  }[FILTERS.sort];

  // NEW обявите винаги най-горе
  return [...listings].sort((a, b) => {
    const aNew = isNew(a), bNew = isNew(b);
    if (aNew !== bNew) return aNew ? -1 : 1;
    return sortFn(a, b);
  });
}

// ----- Rendering -----
function renderCard(r) {
  const card = document.createElement("a");
  card.className = "card" + (isNew(r) ? " new" : "");
  card.href = r.url;
  card.target = "_blank";
  card.rel = "noopener";
  card.dataset.id = r.id;
  card.addEventListener("click", () => {
    if (!STATE.seen_ids.has(r.id)) {
      STATE.seen_ids.add(r.id);
      saveState();
      card.classList.remove("new");
      const badge = card.querySelector(".new-badge");
      if (badge) badge.remove();
      updateNewPill();
    }
  });

  const newBadge = isNew(r) ? '<span class="new-badge">NEW</span>' : "";
  const region = r.region ? `<div class="card-region">${escape(r.region)}<small>${r.type ? "· " + escape(r.type) : ""}</small></div>` : "";
  const eur = r.price_eur != null
    ? `<div class="card-price">${fmtPrice(r.price_eur)} €<small>${r.area_m2 ? r.area_m2 + " m²" : ""}</small></div>`
    : `<div class="card-price">При запитване</div>`;
  const bgn = r.price_bgn != null ? `<div class="card-bgn">${fmtPrice(r.price_bgn)} лв</div>` : "";
  const specs = [];
  if (r.price_per_m2_eur) specs.push(`<span>${fmtPrice(r.price_per_m2_eur)} €/m²</span>`);
  if (r.floor != null && r.total_floors != null) specs.push(`<span>етаж ${r.floor}/${r.total_floors}</span>`);
  else if (r.floor != null) specs.push(`<span>етаж ${r.floor}</span>`);
  if (r.construction) specs.push(`<span>${escape(r.construction)}</span>`);
  if (r.year_from) {
    const yr = r.year_to && r.year_to !== r.year_from ? `${r.year_from}-${r.year_to}` : r.year_from;
    specs.push(`<span>${yr} г.</span>`);
  }
  if (isNew(r)) specs.push(`<span title="${r.first_seen}">появи се ${fmtRelTime(r.first_seen)}</span>`);

  card.innerHTML = `
    ${newBadge}
    <span class="card-type">${r.type ? escape(r.type) : "Имот"}</span>
    ${region}
    ${eur}
    ${bgn}
    ${specs.length ? `<div class="card-specs">${specs.join("")}</div>` : ""}
  `;
  return card;
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function render() {
  const filtered = sortListings(applyFilters(DATA.listings));
  const grid = $("#grid");
  grid.innerHTML = "";
  for (const r of filtered) grid.appendChild(renderCard(r));
  $("#empty").hidden = filtered.length > 0;
  $("#count-pill").textContent = `${filtered.length} обяви`;
  updateNewPill();
  updateFilterCountBadge();
  updateFacetCounts();
}

function updateNewPill() {
  const newCount = DATA.listings.filter((r) => r.still_active && isNew(r)).length;
  const pill = $("#new-pill");
  if (newCount > 0) {
    pill.hidden = false;
    pill.textContent = `${newCount} нови`;
  } else {
    pill.hidden = true;
  }
}

// ----- Filter UI -----
// Регистър на чиповете по фасет, за да обновяваме броевете на място
const CHIP_REGISTRY = { type: [], region: [] };

function buildChips(containerId, items, filterSet, facetKey) {
  const c = document.getElementById(containerId);
  c.innerHTML = "";
  CHIP_REGISTRY[facetKey] = [];
  for (const [name] of items) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.type = "button";
    const label = document.createElement("span");
    label.textContent = name;
    const countSpan = document.createElement("span");
    countSpan.className = "count";
    btn.append(label, countSpan);
    btn.addEventListener("click", () => {
      if (filterSet.has(name)) filterSet.delete(name);
      else filterSet.add(name);
      btn.classList.toggle("active");
      render();
    });
    c.appendChild(btn);
    CHIP_REGISTRY[facetKey].push({ name, btn, countSpan });
  }
}

// Обновява броевете и затъмнява недостъпните опции спрямо другите филтри
function updateFacetCounts() {
  for (const [facetKey, dataKey] of [["type", "type"], ["region", "region"]]) {
    const counts = facetCount(dataKey);
    for (const { name, btn, countSpan } of CHIP_REGISTRY[facetKey]) {
      const n = counts.get(name) || 0;
      countSpan.textContent = n;
      // Затъмни опции с 0 налични (освен ако са избрани в момента)
      btn.classList.toggle("chip-empty", n === 0 && !btn.classList.contains("active"));
    }
  }
}

function setupNumberFilter(id, key) {
  const el = document.getElementById(id);
  let timer;
  el.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const v = el.value.trim();
      FILTERS[key] = v === "" ? null : Number(v);
      render();
    }, 200);
  });
}

function buildFilters() {
  // Тип имот: подреден по брой активни
  const typeCounts = new Map();
  const regionCounts = new Map();
  for (const r of DATA.listings) {
    if (!r.still_active) continue;
    if (r.type) typeCounts.set(r.type, (typeCounts.get(r.type) || 0) + 1);
    if (r.region) regionCounts.set(r.region, (regionCounts.get(r.region) || 0) + 1);
  }
  const types = [...typeCounts.entries()].sort((a, b) => b[1] - a[1]);
  const regions = [...regionCounts.entries()].sort((a, b) => b[1] - a[1]);
  buildChips("filter-types", types, FILTERS.types, "type");
  buildChips("filter-regions", regions, FILTERS.regions, "region");

  setupNumberFilter("price-min", "priceMin");
  setupNumberFilter("price-max", "priceMax");
  setupNumberFilter("area-min", "areaMin");
  setupNumberFilter("area-max", "areaMax");
  setupNumberFilter("ppm-min", "ppmMin");
  setupNumberFilter("ppm-max", "ppmMax");

  // Sort: синхронизирани desktop (#sort) и mobile (#sort-mobile)
  const onSort = (v) => {
    FILTERS.sort = v;
    $("#sort").value = v;
    $("#sort-mobile").value = v;
    render();
  };
  $("#sort").addEventListener("change", (e) => onSort(e.target.value));
  $("#sort-mobile").addEventListener("change", (e) => onSort(e.target.value));

  $("#clear-filters").addEventListener("click", () => {
    FILTERS.types.clear();
    FILTERS.regions.clear();
    FILTERS.priceMin = FILTERS.priceMax = null;
    FILTERS.areaMin = FILTERS.areaMax = null;
    FILTERS.ppmMin = FILTERS.ppmMax = null;
    document.querySelectorAll(".chip.active").forEach((c) => c.classList.remove("active"));
    document.querySelectorAll(".filters input[type=number]").forEach((i) => i.value = "");
    render();
  });

  $("#toggle-only-new").addEventListener("click", (e) => {
    FILTERS.onlyNew = !FILTERS.onlyNew;
    e.currentTarget.classList.toggle("active", FILTERS.onlyNew);
    render();
  });

  $("#mark-all-seen").addEventListener("click", () => {
    for (const r of DATA.listings) {
      if (r.still_active) STATE.seen_ids.add(r.id);
    }
    saveState();
    render();
  });

  setupDrawer();
}

// ----- Mobile drawer -----
function setupDrawer() {
  const filters = $("#filters");
  const backdrop = $("#filter-backdrop");
  const toggle = $("#filter-toggle");

  // iOS-съвместимо заключване на фона (алгоритъмът на body-scroll-lock):
  // позволяваме скрол ВЪТРЕ в панела, но ръчно спираме "изтичането" към
  // фона в краищата. Не разчитаме на overscroll-behavior (липсва на iOS<16)
  // нито на body position:fixed (чупи вътрешния скрол на iOS).
  let startY = 0;
  const onTouchStart = (e) => {
    if (e.touches.length === 1) startY = e.touches[0].clientY;
  };
  const onTouchMove = (e) => {
    if (e.touches.length !== 1) return;
    // жест извън панела → изобщо не скролваме
    if (!filters.contains(e.target)) { e.preventDefault(); return; }
    const dy = e.touches[0].clientY - startY;      // >0 = пръстът надолу
    const atTop = filters.scrollTop <= 0;
    const atBottom =
      filters.scrollTop + filters.clientHeight >= filters.scrollHeight - 1;
    // в горния/долния край спираме, за да не поеме скрола фонът
    if ((atTop && dy > 0) || (atBottom && dy < 0)) e.preventDefault();
  };

  const open = () => {
    filters.classList.add("open");
    backdrop.hidden = false;
    requestAnimationFrame(() => backdrop.classList.add("show"));
    toggle.setAttribute("aria-expanded", "true");
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, { passive: false });
  };
  const close = () => {
    filters.classList.remove("open");
    backdrop.classList.remove("show");
    setTimeout(() => { backdrop.hidden = true; }, 220);
    toggle.setAttribute("aria-expanded", "false");
    document.removeEventListener("touchstart", onTouchStart);
    document.removeEventListener("touchmove", onTouchMove);
  };

  toggle.addEventListener("click", () => {
    filters.classList.contains("open") ? close() : open();
  });
  backdrop.addEventListener("click", close);
  $("#filter-close").addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}

function activeFilterCount() {
  let n = FILTERS.types.size + FILTERS.regions.size;
  for (const k of ["priceMin", "priceMax", "areaMin", "areaMax", "ppmMin", "ppmMax"]) {
    if (FILTERS[k] != null) n++;
  }
  if (FILTERS.onlyNew) n++;
  return n;
}

function updateFilterCountBadge() {
  const n = activeFilterCount();
  const badge = $("#active-filter-count");
  if (n > 0) { badge.hidden = false; badge.textContent = n; }
  else { badge.hidden = true; }
}

// ----- Bootstrap -----
async function init() {
  try {
    const res = await fetch(DATA_FILE + "?t=" + Date.now());
    DATA = await res.json();
  } catch (e) {
    $("#grid").innerHTML = `<div class="empty">Не мога да заредя data.json: ${escape(e.message)}</div>`;
    return;
  }
  $("#updated").textContent = "обновено " + fmtRelTime(DATA.generated_at);
  buildFilters();
  render();
}

init();
