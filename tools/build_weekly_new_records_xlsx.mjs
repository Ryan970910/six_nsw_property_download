import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const versionSuffix = process.argv[2] || "";
const baseName = `weekly_dat_new_records_20260504_20260615${versionSuffix}`;
const inputCsv = path.resolve(`outputs/weekly_dat_new_records/${baseName}.csv`);
const summaryCsv = path.resolve(`outputs/weekly_dat_new_records/${baseName}.summary.csv`);
const outputDir = path.resolve("outputs/weekly_dat_new_records");
const outputPath = path.join(outputDir, `${baseName}.xlsx`);

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

function countCsvRows(csvText) {
  return csvText.split(/\r?\n/).filter((line) => line.length > 0).length;
}

function countCsvColumns(csvText) {
  const header = csvText.split(/\r?\n/, 1)[0] || "";
  let columns = 1;
  let quoted = false;
  for (let i = 0; i < header.length; i += 1) {
    const char = header[i];
    if (char === '"') quoted = !quoted;
    if (char === "," && !quoted) columns += 1;
  }
  return columns;
}

function applyHeaderStyle(range) {
  range.format = {
    fill: "#1F4E79",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
  };
}

function addTable(sheet, address, name) {
  const table = sheet.tables.add(address, true, name);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
}

function styleSheet(sheet, headerRange, usedRange) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  applyHeaderStyle(headerRange);
  usedRange.format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
  usedRange.format.autofitColumns();
  usedRange.format.autofitRows();
}

await fs.mkdir(outputDir, { recursive: true });

const recordsText = await fs.readFile(inputCsv, "utf8");
const summaryText = await fs.readFile(summaryCsv, "utf8");
const recordRows = countCsvRows(recordsText);
const recordCols = countCsvColumns(recordsText);
const summaryRows = countCsvRows(summaryText);
const summaryCols = countCsvColumns(summaryText);

const workbook = await Workbook.fromCSV(recordsText, { sheetName: "New Records" });
await workbook.fromCSV(summaryText, { sheetName: "Summary" });

const records = workbook.worksheets.getItem("New Records");
const summary = workbook.worksheets.getItem("Summary");

const recordLastCol = columnLetter(recordCols - 1);
const summaryLastCol = columnLetter(summaryCols - 1);

styleSheet(records, records.getRangeByIndexes(0, 0, 1, recordCols), records.getRange(`A1:${recordLastCol}${recordRows}`));
records.getRange(`B2:B${recordRows}`).format.numberFormat = "0";
records.getRange(`D2:D${recordRows}`).format.numberFormat = "$#,##0";
records.getRange(`E2:E${recordRows}`).format.numberFormat = "yyyy-mm-dd";
records.getRange(`F2:F${recordRows}`).format.numberFormat = "#,##0.0";
records.getRange(`I2:I${recordRows}`).format.numberFormat = "0";
records.getRange(`K2:K${recordRows}`).format.numberFormat = "yyyy-mm-dd";
records.getRange(`S2:T${recordRows}`).format.numberFormat = "yyyy-mm-dd hh:mm";
addTable(records, `A1:${recordLastCol}${recordRows}`, "WeeklyDatNewRecords");

styleSheet(summary, summary.getRangeByIndexes(0, 0, 1, summaryCols), summary.getRange(`A1:${summaryLastCol}${summaryRows}`));
summary.getRange(`B2:G${summaryRows}`).format.numberFormat = "#,##0";
addTable(summary, `A1:${summaryLastCol}${summaryRows}`, "WeeklyDatNewRecordsSummary");

const inspectSummary = await workbook.inspect({
  kind: "table",
  sheetId: "Summary",
  range: `A1:${summaryLastCol}${summaryRows}`,
  include: "values",
  tableMaxRows: 10,
  tableMaxCols: 8,
  maxChars: 3000,
});
console.log(inspectSummary.ndjson);

const inspectRecords = await workbook.inspect({
  kind: "table",
  sheetId: "New Records",
  range: "A1:H8",
  include: "values",
  tableMaxRows: 8,
  tableMaxCols: 8,
  maxChars: 3000,
});
console.log(inspectRecords.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const summaryPreview = await workbook.render({
  sheetName: "Summary",
  range: `A1:${summaryLastCol}${summaryRows}`,
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "weekly_dat_new_records_summary_preview.png"), new Uint8Array(await summaryPreview.arrayBuffer()));

const recordsPreview = await workbook.render({
  sheetName: "New Records",
  range: "A1:H12",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "weekly_dat_new_records_preview.png"), new Uint8Array(await recordsPreview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`Saved ${outputPath}`);
