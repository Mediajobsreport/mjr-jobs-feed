const fs = require("fs");
const path = require("path");
const XLSX = require("xlsx");
const AdmZip = require("adm-zip");

const OUTPUT_DIR = path.join(process.cwd(), "..", "data");
const OUTPUT_FILE = path.join(OUTPUT_DIR, "mjr-recalls.json");
const TEMP_OUTPUT_FILE = OUTPUT_FILE + ".tmp";

const FDA_XLSX =
  "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/datatables-data?_format=xlsx&page=";

const FDA_2026_XML =
  "https://www.fda.gov/media/191968/download?attachment=";

const CPSC_API =
  "https://www.saferproducts.gov/RestWebServices/Recall?format=json";

const USDA_API =
  "https://www.fsis.usda.gov/fsis/api/recall/v/1";

const NHTSA_ZIP =
  "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip";

const FDA_PAGE =
  "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts";

const FDA_ANIMAL_PAGE =
  "https://www.fda.gov/animal-veterinary/safety-health/recalls-withdrawals";

const CPSC_PAGE =
  "https://www.cpsc.gov/Recalls";

const USDA_PAGE =
  "https://www.fsis.usda.gov/recalls";

const NHTSA_PAGE =
  "https://www.nhtsa.gov/recalls";

/*
  SAFETY FLOOR

  These are intentionally far below the normal counts.

  Normal recent runs:
  FDA   ~1,000
  CPSC  ~9,990
  USDA  ~2,023
  NHTSA ~100

  If a source suddenly falls below these floors, the build stops
  BEFORE overwriting the existing live JSON.
*/
const MIN_SOURCE_COUNTS = {
  FDA: 500,
  CPSC: 5000,
  USDA: 500,
  NHTSA: 50
};

const MAJOR_BRANDS = [
  "great value",
  "walmart",
  "mainstays",
  "costco",
  "kirkland",
  "target",
  "amazon",
  "aldi",
  "kroger",
  "publix",
  "trader joe's",
  "trader joes",
  "whole foods",
  "h-e-b",
  "heb",
  "wegmans",
  "safeway",
  "albertsons",
  "meijer",
  "food lion",
  "nestle",
  "kraft",
  "heinz",
  "pepsico",
  "coca-cola",
  "general mills",
  "kellogg",
  "kellanova",
  "campbell",
  "conagra",
  "tyson",
  "perdue",
  "smucker",
  "purina",
  "pedigree",
  "iams",
  "royal canin",
  "hill's",
  "hills",
  "blue buffalo",
  "fromm",
  "northwest naturals",
  "freshpet",
  "abbott",
  "baxter",
  "b. braun",
  "b braun",
  "medtronic",
  "ge healthcare",
  "boston scientific",
  "cardinal health",
  "stryker",
  "philips",
  "cuisinart",
  "conair",
  "apple",
  "samsung",
  "sony",
  "lg",
  "whirlpool",
  "frigidaire",
  "maytag",
  "kitchenaid",
  "dewalt",
  "ryobi",
  "milwaukee",
  "ikea",
  "home depot",
  "lowe's",
  "lowes",
  "ford",
  "lincoln",
  "general motors",
  "chevrolet",
  "gmc",
  "buick",
  "cadillac",
  "toyota",
  "lexus",
  "honda",
  "acura",
  "nissan",
  "infiniti",
  "hyundai",
  "kia",
  "subaru",
  "mazda",
  "volkswagen",
  "audi",
  "bmw",
  "mercedes",
  "volvo",
  "tesla",
  "rivian",
  "stellantis",
  "chrysler",
  "dodge",
  "jeep",
  "ram"
];

function clean(v) {
  return String(v == null ? "" : v)
    .replace(/^\uFEFF/, "")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]*>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&ndash;/gi, "–")
    .replace(/&mdash;/gi, "—")
    .replace(/\s+/g, " ")
    .trim();
}

function lower(v) {
  return clean(v).toLowerCase();
}

function slug(v) {
  return lower(v)
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 70);
}

function normalizeFieldName(v) {
  return clean(v)
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function normalizeMatchText(v) {
  return clean(v)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function safeDate(v) {
  const s = clean(v);

  if (!s) return "";

  if (/^\d{8}$/.test(s)) {
    return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  }

  let m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);

  if (m) {
    return `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}`;
  }

  m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);

  if (m) {
    return `${m[1]}-${m[2]}-${m[3]}`;
  }

  const d = new Date(s);

  return Number.isNaN(d.getTime())
    ? ""
    : d.toISOString().slice(0, 10);
}

function daysOld(date) {
  if (!date) return 999;

  const d = new Date(date + "T12:00:00Z");

  return Number.isNaN(d.getTime())
    ? 999
    : Math.max(
        0,
        Math.floor((Date.now() - d.getTime()) / 86400000)
      );
}

function shorten(v, max) {
  const s = clean(v);

  if (s.length <= max) return s;

  return (
    s
      .slice(0, max - 1)
      .replace(/\s+\S*$/, "") +
    "…"
  );
}

