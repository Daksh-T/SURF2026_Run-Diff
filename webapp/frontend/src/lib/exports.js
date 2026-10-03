import { api } from "./api.js";

// Accept lazy content so browser users choose a destination during the click gesture,
// before any API request can expire transient user activation.
export async function exportText(source, filename) {
  window.dispatchEvent(new Event("rundiff:export-start"));
  const content = () => typeof source === "function" ? source() : source;
  let result;
  try {
    if (window.rundiffDesktop?.saveExport) {
      result = await window.rundiffDesktop.saveExport(filename, await content());
    } else if (window.webkit?.messageHandlers?.rundiffExport) {
      result = await window.webkit.messageHandlers.rundiffExport.postMessage({ filename, content: await content() });
    } else if (window.__TAURI__?.core?.invoke) {
      result = await window.__TAURI__.core.invoke("save_export", { filename, content: await content() });
    } else if (window.showDirectoryPicker) {
      const directory = await window.showDirectoryPicker({ id: "rundiff-exports", mode: "readwrite", startIn: "downloads" });
      // Preserve earlier browser exports, for which no native overwrite confirmation exists.
      const dot = filename.lastIndexOf(".");
      let name = filename;
      for (let index = 1; ; index++) {
        try {
          await directory.getFileHandle(name);
          name = `${filename.slice(0, dot)} (${index})${filename.slice(dot)}`;
        } catch (error) {
          if (error.name === "NotFoundError") break;
          throw error;
        }
      }
      const text = await content();
      const handle = await directory.getFileHandle(name, { create: true });
      const output = await handle.createWritable();
      try {
        await output.write(text);
        await output.close();
      } catch (error) {
        await output.abort().catch(() => {});
        throw error;
      }
      result = { filename: handle.name, location: directory.name };
    } else {
      // Older browsers retain local Downloads exports. All desktop shells use a native picker.
      result = await api.saveExport(filename, await content());
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    throw error instanceof Error ? error : new Error(String(error));
  }
  if (!result || result.cancelled) return;
  window.dispatchEvent(new CustomEvent("rundiff:export-complete", { detail: result }));
}

export function exportJson(source, filename) {
  return exportText(async () => JSON.stringify(typeof source === "function" ? await source() : source, null, 2), filename);
}

export async function exportCsv(url, filename) {
  return exportText(async () => {
    const key = localStorage.getItem("tutor.authorKey");
    const response = await fetch(url, { headers: key ? { "X-Author-Key": key } : {} });
    if (!response.ok) throw new Error("Could not export CSV. Please try again.");
    return response.text();
  }, filename);
}
