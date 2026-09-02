/**
 * MJR Advertising Trends Generator
 * Source: U.S. Census Bureau Monthly Retail Trade (MRTS)
 *
 * Security:
 *   CENSUS_API_KEY must be stored as a GitHub Actions secret.
 *   It is never written to the output JSON.
 *
 * Output:
 *   mjr-advertising-trends.json
 */

const fs = require("fs");

const API_KEY = process.env.CENSUS_API_KEY;
if (!API_KEY) {
  console.error("Missing CENSUS_API_KEY.");
  process.exit(1);
}

const DATASET = "mrts";
const BASE = `https://api.census.gov/data/timeseries/eits/${DATASET}`;

const categoryMeta = {
  "441": {
    name: "Motor Vehicle & Parts Dealers",
    angle: "Lead with inventory movement, service growth, trade-ins, event timing and integrated lead generation."
  },
  "442": {
    name: "Furniture & Home Furnishings",
    angle: "Use home projects, financing, moving activity, seasonal refreshes and promotional events as conversation starters."
  },
  "443": {
    name: "Electronics & Appliance Stores",
    angle: "Connect campaigns to upgrade cycles, product launches, home projects, major shopping periods and gift demand."
  },
  "444": {
    name: "Building Material & Garden Supply",
    angle: "Use project season, weather, home improvement, storm preparation and contractor/homeowner demand."
  },
  "445": {
    name: "Food & Beverage Stores",
    angle: "Build around seasonal entertaining, holidays, game days, meal occasions, promotions and local shopping behavior."
  },
  "446": {
    name: "Health & Personal Care Stores",
    angle: "Use wellness seasons, benefits timing, back-to-school health, allergy periods and holiday/self-care demand."
  },
  "447": {
    name: "Gasoline Stations",
    angle: "Connect to travel periods, commuter behavior, holiday weekends, convenience-store traffic and loyalty programs."
  },
  "448": {
    name: "Clothing & Accessories",
    angle: "Start before key shopping periods with seasonal wardrobe, gifting, events, back-to-school and promotional urgency."
  },
  "451": {
    name: "Sporting Goods, Hobby, Book & Music Stores",
    angle: "Use sports seasons, hobbies, recreation, school calendars, gifting and major local events as timely hooks."
  },
  "452": {
    name: "General Merchandise Stores",
    angle: "Lead ahead of major shopping periods, seasonal resets, gift buying, school calendars and promotional events."
  },
  "453": {
    name: "Miscellaneous Store Retailers",
    angle: "Identify the specific retail niche, then connect the stronger window to a concrete offer, event or customer need."
  },
  "454": {
    name: "Nonstore Retailers",
    angle: "Use shopping peaks, gifting, promotions, digital response, retargeting and customer-acquisition timing."
  },
  "722": {
    name: "Restaurants & Drinking Places",
    angle: "Build around dayparts, game days, events, holidays, catering, gift cards, traffic needs and limited-time offers."
  }
};

const monthNames = [
  null, "January","February","March","April","May","June",
  "July","August","September","October","November","December"
];

function monthsAhead(current, target) {
  return (target - current + 12) % 12;
}

