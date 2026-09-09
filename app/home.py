from __future__ import annotations

from app import __version__


_VERSION_TOKEN = "__APP_VERSION__"

_HOME_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Production-oriented reference implementation for reliable enterprise AI agents.">
  <title>Enterprise Agent Reliability Lab</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a0e14;
      --panel: #111823;
      --panel-strong: #172130;
      --line: #2a3748;
      --text: #edf2f7;
      --muted: #a8b3c2;
      --accent: #75d5c7;
      --accent-strong: #a7f3d0;
      --code: #0d141e;
      --max: 1120px;
    }

    * { box-sizing: border-box; }

    html { scroll-behavior: smooth; }

    body {
      margin: 0;
      background:
        radial-gradient(circle at 80% 0%, rgba(56, 189, 172, 0.10), transparent 32rem),
        var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }

    a { color: inherit; }

    .shell {
      width: min(var(--max), calc(100% - 2.5rem));
      margin-inline: auto;
    }

    header {
      padding: 4.75rem 0 3.5rem;
      border-bottom: 1px solid var(--line);
    }

    .eyebrow {
      margin: 0 0 1rem;
      color: var(--accent);
      font: 700 0.78rem/1.2 ui-monospace, SFMono-Regular, Consolas, monospace;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    h1, h2, h3, p { margin-top: 0; }

    h1 {
      max-width: 850px;
      margin-bottom: 1.1rem;
      font-size: clamp(2.35rem, 6vw, 4.75rem);
      line-height: 1.03;
      letter-spacing: -0.045em;
    }

    h2 {
      margin-bottom: 0.6rem;
      font-size: clamp(1.55rem, 3vw, 2.2rem);
      letter-spacing: -0.025em;
    }

    h3 { margin-bottom: 0.45rem; font-size: 1rem; }

    .lede {
      max-width: 780px;
      margin-bottom: 1.8rem;
      color: var(--muted);
      font-size: clamp(1.05rem, 2vw, 1.25rem);
    }

    .actions { display: flex; flex-wrap: wrap; gap: 0.7rem; }

    .button {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      padding: 0.65rem 1rem;
      border: 1px solid var(--line);
      border-radius: 0.5rem;
      background: var(--panel);
      text-decoration: none;
      font-weight: 650;
    }

    .button:hover, .button:focus-visible {
      border-color: var(--accent);
      outline: none;
    }

    .button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #07110f;
    }

    main { padding: 1rem 0 4rem; }

    section { padding: 3.25rem 0; border-bottom: 1px solid var(--line); }

    .section-intro { max-width: 760px; color: var(--muted); }

    .flow {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 0.75rem;
      margin: 1.6rem 0 0;
      padding: 0;
      list-style: none;
      counter-reset: flow;
    }

    .flow li {
      position: relative;
      min-height: 7rem;
      padding: 1rem;
      border: 1px solid var(--line);
      border-radius: 0.65rem;
      background: var(--code);
      font: 600 0.9rem/1.45 ui-monospace, SFMono-Regular, Consolas, monospace;
      counter-increment: flow;
    }

    .flow li::before {
      display: block;
      margin-bottom: 0.75rem;
      color: var(--accent);
      content: "0" counter(flow);
      font-size: 0.75rem;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.8rem;
      margin-top: 1.6rem;
    }

    .card {
      padding: 1.2rem;
      border: 1px solid var(--line);
      border-radius: 0.65rem;
      background: var(--panel);
    }

    .card p { margin-bottom: 0; color: var(--muted); font-size: 0.94rem; }

    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.9rem;
      margin-top: 1.5rem;
    }

    .checklist { margin: 0; padding-left: 1.2rem; color: var(--muted); }
    .checklist li + li { margin-top: 0.45rem; }

    .release {
      display: inline-block;
      margin-bottom: 1rem;
      padding: 0.2rem 0.55rem;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--accent-strong);
      font: 600 0.78rem/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
    }

    footer { padding: 2rem 0 3rem; color: var(--muted); font-size: 0.9rem; }

    @media (max-width: 820px) {
      header { padding-top: 3.2rem; }
      .flow, .grid { grid-template-columns: repeat(2, 1fr); }
    }

    @media (max-width: 560px) {
      .shell { width: min(100% - 1.4rem, var(--max)); }
      header { padding: 2.5rem 0; }
      section { padding: 2.4rem 0; }
      .flow, .grid, .split { grid-template-columns: 1fr; }
      .flow li { min-height: 0; }
      .button { width: 100%; justify-content: center; }
    }
  </style>
