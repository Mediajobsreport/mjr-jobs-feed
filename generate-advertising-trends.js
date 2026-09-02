/**
 * MJR Advertising Trends Generator v2
 * Fixes the first parser by:
 *  - parsing Census month identifiers from time_slot_date / time_slot_name / time_slot_id
 *  - discovering the actual non-seasonally-adjusted flag returned by Census
 *  - selecting a complete monthly SALES series by category/year instead of assuming one row shape
 *  - writing useful diagnostics when a category is incomplete
 */

const fs = require("fs");

const API_KEY = process.env.CENSUS_API_KEY;
if (!API_KEY) {
  console.error("Missing CENSUS_API_KEY.");
  process.exit(1);
}

const BASE = "https://api.census.gov/data/timeseries/eits/mrts";

const categoryMeta = {
  "441": {name:"Motor Vehicle & Parts Dealers",angle:"Lead with inventory movement, service growth, trade-ins, event timing and integrated lead generation."},
  "442": {name:"Furniture & Home Furnishings",angle:"Use home projects, financing, moving activity, seasonal refreshes and promotional events as conversation starters."},
  "443": {name:"Electronics & Appliance Stores",angle:"Connect campaigns to upgrade cycles, product launches, home projects, major shopping periods and gift demand."},
  "444": {name:"Building Material & Garden Supply",angle:"Use project season, weather, home improvement, storm preparation and contractor/homeowner demand."},
  "445": {name:"Food & Beverage Stores",angle:"Build around seasonal entertaining, holidays, game days, meal occasions, promotions and local shopping behavior."},
  "446": {name:"Health & Personal Care Stores",angle:"Use wellness seasons, benefits timing, back-to-school health, allergy periods and holiday/self-care demand."},
  "447": {name:"Gasoline Stations",angle:"Connect to travel periods, commuter behavior, holiday weekends, convenience-store traffic and loyalty programs."},
  "448": {name:"Clothing & Accessories",angle:"Start before key shopping periods with seasonal wardrobe, gifting, events, back-to-school and promotional urgency."},
  "451": {name:"Sporting Goods, Hobby, Book & Music Stores",angle:"Use sports seasons, hobbies, recreation, school calendars, gifting and major local events as timely hooks."},
  "452": {name:"General Merchandise Stores",angle:"Lead ahead of major shopping periods, seasonal resets, gift buying, school calendars and promotional events."},
  "453": {name:"Miscellaneous Store Retailers",angle:"Identify the specific retail niche, then connect the stronger window to a concrete offer, event or customer need."},
  "454": {name:"Nonstore Retailers",angle:"Use shopping peaks, gifting, promotions, digital response, retargeting and customer-acquisition timing."},
  "722": {name:"Restaurants & Drinking Places",angle:"Build around dayparts, game days, events, holidays, catering, gift cards, traffic needs and limited-time offers."}
};

const monthNames = [null,"January","February","March","April","May","June","July","August","September","October","November","December"];

function monthNumber({id,name,date}) {
  const vals = [date,name,id].filter(Boolean).map(v=>String(v).trim());

  for (const v of vals) {
    // ISO-ish dates: 2025-01, 2025-01-01
    let m = v.match(/^\d{4}-(\d{2})(?:-\d{2})?$/);
    if (m) return Number(m[1]);

    // M01 / M1 / 01 / 1
    m = v.match(/^M?0?([1-9]|1[0-2])$/i);
    if (m) return Number(m[1]);

    // Month names
    const idx = monthNames.findIndex(x => x && x.toLowerCase() === v.toLowerCase());
    if (idx > 0) return idx;

    // Names like "Jan", "January 2025"
    const low = v.toLowerCase();
    const abbrev = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
    const ai = abbrev.findIndex(a => low.startsWith(a));
    if (ai >= 0) return ai + 1;
  }
  return null;
}

