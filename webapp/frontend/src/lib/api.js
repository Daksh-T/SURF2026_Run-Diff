// Tiny fetch wrapper. The dev server proxies /api/* to the FastAPI backend.
async function j(method, path, body) {
  const headers = {};
  if (body) headers["content-type"] = "application/json";
  if (path.startsWith("/api/instructor/") || path === "/api/auth/set") {
    const key = localStorage.getItem("tutor.authorKey");
    if (key) headers["X-Author-Key"] = key;
  }
  const res = await fetch(path, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  // auth
  authStatus: () => j("GET", "/api/auth/status"),
  authSet: (password, current) => j("POST", "/api/auth/set", { password, current }),
  authClear: (current) => j("POST", "/api/auth/clear", { current }),
  authCheck: (password) => j("POST", "/api/auth/check", { password }),

  // student
  studentSets: (classId) => j("GET", `/api/student/sets${classId ? `?class_id=${encodeURIComponent(classId)}` : ""}`),
  studentSet: (id, classId) => j("GET", `/api/student/sets/${id}${classId ? `?class_id=${encodeURIComponent(classId)}` : ""}`),
  grade: (set_id, problem_id, sql, cls) => j("POST", "/api/student/grade", { set_id, problem_id, sql, ...cls }),
  hint: (set_id, problem_id, sql, level, cls) => j("POST", "/api/student/hint", { set_id, problem_id, sql, level, ...cls }),
  join: (passphrase, name) => j("POST", "/api/student/join", { passphrase, name }),
  // connect to a class server over the LAN: pull the assignment by code, import it, then join
  connectToServer: (url, code, name) => j("POST", "/api/student/connect", { url, code, name }),
  importAssignment: (assignment) => j("POST", "/api/student/import-assignment", { assignment }),
  syncAttempts: (classId) => j("POST", `/api/student/sync/${classId}`),
  attemptsExport: (classId) => j("GET", `/api/student/attempts-export/${classId}`),
  classStatus: (classId, student) => j("GET", `/api/student/class-status/${classId}${student ? `?student=${encodeURIComponent(student)}` : ""}`),

  // instructor
  instructorSets: () => j("GET", "/api/instructor/sets"),
  newSet: (title) => j("POST", "/api/instructor/sets", { title }),
  getSet: (id) => j("GET", `/api/instructor/sets/${id}`),
  renameSet: (id, title) => j("PATCH", `/api/instructor/sets/${id}`, { title }),
  removeSet: (id) => j("DELETE", `/api/instructor/sets/${id}`),
  author: (payload) => j("POST", "/api/instructor/author", payload),
  authorBatch: (sections) => j("POST", "/api/instructor/author-batch", { sections }),
  job: (id) => j("GET", `/api/instructor/jobs/${id}`),
  addProblem: (set_id, problem) => j("POST", `/api/instructor/sets/${set_id}/problems`, { problem }),
  removeProblem: (set_id, problem_id) => j("DELETE", `/api/instructor/sets/${set_id}/problems/${problem_id}`),
  reorderProblems: (set_id, order) => j("POST", `/api/instructor/sets/${set_id}/reorder`, { order }),
  updateProblem: (set_id, problem_id, fields) => j("PATCH", `/api/instructor/sets/${set_id}/problems/${problem_id}`, fields),
  reauthorProblem: (set_id, problem_id, ddl) => j("POST", `/api/instructor/sets/${set_id}/problems/${problem_id}/reauthor`, { ddl }),
  exportSet: (set_id) => j("GET", `/api/instructor/sets/${set_id}/export`),
  importSet: (set) => j("POST", "/api/instructor/sets/import", { set }),
  publish: (set_id) => j("POST", `/api/instructor/sets/${set_id}/publish`),

  // classes
  listClasses: () => j("GET", "/api/instructor/classes"),
  getClass: (id) => j("GET", `/api/instructor/classes/${id}`),
  newClass: (title, set_ids, mode, roster) =>
    j("POST", "/api/instructor/classes", { title, set_ids, mode, roster }),
  updateClass: (id, fields) => j("PATCH", `/api/instructor/classes/${id}`, fields),
  deleteClass: (id) => j("DELETE", `/api/instructor/classes/${id}`),
  deleteStudent: (id, student) => j("DELETE", `/api/instructor/classes/${id}/student/${encodeURIComponent(student)}`),
  deleteAttempt: (id, uid) => j("DELETE", `/api/instructor/classes/${id}/attempt/${encodeURIComponent(uid)}`),
  setSession: (id, state) => j("PATCH", `/api/instructor/classes/${id}/session`, { state }),
  exportAssignment: (id) => j("GET", `/api/instructor/classes/${id}/export-assignment`),
  importAttempts: (id, obj) => j("POST", `/api/instructor/classes/${id}/import-attempts`, obj),

  // analytics (set_id optional — restricts to one of a class's assigned sets)
  classAnalytics: (id, setId) =>
    j("GET", `/api/instructor/classes/${id}/analytics${setId ? `?set_id=${encodeURIComponent(setId)}` : ""}`),
  classAnalyticsCsvUrl: (id, setId) =>
    `/api/instructor/classes/${id}/analytics.csv${setId ? `?set_id=${encodeURIComponent(setId)}` : ""}`,
  classStudent: (id, student, setId) =>
    j("GET", `/api/instructor/classes/${id}/student/${encodeURIComponent(student)}${setId ? `?set_id=${encodeURIComponent(setId)}` : ""}`),
  instructorConfig: () => j("GET", "/api/instructor/config"),
  setInstructorConfig: (fields) => j("PATCH", "/api/instructor/config", fields),
  hostInfo: () => j("GET", "/api/instructor/host-info"),
  classLive: (id, since, setId) => {
    const qs = new URLSearchParams();
    if (since) qs.set("since", since);
    if (setId) qs.set("set_id", setId);
    const s = qs.toString();
    return j("GET", `/api/instructor/classes/${id}/live${s ? `?${s}` : ""}`);
  },

  // first-run Ollama setup (not author-gated)
  setupStatus: () => j("GET", "/api/setup/status"),
  setupPull: () => j("POST", "/api/setup/pull"),
  setupPullStatus: (jobId) => j("GET", `/api/setup/pull/${jobId}`),
};
