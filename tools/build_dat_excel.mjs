import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputPath = path.resolve("data/valuation_weekly/20260629/extracted/001_SALES_DATA_NNME_29062026.DAT");
const outputDir = path.resolve("outputs/dat_excel");
const outputPath = path.join(outputDir, "001_SALES_DATA_NNME_29062026.xlsx");

function parseDateYYYYMMDD(value) {
  if (!value || !/^\d{8}$/.test(value)) return "";
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(4, 6));
  const day = Number(value.slice(6, 8));
  return new Date(Date.UTC(year, month - 1, day));
}

function parseExtractDate(value) {
  if (!value) return "";
  const token = value.slice(0, 8);
  return parseDateYYYYMMDD(token);
}

function numberOrBlank(value) {
  if (value === "" || value == null) return "";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : value;
}

function areaToSqm(area, unit) {
  const value = Number(area);
  if (!Number.isFinite(value)) return "";
  if (unit === "H") return value * 10000;
  return value;
}

function columnLetter(indexZeroBased) {
  let n = indexZeroBased + 1;
  let label = "";
  while (n > 0) {
    const remainder = (n - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    n = Math.floor((n - 1) / 26);
  }
  return label;
}

function fullAddress(fields) {
  const unit = fields[5] || "";
  const house = fields[7] || "";
  const street = fields[8] || "";
  const suburb = fields[9] || "";
  const postcode = fields[10] || "";
  const streetAddress = [unit, house, street].filter(Boolean).join(" ");
  return `${streetAddress}, ${suburb} NSW ${postcode}`.trim();
}

function parseSaleRecord(fields, sourceFile) {
  return [
    fields[1] || "", // district_code
    numberOrBlank(fields[2]), // property_id
    numberOrBlank(fields[3]), // sale_record_number
    parseExtractDate(fields[4]), // extraction_date
    fields[5] || "", // unit_number
    fields[6] || "", // house_number_suffix / currently blank in sample
    fields[7] || "", // house_number
    fields[8] || "", // street_name
    fields[9] || "", // suburb
    fields[10] || "", // postcode
    numberOrBlank(fields[11]), // area_raw
    fields[12] || "", // area_unit
    areaToSqm(fields[11], fields[12]), // area_sqm
    parseDateYYYYMMDD(fields[13]), // contract_date / likely sale_date
    parseDateYYYYMMDD(fields[14]), // settlement_or_transfer_date
    numberOrBlank(fields[15]), // sale_price
    fields[16] || "", // zoning
    fields[17] || "", // property_type_code
    fields[18] || "", // property_description
    fields[19] || "", // unknown
    fields[20] || "", // sale_code
    fields[21] || "", // unknown
    fields[22] || "", // unknown
    fields[23] || "", // dealing_number
    fullAddress(fields),
    sourceFile,
  ];
}

function maxFieldCount(records) {
  return Math.max(...records.map((fields) => fields.length));
}

function applyHeaderStyle(range) {
  range.format = {
    fill: "#1F4E79",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
  };
}

function applyTableStyle(sheet, rangeAddress, tableName) {
  const table = sheet.tables.add(rangeAddress, true, tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
}

function setCommonSheetStyle(sheet, headerRange, usedRange) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  applyHeaderStyle(headerRange);
  usedRange.format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
  usedRange.format.autofitColumns();
  usedRange.format.autofitRows();
}

const text = await fs.readFile(inputPath, "utf8");
const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
const records = lines.map((line) => line.split(";"));
const sourceFile = path.basename(inputPath);

const saleHeaders = [
  "district_code",
  "property_id",
  "sale_record_number",
  "extraction_date",
  "unit_number",
  "house_number_suffix",
  "house_number",
  "street_name",
  "suburb",
  "postcode",
  "area_raw",
  "area_unit",
  "area_sqm",
  "contract_date",
  "settlement_or_transfer_date",
  "sale_price",
  "zoning",
  "property_type_code",
  "property_description",
  "field_19",
  "sale_code",
  "field_21",
  "field_22",
  "dealing_number",
  "full_address",
  "source_file",
];
const saleRows = records.filter((fields) => fields[0] === "B").map((fields) => parseSaleRecord(fields, sourceFile));

const fieldCount = maxFieldCount(records);
const rawHeaders = ["line_number", "record_type", ...Array.from({ length: fieldCount - 1 }, (_, index) => `field_${String(index + 1).padStart(2, "0")}`)];
const rawRows = records.map((fields, index) => {
  const row = [index + 1, fields[0] || ""];
  for (let i = 1; i < fieldCount; i += 1) row.push(fields[i] || "");
  return row;
});

const summaryMap = new Map();
for (const fields of records) {
  const key = fields[0] || "(blank)";
  summaryMap.set(key, (summaryMap.get(key) || 0) + 1);
}
const summaryRows = Array.from(summaryMap.entries()).sort(([a], [b]) => a.localeCompare(b)).map(([type, count]) => [type, count]);

const workbook = Workbook.create();

const summary = workbook.worksheets.add("Record Summary");
summary.getRange("A1:B1").values = [["record_type", "count"]];
summary.getRangeByIndexes(1, 0, summaryRows.length, 2).values = summaryRows;
summary.getRange("D1:E5").values = [
  ["Source file", sourceFile],
  ["Total raw records", records.length],
  ["B sale records", saleRows.length],
  ["Created from", inputPath],
  ["Notes", "B rows are mapped to easier-to-read sale fields; Raw Records preserves every field."],
];
setCommonSheetStyle(summary, summary.getRange("A1:B1"), summary.getRange(`A1:E${Math.max(summaryRows.length + 1, 5)}`));
summary.getRange(`B2:B${summaryRows.length + 1}`).format.numberFormat = "#,##0";
summary.getRange("A1:B" + (summaryRows.length + 1)).format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
applyTableStyle(summary, `A1:B${summaryRows.length + 1}`, "RecordSummaryTable");

const sales = workbook.worksheets.add("Sales Records");
sales.getRangeByIndexes(0, 0, 1, saleHeaders.length).values = [saleHeaders];
if (saleRows.length) {
  sales.getRangeByIndexes(1, 0, saleRows.length, saleHeaders.length).values = saleRows;
}
setCommonSheetStyle(sales, sales.getRangeByIndexes(0, 0, 1, saleHeaders.length), sales.getRangeByIndexes(0, 0, saleRows.length + 1, saleHeaders.length));
sales.getRange(`B2:D${saleRows.length + 1}`).format.numberFormat = "0";
sales.getRange(`D2:D${saleRows.length + 1}`).format.numberFormat = "yyyy-mm-dd";
sales.getRange(`K2:K${saleRows.length + 1}`).format.numberFormat = "#,##0.000";
sales.getRange(`M2:M${saleRows.length + 1}`).format.numberFormat = "#,##0.0";
sales.getRange(`N2:O${saleRows.length + 1}`).format.numberFormat = "yyyy-mm-dd";
sales.getRange(`P2:P${saleRows.length + 1}`).format.numberFormat = "$#,##0";
applyTableStyle(sales, `A1:Z${saleRows.length + 1}`, "SalesRecordsTable");

const raw = workbook.worksheets.add("Raw Records");
raw.getRangeByIndexes(0, 0, 1, rawHeaders.length).values = [rawHeaders];
raw.getRangeByIndexes(1, 0, rawRows.length, rawHeaders.length).values = rawRows;
setCommonSheetStyle(raw, raw.getRangeByIndexes(0, 0, 1, rawHeaders.length), raw.getRangeByIndexes(0, 0, rawRows.length + 1, rawHeaders.length));
raw.getRange(`A2:A${rawRows.length + 1}`).format.numberFormat = "#,##0";
applyTableStyle(raw, `A1:${columnLetter(rawHeaders.length - 1)}${rawRows.length + 1}`, "RawRecordsTable");

await fs.mkdir(outputDir, { recursive: true });

const inspectSales = await workbook.inspect({
  kind: "table",
  sheetId: "Sales Records",
  range: "A1:Z8",
  include: "values",
  tableMaxRows: 8,
  tableMaxCols: 10,
  maxChars: 3000,
});
console.log(inspectSales.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Sales Records",
  range: "A1:Z12",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "001_sales_records_preview.png"), new Uint8Array(await preview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`Saved ${outputPath}`);