function isNotSeasonallyAdjusted(v) {
  const s = String(v ?? "").trim().toLowerCase();
  return ["no","n","0","false","not seasonally adjusted","nsa"].includes(s);
}

function monthsAhead(current,target){ return (target-current+12)%12; }

async function fetchYear(year){
  const p = new URLSearchParams({
    get:"data_type_code,time_slot_id,time_slot_name,time_slot_date,seasonally_adj,category_code,cell_value,error_data",
    time:String(year),
    key:API_KEY
  });
  const r = await fetch(`${BASE}?${p}`);
  const txt = await r.text();
  if(!r.ok) throw new Error(`Census ${year} HTTP ${r.status}: ${txt.slice(0,500)}`);
  try { return JSON.parse(txt); }
  catch { throw new Error(`Census ${year} returned non-JSON: ${txt.slice(0,500)}`); }
}

function parseYear(rows,year,debug){
  if(!Array.isArray(rows) || rows.length<2) return {};
  const headers=rows[0];
  const ix=Object.fromEntries(headers.map((h,i)=>[h,i]));
  const byCategory={};

  for(const row of rows.slice(1)){
    const code=String(row[ix.category_code] ?? "").trim();
    const sa=String(row[ix.seasonally_adj] ?? "").trim();
    const type=String(row[ix.data_type_code] ?? "").trim();
    const id=row[ix.time_slot_id], name=row[ix.time_slot_name], date=row[ix.time_slot_date];
    const month=monthNumber({id,name,date});
    const raw=String(row[ix.cell_value] ?? "").replace(/,/g,"").trim();
    const value=Number(raw);

    debug.seasonally_adj_values[sa]=(debug.seasonally_adj_values[sa]||0)+1;
    debug.data_type_counts[type]=(debug.data_type_counts[type]||0)+1;

    if(!categoryMeta[code]) continue;
    debug.target_category_rows[code]=(debug.target_category_rows[code]||0)+1;

    if(!isNotSeasonallyAdjusted(sa)) continue;
    if(!month || !Number.isFinite(value) || value<=0) continue;

    byCategory[code] ??= {};
    byCategory[code][type] ??= {};
    // one value per month/type
    if(byCategory[code][type][month] == null) byCategory[code][type][month]=value;
  }

  // Choose the most plausible complete monthly SALES series.
  // Prefer standard monthly sales code SM when it exists and has 12 months.
  const out={};
  for(const [code,types] of Object.entries(byCategory)){
    const candidates=Object.entries(types)
      .map(([type,months])=>({type,months,count:Object.keys(months).length}))
      .filter(x=>x.count===12);

    let chosen=candidates.find(x=>x.type.toUpperCase()==="SM");
    if(!chosen){
      // Fall back to complete series with the largest typical values.
      // This is safer than selecting percentage/change series, which are generally much smaller.
      chosen=candidates
        .map(x=>({...x,median:Object.values(x.months).sort((a,b)=>a-b)[Math.floor(Object.values(x.months).length/2)]}))
        .sort((a,b)=>b.median-a.median)[0];
    }

    debug.category_type_month_counts[`${year}:${code}`]=Object.fromEntries(
      Object.entries(types).map(([t,m])=>[t,Object.keys(m).length])
    );

    if(chosen){
      out[code]={type:chosen.type,months:chosen.months};
      debug.chosen_series[`${year}:${code}`]=chosen.type;
    }
  }
  return out;
}

