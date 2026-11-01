import {useRef, useState} from 'react';
import type {Workbench} from '../hooks/useWorkbench';
export function IngestionSidebar({m}:{m:Workbench}) {
  const file=useRef<HTMLInputElement>(null), [dragging,setDragging]=useState(false);
  const job=m.job, progress=Math.max(0,Math.min(100,Number(job?.progress) || 0));
  return <aside className="panel panel-left">
    <div className="panel-header"><div><h2 className="panel-title">Ingesta Asíncrona</h2><p className="panel-subtitle">Texto, tablas, figuras y coordenadas</p></div>
      <span id="doc-count-badge" className="badge-count">{m.documents.length} PDFs</span></div>
    <button type="button" className={'dropzone '+(dragging ? 'dragging':'')} id="dropzone" onClick={()=>file.current?.click()}
      onDragOver={event=>{event.preventDefault();setDragging(true);}} onDragLeave={()=>setDragging(false)}
      onDrop={event=>{event.preventDefault();setDragging(false);if (event.dataTransfer.files[0]) void m.ingest(event.dataTransfer.files[0]);}}>
      <span className="dropzone-title">Subir un manual PDF</span><span className="dropzone-hint">Arrastra el archivo o haz clic · Ingesta en segundo plano</span>
    </button>
    <input id="file-input" ref={file} hidden type="file" accept=".pdf,application/pdf" onChange={event=>{
      const selected=event.currentTarget.files?.[0]; event.currentTarget.value=''; if (selected) void m.ingest(selected);
    }}/>
    <button id="btn-seed-demo" className="btn btn-secondary btn-full" onClick={()=>void m.ingest()}>Cargar manual de ejemplo</button>
    <section id="job-tracker" className={'job-tracker '+(!job ? 'hidden':'')} aria-live="polite">
      <div className="job-tracker-header"><strong id="job-filename">{job?.file_name}</strong><span id="job-status-pill" className={'job-status-pill '+job?.status}>{job?.status}</span></div>
      <p id="job-id-label" className="mono">{job?.job_id}</p>
      <div className="progress-track"><div id="job-progress-fill" className="progress-fill" style={{width:progress+'%'}}/></div>
      <div className="job-progress-meta"><span id="job-stage-message">{job?.error || job?.message}</span><strong id="job-progress-pct">{Math.round(progress)}%</strong></div>
    </section>
    <div className="section-divider"><span>CORPUS DOCUMENTAL INDEXADO</span></div>
    <p className="panel-subtitle corpus-hint">Marca los PDFs que quieres consultar. Sin selección se consulta todo el corpus.</p>
    <div className="doc-list" id="doc-list">
      {!m.documents.length && <p className="citations-placeholder">Carga un PDF propio o el manual de ejemplo.</p>}
      {m.documents.map(doc=><article className="doc-card" key={doc.document_id}>
        <div className="doc-card-top"><input type="checkbox" aria-label={'Consultar '+doc.file_name} checked={m.selected.has(doc.document_id)}
          onChange={event=>m.selectDocument(doc.document_id,event.target.checked)}/>
          <div className="doc-name" title={doc.file_name}>{doc.file_name}</div>
          <button className="doc-delete-btn" title="Eliminar documento" onClick={()=>void m.deleteDocument(doc.document_id)}>✕</button></div>
        <div className="doc-metrics">{[[doc.page_count,'Páginas'],[doc.chunk_count,'Chunks'],[doc.visual_count,'Figuras'],[doc.table_count,'Fragmentos de tabla']].map(([value,label])=>
          <div className="doc-metric" key={label}><strong>{value}</strong><span>{label}</span></div>)}</div>
        <div className="doc-hash mono"><span>SHA256: {doc.sha256?.slice(0,10)}…</span><span>{doc.status}</span></div>
      </article>)}
    </div>
    <footer className="arch-card"><strong>Contexto y procedencia</strong><p>El generador y el índice se configuran por separado. Cada respuesta conserva sus citas y figuras.</p></footer>
  </aside>;
}