function escapeRegex(v) {
  return v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function containsBrand(text, brand) {
  const b = lower(brand);

  const pattern =
    "(^|[^a-z0-9])" +
    escapeRegex(b).replace(/\s+/g, "\\s+") +
    "([^a-z0-9]|$)";

  return new RegExp(pattern, "i").test(lower(text));
}

function hasMajorBrand(text) {
  return MAJOR_BRANDS.some(
    brand =>
      containsBrand(
        text,
        brand
      )
  );
}

function isPetRecall(v) {
  return /pet food|dog food|cat food|dog treat|cat treat|feline|canine|milk replacer|animal feed|pet treat/i
    .test(v);
}

function classify(text, productType, source) {
  const s = lower(`${text} ${productType}`);

  if (
    source === "NHTSA" ||
    source === "CPSC"
  ) {
    return "consumer";
  }

  if (
    /dietary supplement|supplements|drug|drugs|medical device|medical devices|pharmaceutical|injection|tablet|capsule|syringe|catheter|implant|infusion/
      .test(s)
  ) {
    return "health";
  }

  if (
    isPetRecall(s) ||
    /food|beverage|ingredient|meat|poultry|egg|cheese|seafood|allergen|salmonella|listeria|e\. coli|stec/
      .test(s)
  ) {
    return "food";
  }

  return "consumer";
}

function freshnessScore(date) {
  const age = daysOld(date);

  if (age <= 1) return 70;
  if (age <= 2) return 64;
  if (age <= 3) return 58;
  if (age <= 5) return 50;
  if (age <= 7) return 42;
  if (age <= 10) return 30;
  if (age <= 14) return 18;
  if (age <= 21) return 8;

  return 0;
}

function severityScore(v) {
  const s = lower(v);
  let n = 0;

  if (
    /actual death|fatalit|deaths? reported|has died|resulted in death/
      .test(s)
  ) {
    n += 55;
  } else if (
    /death|fatal|life-threatening|life threatening|do not drive/
      .test(s)
  ) {
    n += 28;
  }

  if (
    /fire hazard|fire risk|electrocution|explosion|crash risk|crash hazard/
      .test(s)
  ) {
    n += 35;
  }

  if (
    /salmonella|listeria|e\. coli|stec|botulism/
      .test(s)
  ) {
    n += 45;
  }

  if (
    /undeclared milk|undeclared peanut|undeclared egg|undeclared allergen/
      .test(s)
  ) {
    n += 28;
  }

  if (
    /choking|suffocation|lead exposure|poisoning|burn hazard/
      .test(s)
  ) {
    n += 25;
  }

  if (
    /particulate|foreign material|foreign object|contamination|sterility assurance/
      .test(s)
  ) {
    n += 18;
  }

  return n;
}

function scaleScore(units) {
  const n = Number(units) || 0;

  if (n >= 3000000) return 100;
  if (n >= 1000000) return 92;
  if (n >= 500000) return 82;
  if (n >= 250000) return 72;
  if (n >= 100000) return 62;
  if (n >= 50000) return 50;
  if (n >= 25000) return 40;
  if (n >= 10000) return 30;
  if (n >= 5000) return 20;
  if (n >= 1000) return 12;

  return 0;
}

function totalScore(item) {
  const brandText = [
    item.title,
    item.brand,
    item.company,
    item.product
  ].join(" ");

  const hazardText = [
    item.title,
    item.reason
  ].join(" ");

  let n =
    freshnessScore(item.date) +
    severityScore(hazardText) +
    scaleScore(item.units);

  if (hasMajorBrand(brandText)) {
    n += 55;
  }

  if (item.pet) {
    n += 18;
  }

  if (
    item.source === "NHTSA" &&
    item.units >= 100000
  ) {
    n += 15;
  }

  return n;
}

function parseNumber(v) {
  const m =
    clean(v)
      .replace(/,/g, "")
      .match(/(\d+(?:\.\d+)?)/);

  return m
    ? Number(m[1]) || 0
    : 0;
}

function extractUnits(v) {
  const s = clean(v);

  const patterns = [
    /(?:about|approximately|nearly|more than|over)?\s*([\d,]+)\s+(?:units|vehicles|cars|trucks|products|items|devices|dressers|brushes|packages|cases|pounds)\b/i,
    /recall(?:s|ed|ing)?\s+(?:about|approximately|nearly|more than|over)?\s*([\d,]+)/i
  ];

  for (const p of patterns) {
    const m = s.match(p);

    if (m) {
      const n =
        Number(
          m[1]
            .replace(/,/g, "")
        );

      if (Number.isFinite(n)) {
        return n;
      }
    }
  }

  return 0;
}

/* =========================================================
   NETWORK RETRY / RELIABILITY
   ========================================================= */

function sleep(ms) {
  return new Promise(
    resolve =>
      setTimeout(
        resolve,
        ms
      )
  );
}

function isRetryableStatus(status) {
  return [
    408,
    425,
    429,
    500,
    502,
    503,
    504
  ].includes(status);
}

function isRetryableError(err) {
  const code =
    err &&
    (
      err.code ||
      err.cause?.code
    );

  if (
    [
      "ECONNRESET",
      "ETIMEDOUT",
      "ECONNREFUSED",
      "EAI_AGAIN",
      "ENETUNREACH",
      "EPIPE",
      "UND_ERR_SOCKET",
      "UND_ERR_CONNECT_TIMEOUT",
      "UND_ERR_HEADERS_TIMEOUT",
      "UND_ERR_BODY_TIMEOUT"
    ].includes(code)
  ) {
    return true;
  }

  const message =
    String(
      err?.message ||
      err ||
      ""
    ).toLowerCase();

  return (
    message.includes("terminated") ||
    message.includes("fetch failed") ||
    message.includes("socket") ||
    message.includes("connection reset") ||
    message.includes("network")
  );
}

async function fetchWithRetry(
  url,
  options = {},
  label = "",
  maxAttempts = 4
) {
  let lastError = null;

  for (
    let attempt = 1;
    attempt <= maxAttempts;
    attempt++
  ) {
    try {
      const response =
        await fetch(
          url,
          options
        );

      if (response.ok) {
        if (attempt > 1) {
          console.log(
            `${label || url} succeeded on retry ${attempt}/${maxAttempts}`
          );
        }

        return response;
      }

      const error =
        new Error(
          `${url} returned HTTP ${response.status}`
        );

      error.status =
        response.status;

      if (
        !isRetryableStatus(
          response.status
        ) ||
        attempt === maxAttempts
      ) {
        throw error;
      }

      lastError =
        error;

      console.warn(
        `${label || url} temporary HTTP ${response.status}; retry ${attempt}/${maxAttempts}`
      );
    } catch (err) {
      lastError =
        err;

      const retryable =
        isRetryableError(
          err
        ) ||
        isRetryableStatus(
          err?.status
        );

      if (
        !retryable ||
        attempt === maxAttempts
      ) {
        throw err;
      }

      console.warn(
        `${label || url} temporary network failure; retry ${attempt}/${maxAttempts}:`,
        err.message ||
        err
      );
    }

    /*
      Short progressive delay:
      1.5 sec
      3 sec
      6 sec
    */
    const delay =
      1500 *
      Math.pow(
        2,
        attempt - 1
      );

    await sleep(
      delay
    );
  }

  throw lastError ||
    new Error(
      `Unable to fetch ${url}`
    );
}

async function fetchBuffer(
  url,
  label = ""
) {
  const r =
    await fetchWithRetry(
      url,
      {
        headers: {
          "User-Agent":
            "MediaJobsReport-RecallFeed/2.2"
        },

        redirect:
          "follow"
      },
      label ||
      url
    );

  return Buffer.from(
    await r.arrayBuffer()
  );
}

async function fetchJSON(
  url,
  label = ""
) {
  const r =
    await fetchWithRetry(
      url,
      {
        headers: {
          "User-Agent":
            "MediaJobsReport-RecallFeed/2.2",

          "Accept":
            "application/json"
        },

        redirect:
          "follow"
      },
      label ||
      url
    );

  return r.json();
}

async function fetchText(
  url,
  label = ""
) {
  const r =
    await fetchWithRetry(
      url,
      {
        headers: {
          "User-Agent":
            "MediaJobsReport-RecallFeed/2.2",

          "Accept":
            "text/html,application/xhtml+xml"
        },

        redirect:
          "follow"
      },
      label ||
      url
    );

  return r.text();
}

async function fetchXML(
  url,
  label = ""
) {
  const r =
    await fetchWithRetry(
      url,
      {
        headers: {
          "User-Agent":
            "MediaJobsReport-RecallFeed/2.2",

          "Accept":
            "application/xml,text/xml,text/plain,*/*"
        },

        redirect:
          "follow"
      },
      label ||
      url
    );

  return r.text();
}

async function mapLimit(
  items,
  limit,
  worker
) {
  const results =
    new Array(
      items.length
    );

  let next =
    0;

  async function runner() {
    while (true) {
      const i =
        next++;

      if (
        i >=
        items.length
      ) {
        return;
      }

      results[i] =
        await worker(
          items[i],
          i
        );
    }
  }

  await Promise.all(
    Array.from(
      {
        length:
          Math.min(
            limit,
            items.length
          )
      },

      () =>
        runner()
    )
  );

  return results;
}

function absoluteUrl(
  v,
  base
) {
  const s =
    clean(v);

  if (!s) {
    return "";
  }

  try {
    return new URL(
      s,
      base
    ).toString();
  } catch (_) {
    return "";
  }
}

function isGenericRecallPage(
  url,
  source
) {
  const s =
    lower(url)
      .replace(
        /\/$/,
        ""
      );

  if (!s) {
    return true;
  }

  if (source === "FDA") {
    return (
      s ===
      lower(
        FDA_PAGE
      ).replace(
        /\/$/,
        ""
      )
    );
  }

  if (source === "CPSC") {
    return (
      s ===
      lower(
        CPSC_PAGE
      ).replace(
        /\/$/,
        ""
      )
    );
  }

  if (source === "USDA") {
    return (
      s ===
      lower(
        USDA_PAGE
      ).replace(
        /\/$/,
        ""
      )
    );
  }

  if (source === "NHTSA") {
    return (
      s ===
      lower(
        NHTSA_PAGE
      ).replace(
        /\/$/,
        ""
      )
    );
  }

  return false;
}

function firstSpecificUrl(
  values,
  base,
  source
) {
  for (const v of values) {
    const url =
      absoluteUrl(
        v,
        base
      );

    if (
      url &&
      !isGenericRecallPage(
        url,
        source
      )
    ) {
      return url;
    }
  }

  return "";
}

function tokenSimilarity(
  a,
  b
) {
  const aa =
    new Set(
      normalizeMatchText(a)
        .split(" ")
        .filter(
          x =>
            x.length > 2
        )
    );

  const bb =
    new Set(
      normalizeMatchText(b)
        .split(" ")
        .filter(
          x =>
            x.length > 2
        )
    );

  if (
    !aa.size ||
    !bb.size
  ) {
    return 0;
  }

  let common =
    0;

  for (const x of aa) {
    if (bb.has(x)) {
      common++;
    }
  }

  return (
    common /
    Math.max(
      aa.size,
      bb.size
    )
  );
}

function tokenCoverage(
  needle,
  haystack
) {
  const needed =
    new Set(
      normalizeMatchText(
        needle
      )
        .split(" ")
        .filter(
          x =>
            x.length > 2
        )
    );

  const available =
    new Set(
      normalizeMatchText(
        haystack
      )
        .split(" ")
        .filter(
          x =>
            x.length > 2
        )
    );

  if (
    !needed.size ||
    !available.size
  ) {
    return 0;
  }

  let common =
    0;

  for (const x of needed) {
    if (available.has(x)) {
      common++;
    }
  }

  return common / needed.size;
}

function cleanProductName(v) {
  return shorten(
    clean(v)
      .replace(
        /\bpackaged in the following configurations?:.*$/i,
        ""
      )
      .replace(
        /\bpackaged as follows?:.*$/i,
        ""
      )
      .replace(
        /\bnet (?:wt|weight)\b.*$/i,
        ""
      )
      .replace(
        /\bupc\b.*$/i,
        ""
      )
      .replace(
        /\bdistributed by\b.*$/i,
        ""
      )
      .replace(
        /\bkeep refrigerated\b.*$/i,
        ""
      )
      .trim(),

    80
  );
}

function makeFDAHeadline(
  brand,
  product,
  company
) {
  const b =
    clean(
      brand
    );

  const p =
    cleanProductName(
      product
    );

  const s =
    lower(
      `${b} ${p} ${company}`
    );

  if (
    /northwest naturals/
      .test(s) &&
    /chicken/
      .test(s)
  ) {
    return "Northwest Naturals Chicken Recipe Pet Food Recalled";
  }

  if (
    /b\.?\s*braun/
      .test(s) &&
    /sodium chloride/
      .test(s)
  ) {
    return "B. Braun Sodium Chloride Injection Recalled";
  }

  if (
    /baxter/
      .test(s) &&
    /sodium chloride/
      .test(s)
  ) {
    return "Baxter Sodium Chloride Injection Recalled";
  }

  if (
    /feline milk replacer/
      .test(s)
  ) {
    return "Shelter’s Choice and Breeder’s Edge Feline Milk Replacers Recalled";
  }

  if (
    b &&
    p
  ) {
    return shorten(
      `${b} ${p} Recalled`,
      100
    );
  }

  if (p) {
    return shorten(
      `${p} Recalled`,
      100
    );
  }

  if (b) {
    return shorten(
      `${b} Product Recalled`,
      100
    );
  }

  return "FDA Product Recall";
}

/* =========================================================
   FDA XLSX
   ========================================================= */

function fdaRowsFromSheet(sheet) {
  const matrix =
    XLSX.utils
      .sheet_to_json(
        sheet,
        {
          header:
            1,

          defval:
            "",

          raw:
            false
        }
      );

  const normalized =
    matrix.map(
      row =>
        row.map(
          cell =>
            normalizeFieldName(
              cell
            )
        )
    );

  const headerIndex =
    normalized
      .findIndex(
        row =>
          row.includes(
            "date"
          ) &&
          row.includes(
            "brandnames"
          ) &&
          row.includes(
            "productdescription"
          ) &&
          row.includes(
            "companyname"
          )
      );

  if (
    headerIndex < 0
  ) {
    console.log(
      "FDA first rows:",
      matrix.slice(
        0,
        8
      )
    );

    throw new Error(
      "FDA header row not found"
    );
  }

  const headers =
    matrix[
      headerIndex
    ].map(
      cell =>
        clean(
          cell
        )
    );

  console.log(
    "FDA header row:",
    headers
  );

  return matrix
    .slice(
      headerIndex + 1
    )
    .map(
      (
        row,
        offset
      ) => ({
        row,

        sheetRow:
          headerIndex +
          1 +
          offset
      })
    )
    .filter(
      x =>
        x.row.some(
          cell =>
            clean(
              cell
            )
        )
    )
    .map(
      ({
        row,
        sheetRow
      }) => {
        const obj =
          {};

        const directLinks =
          [];

        headers.forEach(
          (
            header,
            i
          ) => {
            if (header) {
              obj[
                header
              ] =
                clean(
                  row[i]
                );
            }

            const cellAddress =
              XLSX.utils
                .encode_cell(
                  {
                    r:
                      sheetRow,

                    c:
                      i
                  }
                );

            const cell =
              sheet[
                cellAddress
              ];

            const target =
              clean(
                cell &&
                cell.l &&
                cell.l.Target
              );

            if (target) {
              directLinks.push(
                target
              );
            }
          }
        );

        obj.__direct_url =
          firstSpecificUrl(
            directLinks,
            "https://www.fda.gov",
            "FDA"
          );

        return obj;
      }
    );
}

function pickField(
  row,
  names
) {
  const keys =
    Object.keys(
      row
    );

  for (const name of names) {
    const wanted =
      normalizeFieldName(
        name
      );

    const exact =
      keys.find(
        k =>
          normalizeFieldName(
            k
          ) ===
          wanted
      );

    if (exact) {
      return clean(
        row[
          exact
        ]
      );
    }
  }

  return "";
}

/* =========================================================
   FDA HTML FALLBACK
   ========================================================= */

function parseFDAListingRows(html) {
  const found =
    [];

  const rowRe =
    /<tr\b[^>]*>([\s\S]*?)<\/tr>/gi;

  let rowMatch;

  while (
    (
      rowMatch =
        rowRe.exec(
          html
        )
    )
  ) {
    const rowHtml =
      rowMatch[1];

    const cells =
      [];

    const cellRe =
      /<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi;

    let cellMatch;

    while (
      (
        cellMatch =
          cellRe.exec(
            rowHtml
          )
      )
    ) {
      cells.push(
        clean(
          cellMatch[1]
        )
      );
    }

    if (
      cells.length < 5
    ) {
      continue;
    }

    const linkMatches = [
      ...rowHtml.matchAll(
        /<a\b[^>]*href=["']([^"']+)["'][^>]*>/gi
      )
    ];

    const link =
      linkMatches
        .map(
          m =>
            absoluteUrl(
              m[1],
              "https://www.fda.gov"
            )
        )
        .find(
          url =>
            /\/safety\/recalls-market-withdrawals-safety-alerts\/[^/?#]+/i
              .test(
                url
              ) &&
            lower(
              url
            )
              .replace(
                /\/$/,
                ""
              ) !==
            lower(
              FDA_PAGE
            )
              .replace(
                /\/$/,
                ""
              )
        );

    if (!link) {
      continue;
    }

    found.push(
      {
        date:
          safeDate(
            cells[0]
          ),

        brand:
          clean(
            cells[1] ||
            ""
          ),

        product:
          clean(
            cells[2] ||
            ""
          ),

        productType:
          clean(
            cells[3] ||
            ""
          ),

        reason:
          clean(
            cells[4] ||
            ""
          ),

        company:
          clean(
            cells[5] ||
            ""
          ),

        url:
          link
      }
    );
  }

  return found;
}

async function loadFDADetailLinks() {
  const found =
    [];

  const pages =
    Array.from(
      {
        length:
          20
      },

      (
        _,
        i
      ) =>
        i === 0
          ? FDA_PAGE
          : `${FDA_PAGE}?Page=${i}`
    );

  pages.push(
    FDA_ANIMAL_PAGE
  );

  await mapLimit(
    pages,
    4,
    async pageUrl => {
      try {
        const html =
          await fetchText(
            pageUrl,
            "FDA listing"
          );

        found.push(
          ...parseFDAListingRows(
            html
          )
        );
      } catch (err) {
        console.warn(
          "FDA listing page failed:",
          pageUrl,
          err.message ||
          err
        );
      }
    }
  );

  const unique =
    new Map();

  for (const row of found) {
    unique.set(
      `${row.date}|${normalizeMatchText(row.brand)}|${normalizeMatchText(row.product)}|${row.url}`,
      row
    );
  }

  console.log(
    `FDA detail links discovered: ${unique.size}`
  );

  return Array.from(
    unique.values()
  );
}

function matchFDADetailUrl(
  item,
  links
) {
  const sameDate =
    links.filter(
      x =>
        !item.date ||
        x.date ===
          item.date
    );

  const pool =
    sameDate.length
      ? sameDate
      : links;

  const exact =
    pool.find(
      x =>
        normalizeMatchText(
          x.brand
        ) ===
          normalizeMatchText(
            item.brand
          ) &&
        normalizeMatchText(
          x.product
        ) ===
          normalizeMatchText(
            item.product
          )
    );

  if (exact) {
    return exact.url;
  }

  let best =
    null;

  let bestScore =
    0;

  for (const x of pool) {
    const brandScore =
      tokenSimilarity(
        item.brand,
        x.brand
      );

    const productScore =
      tokenSimilarity(
        item.product,
        x.product
      );

    const companyScore =
      tokenSimilarity(
        item.company,
        x.company
      );

    const score =
      (
        brandScore *
        0.45
      ) +
      (
        productScore *
        0.45
      ) +
      (
        companyScore *
        0.10
      );

    if (
      score >
      bestScore
    ) {
      bestScore =
        score;

      best =
        x;
    }
  }

  return (
    best &&
    bestScore >= 0.62
  )
    ? best.url
    : "";
}

/* =========================================================
   FDA OFFICIAL 2026 XML
   ========================================================= */

function decodeXml(v) {
  return String(
    v == null
      ? ""
      : v
  )
    .replace(
      /<!\[CDATA\[([\s\S]*?)\]\]>/g,
      "$1"
    )
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&apos;/gi, "'")
    .replace(/&#39;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#x2F;/gi, "/")
    .replace(/&#47;/gi, "/");
}

function extractFDAUrlsFromXml(xml) {
  const raw =
    decodeXml(
      xml
    );

  const found =
    [];

  const seen =
    new Set();

  const urlRegex =
    /(?:https?:\/\/(?:www\.)?fda\.gov)?\/safety\/recalls-market-withdrawals-safety-alerts\/[a-z0-9][a-z0-9-]*/gi;

  let match;

  while (
    (
      match =
        urlRegex.exec(
          raw
        )
    )
  ) {
    const url =
      absoluteUrl(
        match[0],
        "https://www.fda.gov"
      )
        .replace(
          /[)"'<>\],.;]+$/g,
          ""
        );

    if (
      !url ||
      isGenericRecallPage(
        url,
        "FDA"
      ) ||
      seen.has(
        url
      )
    ) {
      continue;
    }

    seen.add(
      url
    );

    const start =
      Math.max(
        0,
        match.index -
          3000
      );

    const end =
      Math.min(
        raw.length,
        match.index +
          match[0].length +
          3000
      );

    const context =
      clean(
        raw.slice(
          start,
          end
        )
      );

    found.push(
      {
        url,
        context
      }
    );
  }

  return found;
}

async function loadFDAOfficialXmlLinks() {
  try {
    const xml =
      await fetchXML(
        FDA_2026_XML,
        "FDA official XML"
      );

    console.log(
      `FDA official XML bytes: ${Buffer.byteLength(xml, "utf8")}`
    );

    const links =
      extractFDAUrlsFromXml(
        xml
      );

    console.log(
      `FDA official XML detail URLs discovered: ${links.length}`
    );

    return links;
  } catch (err) {
    console.warn(
      "FDA official 2026 XML failed:",
      err.message ||
      err
    );

    /*
      XML is supplemental.

      Failure here should not fail FDA because the XLSX
      feed and existing HTML matcher still work.
    */
    return [];
  }
}

function fdaDateMatch(
  date,
  context
) {
  if (!date) {
    return false;
  }

  const d =
    new Date(
      date +
      "T12:00:00Z"
    );

  if (
    Number.isNaN(
      d.getTime()
    )
  ) {
    return false;
  }

  const monthLong =
    d.toLocaleString(
      "en-US",
      {
        month:
          "long",

        timeZone:
          "UTC"
      }
    );

  const monthShort =
    d.toLocaleString(
      "en-US",
      {
        month:
          "short",

        timeZone:
          "UTC"
      }
    );

  const day =
    d.getUTCDate();

  const year =
    d.getUTCFullYear();

  const normalizedContext =
    normalizeMatchText(
      context
    );

  const tests = [
    `${monthLong} ${day} ${year}`,
    `${monthShort} ${day} ${year}`,
    `${monthLong} ${day}, ${year}`,
    `${monthShort} ${day}, ${year}`,
    `${d.getUTCMonth() + 1}/${day}/${year}`,
    `${String(d.getUTCMonth() + 1).padStart(2, "0")}/${String(day).padStart(2, "0")}/${year}`,
    `${year}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`
  ];

  return tests.some(
    test =>
      normalizedContext.includes(
        normalizeMatchText(
          test
        )
      )
  );
}

function fdaXmlMatchScore(
  item,
  candidate
) {
  const context =
    normalizeMatchText(
      candidate.context
    );

  if (!context) {
    return 0;
  }

  const brand =
    normalizeMatchText(
      item.brand
    );

  const product =
    normalizeMatchText(
      cleanProductName(
        item.product
      )
    );

  const company =
    normalizeMatchText(
      item.company
    );

  let score =
    0;

  if (
    brand &&
    context.includes(
      brand
    )
  ) {
    score +=
      45;
  } else if (brand) {
    const coverage =
      tokenCoverage(
        brand,
        context
      );

    if (
      coverage >= 0.80
    ) {
      score +=
        35;
    } else if (
      coverage >= 0.60
    ) {
      score +=
        25;
    }
  }

  if (
    product &&
    context.includes(
      product
    )
  ) {
    score +=
      45;
  } else if (product) {
    const coverage =
      tokenCoverage(
        product,
        context
      );

    if (
      coverage >= 0.85
    ) {
      score +=
        40;
    } else if (
      coverage >= 0.70
    ) {
      score +=
        32;
    } else if (
      coverage >= 0.55
    ) {
      score +=
        22;
    } else if (
      coverage >= 0.40
    ) {
      score +=
        12;
    }
  }

  if (
    company &&
    context.includes(
      company
    )
  ) {
    score +=
      20;
  } else if (company) {
    const coverage =
      tokenCoverage(
        company,
        context
      );

    if (
      coverage >= 0.80
    ) {
      score +=
        15;
    } else if (
      coverage >= 0.60
    ) {
      score +=
        8;
    }
  }

  if (
    fdaDateMatch(
      item.date,
      candidate.context
    )
  ) {
    score +=
      15;
  }

  const keyProductWords =
    normalizeMatchText(
      cleanProductName(
        item.product
      )
    )
      .split(
        " "
      )
      .filter(
        x =>
          x.length >=
          5
      )
      .slice(
        0,
        8
      );

  let keyMatches =
    0;

  for (
    const word of
      keyProductWords
  ) {
    if (
      context.includes(
        word
      )
    ) {
      keyMatches++;
    }
  }

  if (
    keyMatches >=
    4
  ) {
    score +=
      10;
  } else if (
    keyMatches >=
    2
  ) {
    score +=
      5;
  }

  return score;
}

function matchFDAOfficialXmlUrl(
  item,
  xmlLinks
) {
  if (
    !Array.isArray(
      xmlLinks
    ) ||
    !xmlLinks.length
  ) {
    return "";
  }

  const scored =
    xmlLinks
      .map(
        candidate => ({
          ...candidate,

          score:
            fdaXmlMatchScore(
              item,
              candidate
            )
        })
      )
      .filter(
        x =>
          x.score >
          0
      )
      .sort(
        (
          a,
          b
        ) =>
          b.score -
          a.score
      );

  if (
    !scored.length
  ) {
    return "";
  }

  const best =
    scored[0];

  const second =
    scored[1] ||
    null;

  if (
    best.score <
    70
  ) {
    return "";
  }

  if (
    second &&
    second.score >=
      best.score -
        5
  ) {
    return "";
  }

  return best.url;
}

/* =========================================================
   FDA LOAD
   ========================================================= */

async function loadFDA() {
  const [
    buffer,
    detailLinks,
    officialXmlLinks
  ] =
    await Promise.all(
      [
        fetchBuffer(
          FDA_XLSX,
          "FDA XLSX"
        ),

        loadFDADetailLinks(),

        loadFDAOfficialXmlLinks()
      ]
    );

  const wb =
    XLSX.read(
      buffer,
      {
        type:
          "buffer"
      }
    );

  const sheet =
    wb.Sheets[
      wb.SheetNames[
        0
      ]
    ];

  const rows =
    fdaRowsFromSheet(
      sheet
    );

  console.log(
    "FDA mapped headers:",
    Object.keys(
      rows[0] ||
      {}
    )
  );

  let alreadyDirect =
    0;

  let matchedXml =
    0;

  let matchedListing =
    0;

  const items =
    rows
      .map(
        row => {
          const date =
            safeDate(
              pickField(
                row,
                [
                  "Date",
                  "FDA Publish Date"
                ]
              )
            );

          const brand =
            pickField(
              row,
              [
                "Brand Name(s)",
                "Brand Name",
                "Brand Names"
              ]
            );

          const product =
            pickField(
              row,
              [
                "Product Description"
              ]
            );

          const productType =
            pickField(
              row,
              [
                "Product Type",
                "Product Types"
              ]
            );

          const reason =
            pickField(
              row,
              [
                "Recall Reason Description",
                "Reason for Announcement"
              ]
            );

          const company =
            pickField(
              row,
              [
                "Company Name"
              ]
            );

          const combined = [
            brand,
            product,
            productType,
            reason,
            company
          ].join(
            " "
          );

          const directSpreadsheetUrl =
            clean(
              row.__direct_url
            );

          const item = {
            id:
              `FDA-${date}-${slug(
                `${brand}-${product}-${company}`
              )}`,

            source:
              "FDA",

            category:
              classify(
                combined,
                productType,
                "FDA"
              ),

            title:
              makeFDAHeadline(
                brand,
                product,
                company
              ),

            reason:
              shorten(
                reason ||
                "FDA recall notice.",
                220
              ),

            brand,
            company,
            product,
            date,

            units:
              0,

            pet:
              isPetRecall(
                combined
              ),

            majorBrand:
              hasMajorBrand(
                `${brand} ${company} ${product}`
              ),

            url:
              directSpreadsheetUrl ||
              ""
          };

          if (
            item.url
          ) {
            alreadyDirect++;
          }

          if (
            !item.url
          ) {
            const xmlUrl =
              matchFDAOfficialXmlUrl(
                item,
                officialXmlLinks
              );

            if (
              xmlUrl
            ) {
              item.url =
                xmlUrl;

              matchedXml++;
            }
          }

          if (
            !item.url
          ) {
            const listingUrl =
              matchFDADetailUrl(
                item,
                detailLinks
              );

            if (
              listingUrl
            ) {
              item.url =
                listingUrl;

              matchedListing++;
            }
          }

          if (
            !item.url
          ) {
            item.url =
              FDA_PAGE;
          }

          item.score =
            totalScore(
              item
            );

          return item;
        }
      )
      .filter(
        item =>
          item.date &&
          (
            item.brand ||
            item.product ||
            item.company
          )
      );

  const totalSpecific =
    items.filter(
      item =>
        !isGenericRecallPage(
          item.url,
          "FDA"
        )
    ).length;

  console.log(
    `FDA direct URLs from spreadsheet: ${alreadyDirect}/${items.length}`
  );

  console.log(
    `FDA individual detail URLs matched from official XML: ${matchedXml}/${items.length}`
  );

  console.log(
    `FDA individual detail URLs matched from listing fallback: ${matchedListing}/${items.length}`
  );

  console.log(
    `FDA total individual detail URLs: ${totalSpecific}/${items.length}`
  );

  return items;
}

/* =========================================================
   CPSC
   ========================================================= */

async function loadCPSC() {
  const data =
    await fetchJSON(
      CPSC_API,
      "CPSC API"
    );

  const rows =
    Array.isArray(
      data
    )
      ? data
      : [];

  return rows.map(
    row => {
      const products =
        (
          row.Products ||
          []
        )
          .map(
            x =>
              clean(
                x.Name
              )
          )
          .filter(
            Boolean
          );

      const mfgs =
        (
          row.Manufacturers ||
          []
        )
          .map(
            x =>
              clean(
                x.Name
              )
          )
          .filter(
            Boolean
          );

      const retailers =
        (
          row.Retailers ||
          []
        )
          .map(
            x =>
              clean(
                x.Name
              )
          )
          .filter(
            Boolean
          );

      const hazards =
        (
          row.Hazards ||
          []
        )
          .map(
            x =>
              clean(
                x.Name
              )
          )
          .filter(
            Boolean
          );

      const directUnits =
        Math.max(
          0,
          ...(
            row.Products ||
            []
          )
            .map(
              x =>
                parseNumber(
                  x.NumberOfUnits ||
                  x.NumberofUnits ||
                  x.Units ||
                  ""
                )
            )
        );

      const date =
        safeDate(
          row.RecallDate ||
          row.LastPublishDate
        );

      const title =
        clean(
          row.Title ||
          row.RecallTitle ||
          products[0] ||
          "Consumer Product Recall"
        );

      const reason =
        hazards.join(
          "; "
        ) ||
        clean(
          row.Description
        ) ||
        "Consumer product safety recall.";

      const fallbackUnits =
        extractUnits(
          [
            title,
            reason,
            clean(
              row.Description
            )
          ].join(
            " "
          )
        );

      const units =
        directUnits ||
        fallbackUnits;

      const titleForBrand =
        title
          .replace(
            /\bsold on walmart\.com\b.*$/i,
            ""
          )
          .replace(
            /\bsold on amazon(?:\.com)?\b.*$/i,
            ""
          )
          .replace(
            /\bsold on temu\b.*$/i,
            ""
          );

      const brandSignal = [
        titleForBrand,
        mfgs.join(
          " "
        ),
        products.join(
          " "
        )
      ].join(
        " "
      );

      const item = {
        id:
          `CPSC-${clean(
            row.RecallID ||
            ""
          )}-${slug(
            title
          )}`,

        source:
          "CPSC",

        category:
          "consumer",

        title:
          shorten(
            title,
            105
          ),

        reason:
          shorten(
            reason,
            220
          ),

        brand:
          mfgs[0] ||
          "",

        company:
          mfgs.join(
            ", "
          ),

        retailers:
          retailers.join(
            ", "
          ),

        product:
          products.join(
            ", "
          ),

        date,
        units,

        pet:
          false,

        majorBrand:
          hasMajorBrand(
            brandSignal
          ),

        url:
          firstSpecificUrl(
            [
              row.RecallURL,
              row.URL,
              row.InconjunctionURL
            ],
            "https://www.cpsc.gov",
            "CPSC"
          ) ||
          (
            clean(
              row.RecallID
            )
              ? `${CPSC_API}&RecallID=${encodeURIComponent(
                  clean(
                    row.RecallID
                  )
                )}`
              : CPSC_PAGE
          )
      };

      item.score =
        totalScore(
          item
        );

      return item;
    }
  );
}

/* =========================================================
   USDA
   ========================================================= */

function parseUSDAListingRows(html) {
  const found =
    [];

  const re =
    /<a\b[^>]*href=["']([^"']*\/recalls-alerts\/[^"'#?]+)["'][^>]*>([\s\S]*?)<\/a>/gi;

  let m;

  while (
    (
      m =
        re.exec(
          html
        )
    )
  ) {
    const href =
      absoluteUrl(
        m[1],
        "https://www.fsis.usda.gov"
      );

    const title =
      clean(
        m[2]
      );

    if (
      href &&
      title
    ) {
      found.push(
        {
          title,
          url:
            href
        }
      );
    }
  }

  return found;
}

async function loadUSDARecallLinks() {
  const found =
    [];

  const pages =
    Array.from(
      {
        length:
          15
      },

      (
        _,
        i
      ) =>
        `https://www.fsis.usda.gov/recalls-alerts?page=${i}`
    );

  await mapLimit(
    pages,
    4,
    async pageUrl => {
      try {
        const html =
          await fetchText(
            pageUrl,
            "USDA recall listing"
          );

        found.push(
          ...parseUSDAListingRows(
            html
          )
        );
      } catch (err) {
        console.warn(
          "USDA listing page failed:",
          pageUrl,
          err.message ||
          err
        );
      }
    }
  );

  const unique =
    new Map();

  for (const x of found) {
    unique.set(
      x.url,
      x
    );
  }

  console.log(
    `USDA detail links discovered: ${unique.size}`
  );

  return Array.from(
    unique.values()
  );
}

function matchUSDARecallUrl(
  title,
  links
) {
  const wanted =
    normalizeMatchText(
      title
    );

  if (!wanted) {
    return "";
  }

  const exact =
    links.find(
      x =>
        normalizeMatchText(
          x.title
        ) ===
        wanted
    );

  if (exact) {
    return exact.url;
  }

  let best =
    null;

  let bestScore =
    0;

  for (const x of links) {
    const score =
      tokenSimilarity(
        title,
        x.title
      );

    if (
      score >
      bestScore
    ) {
      bestScore =
        score;

      best =
        x;
    }
  }

  return (
    best &&
    bestScore >= 0.68
  )
    ? best.url
    : "";
}

async function loadUSDA() {
  const [
    data,
    detailLinks
  ] =
    await Promise.all(
      [
        fetchJSON(
          USDA_API,
          "USDA API"
        ),

        loadUSDARecallLinks()
      ]
    );

  const rows =
    Array.isArray(
      data
    )
      ? data
      : (
          data &&
          Array.isArray(
            data.data
          )
            ? data.data
            : []
        );

  let matched =
    0;

  const items =
    rows.map(
      row => {
        const title =
          clean(
            row.title ||
            row.recall_title ||
            row.field_title ||
            "USDA Food Recall"
          );

        const reason =
          clean(
            row.reason ||
            row.summary ||
            row.field_recall_reason ||
            "USDA food safety recall."
          );

        const date =
          safeDate(
            row.date ||
            row.recall_date ||
            row.field_recall_date
          );

        const company =
          clean(
            row.company ||
            row.establishment ||
            row.field_establishment
          );

        const combined =
          `${title} ${reason} ${company}`;

        const apiUrl =
          firstSpecificUrl(
            [
              row.url,
              row.recall_url,
              row.field_recall_url,
              row.field_url,
              row.path,
              row.view_node,
              row.alias,
              row.uri
            ],
            "https://www.fsis.usda.gov",
            "USDA"
          );

        const listedUrl =
          matchUSDARecallUrl(
            title,
            detailLinks
          );

        if (
          listedUrl
        ) {
          matched++;
        }

        const item = {
          id:
            `USDA-${date}-${slug(
              title
            )}`,

          source:
            "USDA",

          category:
            "food",

          title:
            shorten(
              title,
              105
            ),

          reason:
            shorten(
              reason,
              220
            ),

          brand:
            "",

          company,

          product:
            "",

          date,

          units:
            extractUnits(
              combined
            ),

          pet:
            false,

          majorBrand:
            hasMajorBrand(
              `${title} ${company}`
            ),

          url:
            listedUrl ||
            apiUrl ||
            USDA_PAGE
        };

        item.score =
          totalScore(
            item
          );

        return item;
      }
    );

  console.log(
    `USDA individual detail URLs matched from listing: ${matched}/${items.length}`
  );

  return items;
}

/* =========================================================
   NHTSA
   ========================================================= */

const NHTSA_FIELDS = [
  "RECORD_ID",
  "CAMPNO",
  "MAKETXT",
  "MODELTXT",
  "YEARTXT",
  "MFGCAMPNO",
  "COMPNAME",
  "MFGNAME",
  "BGMAN",
  "ENDMAN",
  "RCLTYPECD",
  "POTAFF",
  "ODATE",
  "INFLUENCED_BY",
  "MFGTXT",
  "RCDATE",
  "DATEA",
  "RPNO",
  "FMVSS",
  "DESC_DEFECT",
  "CONEQUENCE_DEFECT",
  "CORRECTIVE_ACTION",
  "NOTES",
  "RCL_CMPT_ID",
  "MFR_COMP_NAME",
  "MFR_COMP_DESC",
  "MFR_COMP_PTNO"
];

function detectDelimiter(line) {
  const counts = {
    "\t":
      (
        line.match(
          /\t/g
        ) ||
        []
      ).length,

    "|":
      (
        line.match(
          /\|/g
        ) ||
        []
      ).length,

    ",":
      (
        line.match(
          /,/g
        ) ||
        []
      ).length
  };

  return Object.entries(
    counts
  )
    .sort(
      (
        a,
        b
      ) =>
        b[1] -
        a[1]
    )[0][0];
}

function parseDelimitedLine(
  line,
  delimiter
) {
  if (
    delimiter !==
    ","
  ) {
    return line
      .split(
        delimiter
      )
      .map(
        clean
      );
  }

  const out =
    [];

  let cur =
    "";

  let quoted =
    false;

  for (
    let i = 0;
    i < line.length;
    i++
  ) {
    const ch =
      line[i];

    if (
      ch === '"'
    ) {
      if (
        quoted &&
        line[
          i + 1
        ] === '"'
      ) {
        cur +=
          '"';

        i++;
      } else {
        quoted =
          !quoted;
      }
    } else if (
      ch === "," &&
      !quoted
    ) {
      out.push(
        clean(
          cur
        )
      );

      cur =
        "";
    } else {
      cur +=
        ch;
    }
  }

  out.push(
    clean(
      cur
    )
  );

  return out;
}

function vehicleDisplayName(v) {
  const special =
    new Map(
      [
        [
          "BMW",
          "BMW"
        ],
        [
          "GMC",
          "GMC"
        ],
        [
          "MINI",
          "MINI"
        ],
        [
          "FIAT",
          "FIAT"
        ],
        [
          "EV",
          "EV"
        ],
        [
          "PHEV",
          "PHEV"
        ],
        [
          "HEV",
          "HEV"
        ],
        [
          "SUV",
          "SUV"
        ],
        [
          "AWD",
          "AWD"
        ],
        [
          "4WD",
          "4WD"
        ],
        [
          "2WD",
          "2WD"
        ]
      ]
    );

  return clean(
    v
  )
    .split(
      /\s+/
    )
    .map(
      word => {
        const upper =
          word.toUpperCase();

        if (
          special.has(
            upper
          )
        ) {
          return special.get(
            upper
          );
        }

        if (
          /^[A-Z]*\d[A-Z0-9-]*$/i
            .test(
              word
            )
        ) {
          return upper;
        }

        return word
          .toLowerCase()
          .replace(
            /(^|[-/])([a-z])/g,
            (
              _,
              sep,
              ch
            ) =>
              sep +
              ch.toUpperCase()
          );
      }
    )
    .join(
      " "
    );
}

function collectUrls(
  value,
  out = []
) {
  if (
    typeof value ===
    "string"
  ) {
    if (
      /^https?:\/\//i
        .test(
          value
        )
    ) {
      out.push(
        value
      );
    }

    return out;
  }

  if (
    Array.isArray(
      value
    )
  ) {
    for (
      const x of value
    ) {
      collectUrls(
        x,
        out
      );
    }

    return out;
  }

  if (
    value &&
    typeof value ===
      "object"
  ) {
    for (
      const x of
        Object.values(
          value
        )
    ) {
      collectUrls(
        x,
        out
      );
    }
  }

  return out;
}

async function resolveNHTSADocument(
  campaign
) {
  const endpoint =
    "https://api.nhtsa.gov/safetyIssues/byNhtsaId" +
    `?nhtsaId=${encodeURIComponent(
      campaign
    )}` +
    "&filter=issueType&filterValue=recalls";

  try {
    const data =
      await fetchJSON(
        endpoint,
        `NHTSA document ${campaign}`
      );

    const urls =
      collectUrls(
        data,
        []
      );

    const report =
      urls.find(
        url =>
          /static\.nhtsa\.gov\/odi\/rcl\/\d{4}\/RCLRPT-[^/?#]+\.pdf/i
            .test(
              url
            )
      );

    if (
      report
    ) {
      return report;
    }

    return (
      urls.find(
        url =>
          /static\.nhtsa\.gov\/odi\/rcl\/\d{4}\/[^/?#]+\.pdf/i
            .test(
              url
            )
      ) ||
      ""
    );
  } catch (err) {
    /*
      Individual PDF resolution failure does not kill NHTSA.
      We still have the NHTSA recall search fallback.
    */
    console.warn(
      `NHTSA document lookup failed for ${campaign}:`,
      err.message ||
      err
    );

    return "";
  }
}

async function loadNHTSA() {
  const buffer =
    await fetchBuffer(
      NHTSA_ZIP,
      "NHTSA recall ZIP"
    );

  const zip =
    new AdmZip(
      buffer
    );

  const candidates =
    zip
      .getEntries()
      .filter(
        e =>
          !e.isDirectory
      )
      .filter(
        e =>
          /\.(txt|csv|dat)$/i
            .test(
              e.entryName
            )
      )
      .sort(
        (
          a,
          b
        ) =>
          b.header.size -
          a.header.size
      );

  if (
    !candidates.length
  ) {
    console.log(
      "NHTSA ZIP entries:",
      zip
        .getEntries()
        .map(
          e =>
            e.entryName
        )
    );

    throw new Error(
      "No usable NHTSA flat data file found"
    );
  }

  const entry =
    candidates[0];

  console.log(
    "NHTSA using ZIP entry:",
    entry.entryName,
    "bytes:",
    entry.header.size
  );

  const text =
    entry
      .getData()
      .toString(
        "utf8"
      )
      .replace(
        /^\uFEFF/,
        ""
      );

  const lines =
    text
      .split(
        /\r?\n/
      )
      .filter(
        line =>
          clean(
            line
          )
      );

  if (
    !lines.length
  ) {
    throw new Error(
      "NHTSA data file was empty"
    );
  }

  const delimiter =
    detectDelimiter(
      lines[0]
    );

  const first =
    parseDelimitedLine(
      lines[0],
      delimiter
    );

  const firstUpper =
    first.map(
      x =>
        clean(
          x
        )
          .toUpperCase()
    );

  const hasHeader =
    firstUpper.includes(
      "CAMPNO"
    ) ||
    firstUpper.includes(
      "RECALL_CAMPNO"
    ) ||
    firstUpper.includes(
      "MAKETXT"
    );

  const headers =
    hasHeader
      ? firstUpper
      : NHTSA_FIELDS;

  const dataLines =
    hasHeader
      ? lines.slice(
          1
        )
      : lines;

  const campaigns =
    {};

  for (
    const line of
      dataLines
  ) {
    const values =
      parseDelimitedLine(
        line,
        delimiter
      );

    const row =
      {};

    headers.forEach(
      (
        field,
        i
      ) => {
        row[
          field
        ] =
          clean(
            values[i] ||
            ""
          );
      }
    );

    const campaign =
      row.CAMPNO ||
      row.RECALL_CAMPNO ||
      row.NHTSA_CAMPAIGN_NUMBER ||
      "";

    if (
      !campaign
    ) {
      continue;
    }

    const date =
      safeDate(
        row.RCDATE ||
        row.RECALL_DATE ||
        row.DATEA ||
        row.ODATE
      );

    if (
      date &&
      daysOld(
        date
      ) >
        45
    ) {
      continue;
    }

    const make =
      row.MAKETXT ||
      row.MAKE ||
      row.MFGTXT ||
      "";

    const model =
      row.MODELTXT ||
      row.MODEL ||
      "";

    const manufacturer =
      row.MFGNAME ||
      row.MANUFACTURER ||
      row.MFR_NAME ||
      "";

    const component =
      row.COMPNAME ||
      row.COMPONENT ||
      "";

    const defect =
      row.DESC_DEFECT ||
      row.DEFECT ||
      row.DEFECT_SUMMARY ||
      "";

    const consequence =
      row.CONEQUENCE_DEFECT ||
      row.CONSEQUENCE_DEFECT ||
      row.CONSEQUENCE ||
      "";

    const units =
      parseNumber(
        row.POTAFF ||
        row.POTENTIAL_NUMBER_OF_UNITS_AFFECTED ||
        row.UNITS_AFFECTED ||
        ""
      );

    if (
      !campaigns[
        campaign
      ]
    ) {
      campaigns[
        campaign
      ] = {
        campaign,
        date,
        make,
        manufacturer,

        models:
          [],

        component,
        defect,
        consequence,
        units
      };
    }

    const g =
      campaigns[
        campaign
      ];

    if (
      model &&
      !g.models.includes(
        model
      )
    ) {
      g.models.push(
        model
      );
    }

    g.units =
      Math.max(
        g.units,
        units
      );

    if (
      !g.date &&
      date
    ) {
      g.date =
        date;
    }

    if (
      !g.make &&
      make
    ) {
      g.make =
        make;
    }

    if (
      !g.manufacturer &&
      manufacturer
    ) {
      g.manufacturer =
        manufacturer;
    }

    if (
      !g.component &&
      component
    ) {
      g.component =
        component;
    }

    if (
      !g.defect &&
      defect
    ) {
      g.defect =
        defect;
    }

    if (
      !g.consequence &&
      consequence
    ) {
      g.consequence =
        consequence;
    }
  }

  const campaignRows =
    Object.values(
      campaigns
    );

  const documents =
    await mapLimit(
      campaignRows,
      6,
      async g => ({
        campaign:
          g.campaign,

        url:
          await resolveNHTSADocument(
            g.campaign
          )
      })
    );

  const documentMap =
    new Map(
      documents.map(
        x => [
          x.campaign,
          x.url
        ]
      )
    );

  console.log(
    `NHTSA readable documents resolved: ${
      documents.filter(
        x =>
          x.url
      ).length
    }/${campaignRows.length}`
  );

  return campaignRows
    .map(
      g => {
        const makeRaw =
          clean(
            g.make ||
            g.manufacturer
          );

        const make =
          vehicleDisplayName(
            makeRaw
          );

        const displayModels =
          g.models.map(
            vehicleDisplayName
          );

        const title =
          make
            ? (
                displayModels.length ===
                  1
                  ? `${make} ${displayModels[0]} Vehicles Recalled`
                  : `${make} Vehicles Recalled`
              )
            : "Vehicle Recall";

        let reason =
          clean(
            g.defect ||
            g.consequence ||
            g.component
          );

        if (
          g.units >
          0
        ) {
          reason +=
            ` ${g.units.toLocaleString(
              "en-US"
            )} vehicles or units may be affected.`;
        }

        const brandSignal = [
          makeRaw,
          g.manufacturer,
          g.models.join(
            " "
          )
        ].join(
          " "
        );

        const item = {
          id:
            `NHTSA-${g.campaign}`,

          source:
            "NHTSA",

          category:
            "consumer",

          title:
            shorten(
              title,
              105
            ),

          reason:
            shorten(
              reason ||
              "Vehicle safety recall.",
              220
            ),

          brand:
            make,

          company:
            clean(
              g.manufacturer
            ),

          product:
            displayModels
              .slice(
                0,
                8
              )
              .join(
                ", "
              ),

          date:
            g.date,

          units:
            g.units,

          pet:
            false,

          majorBrand:
            hasMajorBrand(
              brandSignal
            ),

          campaign:
            g.campaign,

          url:
            documentMap.get(
              g.campaign
            ) ||
            `${NHTSA_PAGE}?nhtsaId=${encodeURIComponent(
              g.campaign
            )}`
        };

        item.score =
          totalScore(
            item
          );

        return item;
      }
    )
    .filter(
      item =>
        item.date
    );
}

/* =========================================================
   FINAL FEED
   ========================================================= */

function dedupe(items) {
  const seen =
    new Map();

  for (
    const item of
      items
  ) {
    const key =
      item.source ===
        "NHTSA" &&
      item.campaign
        ? `NHTSA|${item.campaign}`
        : `${item.source}|${slug(
            item.title
          )}|${item.date}`;

    if (
      !seen.has(
        key
      ) ||
      item.score >
        seen.get(
          key
        ).score
    ) {
      seen.set(
        key,
        item
      );
    }
  }

  return Array.from(
    seen.values()
  );
}

function rank(items) {
  return items.sort(
    (
      a,
      b
    ) =>
      (
        b.score -
        a.score
      ) ||
      String(
        b.date
      )
        .localeCompare(
          String(
            a.date
          )
        )
  );
}

function diversifyLead(
  items,
  leadCount = 15
) {
  const remaining = [
    ...items
  ];

  const chosen =
    [];

  const sourceCounts =
    {};

  while (
    remaining.length &&
    chosen.length <
      leadCount
  ) {
    let bestIndex =
      0;

    let bestAdjusted =
      -Infinity;

    for (
      let i = 0;
      i <
        remaining.length;
      i++
    ) {
      const item =
        remaining[i];

      const count =
        sourceCounts[
          item.source
        ] ||
        0;

      let penalty =
        0;

      if (
        count >=
        4
      ) {
        penalty =
          28 *
          (
            count -
            3
          );
      } else if (
        count >=
        2
      ) {
        penalty =
          10 *
          (
            count -
            1
          );
      }

      const adjusted =
        item.score -
        penalty;

      if (
        adjusted >
        bestAdjusted
      ) {
        bestAdjusted =
          adjusted;

        bestIndex =
          i;
      }
    }

    const [
      picked
    ] =
      remaining.splice(
        bestIndex,
        1
      );

    chosen.push(
      picked
    );

    sourceCounts[
      picked.source
    ] =
      (
        sourceCounts[
          picked.source
        ] ||
        0
      ) +
      1;
  }

  return chosen.concat(
    remaining
  );
}

function validateSources(
  sourceResults
) {
  const problems =
    [];

  for (
    const [
      name,
      result
    ] of
      Object.entries(
        sourceResults
      )
  ) {
    if (
      !result.ok
    ) {
      problems.push(
        `${name} failed completely`
      );

      continue;
    }

    const minimum =
      MIN_SOURCE_COUNTS[
        name
      ];

    if (
      minimum != null &&
      result.count <
        minimum
    ) {
      problems.push(
        `${name} returned only ${result.count} records; safety minimum is ${minimum}`
      );
    }
  }

  if (
    problems.length
  ) {
    console.error(
      "\n========================================"
    );

    console.error(
      "BUILD SAFETY STOP"
    );

    console.error(
      "The existing mjr-recalls.json WILL NOT be replaced."
    );

    console.error(
      "Problems detected:"
    );

    for (
      const problem of
        problems
    ) {
      console.error(
        `- ${problem}`
      );
    }

    console.error(
      "========================================\n"
    );

    throw new Error(
      "Recall source safety validation failed"
    );
  }
}

function atomicWriteJSON(
  filename,
  tempFilename,
  data
) {
  fs.writeFileSync(
    tempFilename,
    JSON.stringify(
      data,
      null,
      2
    ) +
    "\n",
    "utf8"
  );

  fs.renameSync(
    tempFilename,
    filename
  );
}

async function run() {
  console.log(
    "Building MJR recall feed v2.2..."
  );

  /*
    We still run the sources concurrently.

    Each network request now has automatic retry protection.
  */
  const results =
    await Promise.allSettled(
      [
        loadFDA(),
        loadCPSC(),
        loadUSDA(),
        loadNHTSA()
      ]
    );

  const names = [
    "FDA",
    "CPSC",
    "USDA",
    "NHTSA"
  ];

  const sources =
    {};

  let combined =
    [];

  results.forEach(
    (
      result,
      i
    ) => {
      const name =
        names[i];

      if (
        result.status ===
        "fulfilled"
      ) {
        sources[
          name
        ] = {
          ok:
            true,

          count:
            result.value.length
        };

        combined =
          combined.concat(
            result.value
          );

        console.log(
          `${name}: ${result.value.length} records`
        );
      } else {
        sources[
          name
        ] = {
          ok:
            false,

          count:
            0,

          error:
            String(
              result.reason
                ?.message ||
              result.reason
            )
        };

        console.error(
          `${name} FAILED:`,
          result.reason
        );
      }
    }
  );

  /*
    CRITICAL:

    Stop here before generating or writing anything if
    one of the main feeds failed or collapsed.
  */
  validateSources(
    sources
  );

  combined =
    rank(
      dedupe(
        combined
      )
        .filter(
          item =>
            item.date &&
            daysOld(
              item.date
            ) <=
              45
        )
    );

  combined =
    diversifyLead(
      combined,
      15
    );

  /*
    Another final protection.

    We expect enough valid records to publish 120.
    If we don't have 120, keep the existing feed.
  */
  if (
    combined.length <
    120
  ) {
    console.error(
      `BUILD SAFETY STOP: only ${combined.length} recent recalls available.`
    );

    console.error(
      "Existing mjr-recalls.json will not be replaced."
    );

    throw new Error(
      "Not enough recent recalls to publish safely"
    );
  }

  combined =
    combined.slice(
      0,
      120
    );

  const newestDate =
    combined.reduce(
      (
        best,
        item
      ) =>
        item.date >
        best
          ? item.date
          : best,

      ""
    );

  const output = {
    generated:
      new Date()
        .toISOString(),

    version:
      "2.2",

    newestDate,

    stale:
      newestDate
        ? daysOld(
            newestDate
          ) >
          7
        : true,

    sources,

    count:
      combined.length,

    items:
      combined
  };

  fs.mkdirSync(
    OUTPUT_DIR,
    {
      recursive:
        true
    }
  );

  /*
    Atomic write:
    first create .tmp, then rename only after the file
    has been completely written.
  */
  atomicWriteJSON(
    OUTPUT_FILE,
    TEMP_OUTPUT_FILE,
    output
  );

  console.log(
    `Wrote ${combined.length} recalls to ${OUTPUT_FILE}`
  );

  console.log(
    "Newest recall date:",
    newestDate ||
    "none"
  );

  console.log(
    "\nTop 15 recalls:"
  );

  combined
    .slice(
      0,
      15
    )
    .forEach(
      (
        item,
        i
      ) => {
        console.log(
          `${String(
            i + 1
          ).padStart(
            2,
            " "
          )}. ` +
          `[${item.source}] ${item.date} | ${item.score} | ` +
          `${item.units || 0} units | ${item.title}`
        );
      }
    );
}

run()
  .catch(
    err => {
      /*
        Clean up any abandoned temp file.
      */
      try {
        if (
          fs.existsSync(
            TEMP_OUTPUT_FILE
          )
        ) {
          fs.unlinkSync(
            TEMP_OUTPUT_FILE
          );
        }
      } catch (_) {
        // No action needed.
      }

      console.error(
        "\nBUILD FAILED:",
        err.message ||
        err
      );

      console.error(
        "Existing recall feed was preserved."
      );

      process.exit(
        1
      );
    }
  );
