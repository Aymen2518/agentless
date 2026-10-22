// agentless landing page. No dependencies.
(() => {
  "use strict";

  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  /* ---------------------------------------------------------------
   * Background: a slow flow field of particles that links nearby
   * points and leans toward the pointer.
   * ------------------------------------------------------------- */
  function field() {
    const cv = $("#field");
    const ctx = cv.getContext("2d");
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const mouse = { x: -1e4, y: -1e4 };
    let W, H, pts = [], running = true, t = 0;

    function size() {
      W = cv.width = innerWidth * dpr;
      H = cv.height = innerHeight * dpr;
      const n = Math.round(clamp((innerWidth * innerHeight) / 16000, 30, 110));
      pts = Array.from({ length: n }, () => ({
        x: Math.random() * W, y: Math.random() * H, vx: 0, vy: 0,
        hue: Math.random() < 0.7 ? "122,162,255" : "197,139,255",
      }));
    }

    function frame() {
      if (!running) return;
      t += 0.0016;
      ctx.clearRect(0, 0, W, H);

      // Two slow glows that drift with time and scroll.
      const sy = scrollY * dpr * 0.15;
      glow(W * (0.82 + Math.sin(t * 2) * 0.05), H * 0.05 - sy, W * 0.55, "122,162,255", 0.13);
      glow(W * (0.1 + Math.cos(t * 1.6) * 0.05), H * 0.55 - sy * 0.6, W * 0.45, "197,139,255", 0.08);

      const link = 130 * dpr, link2 = link * link;
      for (const p of pts) {
        const a = Math.sin(p.x * 0.0016 + t * 3) + Math.cos(p.y * 0.0019 - t * 2);
        p.vx += Math.cos(a) * 0.035 * dpr;
        p.vy += Math.sin(a) * 0.035 * dpr;
        const dx = mouse.x - p.x, dy = mouse.y - p.y, d2 = dx * dx + dy * dy;
        if (d2 < 40000 * dpr * dpr) { p.vx += dx * 0.00012; p.vy += dy * 0.00012; }
        p.vx *= 0.94; p.vy *= 0.94;
        p.x += p.vx; p.y += p.vy;
        if (p.x < -20) p.x = W + 20; else if (p.x > W + 20) p.x = -20;
        if (p.y < -20) p.y = H + 20; else if (p.y > H + 20) p.y = -20;
      }
      ctx.lineWidth = dpr;
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i];
        for (let j = i + 1; j < pts.length; j++) {
          const q = pts[j], dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy;
          if (d2 < link2) {
            ctx.strokeStyle = `rgba(${p.hue},${(1 - d2 / link2) * 0.16})`;
            ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
          }
        }
        ctx.fillStyle = `rgba(${p.hue},.55)`;
        ctx.fillRect(p.x - dpr, p.y - dpr, 2 * dpr, 2 * dpr);
      }
      requestAnimationFrame(frame);
    }

    function glow(x, y, r, rgb, a) {
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, `rgba(${rgb},${a})`);
      g.addColorStop(1, `rgba(${rgb},0)`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
    }

    size();
    addEventListener("resize", size);
    addEventListener("pointermove", (e) => { mouse.x = e.clientX * dpr; mouse.y = e.clientY * dpr; }, { passive: true });
    document.addEventListener("visibilitychange", () => {
      const was = running;
      running = !document.hidden;
      if (running && !was) requestAnimationFrame(frame);
    });
    if (reduced) {
      running = false;
      glow(W * 0.82, H * 0.05, W * 0.55, "122,162,255", 0.13);
      glow(W * 0.1, H * 0.55, W * 0.45, "197,139,255", 0.08);
    } else requestAnimationFrame(frame);
  }

  /* ---------------------------------------------------------------
   * Headline: characters resolve out of plan symbols.
   * ------------------------------------------------------------- */
  function scramble() {
    const glyphs = "+~±-=✋{}$:/";
    $$("[data-scramble]").forEach((el, li) => {
      const text = el.textContent;
      if (reduced) return;
      const start = performance.now() + li * 260;
      const per = 38;
      el.textContent = "";
      (function tick(now) {
        const done = Math.floor((now - start) / per);
        let html = "";
        for (let i = 0; i < text.length; i++) {
          const ch = text[i];
          if (i < done || ch === " ") html += esc(ch);
          else if (i < done + 6 && now > start) html += `<span class="glyph">${esc(glyphs[(Math.random() * glyphs.length) | 0])}</span>`;
          else html += `<span style="opacity:0">${esc(ch)}</span>`;
        }
        el.innerHTML = html;
        if (done < text.length) requestAnimationFrame(tick);
        else el.textContent = text;
      })(performance.now());
    });
  }

  /* ---------------------------------------------------------------
   * Hero reconciler: type agent.yaml, plan it into five resources,
   * then apply them in order.
   * ------------------------------------------------------------- */
  const YAML = [
    ["<span class=k>service</span>: report-agent"],
    ["<span class=k>provider</span>:"],
    ["  project: <span class=s>${param:project}</span>"],
    ["  region: europe-west1"],
    ["<span class=k>agent</span>:", "engine"],
    ["  runtime: { cpu: <span class=s>\"2\"</span>, memory: 4Gi }", "engine"],
    ["  secrets:", "engine iam"],
    ["    TOKEN: { secret: slack-token }", "engine iam"],
    ["<span class=k>identity</span>:", "serviceAccount"],
    ["  type: serviceAccount", "serviceAccount agentIdentity"],
    ["  serviceAccount: { create: true }", "serviceAccount"],
    ["  roles:", "iam"],
    ["    project: [roles/aiplatform.user]", "iam"],
    ["<span class=k>publish</span>:", "geminiEnterprise"],
    ["  geminiEnterprise:", "geminiEnterprise"],
    ["    app: <span class=s>${param:geApp}</span>", "geminiEnterprise"],
  ];

  const PLAN = {
    serviceAccount: "  <span class=s>+ serviceAccount     create   sa-report-agent-dev</span>",
    agentIdentity: "  <span class=c>= agentIdentity      noop     not used</span>",
    iam: "  <span class=s>+ iam                create   4 to add</span>",
    engine: "  <span class=s>+ engine             create   report-agent (source 9f2c41ab07e3)</span>",
    geminiEnterprise: "  <span class=s>+ geminiEnterprise   create   acme-assistant</span>",
  };

  function reconciler() {
    const root = $("#reconciler");
    const pre = $("#rc-yaml");
    const svg = $("#rc-wires");
    const foot = $("#rc-foot");
    const nodes = $$("#rc-nodes li");
    const byName = Object.fromEntries(nodes.map((n) => [n.dataset.node, n]));
    let run = 0;

    // Gentle 3D tilt that follows the pointer.
    if (!reduced) {
      root.addEventListener("pointermove", (e) => {
        const r = root.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
        root.style.setProperty("--ry", `${x * 8 - 2}deg`);
        root.style.setProperty("--rx", `${-y * 6}deg`);
      });
      root.addEventListener("pointerleave", () => { root.style.removeProperty("--ry"); root.style.removeProperty("--rx"); });
      let a = 0;
      (function spin() { a = (a + 1.2) % 360; root.style.setProperty("--spin", `${a}deg`); requestAnimationFrame(spin); })();
    }

    function lines() { return $$(".yl", pre); }

    function wire(name, on) {
      $$(`path[data-n="${name}"]`, svg).forEach((p) => p.classList.toggle("on", on));
    }

    function drawWires() {
      svg.innerHTML = "";
      const box = svg.getBoundingClientRect();
      const ls = lines();
      if (!ls.length) return;
      for (const node of nodes) {
        const name = node.dataset.node;
        const src = ls.filter((l) => (l.dataset.n || "").split(" ").includes(name));
        if (!src.length) continue;
        const a = src[0].getBoundingClientRect(), b = src[src.length - 1].getBoundingClientRect();
        const nr = node.getBoundingClientRect();
        const x1 = Math.min(a.left + a.width, box.left + box.width * 0.52) - box.left + 4;
        const y1 = (a.top + b.bottom) / 2 - box.top;
        const x2 = nr.left - box.left - 2, y2 = nr.top + nr.height / 2 - box.top;
        const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const mx = (x1 + x2) / 2;
        p.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
        p.dataset.n = name;
        svg.appendChild(p);
      }
    }

    // A dot that rides a wire from the YAML into its node.
    function pulse(name) {
      const path = $(`path[data-n="${name}"]`, svg);
      if (!path || reduced) return;
      const len = path.getTotalLength();
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("r", "3.2");
      svg.appendChild(dot);
      const t0 = performance.now();
      (function step(now) {
        const k = clamp((now - t0) / 650);
        const pt = path.getPointAtLength(len * (1 - Math.pow(1 - k, 3)));
        dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y);
        if (k < 1) requestAnimationFrame(step); else dot.remove();
      })(t0);
    }

    function hot(name, on) {
      lines().forEach((l) => { if ((l.dataset.n || "").split(" ").includes(name)) l.classList.toggle("hot", on); });
    }

    async function play() {
      const me = ++run;
      const alive = () => me === run;
      nodes.forEach((n) => (n.className = ""));
      root.style.setProperty("--glow", 0);
      svg.innerHTML = "";
      foot.innerHTML = '<span class="c">$</span> agentless deploy --stage dev';

      // Type the file.
      pre.innerHTML = "";
      for (const [html, n] of YAML) {
        if (!alive()) return;
        const line = document.createElement("span");
        line.className = "yl";
        if (n) line.dataset.n = n;
        pre.appendChild(line);
        if (reduced) { line.innerHTML = html; continue; }
        const tmp = document.createElement("div");
        tmp.innerHTML = html;
        const plain = tmp.textContent;
        for (let i = 1; i <= plain.length; i += 2) {
          if (!alive()) return;
          line.innerHTML = esc(plain.slice(0, i)) + '<i class="caret"></i>';
          await sleep(9);
        }
        line.innerHTML = html;
      }
      drawWires();

      // Plan.
      let plan = '<span class=c>$</span> agentless deploy --stage dev\n<span class=b>Deploying report-agent → stage dev (acme-agents-dev, europe-west1)</span>';
      foot.innerHTML = plan;
      await sleep(reduced ? 0 : 450);
      for (const node of nodes) {
        if (!alive()) return;
        const name = node.dataset.node;
        hot(name, true); wire(name, true); pulse(name);
        await sleep(reduced ? 0 : 520);
        node.classList.add("planned");
        if (name === "agentIdentity") node.classList.add("noop");
        foot.innerHTML = `<span class=b>Changes:</span>\n${PLAN[name]}`;
        hot(name, false);
      }
      foot.innerHTML = "<span class=b>Plan: 4 to create.</span>";
      await sleep(reduced ? 0 : 700);

      // Apply in order.
      for (const node of nodes) {
        if (!alive()) return;
        const name = node.dataset.node;
        if (name === "agentIdentity") { node.classList.add("done"); wire(name, false); continue; }
        node.classList.add("applying");
        foot.innerHTML = `<span class=n>~ applying ${name}…</span>`;
        await sleep(reduced ? 0 : name === "engine" ? 1500 : 650);
        node.classList.remove("applying");
        node.classList.add("done");
        wire(name, false);
      }
      if (!alive()) return;
      foot.innerHTML = '<span class=s>✔ deployed</span>  <span class=c>queryUrl</span> <span class=u>https://europe-west1-aiplatform.googleapis.com/…:streamQuery</span>';
      root.style.setProperty("--glow", 1);
      await sleep(9000);
      if (alive() && !reduced && isVisible(root)) play();
    }

    function isVisible(el) {
      const r = el.getBoundingClientRect();
      return r.bottom > 0 && r.top < innerHeight;
    }

    $(".rc-replay", root).addEventListener("click", play);
    addEventListener("resize", () => { if (lines().length === YAML.length) { drawWires(); } });
    // Hovering a resource highlights the YAML it comes from.
    nodes.forEach((n) => {
      n.addEventListener("pointerenter", () => { hot(n.dataset.node, true); wire(n.dataset.node, true); });
      n.addEventListener("pointerleave", () => { hot(n.dataset.node, false); wire(n.dataset.node, false); });
    });
    setTimeout(play, reduced ? 0 : 700);
  }

  /* ---------------------------------------------------------------
   * Scroll-scrubbed sections.
   * ------------------------------------------------------------- */
  function scrubbed() {
    const collapse = $("#why"), stage = $("#collapse-stage"), shards = $$(".shard", stage);
    const pipe = $("#how"), beam = $("#rail-beam"), stages = $$("#stages li"), log = $("#rail-log");
    const bar = $(".progress i"), nav = $(".nav");

    const LOG = [
      "<span class=b>Deploying report-agent → stage dev (acme-agents-dev, europe-west1)</span>",
      "<span class=c>load</span>      agent.yaml, stage dev",
      "<span class=c>resolve</span>   ${param:project} = acme-agents-dev, ${secret:slack-token} = ••••",
      "<span class=c>validate</span>  <span class=s>✔ agent.yaml is valid for stage dev</span>",
      "<span class=c>package</span>   <span class=s>✔ 42 files, sha256 9f2c41ab07e3…</span>",
      "<span class=c>lock</span>      gs://acme-agentless-state-dev/agentless/report-agent/dev/state.json",
      "<span class=c>plan</span>      <span class=n>~ engine  update  memory: 4Gi → 8Gi</span>  <span class=c>(config only, no rebuild)</span>",
      "<span class=c>apply</span>     <span class=n>~ engine</span> update_mask=deploymentSpec.resourceLimits",
      "<span class=c>save</span>      <span class=s>✔ deployed</span>  state + deployment_metadata.json",
    ];

    function sticky() { return getComputedStyle($(".sticky", collapse)).position === "sticky"; }

    function progress(el) {
      const r = el.getBoundingClientRect();
      if (sticky()) return clamp(-r.top / (r.height - innerHeight));
      // Static layout (mobile): progress as the section passes the middle of the screen.
      return clamp((innerHeight * 0.75 - r.top) / (r.height * 0.8));
    }

    let lastStep = -1;
    function update() {
      const doc = document.documentElement;
      bar.parentElement.style.setProperty("--p", scrollY / (doc.scrollHeight - innerHeight || 1));
      nav.classList.toggle("scrolled", scrollY > 8);

      // Three tools fold into one file.
      const c = reduced ? 1 : progress(collapse);
      const t = clamp((c - 0.08) / 0.6);
      const after = clamp((c - 0.55) / 0.3);
      shards.forEach((s, i) => s.style.setProperty("--t", clamp(t * 1.15 - i * 0.07).toFixed(3)));
      collapse.style.setProperty("--after", after.toFixed(3));

      // The deploy beam.
      const p = reduced ? 1 : progress(pipe);
      const k = clamp((p - 0.05) / 0.85);
      beam.parentElement.style.setProperty("--rp", k.toFixed(3));
      const step = Math.min(stages.length - 1, Math.floor(k * stages.length - 0.001));
      stages.forEach((s, i) => { s.classList.toggle("lit", i <= step); s.classList.toggle("now", i === step && k < 1); });
      if (step !== lastStep) {
        lastStep = step;
        log.innerHTML = LOG.slice(0, step + 2).slice(-7).join("\n");
      }
    }

    let queued = false;
    addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; update(); }); } }, { passive: true });
    addEventListener("resize", update);
    update();
  }

  /* ---------------------------------------------------------------
   * Demo terminal: chapters of commands and their output.
   * ------------------------------------------------------------- */
  const CHAPTERS = [
    {
      name: "init",
      steps: [
        ["$", "agentless init . --project acme-agents-dev"],
        ["out", "<span class=s>✔ wrote agent.yaml</span>\n  next: edit project ids and roles, then `agentless plan --stage dev`"],
        ["$", "agentless validate --stage dev"],
        ["out", "<span class=s>✔ agent.yaml is valid for stage dev</span>"],
      ],
    },
    {
      name: "plan",
      steps: [
        ["$", "agentless plan --stage dev"],
        ["out", `<span class=b>Planning report-agent → stage dev (acme-agents-dev, europe-west1)</span>
<span class=b>Changes:</span>
<span class=s>  + serviceAccount     create   sa-report-agent-dev</span>
<span class=c>  = agentIdentity      noop     not used</span>
<span class=s>  + iam                create   4 to add</span>
      + roles/aiplatform.user on project acme-agents-dev → sa-report-agent-dev
      + roles/cloudtrace.agent on project acme-agents-dev → sa-report-agent-dev <span class=c>(automatic: tracing)</span>
      + roles/logging.logWriter on project acme-agents-dev → sa-report-agent-dev <span class=c>(automatic: tracing)</span>
      + roles/secretmanager.secretAccessor on secret slack-token → sa-report-agent-dev
<span class=s>  + engine             create   report-agent (source 9f2c41ab07e3)</span>
<span class=s>  + geminiEnterprise   create   acme-assistant</span>
<span class=b>Plan: 4 to create.</span>`],
      ],
    },
    {
      name: "deploy",
      steps: [
        ["$", "agentless deploy --stage dev -y"],
        ["out", `<span class=b>Deploying report-agent → stage dev (acme-agents-dev, europe-west1)</span>
<span class=b>Changes:</span>
<span class=s>  + serviceAccount     create   sa-report-agent-dev</span>
<span class=s>  + iam                create   4 to add</span>
<span class=s>  + engine             create   report-agent (source 9f2c41ab07e3)</span>
<span class=s>  + geminiEnterprise   create   acme-assistant</span>
<span class=b>Plan: 4 to create.</span>`],
        ["wait", "building and starting the engine", 2600],
        ["out", `<span class=s>✔ deployed</span>
  service           report-agent
  stage             dev
  project           acme-agents-dev
  region            europe-west1
  state             gs://acme-agentless-state-dev/agentless/report-agent/dev/
  engine            projects/4821…/locations/europe-west1/reasoningEngines/7310…
  sourceHash        9f2c41ab07e3
  queryUrl          <span class=u>https://europe-west1-aiplatform.googleapis.com/v1/…:streamQuery</span>`],
      ],
    },
    {
      name: "update",
      steps: [
        ["note", "# agent.yaml: runtime.memory 4Gi → 8Gi, no code change"],
        ["$", "agentless plan --stage dev"],
        ["out", `<span class=b>Planning report-agent → stage dev (acme-agents-dev, europe-west1)</span>
<span class=b>Changes:</span>
<span class=c>  = serviceAccount     noop     sa-report-agent-dev</span>
<span class=c>  = iam                noop     4 managed binding(s)</span>
<span class=n>  ~ engine             update   config only</span>
      ~ memory: 4Gi → 8Gi
<span class=c>  = geminiEnterprise   noop     acme-assistant</span>
<span class=b>Plan: 1 to update.</span>`],
        ["$", "agentless deploy --stage dev -y"],
        ["wait", "updating with update_mask, no rebuild", 1300],
        ["out", "<span class=s>✔ deployed</span>"],
      ],
    },
    {
      name: "invoke",
      steps: [
        ["$", "agentless invoke --stage dev -m \"Summarise yesterday's incidents\""],
        ["out", `<span class=c>session 5512870349128826880</span>
[report_agent] Two incidents yesterday. INC-2291: checkout latency above SLO for 14 minutes,
resolved by rolling back the pricing cache. INC-2293: a failed nightly export, rerun at 06:10.
Full report written to gs://acme-reports-dev/2026-10-11.md.`],
      ],
    },
    {
      name: "eval",
      steps: [
        ["$", "agentless eval --stage dev"],
        ["out", `  basic-dataset.json: 12 cases against projects/4821…/locations/europe-west1/reasoningEngines/7310… …`],
        ["wait", "running on the Evaluation Service", 2000],
        ["out", `<span class=s>  ✔ final_response_quality       0.86  (min 0.80)</span>
<span class=s>  ✔ tool_use_quality             0.78  (min 0.70)</span>
  · safety                       0.97
  run      projects/4821…/locations/europe-west1/evaluationRuns/3305…
  results  gs://acme-agent-evals/report-agent/dev
  saved    .agentless/dev/eval/20261022-141203.json`],
      ],
    },
    {
      name: "observe",
      steps: [
        ["$", "agentless metrics --stage dev --since 1h"],
        ["out", `<span class=b>report-agent → stage dev, last 1h</span>
  requests  128  (2xx 126, 5xx 2)
  errors    1.6% 5xx
  latency   p50 1.8s  p95 4.2s`],
        ["$", "agentless logs --stage dev --severity WARNING --since 1h"],
        ["out", "<span class=n>2026-10-12T09:14:03Z WARNING</span> tool slack.post_message retried after 429"],
        ["$", "agentless open traces --print"],
        ["out", "<span class=u>https://console.cloud.google.com/traces/explorer?project=acme-agents-dev</span>"],
      ],
    },
    {
      name: "remove",
      steps: [
        ["$", "agentless remove --stage dev -y"],
        ["out", `<span class=b>Will delete:</span>
<span class=r>  - geminiEnterprise   delete   acme-assistant</span>
<span class=r>  - engine             delete   report-agent</span>
<span class=r>  - iam                delete   4 to remove</span>
<span class=r>  - serviceAccount     delete   sa-report-agent-dev</span>
<span class=b>Plan: 4 to delete.</span>`],
        ["wait", "removing in reverse order", 1400],
        ["out", "<span class=s>✔ removed</span>"],
      ],
    },
  ];

  function terminal() {
    const out = $("#term-out"), tabs = $("#chapters"), playBtn = $("#t-play"), prog = $("#t-prog");
    let chapter = 0, run = 0, paused = false, started = false;

    CHAPTERS.forEach((c, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.role = "tab";
      b.textContent = c.name;
      b.addEventListener("click", () => { paused = false; playBtn.textContent = "Pause"; start(i); });
      tabs.appendChild(b);
    });

    async function waitWhilePaused(me) { while (paused && me === run) await sleep(80); }

    async function start(i) {
      const me = ++run;
      chapter = i;
      $$("button", tabs).forEach((b, j) => b.setAttribute("aria-selected", String(j === i)));
      const steps = CHAPTERS[i].steps;
      out.innerHTML = "";
      prog.style.transform = "scaleX(0)";
      for (let s = 0; s < steps.length; s++) {
        const [kind, text, ms] = steps[s];
        await waitWhilePaused(me);
        if (me !== run) return;
        if (kind === "$" || kind === "note") {
          const line = document.createElement("span");
          out.appendChild(line);
          const pre = kind === "$" ? '<span class="s">❯</span> ' : "";
          for (let k = 0; k <= text.length; k++) {
            await waitWhilePaused(me);
            if (me !== run) return;
            const body = kind === "$" ? esc(text.slice(0, k)) : `<span class="c">${esc(text.slice(0, k))}</span>`;
            line.innerHTML = pre + body + '<span class="cur"></span>';
            if (!reduced) await sleep(18 + Math.random() * 30);
          }
          line.innerHTML = pre + (kind === "$" ? esc(text) : `<span class="c">${esc(text)}</span>`);
          out.append("\n");
          await sleep(reduced ? 0 : 260);
        } else if (kind === "wait") {
          const line = document.createElement("span");
          out.appendChild(line);
          const frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
          const t0 = performance.now();
          while (performance.now() - t0 < (reduced ? 0 : ms)) {
            await waitWhilePaused(me);
            if (me !== run) return;
            line.innerHTML = `<span class="n">${frames[((performance.now() / 80) | 0) % frames.length]}</span> <span class="c">${text}…</span>`;
            await sleep(80);
          }
          line.remove();
        } else {
          const block = document.createElement("span");
          block.innerHTML = text + "\n";
          out.appendChild(block);
          await sleep(reduced ? 0 : 700);
        }
        out.scrollTop = out.scrollHeight;
        prog.style.transform = `scaleX(${(s + 1) / steps.length})`;
      }
      out.insertAdjacentHTML("beforeend", '<span class="s">❯</span> <span class="cur"></span>');
      await sleep(reduced ? 1e9 : 2600);
      if (me === run) start((i + 1) % CHAPTERS.length);
    }

    playBtn.addEventListener("click", () => {
      paused = !paused;
      playBtn.textContent = paused ? "Play" : "Pause";
      playBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
    });
    $("#t-copy").addEventListener("click", (e) => {
      const cmds = CHAPTERS[chapter].steps.filter((s) => s[0] === "$").map((s) => s[1]).join("\n");
      copyText(cmds, e.currentTarget);
    });

    // Start when the terminal first scrolls into view.
    new IntersectionObserver((entries, io) => {
      if (entries.some((e) => e.isIntersecting) && !started) { started = true; io.disconnect(); start(0); }
    }, { threshold: 0.35 }).observe($("#term"));
  }

  /* ---------------------------------------------------------------
   * Lifecycle: deploy walks the steps in order, remove walks them
   * in reverse.
   * ------------------------------------------------------------- */
  function lifecycle() {
    const graph = $("#life-graph"), svg = $("#life-svg"), items = $$(".ln", graph), btns = $$(".seg button");
    let run = 0;

    function drawSpine(dir) {
      const box = graph.getBoundingClientRect();
      const pts = items.map((el) => {
        const r = el.getBoundingClientRect();
        return [r.left - box.left - 16, r.top + r.height / 2 - box.top];
      });
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
      const colour = dir === "remove" ? "var(--delete)" : "var(--create)";
      svg.innerHTML = `<path d="${d}" fill="none" stroke="var(--line)" stroke-width="2"/>
        <path id="spine" d="${d}" fill="none" stroke="${colour}" stroke-width="2.5" stroke-linecap="round" style="filter:drop-shadow(0 0 6px ${colour})"/>`;
      const spine = $("#spine", svg), len = spine.getTotalLength();
      spine.style.strokeDasharray = len;
      spine.style.strokeDashoffset = dir === "remove" ? -len : len;
      return { spine, len };
    }

    async function go(dir) {
      const me = ++run;
      btns.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.dir === dir)));
      items.forEach((el) => el.classList.remove("hit", "deploy", "remove"));
      const { spine, len } = drawSpine(dir);
      const order = items.map((_, i) => i);
      if (dir === "remove") order.reverse();
      const step = reduced ? 0 : 300;
      for (let n = 0; n < order.length; n++) {
        if (me !== run) return;
        items[order[n]].classList.add("hit", dir);
        const frac = n / (order.length - 1);
        spine.style.transition = reduced ? "none" : `stroke-dashoffset ${step}ms cubic-bezier(.2,.8,.2,1)`;
        spine.style.strokeDashoffset = dir === "deploy" ? len * (1 - frac) : -len * (1 - frac);
        await sleep(step);
      }
    }

    btns.forEach((b) => b.addEventListener("click", () => go(b.dataset.dir)));
    let seen = false;
    new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !seen) { seen = true; go("deploy"); }
    }, { threshold: 0.5 }).observe(graph);
    addEventListener("resize", () => { if (seen) go($(".seg [aria-checked=true]").dataset.dir); });
  }

  /* ---------------------------------------------------------------
   * Small interactions: card spotlight, magnetic buttons, copy.
   * ------------------------------------------------------------- */
  function interactions() {
    $$(".card").forEach((card) => card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${e.clientX - r.left}px`);
      card.style.setProperty("--my", `${e.clientY - r.top}px`);
    }));

    if (!reduced) $$(".magnetic").forEach((b) => {
      b.addEventListener("pointermove", (e) => {
        const r = b.getBoundingClientRect();
        const x = e.clientX - r.left, y = e.clientY - r.top;
        b.style.setProperty("--mx", `${x}px`);
        b.style.setProperty("--my", `${y}px`);
        b.style.transform = `translate(${(x - r.width / 2) * 0.18}px, ${(y - r.height / 2) * 0.3}px)`;
      });
      b.addEventListener("pointerleave", () => { b.style.transform = ""; });
    });

    $$(".copy[data-copy]").forEach((b) => b.addEventListener("click", () => copyText($(b.dataset.copy).textContent, b)));
  }

  function copyText(text, btn) {
    const done = () => {
      const label = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("done");
      burst(btn);
      setTimeout(() => { btn.textContent = label === "Copied" ? "Copy" : label; btn.classList.remove("done"); }, 1600);
    };
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(done, () => {
      btn.textContent = "Select and copy";
    });
  }

  // Plan-coloured confetti from the button that was pressed.
  function burst(el) {
    if (reduced) return;
    const r = el.getBoundingClientRect();
    const colours = ["#3ddc97", "#7aa2ff", "#c58bff", "#f2c14e"];
    for (let i = 0; i < 22; i++) {
      const s = document.createElement("i");
      s.className = "spark";
      s.style.background = colours[i % colours.length];
      s.style.left = `${r.left + r.width / 2}px`;
      s.style.top = `${r.top + r.height / 2}px`;
      document.body.appendChild(s);
      const a = Math.random() * Math.PI * 2, v = 40 + Math.random() * 70;
      s.animate([
        { transform: "translate(0,0) rotate(0)", opacity: 1 },
        { transform: `translate(${Math.cos(a) * v}px, ${Math.sin(a) * v + 30}px) rotate(${Math.random() * 540}deg)`, opacity: 0 },
      ], { duration: 700 + Math.random() * 400, easing: "cubic-bezier(.2,.8,.2,1)" }).onfinish = () => s.remove();
    }
  }

  field();
  scramble();
  reconciler();
  scrubbed();
  terminal();
  lifecycle();
  interactions();
})();
