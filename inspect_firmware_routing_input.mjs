import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("D:/下载/v4_firmware_routing_template.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 5000,
  tableMaxRows: 20,
  tableMaxCols: 12,
});
console.log(overview.ndjson);
const check = await workbook.inspect({
  kind: "table",
  range: "填写规则!A1:H100",
  include: "values,formulas",
  tableMaxRows: 100,
  tableMaxCols: 8,
});
console.log(check.ndjson);
const preview = await workbook.render({ sheetName: "填写规则", range: "A1:H30", scale: 1.5, format: "png" });
await fs.writeFile("outputs/firmware_routing_template_20260814/user_input_preview.png", new Uint8Array(await preview.arrayBuffer()));
