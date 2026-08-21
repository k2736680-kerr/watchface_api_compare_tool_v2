import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "D:/下载/v4_firmware_routing_template.xlsx";
const outputPath = "outputs/user_supplied_test_data_20260814.json";
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItem("填写规则");
const values = sheet.getRange("A1:H100").values;
const rows = [];

for (let index = 0; index < values.length; index += 1) {
  const row = values[index].map((value) => (value == null ? "" : String(value).trim()));
  const [unused, platform, method, url, body] = row;
  if (!/^https?:\/\//i.test(url) || !/^(get|post)$/i.test(method)) continue;
  rows.push({
    sheet_row: index + 1,
    platform,
    method: method.toUpperCase(),
    url,
    body,
  });
}

await fs.mkdir("outputs", { recursive: true });
await fs.writeFile(outputPath, JSON.stringify({ source: inputPath, extracted_at: new Date().toISOString(), rows }, null, 2), "utf8");
console.log(JSON.stringify({ outputPath, total: rows.length, get: rows.filter((row) => row.method === "GET").length, post: rows.filter((row) => row.method === "POST").length, rows }, null, 2));
