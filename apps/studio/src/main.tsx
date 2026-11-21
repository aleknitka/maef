import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Budget = { max_cost_usd: number; max_duration_seconds: number; max_turns: number };
type Agent = { id: string; version: string; display_name: string; description: string; capabilities: string[]; skills: string[]; mcp_servers: string[]; runtime: { kind: string; model_profile: string; sandbox_profile: string }; budget: Budget; outputs: string[] };
type AgentFile = { path: string; content: string };

function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [files, setFiles] = useState<AgentFile[]>([]);
  const [instruction, setInstruction] = useState('Create a 12-second introduction animation using an existing project logo.');
  const [preview, setPreview] = useState<string>('');
  const [error, setError] = useState<string>('');

  useEffect(() => { fetch('/api/v1/agents').then(r => { if (!r.ok) throw new Error(`API returned ${r.status}`); return r.json(); }).then((data: Agent[]) => { setAgents(data); if (data.length) setSelected(data[0].id); }).catch(e => setError(String(e))); }, []);
  useEffect(() => { if (selected) fetch(`/api/v1/agents/${selected}/files`).then(r => r.json()).then(setFiles).catch(e => setError(String(e))); }, [selected]);
  const agent = agents.find(a => a.id === selected);
  async function createPreview() {
    if (!agent) return;
    setError(''); setPreview('');
    try {
      const response = await fetch('/api/v1/runs/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent_id: agent.id, instruction }) });
      const result = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(result));
      setPreview(JSON.stringify(result, null, 2));
    } catch (e) { setError(String(e)); }
  }
  return <div className="shell">
    <aside className="sidebar"><div className="brand">MAEF <span>STUDIO</span></div><div className="caption">MODULAR AGENT EXECUTION FRAMEWORK</div><div className="navTitle">AGENT REGISTRY</div>{agents.map(a => <button key={a.id} className={'agentItem ' + (selected === a.id ? 'active' : '')} onClick={() => { setSelected(a.id); setPreview(''); }}><span>{a.display_name}</span><small>{a.id}</small></button>)}<div className="sidebarFoot">Bootstrap v0.1 · Local mode</div></aside>
    <main><header><div><div className="eyebrow">CONTROL PLANE / AGENTS</div><h1>Agent workspace</h1><p>Review Git-managed agent definitions, their tools and execution constraints.</p></div><div className="status">● EXECUTION DISABLED</div></header>
    {error && <div className="error">{error}</div>}
    {agent ? <><section className="hero"><div><div className="eyebrow">SPECIALIST / v{agent.version}</div><h2>{agent.display_name}</h2><p>{agent.description}</p><div className="chips">{agent.capabilities.map(c => <span key={c}>{c}</span>)}</div></div><div className="id">{agent.id}</div></section>
    <div className="grid"><section className="card"><h3>Execution profile</h3><div className="row"><span>Runtime</span><strong>{agent.runtime.kind}</strong></div><div className="row"><span>Model</span><strong>{agent.runtime.model_profile}</strong></div><div className="row"><span>Sandbox</span><strong>{agent.runtime.sandbox_profile}</strong></div><div className="row"><span>Maximum cost</span><strong>${agent.budget.max_cost_usd.toFixed(2)}</strong></div><div className="row"><span>Time limit</span><strong>{agent.budget.max_duration_seconds}s</strong></div><div className="row"><span>Maximum turns</span><strong>{agent.budget.max_turns}</strong></div></section>
    <section className="card"><h3>Allocated capabilities</h3><div className="metaLabel">SKILLS</div>{agent.skills.length ? agent.skills.map(s => <div className="pill" key={s}>{s}</div>) : <p className="muted">No skills attached</p>}<div className="metaLabel">MCP SERVERS</div>{agent.mcp_servers.length ? agent.mcp_servers.map(s => <div className="pill" key={s}>{s}</div>) : <p className="muted">No MCP servers attached</p>}<div className="metaLabel">EXPECTED OUTPUTS</div><div className="chips">{agent.outputs.map(s => <span key={s}>{s}</span>)}</div></section></div>
    <section className="card"><h3>Source files <em>read-only</em></h3>{files.map(f => <details key={f.path}><summary>{f.path}</summary><pre>{f.content}</pre></details>)}</section>
    <section className="card"><h3>Task preview <em>no execution</em></h3><p className="muted">Validate delegation parameters against agent defaults. This does not call a model or launch a sandbox.</p><textarea value={instruction} onChange={e => setInstruction(e.target.value)} /><button className="primary" disabled={!instruction.trim()} onClick={createPreview}>Generate execution preview</button>{preview && <pre className="preview">{preview}</pre>}</section></> : <section className="card">No agents found. Check the API connection.</section>}
    </main></div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
