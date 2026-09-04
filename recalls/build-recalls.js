const fs = require("fs");
const path = require("path");
const XLSX = require("xlsx");
const AdmZip = require("adm-zip");

const OUTPUT_DIR = path.join(process.cwd(), "..", "data");
const OUTPUT_FILE = path.join(OUTPUT_DIR, "mjr-recalls.json");

const FDA_XLSX =
  "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/datatables-data?_format=xlsx&page=";

const CPSC_API =
  "https://www.saferproducts.gov/RestWebServices/Recall?format=json";

const USDA_API =
  "https://www.fsis.usda.gov/fsis/api/recall/v/1";

const NHTSA_ZIP =
  "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip";

const FDA_PAGE =
  "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts";

const CPSC_PAGE =
  "https://www.cpsc.gov/Recalls";

const USDA_PAGE =
  "https://www.fsis.usda.gov/recalls";

const NHTSA_PAGE =
  "https://www.nhtsa.gov/recalls";

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
    .replace(/<[^>]*>/g, " ")
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

function normalizeHeader(v) {
  return clean(v)
    .toLowerCase()
    .replace(/[-_]+/g, " ")
    .replace(/[()]/g, "")
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
      .replace(/\s+\S*$/, "") + "…"
  );
}

function escapeRegex(v) {
  return v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function containsBrand(text, brand) {
  const s = lower(text);
  const b = lower(brand);

  const pattern =
    "(^|[^a-z0-9])" +
    escapeRegex(b).replace(/\s+/g, "\\s+") +
    "([^a-z0-9]|$)";

  return new RegExp(pattern, "i").test(s);
}

function hasMajorBrand(text) {
  return MAJOR_BRANDS.some(
    brand => containsBrand(text, brand)
  );
}

function isPetRecall(v) {
  return /pet food|dog food|cat food|dog treat|cat treat|feline|canine|milk replacer|animal feed|pet treat/i
    .test(v);
}

function classify(text, productType, source) {
  const s = lower(text + " " + productType);

  if (source === "NHTSA" || source === "CPSC") {
    return "consumer";
  }

  if (
    /dietary supplement|supplements|drug|drugs|medical device|medical devices|pharmaceutical|injection|tablet|capsule|syringe|catheter|implant|infusion/.test(s)
  ) {
    return "health";
  }

  if (
    isPetRecall(s) ||
    /food|beverage|ingredient|meat|poultry|egg|cheese|seafood|allergen|salmonella|listeria|e\. coli|stec/.test(s)
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
    /actual death|fatalit|deaths? reported|has died|resulted in death/.test(s)
  ) {
    n += 55;
  } else if (
    /death|fatal|life-threatening|life threatening|do not drive/.test(s)
  ) {
    n += 28;
  }

  if (
    /fire hazard|fire risk|electrocution|explosion|crash risk|crash hazard/.test(s)
  ) {
    n += 35;
  }

  if (
    /salmonella|listeria|e\. coli|stec|botulism/.test(s)
  ) {
    n += 45;
  }

  if (
    /undeclared milk|undeclared peanut|undeclared egg|undeclared allergen/.test(s)
  ) {
    n += 28;
  }

  if (
    /choking|suffocation|lead exposure|poisoning|burn hazard/.test(s)
  ) {
    n += 25;
  }

  if (
    /particulate|foreign material|foreign object|contamination|sterility assurance/.test(s)
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
  const s = clean(v).replace(/,/g, "");
  const m = s.match(/(\d+(?:\.\d+)?)/);

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
      const n = Number(
        m[1].replace(/,/g, "")
      );

      if (Number.isFinite(n)) {
        return n;
      }
    }
  }

  return 0;
}

async function fetchBuffer(url) {
  const r = await fetch(
    url,
    {
      headers: {
        "User-Agent":
          "MediaJobsReport-RecallFeed/1.2"
      }
    }
  );

  if (!r.ok) {
    throw new Error(
      `${url} returned HTTP ${r.status}`
    );
  }

  return Buffer.from(
    await r.arrayBuffer()
  );
}

async function fetchJSON(url) {
  const r = await fetch(
    url,
    {
      headers: {
        "User-Agent":
          "MediaJobsReport-RecallFeed/1.2",

        "Accept":
          "application/json"
      }
    }
  );

  if (!r.ok) {
    throw new Error(
      `${url} returned HTTP ${r.status}`
    );
  }

  return r.json();
}

function cleanProductName(v) {
  let s = clean(v)
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
    .trim();

  return shorten(s, 80);
}