function build(yearData,years,debug){
  const currentMonth=new Date().getUTCMonth()+1;
  const groups={sell_now:[],start_prospecting:[],on_deck:[],watch_ahead:[]};
  const diagnostics=[];

  for(const [code,meta] of Object.entries(categoryMeta)){
    const shares=Object.fromEntries(Array.from({length:12},(_,i)=>[i+1,[]]));
    const completeYears=[];

    for(const year of years){
      const series=yearData[year]?.[code];
      if(!series || Object.keys(series.months).length!==12) continue;
      const total=Object.values(series.months).reduce((a,b)=>a+b,0);
      if(!(total>0)) continue;
      completeYears.push(year);
      for(let m=1;m<=12;m++) shares[m].push(series.months[m]/total*100);
    }

    if(completeYears.length<2){
      diagnostics.push({
        category_code:code,
        category:meta.name,
        status:"insufficient_complete_years",
        complete_years_found:completeYears
      });
      continue;
    }

    const avg={};
    for(let m=1;m<=12;m++){
      if(shares[m].length) avg[m]=shares[m].reduce((a,b)=>a+b,0)/shares[m].length;
    }

    const strongest=Object.entries(avg)
      .sort((a,b)=>b[1]-a[1]).slice(0,3)
      .map(([m])=>Number(m)).sort((a,b)=>a-b);

    const next=strongest.map(m=>({month:m,ahead:monthsAhead(currentMonth,m)}))
      .sort((a,b)=>a.ahead-b.ahead)[0];
    if(!next) continue;

    let signal=null,lead=null;
    if(next.ahead===0){signal="sell_now";lead="Stronger selling window is active now";}
    else if(next.ahead<=2){signal="start_prospecting";lead=next.ahead===1?"About 30 days ahead":"About 60 days ahead";}
    else if(next.ahead===3){signal="on_deck";lead="About 90 days ahead";}
    else if(next.ahead===4){signal="watch_ahead";lead="About 120 days ahead";}
    else continue;

    groups[signal].push({
      category:meta.name,
      category_code:code,
      signal,
      strong_months:strongest.map(m=>monthNames[m]).join(", "),
      lead_time:lead,
      target_month:monthNames[next.month],
      target_month_avg_share:Number(avg[next.month].toFixed(2)),
      years_used:completeYears,
      rationale:"MJR analysis places the upcoming month among this category’s three strongest average monthly shares across the complete years used.",
      sales_angle:meta.angle
    });
  }

  for(const items of Object.values(groups)){
    items.sort((a,b)=>(b.target_month_avg_share||0)-(a.target_month_avg_share||0));
  }

  return {
    generated_at:new Date().toISOString(),
    source:"U.S. Census Bureau Monthly Retail Trade; MJR analysis",
    methodology:"MJR calculates each month as a share of annual not-seasonally-adjusted sales for the latest three complete years available, averages those monthly shares, identifies each category’s three strongest months, then converts the timing into forward-looking prospecting signals.",
    years_requested:years,
    groups,
    diagnostics,
    parser_debug:{
      seasonally_adj_values:debug.seasonally_adj_values,
      chosen_series:debug.chosen_series,
      category_type_month_counts:debug.category_type_month_counts
    }
  };
}

async function main(){
  const y=new Date().getUTCFullYear();
  const years=[y-1,y-2,y-3];
  const yearData={};
  const debug={
    seasonally_adj_values:{},
    data_type_counts:{},
    target_category_rows:{},
    category_type_month_counts:{},
    chosen_series:{}
  };

  for(const year of years){
    console.log(`Fetching Census MRTS ${year}...`);
    const rows=await fetchYear(year);
    console.log(`Rows returned for ${year}: ${Array.isArray(rows)?Math.max(0,rows.length-1):0}`);
    yearData[year]=parseYear(rows,year,debug);
  }

  console.log("Seasonally-adjusted flags seen:",debug.seasonally_adj_values);
  console.log("Chosen category series:",debug.chosen_series);

  const output=build(yearData,years,debug);
  fs.writeFileSync("mjr-advertising-trends.json",JSON.stringify(output,null,2)+"\n");
  console.log("Wrote mjr-advertising-trends.json");
  console.log("Group counts:",Object.fromEntries(Object.entries(output.groups).map(([k,v])=>[k,v.length])));
}

main().catch(err=>{
  console.error(err);
  process.exit(1);
});