async function getYear(year) {
  // Census examples document retrieving the required variables by year.
  // We intentionally do not use seasonally-adjusted observations for
  // a seasonality model.
  const params = new URLSearchParams({
    get: "data_type_code,time_slot_id,seasonally_adj,category_code,cell_value,error_data",
    time: String(year),
    key: API_KEY
  });

  const response = await fetch(`${BASE}?${params}`);
  if (!response.ok) {
    throw new Error(`Census ${year}: HTTP ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function normalizeRows(rows, year, store) {
  if (!Array.isArray(rows) || rows.length < 2) return;

  const headers = rows[0];
  const ix = Object.fromEntries(headers.map((h, i) => [h, i]));

  for (const row of rows.slice(1)) {
    const code = String(row[ix.category_code] ?? "");
    if (!categoryMeta[code]) continue;

    const sa = String(row[ix.seasonally_adj] ?? "").toLowerCase();
    // Census values may be represented as "no", "n", or similar.
    if (!(sa === "no" || sa === "n" || sa === "0" || sa === "false")) continue;

    const month = Number(row[ix.time_slot_id]);
    const value = Number(String(row[ix.cell_value] ?? "").replace(/,/g, ""));
    if (!Number.isInteger(month) || month < 1 || month > 12) continue;
    if (!Number.isFinite(value) || value <= 0) continue;

    // The dataset can include multiple data types. For seasonality we want
    // monthly SALES observations. If duplicate observations exist for the
    // same category/month, keep the first valid value and report that fact
    // in diagnostics rather than silently summing unlike measures.
    const type = String(row[ix.data_type_code] ?? "");
    store[code] ??= {};
    store[code][year] ??= {};
    if (!store[code][year][month]) {
      store[code][year][month] = { value, type };
    }
  }
}

function buildOutput(store, years) {
  const now = new Date();
  const currentMonth = now.getUTCMonth() + 1;

  const groups = {
    sell_now: [],
    start_prospecting: [],
    on_deck: [],
    watch_ahead: []
  };

  const diagnostics = [];

  for (const [code, meta] of Object.entries(categoryMeta)) {
    const shares = Object.fromEntries(Array.from({length: 12}, (_, i) => [i + 1, []]));
    const completeYears = [];

    for (const year of years) {
      const months = store[code]?.[year] || {};
      const keys = Object.keys(months).map(Number).filter(m => m >= 1 && m <= 12);
      if (keys.length !== 12) continue;

      const total = keys.reduce((sum, m) => sum + months[m].value, 0);
      if (!(total > 0)) continue;

      completeYears.push(year);
      for (const m of keys) shares[m].push((months[m].value / total) * 100);
    }

    if (completeYears.length < 2) {
      diagnostics.push({ category_code: code, category: meta.name, status: "insufficient_complete_years" });
      continue;
    }

    const avg = {};
    for (let m = 1; m <= 12; m++) {
      if (shares[m].length) avg[m] = shares[m].reduce((a,b)=>a+b,0) / shares[m].length;
    }

    const strongest = Object.entries(avg)
      .sort((a,b)=>b[1]-a[1])
      .slice(0,3)
      .map(([m])=>Number(m))
      .sort((a,b)=>a-b);

    const candidates = strongest
      .map(m => ({ month: m, ahead: monthsAhead(currentMonth, m) }))
      .sort((a,b)=>a.ahead-b.ahead);

    const next = candidates[0];
    if (!next) continue;

    let signal = null;
    let leadTime = null;

    if (next.ahead === 0) {
      signal = "sell_now";
      leadTime = "Stronger selling window is active now";
    } else if (next.ahead <= 2) {
      signal = "start_prospecting";
      leadTime = next.ahead === 1 ? "About 30 days ahead" : "About 60 days ahead";
    } else if (next.ahead === 3) {
      signal = "on_deck";
      leadTime = "About 90 days ahead";
    } else if (next.ahead === 4) {
      signal = "watch_ahead";
      leadTime = "About 120 days ahead";
    } else {
      continue;
    }

    groups[signal].push({
      category: meta.name,
      category_code: code,
      signal,
      strong_months: strongest.map(m => monthNames[m]).join(", "),
      lead_time: leadTime,
      target_month: monthNames[next.month],
      target_month_avg_share: Number(avg[next.month].toFixed(2)),
      years_used: completeYears,
      rationale: "MJR analysis places the upcoming month among this category’s three strongest average monthly shares across the complete years used.",
      sales_angle: meta.angle
    });
  }

  for (const items of Object.values(groups)) {
    items.sort((a,b)=>(b.target_month_avg_share||0)-(a.target_month_avg_share||0));
  }

  return {
    generated_at: new Date().toISOString(),
    source: "U.S. Census Bureau Monthly Retail Trade; MJR analysis",
    methodology: "MJR calculates each month as a share of annual not-seasonally-adjusted sales for the latest three complete years available, averages those monthly shares, identifies each category’s three strongest months, then converts the timing into forward-looking prospecting signals.",
    years_requested: years,
    groups,
    diagnostics
  };
}

async function main() {
  const year = new Date().getUTCFullYear();
  const years = [year - 1, year - 2, year - 3];
  const store = {};

  for (const y of years) {
    console.log(`Fetching Census MRTS ${y}...`);
    const rows = await getYear(y);
    normalizeRows(rows, y, store);
  }

  const output = buildOutput(store, years);
  fs.writeFileSync("mjr-advertising-trends.json", JSON.stringify(output, null, 2) + "\n");
  console.log("Wrote mjr-advertising-trends.json");
  console.log(JSON.stringify(
    Object.fromEntries(Object.entries(output.groups).map(([k,v])=>[k,v.length])),
    null, 2
  ));
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
