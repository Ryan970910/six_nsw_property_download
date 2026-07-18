import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const baseName = "weekly_dat_source_deduped_20260105_20260629";
const outputDir = path.resolve("outputs/weekly_dat_new_records");
const inputCsv = path.join(outputDir, `${baseName}.csv`);
const summaryCsv = path.join(outputDir, `${baseName}.summary.csv`);
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

function styleHeader(range) {
  range.format = {
    fill: "#1F4E79",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
  };
}

function styleSheet(sheet, headerRange, usedRange) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  styleHeader(headerRange);
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

const workbook = await Workbook.fromCSV(recordsText, { sheetName: "Source Deduped Records" });
await workbook.fromCSV(summaryText, { sheetName: "Summary" });
const notes = workbook.worksheets.add("Notes");

const records = workbook.worksheets.getItem("Source Deduped Records");
const summary = workbook.worksheets.getItem("Summary");
const recordLastCol = columnLetter(recordCols - 1);
const summaryLastCol = columnLetter(summaryCols - 1);

styleSheet(records, records.getRangeByIndexes(0, 0, 1, recordCols), records.getRange(`A1:${recordLastCol}${recordRows}`));
records.getRange(`C2:C${recordRows}`).format.numberFormat = "0";
records.getRange(`E2:E${recordRows}`).format.numberFormat = "$#,##0";
records.getRange(`F2:F${recordRows}`).format.numberFormat = "yyyy-mm-dd";
records.getRange(`G2:G${recordRows}`).format.numberFormat = "#,##0.0";
records.getRange(`J2:J${recordRows}`).format.numberFormat = "0";
records.getRange(`L2:L${recordRows}`).format.numberFormat = "yyyy-mm-dd";
records.getRange(`T2:U${recordRows}`).format.numberFormat = "yyyy-mm-dd hh:mm";
const recordsTable = records.tables.add(`A1:${recordLastCol}${recordRows}`, true, "WeeklyDatSourceDedupedRecords");
recordsTable.style = "TableStyleMedium2";
recordsTable.showFilterButton = true;

styleSheet(summary, summary.getRangeByIndexes(0, 0, 1, summaryCols), summary.getRange(`A1:${summaryLastCol}${summaryRows}`));
summary.getRange(`B2:E${summaryRows}`).format.numberFormat = "#,##0";
const summaryTable = summary.tables.add(`A1:${summaryLastCol}${summaryRows}`, true, "WeeklyDatSourceDedupedSummary");
summaryTable.style = "TableStyleMedium2";
summaryTable.showFilterButton = true;

notes.showGridLines = false;
notes.getRange("A1:B5").values = [
  ["Workbook", "NSW weekly DAT source-deduped records"],
  ["Date range", "2026-01-05 to 2026-06-29"],
  ["Rows exported", recordRows - 1],
  ["Duplicate handling", "Removed duplicate rows inside each weekly source batch only."],
  ["Database upload/check", "No database records were uploaded. Database duplicate check was not run because real DB settings were not available in .env.example."],
];
notes.getRange("A1:B1").format = { fill: "#1F4E79", font: { bold: true, color: "#FFFFFF" } };
notes.getRange("A1:B5").format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
notes.getRange("A1:B5").format.autofitColumns();
notes.getRange("A1:B5").format.autofitRows();

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
  sheetId: "Source Deduped Records",
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
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const summaryPreview = await workbook.render({
  sheetName: "Summary",
  range: `A1:${summaryLastCol}${summaryRows}`,
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, `${baseName}_summary_preview.png`), new Uint8Array(await summaryPreview.arrayBuffer()));

const recordsPreview = await workbook.render({
  sheetName: "Source Deduped Records",
  range: "A1:H12",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, `${baseName}_records_preview.png`), new Uint8Array(await recordsPreview.arrayBuffer()));

const notesPreview = await workbook.render({
  sheetName: "Notes",
  range: "A1:B5",
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, `${baseName}_notes_preview.png`), new Uint8Array(await notesPreview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`Saved ${outputPath}`);
