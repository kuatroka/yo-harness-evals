(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { options: null, fromJob: null, launchTimer: null };

  async function api(path, init) {
    const response = await fetch(path, init);
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    return { ok: response.ok, status: response.status, payload };
  }

  function describeFailure(result) {
    const payload = result.payload;
    if (payload && typeof payload === "object") {
      return payload.detail || payload.unresolved?.join("; ") || JSON.stringify(payload);
    }
    return typeof payload === "string" && payload ? payload : `HTTP ${result.status}`;
  }

  function feedback(message, tone) {
    const box = $("feedback");
    box.textContent = message;
    box.className = tone ? `feedback ${tone}` : "feedback";
    box.hidden = !message;
  }

  function modelByKey(key) {
    return state.options.models.find((model) => model.key === key) || null;
  }

  function fillSelect(select, values, selected) {
    select.innerHTML = "";
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value.value;
      option.textContent = value.label;
      if (value.title) option.title = value.title;
      select.appendChild(option);
    }
    if (selected != null && values.some((value) => value.value === selected)) {
      select.value = selected;
    }
  }

  function fillAgents(selected) {
    fillSelect(
      $("agent"),
      state.options.agents.map((agent) => ({ value: agent.id, label: agent.label })),
      selected
    );
  }

  function fillEfforts(selected) {
    fillSelect(
      $("effort"),
      state.options.efforts.map((effort) => ({ value: effort, label: effort })),
      selected
    );
  }

  function fillDatasets(selected) {
    fillSelect(
      $("dataset"),
      state.options.datasets.map((dataset) => ({
        value: dataset.name,
        label: `${dataset.name} (${dataset.tasks.length})`,
      })),
      selected || state.options.default_dataset
    );
  }

  function currentDataset() {
    const name = $("dataset").value;
    return (
      state.options.datasets.find((dataset) => dataset.name === name) ||
      state.options.datasets[0] || { name: "", tasks: [] }
    );
  }

  function fillModels(selected) {
    const agent = $("agent").value;
    const models = state.options.models.filter((model) => model.harnesses.includes(agent));
    fillSelect(
      $("model"),
      models.map((model) => ({
        value: model.key,
        label: `${model.label} — ${model.route}`,
        title: model.key,
      })),
      selected
    );
    syncEffort();
  }

  function syncEffort(selected) {
    const model = modelByKey($("model").value);
    if (!model) {
      $("model-notes").textContent = "The registry routes no model to this agent.";
      return;
    }
    const wanted =
      selected && state.options.efforts.includes(selected) ? selected : model.effort;
    if (state.options.efforts.includes(wanted)) $("effort").value = wanted;
    const notes = [];
    if (model.notes) notes.push(model.notes);
    notes.push("Effort comes from the model's registry row, and run_matrix.sh uses that value.");
    $("model-notes").textContent = notes.join(" ");
  }

  function fillTasks() {
    const list = $("task-list");
    list.innerHTML = "";
    for (const task of currentDataset().tasks) {
      const label = document.createElement("label");
      label.className = "task";
      label.dataset.name = task.toLowerCase();
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = task;
      box.addEventListener("change", updateSummary);
      const name = document.createElement("span");
      name.textContent = task;
      label.append(box, name);
      list.appendChild(label);
    }
    updateSummary();
  }

  function taskBoxes() {
    return Array.from($("task-list").querySelectorAll('input[type="checkbox"]'));
  }

  function selectedTasks() {
    return taskBoxes().filter((box) => box.checked).map((box) => box.value);
  }

  function wholeNumber(id, fallback, minimum) {
    const value = Number($(id).value);
    return Number.isFinite(value) && value >= minimum ? Math.trunc(value) : fallback;
  }

  function updateSummary() {
    const count = selectedTasks().length;
    const attempts = wholeNumber("attempts", 2, 1);
    const concurrent = wholeNumber("n_concurrent", 1, 1);
    $("task-count").textContent = String(count);
    $("summary").textContent =
      `${count} task${count === 1 ? "" : "s"} × ${attempts} attempt${attempts === 1 ? "" : "s"}` +
      ` · ${concurrent} concurrent`;
    $("submit").disabled = count === 0;
  }

  function applyFilter() {
    const needle = $("task-filter").value.trim().toLowerCase();
    for (const label of $("task-list").querySelectorAll(".task")) {
      label.hidden = needle.length > 0 && !label.dataset.name.includes(needle);
    }
  }

  function currentSpec() {
    const model = $("model").value;
    const effort = $("effort").value || (modelByKey(model) || {}).effort || "";
    return {
      dataset: $("dataset").value,
      agent: $("agent").value,
      model,
      effort,
      tasks: selectedTasks(),
      n_concurrent: wholeNumber("n_concurrent", 1, 1),
      attempts: wholeNumber("attempts", 2, 1),
      retries: wholeNumber("retries", 2, 0),
      from_job: state.fromJob,
    };
  }

  function apply(partial) {
    if (!partial) return;
    if (
      typeof partial.dataset === "string" &&
      state.options.datasets.some((dataset) => dataset.name === partial.dataset)
    ) {
      fillDatasets(partial.dataset);
      fillTasks();
    }
    if (partial.agent && state.options.agents.some((agent) => agent.id === partial.agent)) {
      fillAgents(partial.agent);
    }
    fillModels(partial.model || undefined);
    if (partial.effort) syncEffort(partial.effort);
    if (Array.isArray(partial.tasks)) {
      const wanted = new Set(partial.tasks);
      for (const box of taskBoxes()) box.checked = wanted.has(box.value);
    }
    if (Number.isFinite(partial.n_concurrent)) $("n_concurrent").value = partial.n_concurrent;
    if (Number.isFinite(partial.attempts)) $("attempts").value = partial.attempts;
    if (Number.isFinite(partial.retries)) $("retries").value = partial.retries;
    if (typeof partial.from_job === "string") state.fromJob = partial.from_job;
    updateSummary();
  }

  async function submit(event) {
    event.preventDefault();
    const spec = currentSpec();
    if (!spec.tasks.length) {
      feedback("Pick at least one task.", "warn");
      return;
    }
    $("submit").disabled = true;
    const result = await api("/api/overlay/launches", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(spec),
    });
    $("submit").disabled = false;

    if (result.status === 201) {
      const { job_prefix: prefix, log_path: logPath } = result.payload;
      feedback(`Started ${prefix}.\nRunner log: ${logPath}`, "ok");
      state.fromJob = null;
      $("from-job").value = "";
      refreshLaunches();
      return;
    }
    if (result.status === 409) {
      feedback(`Not started. ${describeFailure(result)}`, "warn");
      apply(result.payload.partial);
      return;
    }
    feedback(`Not started. ${describeFailure(result)}`, "warn");
  }

  async function loadJob() {
    const name = $("from-job").value.trim();
    if (!name) {
      feedback("Name a job first.", "warn");
      return;
    }
    const result = await api(`/api/overlay/jobs/${encodeURIComponent(name)}/spec`);
    if (!result.ok) {
      feedback(`Cannot load ${name}: ${describeFailure(result)}`, "warn");
      return;
    }
    state.fromJob = name;
    apply(result.payload.spec);
    const unresolved = result.payload.unresolved || [];
    feedback(
      unresolved.length ? `Loaded ${name}, with gaps: ${unresolved.join("; ")}` : `Loaded ${name}.`,
      unresolved.length ? "warn" : "ok"
    );
  }

  function cell(text, className) {
    const element = document.createElement("td");
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function statusCell(status) {
    const element = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `status ${status}`;
    chip.textContent = status;
    element.appendChild(chip);
    return element;
  }

  function renderLaunches() {
    const host = $("launches");
    host.innerHTML = "";
    if (!state.launches.length) {
      host.textContent = "No launches yet.";
      return;
    }
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of ["Started", "Agent", "Model", "Tasks", "Status", "Jobs", ""]) {
      const cellElement = document.createElement("th");
      cellElement.textContent = title;
      headRow.appendChild(cellElement);
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const body = document.createElement("tbody");

    for (const launch of state.launches) {
      const spec = launch.spec || {};
      const row = document.createElement("tr");
      const started = launch.created_at ? new Date(launch.created_at) : null;
      row.appendChild(cell(started && !Number.isNaN(started.valueOf()) ? started.toLocaleString() : "—"));
      row.appendChild(cell(spec.agent || "—"));
      row.appendChild(cell(`${spec.model || "—"}${spec.effort ? ` (${spec.effort})` : ""}`));
      row.appendChild(cell(String((spec.tasks || []).length)));
      row.appendChild(statusCell(launch.status || "pending"));

      const jobs = document.createElement("td");
      jobs.className = "jobs";
      jobs.textContent = (launch.jobs || []).length
        ? launch.jobs.map((job) => `${job.job_name} · ${job.state}`).join("\n")
        : "no job folder yet";
      jobs.style.whiteSpace = "pre-line";
      row.appendChild(jobs);

      const actions = document.createElement("td");
      const rerun = document.createElement("button");
      rerun.type = "button";
      rerun.className = "ghost";
      rerun.textContent = "Rerun";
      const source = (launch.jobs || [])[0]?.job_name || spec.from_job;
      if (source) {
        rerun.addEventListener("click", () => rerunLaunch(source));
      } else {
        rerun.disabled = true;
      }
      actions.appendChild(rerun);
      row.appendChild(actions);
      body.appendChild(row);
    }
    table.appendChild(body);
    host.appendChild(table);
  }

  async function rerunLaunch(jobName) {
    feedback(`Reading ${jobName}…`, null);
    const result = await api("/api/overlay/launches", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ from_job: jobName }),
    });
    if (result.status === 201) {
      feedback(`Started ${result.payload.job_prefix} from ${jobName}.`, "ok");
      refreshLaunches();
      return;
    }
    feedback(`Not started. ${describeFailure(result)}`, "warn");
    if (result.payload && result.payload.partial) apply(result.payload.partial);
  }

  async function refreshLaunches() {
    const result = await api("/api/overlay/launches");
    if (!result.ok || !Array.isArray(result.payload)) return;
    state.launches = result.payload;
    renderLaunches();
  }

  async function fillJobNames() {
    const result = await api("/api/jobs");
    if (!result.ok || !result.payload || !Array.isArray(result.payload.items)) return;
    const list = $("job-names");
    list.innerHTML = "";
    const seen = new Set();
    for (const job of result.payload.items) {
      if (!job || !job.name || seen.has(job.name)) continue;
      seen.add(job.name);
      const option = document.createElement("option");
      option.value = job.name;
      list.appendChild(option);
    }
  }

  async function init() {
    const result = await api("/api/overlay/options");
    if (!result.ok || !result.payload) {
      feedback(
        `The launch overlay is not answering (${describeFailure(result)}). ` +
          "Start the viewer with scripts/harbor_view.sh.",
        "warn"
      );
      return;
    }
    state.options = result.payload;
    fillAgents();
    fillEfforts();
    fillDatasets();
    fillModels();
    fillTasks();

    $("dataset").addEventListener("change", fillTasks);
    $("agent").addEventListener("change", () => fillModels());
    $("model").addEventListener("change", () => syncEffort());
    $("task-filter").addEventListener("input", applyFilter);
    $("tasks-all").addEventListener("click", () => {
      for (const box of taskBoxes()) {
        if (!box.closest(".task").hidden) box.checked = true;
      }
      updateSummary();
    });
    $("tasks-none").addEventListener("click", () => {
      for (const box of taskBoxes()) box.checked = false;
      updateSummary();
    });
    for (const id of ["n_concurrent", "attempts"]) {
      $(id).addEventListener("input", updateSummary);
    }
    $("spec-form").addEventListener("submit", submit);
    $("load-job").addEventListener("click", loadJob);

    await fillJobNames();
    await refreshLaunches();
    state.launchTimer = window.setInterval(() => {
      if (!document.hidden) refreshLaunches();
    }, 5000);
  }

  init();
})();