</head>
<body>
  <header>
    <div class="shell">
      <p class="eyebrow">Reference system / after-sales operations</p>
      <h1>Enterprise Agent Reliability Lab</h1>
      <p class="lede">Production-oriented reference implementation for building, evaluating, and operating reliable enterprise AI agents across planning, memory, tool protocols, failure controls, traces, and regression evaluation.</p>
      <span class="release">v__APP_VERSION__</span>
      <nav class="actions" aria-label="Project links">
        <a class="button primary" href="/docs">API Docs</a>
        <a class="button" href="/redoc">ReDoc</a>
        <a class="button" href="/health">Health</a>
        <a class="button" href="/openapi.json">OpenAPI</a>
        <a class="button" href="https://github.com/qwsvd/enterprise-agent-reliability-lab">GitHub</a>
      </nav>
    </div>
  </header>

  <main class="shell">
    <section aria-labelledby="architecture-title">
      <p class="eyebrow">Implemented execution path</p>
      <h2 id="architecture-title">Architecture</h2>
      <p class="section-intro">The project keeps its bounded Single-Agent reference path and adds a real LangGraph multi-agent path. Planner, dependency scheduler, Executor, and evidence Reviewer collaborate through typed working state while the service layer remains authoritative.</p>
      <ol class="flow">
        <li>User goal<br>episodic memory<br>Planner Agent</li>
        <li>TaskPlan DAG<br>dependency scheduler<br>ready task</li>
        <li>Executor Agent<br>Agent Skill<br>MCP client/server</li>
        <li>Reviewer Agent<br>reflection<br>bounded replan</li>
        <li>Service + SQLite<br>OpenTelemetry<br>eval gate</li>
      </ol>
    </section>

    <section aria-labelledby="capabilities-title">
      <p class="eyebrow">Concrete engineering boundaries</p>
      <h2 id="capabilities-title">Implemented capabilities</h2>
      <div class="grid">
        <article class="card"><h3>Enterprise backend</h3><p>FastAPI, typed Pydantic contracts, SQLAlchemy persistence, SQLite demo state, refund idempotency, and human-approval semantics.</p></article>
        <article class="card"><h3>Agent runtime</h3><p>Provider-neutral <code>AgentRuntime</code> tool-calling loop with validated arguments, structured errors, bounded steps, and an offline ScriptedProvider.</p></article>
        <article class="card"><h3>LangGraph orchestration</h3><p>Typed Planner, dependency-aware Scheduler, Executor, and Reviewer nodes with conditional handoffs, reflection, and bounded replanning.</p></article>
        <article class="card"><h3>Working + episodic memory</h3><p>Explicit run state plus persistent SQLite episodes with bounded deterministic retrieval that informs later planning context.</p></article>
        <article class="card"><h3>MCP integration</h3><p>Official-SDK MCP server and client with dynamic tools/list discovery and tools/call execution over in-process or stdio transport.</p></article>
        <article class="card"><h3>Agent Skills</h3><p>Repository SKILL.md workflows, metadata-first discovery, progressive disclosure, and explicit on-demand loading.</p></article>
        <article class="card"><h3>OpenTelemetry tracing</h3><p>Vendor-neutral run, step, model, retry, Skill, MCP, and tool spans with payload-safe operational attributes.</p></article>
        <article class="card"><h3>External benchmarks</h3><p>Provider-neutral contracts and a tau3-bench adapter that keeps external execution and imported results distinct from local evals.</p></article>
      </div>
    </section>

    <section aria-labelledby="reliability-title">
      <p class="eyebrow">Bounded failure behavior</p>
      <h2 id="reliability-title">Reliability engineering</h2>
      <div class="split">
        <article class="card">
          <h3>Execution controls</h3>
          <ul class="checklist">
            <li>Model, tool-call, and Agent-step budgets</li>
            <li>Bounded retries and typed timeouts</li>
            <li>Repeated-call and consecutive-failure protection</li>
            <li>Bounded orchestration, reflection, and replanning</li>
            <li>Explicit terminal failure reasons</li>
          </ul>
        </article>
        <article class="card">
          <h3>Side-effect safety</h3>
          <ul class="checklist">
            <li>Idempotent refund replay is preserved</li>
            <li>Non-idempotent ticket replay is blocked</li>
            <li>Business rejection is not infrastructure failure</li>
            <li>Pending approval is never reported as completed</li>
          </ul>
        </article>
      </div>
    </section>

    <section aria-labelledby="evaluation-title">
      <p class="eyebrow">Deterministic verification</p>
      <h2 id="evaluation-title">Agent evaluation</h2>
      <p class="section-intro">Versioned, offline end-to-end cases exercise both Single-Agent and Multi-Agent paths, Skills, MCP, service state, reliability, memory, reflection, replanning, and tracing. Typed metrics check plans, dependencies, final outcomes, tool execution, persisted side effects, human approval, termination reasons, and unsupported claims. Regression gates fail when expected behavior degrades.</p>
    </section>

    <section aria-labelledby="delivery-title">
      <p class="eyebrow">Repeatable delivery</p>
      <h2 id="delivery-title">Deployment and CI</h2>
      <div class="split">
        <article class="card"><h3>Docker</h3><p>Multi-stage Python image, non-root runtime, writable SQLite boundary under /data, environment-aware port binding, and a live health check.</p></article>
        <article class="card"><h3>GitHub Actions</h3><p>Pull-request and main-branch validation across supported Python versions, full pytest, dependency and package checks, then a production image build.</p></article>
      </div>
    </section>

    <section aria-labelledby="demo-title">
      <p class="eyebrow">No-key execution proof</p>
      <h2 id="demo-title">Run the deterministic multi-agent demo</h2>
      <p class="section-intro">Install the repository locally and run <code>after-sales-multi-agent-demo --format json</code>. The command executes the LangGraph plan, Skill loading, MCP business tools, memory retrieval, review, tracing, and a measured Single-Agent comparison without an API key or production side effects.</p>
    </section>

    <section aria-labelledby="links-title">
      <p class="eyebrow">Inspect the running system</p>
      <h2 id="links-title">Links</h2>
      <nav class="actions" aria-label="API and source links">
        <a class="button primary" href="/docs">API Docs</a>
        <a class="button" href="/redoc">ReDoc</a>
        <a class="button" href="/health">Health</a>
        <a class="button" href="/openapi.json">OpenAPI</a>
        <a class="button" href="https://github.com/qwsvd/enterprise-agent-reliability-lab">Source on GitHub</a>
      </nav>
    </section>
  </main>

  <footer class="shell">
    <p>Enterprise Agent Reliability Lab v__APP_VERSION__ · Single-Agent and LangGraph multi-agent reference paths · No JavaScript or external presentation assets.</p>
  </footer>
</body>
</html>
"""


def render_homepage() -> str:
    return _HOME_PAGE.replace(_VERSION_TOKEN, __version__)
