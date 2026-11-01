import {memo, useMemo} from 'react';
import type {ChatMessage} from '../types/api';
import {numberText, safeImageUrl, safeMarkdown} from '../services/sanitizer';

interface Props {message:ChatMessage; active:boolean; clock:number; onInspect:(id:string,ref?:number)=>void; onCancel:()=>void; onRetry:(question:string)=>Promise<void>}
export const MessageCard=memo(function MessageCard({message,active,clock,onInspect,onCancel,onRetry}:Props) {
  const data=message.response, citations=data?.citations || [], meta=data?.metadata || {}, metrics=data?.metrics || {};
  const markdown=useMemo(()=>data ? safeMarkdown(data.answer,citations) : '',[data]);
  if (message.kind==='user') return <div className="msg msg-user">{message.text}</div>;
  if (message.kind==='pending') {
    const seconds=Math.max(0,(clock-(message.started || clock))/1000), mode=seconds>=4 ? 'warning':'working';
    return <article className="msg msg-assistant" data-message-id={message.id} data-ops-state={mode}>
      <div className="thinking-inline" data-state={mode}><div className="thinking-inline-left">
        <span className="thinking-bars" aria-hidden="true"><i/><i/><i/></span>
        <span className="thinking-stage-text">{seconds<1.4 ? 'Recuperando contexto documental…': 'Generando respuesta y comprobando referencias…'}</span>
      </div><div className="thinking-inline-right"><span className="mono thinking-elapsed-pill">{seconds.toFixed(1)}s</span>
        <button type="button" className="thinking-cancel-btn" onClick={onCancel}>Detener espera</button></div></div>
    </article>;
  }
  if (!data) return <article className="msg msg-assistant" data-message-id={message.id}>
    <span className={message.kind==='cancelled' ? 'badge-cancelled':'badge-error-state'}>{message.kind==='cancelled' ? 'ESPERA DETENIDA':'NO SE COMPLETÓ LA CONSULTA'}</span>
    <p>{message.text}</p>{message.kind==='error' && message.question && <button type="button" className="retry-query-btn" onClick={()=>void onRetry(message.question!)}>Reintentar consulta</button>}
  </article>;
  const pages=new Set(citations.map(c=>c.document_id+':'+c.page)), docs=new Set(citations.map(c=>c.document_id));
  const unique=new Set(citations.map(c=>c.chunk_id));
  const counts={text:0,table:0,visual:0};
  citations.forEach(c=>{const mode=c.modality || c.kind; if (mode==='visual' || mode==='figure') counts.visual++; else if(mode==='table') counts.table++; else counts.text++;});
  const cells=[
    ['AFIRMACIONES CON REFERENCIA',numberText(meta.accepted_claims,0)+' aceptadas · '+numberText(meta.rejected_claims,0)+' descartadas','Controles estructurales; no son una prueba de verdad.'],
    ['CITAS Y FRAGMENTOS',citations.length+' citas · '+unique.size+' chunks únicos','Referencias explícitas de esta respuesta.'],
    ['COBERTURA DOCUMENTAL',pages.size+' págs · '+docs.size+' docs','Fuentes citadas; no mide exhaustividad.'],
    ['MODALIDADES CITADAS','Texto '+counts.text+' · Tablas '+counts.table+' · Figuras '+counts.visual,'Las leyendas extraídas cuentan como texto.'],
    ['SIMILITUD COSENO',numberText(metrics.max_cosine_similarity),'Similitud densa de recuperación.'],
    ['FUSIÓN HÍBRIDA RRF',numberText(metrics.top_rrf_score,5)+' · Léxico '+numberText(metrics.lexical_score),'Puntuaciones de recuperación.'],
    ['LATENCIA DEL PIPELINE',data.latency_ms+' ms','R: '+(data.timings_ms?.retrieval_ms ?? 'N/A')+' ms · G: '+(data.timings_ms?.generation_ms ?? 'N/A')+' ms'],
    ['TRAZABILIDAD DEL MOTOR',data.model,data.answer_mode],
  ];
  const image=safeImageUrl(data.primary_visual?.image_url);
  return <article className={'msg msg-assistant '+(active ? 'active-inspected':'')} data-message-id={message.id}>
    <header className="assistant-header"><div><span className={data.abstained ? 'badge-abstained':'badge-grounded'}>{data.abstained ? 'ABSTENCIÓN' : data.answer_mode==='extractive_evidence' ? 'EVIDENCIA EXTRACTIVA · SIN INFERENCIA VISUAL':'RESPUESTA CON REFERENCIAS'}</span>
      <span className="mono msg-meta-summary">{data.model} · {data.latency_ms} ms · cobertura declarada: {data.evidence_coverage}</span></div>
      <button className="inspect-btn" type="button" onClick={()=>onInspect(message.id)}>Inspeccionar →</button></header>
    <div className="md-body" dangerouslySetInnerHTML={{__html:markdown}} onClick={event=>{
      const button=(event.target as HTMLElement).closest<HTMLButtonElement>('button.inline-cite-btn');
      if(button && event.currentTarget.contains(button)) onInspect(message.id,Number(button.dataset.ref));
    }}/>
    {data.abstained && <details className="abstention-details"><summary>Ver el motivo y los fragmentos recuperados</summary>
      <p>Estos extractos muestran lo recuperado; no son una respuesta validada.</p><p>{meta.provider_reason}</p>
      {meta.retrieved_sources?.map((source,index)=><div key={index}><strong>{source.document} · página {source.page}</strong><p>{source.snippet}</p></div>)}
    </details>}
    {!data.abstained && image && <figure className="message-visual"><button type="button" onClick={()=>onInspect(message.id)}><img src={image} alt={data.primary_visual?.caption || 'Figura relacionada con la respuesta'}/></button>
      <figcaption>{data.primary_visual?.document_name} · página {data.primary_visual?.page} · {meta.sent_asset_ids?.includes(data.primary_visual!.asset_id) ? 'Imagen enviada al modelo':'Figura relacionada; no enviada al modelo'}</figcaption></figure>}
    <div className="verification-telemetry" aria-label="Diagnóstico de recuperación y referencias">{cells.map(([label,value,detail],index)=><div className="telemetry-cell" key={label}>
      <span>{label}</span><strong>{value}</strong>{index===4 && <meter min={0} max={1} value={Math.max(0,Math.min(1,metrics.max_cosine_similarity || 0))} aria-label="Similitud densa"/>}<small>{detail}</small>
    </div>)}</div>
    <details className="audit-details"><summary>Diagnóstico detallado de recuperación y auditoría del modelo (no mide veracidad)</summary>
      <p className="mono">Coseno: {numberText(metrics.max_cosine_similarity)} · Léxico: {numberText(metrics.lexical_score)} · RRF: {numberText(metrics.top_rrf_score,5)}</p>
      <p className="mono">Checks: {meta.checks || 'references_spans_numeric_consistency'} · Perfil Emb: {meta.embedding_profile_id || 'activo'} · Gen: {meta.generation_id || 'activa'}</p>
      <p>Los identificadores y las comprobaciones numéricas permiten auditar la respuesta; la correspondencia semántica requiere revisión.</p>
    </details>
  </article>;
});
