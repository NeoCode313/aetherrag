import {useEffect, useState} from 'react';
import type {Workbench} from '../hooks/useWorkbench';
import {useChatScroll} from '../hooks/useChatScroll';
import {MessageCard} from './MessageCard';
const prompts=[
  '¿Cuáles son los componentes numerados del corte de la turbina en la Figura 1 y cuál es su potencia y salto nominal en la Tabla 1?',
  '¿Dónde se ubica la sonda RTD ST-104 en el cojinete guía (Figura 2) y a qué temperaturas se activan AL-TEMP-01 y el disparo TRIP-TEMP-02?',
  '¿Qué significa el código de falla E-409 en el acumulador AC-301, qué componentes intervienen en la Figura 3 y cuál es la acción correctiva?',
];
export function ChatPanel({m}:{m:Workbench}) {
  const [input,setInput]=useState(''), [collapsed,setCollapsed]=useState(false);
  const scroll=useChatScroll(m.messages);
  useEffect(()=>{if(m.messages.length) setCollapsed(true);},[m.messages.length]);
  const answers=m.messages.filter(message=>message.kind==='answer');
  function navigate(direction:number) {
    if (!answers.length) return;
    const current=answers.findIndex(answer=>answer.id===m.activeId), next=current<0 ? (direction>0 ? 0:answers.length-1) : Math.max(0,Math.min(answers.length-1,current+direction));
    const answer=answers[next]; m.inspect(answer.id);
    scroll.streamRef.current?.querySelector('[data-message-id="'+answer.id+'"]')?.scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  const submit=(question:string)=>{if(!question.trim() || m.sending) return; setInput('');void m.submit(question);};
  const a=m.activity;
  return <section className="panel panel-center">
    <header className="chat-header"><div><h2 className="panel-title">Consulta documental</h2><p className="panel-subtitle">Respuestas con fuentes, página y contexto visual</p></div>
      <div className="chat-nav"><span id="chat-msg-count" className="badge-count">{answers.length} respuestas</span>
        <button type="button" id="btn-scroll-top" className="btn btn-xs btn-ghost" aria-label="Ir al inicio" onClick={()=>scroll.edge('top')}>↑ Inicio</button>
        <button type="button" id="btn-prev-msg" className="btn btn-xs btn-ghost" aria-label="Respuesta anterior" onClick={()=>navigate(-1)}>←</button>
        <button type="button" id="btn-next-msg" className="btn btn-xs btn-ghost" aria-label="Respuesta siguiente" onClick={()=>navigate(1)}>→</button>
        <button type="button" id="btn-scroll-bottom" className="btn btn-xs btn-ghost" aria-label="Ir al final" onClick={()=>scroll.edge('bottom')}>↓ Final</button></div>
    </header>
    <section id="quick-prompts" className={'quick-prompts '+(collapsed ? 'collapsed':'')}>
      <div className="quick-prompts-header"><span>Preguntas para el manual demo</span><button id="btn-toggle-prompts" type="button" className="btn btn-xs btn-ghost" aria-expanded={!collapsed} onClick={()=>setCollapsed(!collapsed)}>{collapsed ? 'Mostrar sugerencias ▾':'Ocultar sugerencias ▴'}</button></div>
      <div className={'quick-prompts-grid '+(collapsed ? 'hidden':'')}>{prompts.map((question,index)=><button type="button" key={question} className="quick-prompt-btn" disabled={m.sending} onClick={()=>submit(question)}>{index+1}. {question}</button>)}</div>
    </section>
    <div id="chat-stream" className="chat-stream" ref={scroll.streamRef} onScroll={scroll.onScroll} aria-label="Historial de conversación">
      {!m.messages.length && <div className="welcome-card"><span className="eyebrow">AETHERRAG WORKBENCH</span><h3>Pregunta a tus documentos</h3><p>Carga un PDF, selecciona el corpus y formula tu pregunta. Inspecciona cada cita para ver su página y figura asociada.</p></div>}
      {m.messages.map(message=><MessageCard key={message.id} message={message} active={message.id===m.activeId}
        clock={message.kind==='pending' ? a.now : 0} onInspect={m.inspect} onCancel={m.cancelQuery} onRetry={m.submit}/>)}
    </div>
    <button id="btn-jump-latest" type="button" className={'chat-jump-latest '+(!scroll.showJump ? 'hidden':'')} onClick={()=>scroll.edge('bottom')}>↓ Ir al último mensaje</button>
    <section id="live-ops-banner" className={'live-ops-banner '+(a.mode==='idle' ? 'hidden':'')} data-state={a.mode} aria-live="polite">
      <div><strong id="live-ops-title">{a.title}</strong><p id="live-ops-detail">{a.detail}</p></div>
      <span id="live-ops-elapsed" className="mono">{a.elapsed}</span>
      <button id="btn-cancel-ops" type="button" className={'btn btn-xs btn-ghost '+(!['working','warning'].includes(a.mode) ? 'hidden':'')} onClick={m.cancelOperations}>Detener espera</button>
      <button id="btn-dismiss-ops" type="button" className={'btn btn-xs btn-ghost '+(a.mode!=='error' ? 'hidden':'')} onClick={a.dismiss}>Cerrar</button>
    </section>
    <form id="chat-form" className="chat-composer" onSubmit={event=>{event.preventDefault();submit(input);}}>
      <label className="sr-only" htmlFor="chat-input">Pregunta sobre los documentos seleccionados</label>
      <textarea id="chat-input" value={input} maxLength={2500} rows={2} placeholder="Escribe tu pregunta en lenguaje natural…" onChange={event=>setInput(event.target.value)}
        onKeyDown={event=>{if(event.key==='Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {event.preventDefault();event.currentTarget.form?.requestSubmit();}}}/>
      <button id="btn-send" type={m.sending ? 'button':'submit'} className={'btn btn-primary '+(m.sending ? 'is-cancelling':'')} onClick={()=>{if(m.sending) m.cancelQuery();}}>{m.sending ? 'Detener espera (Esc)':'Consultar RAG'}</button>
    </form>
  </section>;
}
