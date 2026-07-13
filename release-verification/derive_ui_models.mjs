#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { deriveVillaniWebRunModel } from "../components/villani-web/dist-model/connectedRunModel.js";
import { deriveFlightRecorderRunModel } from "../components/villani-flight-recorder/dist/render/connectedRunModel.js";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("usage: derive_ui_models.mjs INPUT.json OUTPUT.json");
}

const runs = JSON.parse(await readFile(resolve(inputPath), "utf8"));
const output = {};
for (const [scenarioId, detail] of Object.entries(runs)) {
  output[scenarioId] = {
    web: deriveVillaniWebRunModel(detail),
    flight_recorder: deriveFlightRecorderRunModel(detail),
  };
}
await writeFile(resolve(outputPath), `${JSON.stringify(output, null, 2)}\n`, "utf8");
