import type {Workbench} from '../hooks/useWorkbench';
export function Topbar({m, theme, onTheme}:{m:Workbench; theme:string; onTheme:()=>void}) {
  const status=m.status, a=m.activity;
  return <>
    <header className="topbar">
      <div className="brand"><div className="brand-logo" aria-hidden="true">A</div><div>
        <div className="brand-title">AetherRAG <span className="brand-badge">REACT WORKBENCH</span></div>
        <div className="brand-subtitle">Consulta documental · Texto, tablas y contexto visual</div>
      </div></div>
      <div className="topbar-actions">
        <button className={'btn btn-glass '+(status?.index_compatible===false ? '' : 'hidden')} id="btn-recover-index"
          disabled={status?.migration_status==='running'} onClick={()=>void m.recover(false)}>Recuperar índice anterior</button>
        <button className={'btn btn-glass '+(status?.semantic_retrieval===false ? '' : 'hidden')} id="btn-semantic-index"
          disabled={status?.migration_status==='running'} onClick={()=>void m.recover(true)}>Activar búsqueda semántica</button>
        <div className="telemetry-pills">
          <div className="pill pill-activity" id="pill-activity" data-state={a.mode} aria-live="polite">
            <span className="activity-ring" id="activity-ring" aria-hidden="true"/><strong id="label-activity">{a.label}</strong>
            <span id="timer-activity" className={'mono activity-timer '+(a.mode==='idle' ? 'hidden':'')}>{a.elapsed}</span>
          </div>
          <div className="pill" id="pill-provider"><span id="dot-provider" className={'status-dot '+(status?.provider_ready===false ? 'err':'ok')}/>
            <strong id="label-provider">{status ? (status.environment || 'test')+' · v'+(status.app_version || '4.1.0')+' · '+(status.build_id || 'fixture')+' · '+status.llm_model : m.version}</strong></div>
          <div className="pill" id="pill-vector"><strong id="label-vector">{status ? status.vector_backend.split('(')[0].trim()+' ('+status.total_chunks+' chunks)' : 'Vector DB'}</strong></div>
          <div className="pill" id="pill-cb"><span id="dot-cb" className={'status-dot '+(status?.circuit_breaker_state==='closed' ? 'ok':'err')}/>
            <strong id="label-cb">{(status?.circuit_breaker_state || '—').toUpperCase()}</strong></div>
        </div>
        <button id="btn-open-settings" className="btn btn-glass" onClick={()=>m.setSettingsOpen(true)}>Generador IA</button>
        <button id="btn-session" className="btn btn-glass" onClick={()=>m.setAuth(previous=>({...previous,open:true}))}>{m.user ? 'Sesión: '+m.user : 'Iniciar sesión'}</button>
        <button id="btn-logout" className={'btn btn-ghost '+(!m.user ? 'hidden':'')} onClick={()=>void m.logout()}>Cerrar sesión</button>
        <button id="btn-theme" className="btn btn-icon" aria-label={'Cambiar a tema '+(theme==='dark' ? 'claro':'oscuro')} onClick={onTheme}>{theme==='dark' ? '☀':'☾'}</button>
        <a className="btn btn-icon" href="/docs" target="_blank" rel="noopener noreferrer">API Docs</a>
      </div>
    </header>
    <div id="global-activity-bar" className="global-activity-bar" data-state={a.mode} aria-hidden="true"><div className="global-activity-bar-fill"/></div>
  </>;
}