function makeFDAHeadline(
  brand,
  product,
  company
) {
  const b = clean(brand);
  const p = cleanProductName(product);

  const s = lower(
    `${b} ${p} ${company}`
  );

  if (
    /northwest naturals/.test(s) &&
    /chicken/.test(s)
  ) {
    return "Northwest Naturals Chicken Recipe Pet Food Recalled";
  }

  if (
    /b\.?\s*braun/.test(s) &&
    /sodium chloride/.test(s)
  ) {
    return "B. Braun Sodium Chloride Injection Recalled";
  }

  if (
    /baxter/.test(s) &&
    /sodium chloride/.test(s)
  ) {
    return "Baxter Sodium Chloride Injection Recalled";
  }

  if (
    /feline milk replacer/.test(s)
  ) {
    return "Shelter’s Choice and Breeder’s Edge Feline Milk Replacers Recalled";
  }

  if (
    /great value/.test(s) &&
    /triple berry/.test(s)
  ) {
    return "Great Value Organic Triple Berry Blend Recalled";
  }

  if (b && p) {
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

function fdaRowsFromSheet(sheet) {
  const matrix =
    XLSX.utils.sheet_to_json(
      sheet,
      {
        header: 1,
        defval: "",
        raw: false
      }
    );

  const normalized =
    matrix.map(
      row =>
        row.map(
          cell =>
            normalizeHeader(cell)
        )
    );

  const headerIndex =
    normalized.findIndex(
      row =>
        row.includes("date") &&
        row.includes("brand names") &&
        row.includes("product description") &&
        row.includes("company name")
    );

  if (headerIndex < 0) {
    console.log(
      "FDA first rows:",
      matrix.slice(0, 8)
    );

    throw new Error(
      "FDA header row not found"
    );
  }

  console.log(
    "FDA header row:",
    matrix[headerIndex]
  );

  const headers =
    matrix[headerIndex].map(
      cell => clean(cell)
    );

  return matrix
    .slice(headerIndex + 1)
    .filter(
      row =>
        row.some(
          cell => clean(cell)
        )
    )
    .map(row => {
      const obj = {};

      headers.forEach(
        (header, i) => {
          if (header) {
            obj[header] =
              clean(row[i]);
          }
        }
      );

      return obj;
    });
}

function pickField(row, names) {
  const keys =
    Object.keys(row);

  for (const name of names) {
    const wanted =
      normalizeHeader(name);

    const exact =
      keys.find(
        k =>
          normalizeHeader(k) === wanted
      );

    if (exact) {
      return clean(
        row[exact]
      );
    }
  }

  return "";
}

async function loadFDA() {
  const buffer =
    await fetchBuffer(
      FDA_XLSX
    );

  const wb =
    XLSX.read(
      buffer,
      {
        type: "buffer"
      }
    );

  const sheet =
    wb.Sheets[
      wb.SheetNames[0]
    ];

  const rows =
    fdaRowsFromSheet(
      sheet
    );

  console.log(
    "FDA mapped headers:",
    Object.keys(
      rows[0] || {}
    )
  );

  return rows
    .map(row => {
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
            "Brand Names",
            "Brand Name(s)",
            "Brand Name",
            "Brand-Names"
          ]
        );

      const product =
        pickField(
          row,
          [
            "Product Description",
            "Product-Description"
          ]
        );

      const productType =
        pickField(
          row,
          [
            "Product Types",
            "Product Type",
            "Product-Types"
          ]
        );

      const reason =
        pickField(
          row,
          [
            "Recall Reason Description",
            "Reason for Announcement",
            "Recall-Reason-Description"
          ]
        );

      const company =
        pickField(
          row,
          [
            "Company Name",
            "Company-Name"
          ]
        );

      const terminated =
        pickField(
          row,
          [
            "Terminated Recall"
          ]
        );

      const combined = [
        brand,
        product,
        productType,
        reason,
        company
      ].join(" ");

      const item = {
        id:
          `FDA-${date}-${slug(
            brand +
            "-" +
            product +
            "-" +
            company
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
        productType,
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

        terminated:
          lower(terminated) === "terminated",

        url:
          FDA_PAGE
      };

      item.score =
        totalScore(item);

      return item;
    })
    .filter(
      item =>
        item.date &&
        (
          item.brand ||
          item.product ||
          item.company
        )
    );
}

async function loadCPSC() {
  const data =
    await fetchJSON(
      CPSC_API
    );

  const rows =
    Array.isArray(data)
      ? data
      : [];

  return rows.map(row => {
    const products =
      (row.Products || [])
        .map(
          x => clean(x.Name)
        )
        .filter(Boolean);

    const mfgs =
      (row.Manufacturers || [])
        .map(
          x => clean(x.Name)
        )
        .filter(Boolean);

    const retailers =
      (row.Retailers || [])
        .map(
          x => clean(x.Name)
        )
        .filter(Boolean);

    const hazards =
      (row.Hazards || [])
        .map(
          x => clean(x.Name)
        )
        .filter(Boolean);

    const directUnits =
      Math.max(
        0,
        ...(row.Products || [])
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
      hazards.join("; ") ||
      clean(row.Description) ||
      "Consumer product safety recall.";

    const fallbackUnits =
      extractUnits(
        [
          title,
          reason,
          clean(row.Description)
        ].join(" ")
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
      mfgs.join(" "),
      products.join(" ")
    ].join(" ");

    const item = {
      id:
        `CPSC-${clean(
          row.RecallID || ""
        )}-${slug(title)}`,

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
        mfgs[0] || "",

      company:
        mfgs.join(", "),

      retailers:
        retailers.join(", "),

      product:
        products.join(", "),

      date,
      units,

      pet:
        false,

      majorBrand:
        hasMajorBrand(
          brandSignal
        ),

      url:
        clean(
          row.URL ||
          row.RecallURL
        ) ||
        CPSC_PAGE
    };

    item.score =
      totalScore(item);

    return item;
  });
}

async function loadUSDA() {
  const data =
    await fetchJSON(
      USDA_API
    );

  const rows =
    Array.isArray(data)
      ? data
      : (
          data &&
          Array.isArray(data.data)
            ? data.data
            : []
        );

  return rows.map(row => {
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

    const item = {
      id:
        `USDA-${date}-${slug(title)}`,

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
        clean(row.url) ||
        USDA_PAGE
    };

    item.score =
      totalScore(item);

    return item;
  });
}

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
      (line.match(/\t/g) || []).length,

    "|":
      (line.match(/\|/g) || []).length,

    ",":
      (line.match(/,/g) || []).length
  };

  return Object.entries(counts)
    .sort(
      (a, b) =>
        b[1] - a[1]
    )[0][0];
}

function parseDelimitedLine(
  line,
  delimiter
) {
  if (delimiter !== ",") {
    return line
      .split(delimiter)
      .map(clean);
  }

  const out = [];
  let cur = "";
  let quoted = false;

  for (
    let i = 0;
    i < line.length;
    i++
  ) {
    const ch = line[i];

    if (ch === '"') {
      if (
        quoted &&
        line[i + 1] === '"'
      ) {
        cur += '"';
        i++;
      } else {
        quoted = !quoted;
      }

    } else if (
      ch === "," &&
      !quoted
    ) {
      out.push(
        clean(cur)
      );

      cur = "";

    } else {
      cur += ch;
    }
  }

  out.push(
    clean(cur)
  );

  return out;
}

async function loadNHTSA() {
  const buffer =
    await fetchBuffer(
      NHTSA_ZIP
    );

  const zip =
    new AdmZip(
      buffer
    );

  const candidates =
    zip.getEntries()
      .filter(
        e =>
          !e.isDirectory
      )
      .filter(
        e =>
          /\.(txt|csv|dat)$/i
            .test(e.entryName)
      )
      .sort(
        (a, b) =>
          b.header.size -
          a.header.size
      );

  if (!candidates.length) {
    console.log(
      "NHTSA ZIP entries:",
      zip.getEntries()
        .map(
          e => e.entryName
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
      .toString("utf8")
      .replace(/^\uFEFF/, "");

  const lines =
    text
      .split(/\r?\n/)
      .filter(
        line => clean(line)
      );

  if (!lines.length) {
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
        clean(x).toUpperCase()
    );

  const hasHeader =
    firstUpper.includes("CAMPNO") ||
    firstUpper.includes("RECALL_CAMPNO") ||
    firstUpper.includes("MAKETXT");

  const headers =
    hasHeader
      ? firstUpper
      : NHTSA_FIELDS;

  const dataLines =
    hasHeader
      ? lines.slice(1)
      : lines;

  const campaigns = {};

  for (const line of dataLines) {
    const values =
      parseDelimitedLine(
        line,
        delimiter
      );

    const row = {};

    headers.forEach(
      (field, i) => {
        row[field] =
          clean(
            values[i] || ""
          );
      }
    );

    const campaign =
      row.CAMPNO ||
      row.RECALL_CAMPNO ||
      row.NHTSA_CAMPAIGN_NUMBER ||
      "";

    if (!campaign) {
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
      daysOld(date) > 45
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

    if (!campaigns[campaign]) {
      campaigns[campaign] = {
        campaign,
        date,
        make,
        manufacturer,
        models: [],
        component,
        defect,
        consequence,
        units
      };
    }

    const g =
      campaigns[campaign];

    if (
      model &&
      !g.models.includes(model)
    ) {
      g.models.push(model);
    }

    g.units =
      Math.max(
        g.units,
        units
      );

    if (!g.date && date) {
      g.date = date;
    }

    if (!g.make && make) {
      g.make = make;
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

  return Object.values(
    campaigns
  )
    .map(g => {
      const make =
        clean(
          g.make ||
          g.manufacturer
        );

      const title =
        make
          ? (
              g.models.length === 1
                ? `${make} ${g.models[0]} Vehicles Recalled`
                : `${make} Vehicles Recalled`
            )
          : "Vehicle Recall";

      let reason =
        clean(
          g.defect ||
          g.consequence ||
          g.component
        );

      if (g.units > 0) {
        reason +=
          ` ${g.units.toLocaleString("en-US")} vehicles or units may be affected.`;
      }

      const brandSignal = [
        make,
        g.manufacturer,
        g.models.join(" ")
      ].join(" ");

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
          g.models
            .slice(0, 8)
            .join(", "),

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
          NHTSA_PAGE
      };

      item.score =
        totalScore(item);

      return item;
    })
    .filter(
      item => item.date
    );
}

function dedupe(items) {
  const seen =
    new Map();

  for (const item of items) {
    const key =
      item.source === "NHTSA" &&
      item.campaign
        ? `NHTSA|${item.campaign}`
        : `${item.source}|${slug(item.title)}|${item.date}`;

    if (
      !seen.has(key) ||
      item.score >
        seen.get(key).score
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
    (a, b) =>
      (b.score - a.score) ||
      String(b.date)
        .localeCompare(
          String(a.date)
        )
  );
}

function diversifyLead(
  items,
  leadCount = 15
) {
  const remaining =
    [...items];

  const chosen = [];
  const sourceCounts = {};

  while (
    remaining.length &&
    chosen.length < leadCount
  ) {
    let bestIndex = 0;
    let bestAdjusted =
      -Infinity;

    for (
      let i = 0;
      i < remaining.length;
      i++
    ) {
      const item =
        remaining[i];

      const count =
        sourceCounts[
          item.source
        ] || 0;

      let penalty = 0;

      if (count >= 4) {
        penalty =
          28 * (count - 3);

      } else if (count >= 2) {
        penalty =
          10 * (count - 1);
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

    const picked =
      remaining.splice(
        bestIndex,
        1
      )[0];

    chosen.push(
      picked
    );

    sourceCounts[
      picked.source
    ] =
      (
        sourceCounts[
          picked.source
        ] || 0
      ) + 1;
  }

  return chosen.concat(
    remaining
  );
}

async function run() {
  console.log(
    "Building MJR recall feed v1.2..."
  );

  const results =
    await Promise.allSettled([
      loadFDA(),
      loadCPSC(),
      loadUSDA(),
      loadNHTSA()
    ]);

  const names = [
    "FDA",
    "CPSC",
    "USDA",
    "NHTSA"
  ];

  const sources = {};
  let combined = [];

  results.forEach(
    (result, i) => {
      const name =
        names[i];

      if (
        result.status ===
        "fulfilled"
      ) {
        sources[name] = {
          ok: true,
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
        sources[name] = {
          ok: false,
          count: 0,
          error:
            String(
              result.reason?.message ||
              result.reason
            )
        };

        console.warn(
          `${name} FAILED:`,
          result.reason
        );
      }
    }
  );

  combined =
    rank(
      dedupe(
        combined
      )
        .filter(
          item =>
            item.date &&
            daysOld(item.date) <= 45
        )
    );

  combined =
    diversifyLead(
      combined,
      15
    )
      .slice(
        0,
        120
      );

  const newestDate =
    combined.reduce(
      (best, item) =>
        item.date > best
          ? item.date
          : best,
      ""
    );

  const output = {
    generated:
      new Date()
        .toISOString(),

    version:
      "1.2",

    newestDate,

    stale:
      newestDate
        ? daysOld(
            newestDate
          ) > 7
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
      recursive: true
    }
  );

  fs.writeFileSync(
    OUTPUT_FILE,
    JSON.stringify(
      output,
      null,
      2
    ) + "\n",
    "utf8"
  );

  console.log(
    `Wrote ${combined.length} recalls to ${OUTPUT_FILE}`
  );

  console.log(
    "Newest recall date:",
    newestDate || "none"
  );

  console.log(
    "\nTop 15 recalls:"
  );

  combined
    .slice(0, 15)
    .forEach(
      (item, i) => {
        console.log(
          `${String(i + 1).padStart(2, " ")}. ` +
          `[${item.source}] ${item.date} | ${item.score} | ` +
          `${item.units || 0} units | ${item.title}`
        );
      }
    );
}

run()
  .catch(err => {
    console.error(err);
    process.exit(1);
  });
