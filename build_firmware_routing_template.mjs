import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "outputs/firmware_routing_template_20260814";
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("填写规则");
const guide = workbook.worksheets.add("填写说明");
sheet.showGridLines = false;
guide.showGridLines = false;

sheet.mergeCells("A1:H1");
sheet.getRange("A1").values = [["V4 固件路由与旧接口对照规则（请填写）"]];
sheet.getRange("A1:H1").format = {
  fill: "#17365D", font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center", verticalAlignment: "center",
};
sheet.getRange("A1:H1").format.rowHeight = 32;
sheet.mergeCells("A2:H2");
sheet.getRange("A2").values = [["填写每个固件应由 V4 自动选择的正确数据源。黄色单元格为待填写项；同一固件只需填一行。"]];
sheet.getRange("A2:H2").format = { fill: "#EAF2F8", font: { color: "#334155" }, wrapText: true };
sheet.getRange("A2:H2").format.rowHeight = 28;
sheet.mergeCells("A3:H3");
sheet.getRange("A3").values = [["示例：MOY-NN2-1.0.0 已确认应路由到 v3。填写完成后把此文件发回，我会按它重新生成 V4 对比报告。"]];
sheet.getRange("A3:H3").format = { fill: "#F8FAFC", font: { color: "#475569", italic: true }, wrapText: true };
sheet.getRange("A3:H3").format.rowHeight = 28;

const headers = [["序号", "完整固件版本 fv（必填）", "固件编号 firmware（可选）", "正确数据源（必填）", "对应旧列表接口（可选）", "验证样例表盘 ID（可选）", "要求内容一致", "备注"]];
sheet.getRange("A5:H5").values = headers;
sheet.getRange("A5:H5").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true };
sheet.getRange("A5:H5").format.rowHeight = 32;

const rows = [
  [1, "MOY-NN2-1.0.0", "NN", "v3", "/faces/v3/list", "", "是", "已确认：应路由到 v3"],
  [2, "", "", "", "", "", "是", ""],
  [3, "", "", "", "", "", "是", ""],
  [4, "", "", "", "", "", "是", ""],
  [5, "", "", "", "", "", "是", ""],
  [6, "", "", "", "", "", "是", ""],
  [7, "", "", "", "", "", "是", ""],
  [8, "", "", "", "", "", "是", ""],
  [9, "", "", "", "", "", "是", ""],
  [10, "", "", "", "", "", "是", ""],
];
sheet.getRange("A6:H15").values = rows;
sheet.getRange("A6:H15").format = { verticalAlignment: "center", wrapText: true };
sheet.getRange("A6:H15").format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
sheet.getRange("A6:H15").format.rowHeight = 26;
sheet.getRange("B7:H15").format.fill = "#FFF2CC";
sheet.getRange("B6:H6").format.fill = "#E2F0D9";
sheet.getRange("A6:A15").format.horizontalAlignment = "center";
sheet.getRange("D6:D15").format.horizontalAlignment = "center";
sheet.getRange("G6:G15").format.horizontalAlignment = "center";
sheet.getRange("D6:D100").dataValidation = { rule: { type: "list", values: ["v3", "new", "sifli"] } };
sheet.getRange("G6:G100").dataValidation = { rule: { type: "list", values: ["是", "否"] } };
sheet.freezePanes.freezeRows(5);

const widths = [["A:A", 8], ["B:B", 27], ["C:C", 20], ["D:D", 18], ["E:E", 27], ["F:F", 24], ["G:G", 16], ["H:H", 34]];
for (const [range, width] of widths) sheet.getRange(range).format.columnWidth = width;
sheet.tables.add("A5:H15", true, "FirmwareRulesTable");

guide.mergeCells("A1:D1");
guide.getRange("A1").values = [["填写说明与旧接口映射"]];
guide.getRange("A1:D1").format = { fill: "#17365D", font: { bold: true, color: "#FFFFFF", size: 15 }, horizontalAlignment: "center" };
guide.getRange("A1:D1").format.rowHeight = 30;
guide.getRange("A3:D3").values = [["正确数据源", "旧标签接口", "旧列表接口", "旧详情接口"]];
guide.getRange("A3:D3").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
guide.getRange("A4:D6").values = [
  ["v3", "/faces/v3/tag-list", "/faces/v3/list", "/faces/v3/face-detail"],
  ["new", "/faces/new/tag-face-list", "/faces/new/face-list", "/faces/new/face-detail"],
  ["sifli", "/faces/sifli/tag-list", "/faces/sifli", "/faces/sifli/{id}"],
];
guide.getRange("A4:D6").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
guide.getRange("A4:D6").format.rowHeight = 24;
guide.mergeCells("A8:D8");
guide.getRange("A8").values = [["填写建议"]];
guide.getRange("A8:D8").format = { fill: "#D9EAF7", font: { bold: true, color: "#17365D" } };
guide.getRange("A9:D12").merge(true);
guide.getRange("A9").values = [["1. 完整固件版本 fv 请按真实请求填写，例如 MOY-XXX3-2.0.0。\n2. 正确数据源请选择 v3、new 或 sifli。\n3. 如果同一固件的 V4 返回内容本就允许不同，请把“要求内容一致”改为“否”，并在备注说明原因。\n4. 可选填写一个样例表盘 ID，便于复测详情接口。"]];
guide.getRange("A9:D12").format = { wrapText: true, verticalAlignment: "top", fill: "#F8FAFC" };
guide.getRange("A:A").format.columnWidth = 22;
guide.getRange("B:D").format.columnWidth = 30;
guide.freezePanes.freezeRows(3);

const check = await workbook.inspect({ kind: "table", range: "填写规则!A1:H15", include: "values,formulas", tableMaxRows: 16, tableMaxCols: 8 });
console.log(check.ndjson);
const preview = await workbook.render({ sheetName: "填写规则", range: "A1:H15", scale: 1.5, format: "png" });
await fs.writeFile(`${outputDir}/preview.png`, new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(`${outputDir}/v4_firmware_routing_template.xlsx`);
