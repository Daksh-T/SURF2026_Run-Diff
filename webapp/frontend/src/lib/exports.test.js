import { afterEach, beforeEach, expect, test } from "bun:test";
import { exportCsv, exportJson, exportText } from "./exports.js";

let notices;
const originalWindow = globalThis.window;
const originalFetch = globalThis.fetch;
const originalStorage = globalThis.localStorage;

beforeEach(() => {
  notices = [];
  globalThis.window = {
    dispatchEvent: (event) => { if (event.type === "rundiff:export-complete") notices.push(event.detail); },
  };
  globalThis.localStorage = { getItem: () => "author-password" };
});

afterEach(() => {
  globalThis.window = originalWindow;
  globalThis.fetch = originalFetch;
  globalThis.localStorage = originalStorage;
});

test("Mac exports report the native chosen filename and directory only after saving", async () => {
  let resolve;
  window.webkit = { messageHandlers: { rundiffExport: { postMessage: (payload) => {
    expect(payload).toEqual({ filename: "set.json", content: '{\n  "id": "set"\n}' });
    return new Promise((done) => { resolve = done; });
  } } } };
  const saving = exportJson(() => ({ id: "set" }), "set.json");
  await Promise.resolve();
  await Promise.resolve();
  expect(notices).toEqual([]);
  resolve({ filename: "renamed.json", location: "Class materials" });
  await saving;
  expect(notices).toEqual([{ filename: "renamed.json", location: "Class materials" }]);
});

test("Electron cancellation never reports export success", async () => {
  window.rundiffDesktop = { saveExport: async () => ({ cancelled: true }) };
  await exportJson({}, "set.json");
  expect(notices).toEqual([]);
});

test("Tauri exports use the native command and return the chosen directory", async () => {
  window.__TAURI__ = { core: { invoke: async (command, payload) => {
    expect(command).toBe("save_export");
    expect(payload).toEqual({ filename: "attempts.json", content: "{}" });
    return { filename: "attempts.json", location: "Course" };
  } } };
  await exportText("{}", "attempts.json");
  expect(notices).toEqual([{ filename: "attempts.json", location: "Course" }]);
});

test("browser cancellation skips fetching export data", async () => {
  let requested = false;
  window.showDirectoryPicker = async () => { throw new DOMException("Cancelled", "AbortError"); };
  await exportJson(() => { requested = true; return {}; }, "set.json");
  expect(requested).toBe(false);
  expect(notices).toEqual([]);
});

test("browser exports preserve existing files and confirm only after closing", async () => {
  const operations = [];
  const output = {
    write: async (value) => operations.push(value),
    close: async () => { expect(notices).toEqual([]); operations.push("closed"); },
  };
  window.showDirectoryPicker = async () => ({
    name: "Class materials",
    getFileHandle: async (name, options) => {
      if (!options && name === "set.json") return {};
      if (!options) throw new DOMException("Missing", "NotFoundError");
      expect(name).toBe("set (1).json");
      return { name, createWritable: async () => output };
    },
  });
  await exportText("contents", "set.json");
  expect(operations).toEqual(["contents", "closed"]);
  expect(notices).toEqual([{ filename: "set (1).json", location: "Class materials" }]);
});

test("browser write errors abort without reporting success", async () => {
  let aborted = false;
  window.showDirectoryPicker = async () => ({
    getFileHandle: async (name, options) => {
      if (!options) throw new DOMException("Missing", "NotFoundError");
      return { createWritable: async () => ({
        write: async () => { throw new Error("Disk full"); },
        abort: async () => { aborted = true; },
      }) };
    },
  });
  await expect(exportText("contents", "set.json")).rejects.toThrow("Disk full");
  expect(aborted).toBe(true);
  expect(notices).toEqual([]);
});

test("native write errors never trigger the Downloads fallback", async () => {
  globalThis.fetch = () => { throw new Error("Unexpected fallback"); };
  window.rundiffDesktop = { saveExport: async () => { throw new Error("No permission"); } };
  await expect(exportText("{}", "set.json")).rejects.toThrow("No permission");
  expect(notices).toEqual([]);
});

test("CSV exports retain author authentication and use the same picker", async () => {
  globalThis.fetch = async (url, options) => {
    expect(url).toBe("/analytics.csv");
    expect(options.headers).toEqual({ "X-Author-Key": "author-password" });
    return { ok: true, text: async () => "student,score\nAda,1" };
  };
  window.rundiffDesktop = { saveExport: async (filename, content) => {
    expect(content).toBe("student,score\nAda,1");
    return { filename, location: "Results" };
  } };
  await exportCsv("/analytics.csv", "analytics.csv");
  expect(notices).toEqual([{ filename: "analytics.csv", location: "Results" }]);
});
