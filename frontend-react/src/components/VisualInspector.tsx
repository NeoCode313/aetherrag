import type {Workbench} from '../hooks/useWorkbench';
import type {BBox, VisualAsset} from '../types/api';
import {boxText, numberText, safeImageUrl} from '../services/sanitizer';

function Rectangle({box,color}:{box?:BBox; color:string}) {
  if (!box || box.length!==4 || !box.every(Number.isFinite) || box[0]>=box[2] || box[1]>=box[3]) return null;
  return <rect x={box[0]*1000} y={box[1]*1000} width={(box[2]-box[0])*1000} height={(box[3]-box[1])*1000}
    fill={color} fillOpacity={.12} stroke={color} strokeWidth={3}/>;
}
export function VisualInspector({m}:{m:Workbench}) {
  const data=m.activeResponse, citations=data?.citations || [];
  const visuals=[data?.primary_visual,...(data?.related_visuals || [])].filter((value):value is VisualAsset=>Boolean(value));
  const related=visuals.filter((value,index)=>visuals.findIndex(other=>other.asset_id===value.asset_id)===index);
  const citation=citations.find(c=>c.ref===m.activeRef) || citations[0];
  const linked=m.activeRef ? related.find(visual=>visual.anchor_chunk_id===citation?.chunk_id) : data?.primary_visual;
  const synthetic:VisualAsset | null=citation ? {asset_id:'page-'+citation.chunk_id,document_id:citation.document_id,
    document_name:citation.document_name,page:citation.page,caption:'Región documental citada',image_url:citation.page_image_url,
    page_image_url:citation.page_image_url,bbox:citation.bbox,anchor_chunk_id:citation.chunk_id,distance:null,reason:'source_page'} : null;
  const visual=m.visual || linked || synthetic;
  const anchor=citations.find(c=>c.chunk_id===visual?.anchor_chunk_id);
  const samePage=anchor ? Boolean(safeImageUrl(anchor.page_image_url) &&
    safeImageUrl(anchor.page_image_url)===safeImageUrl(visual?.page_image_url)) : visual?.distance!=null;
  const anchorBox=samePage && visual?.distance!=null ? visual.anchor_bbox : undefined;
  const sent=Boolean(visual && data?.metadata?.sent_asset_ids?.includes(visual.asset_id));
  const crop=safeImageUrl(visual?.image_url), page=safeImageUrl(visual?.page_image_url);
  const open=()=>{const src=m.view==='crop' ? crop : page;if(src) m.setLightbox({src,caption:visual?.caption || 'Página citada'});};
  return <aside className="panel panel-right">
    <div className="panel-header"><div><h2 className="panel-title">Contexto visual</h2><p className="panel-subtitle">Fuente, página y coordenadas espaciales</p></div>
      <div className="inspector-header-actions"><div id="visual-view-tabs" className="view-tabs">
        <button id="tab-crop" type="button" className={'tab-btn '+(m.view==='crop' ? 'active':'')} onClick={()=>m.setView('crop')}>Diagrama</button>
        <button id="tab-page" type="button" className={'tab-btn '+(m.view==='page' ? 'active':'')} onClick={()=>m.setView('page')}>Mapeo BBox</button></div>
        <button type="button" className="btn-clear-inspector" id="btn-clear-inspector" onClick={m.clearInspector}>Limpiar</button></div>
    </div>
    <div className="visual-stage" id="visual-stage">
      <div id="visual-empty" className={'visual-empty '+(visual ? 'hidden':'')}><h4>Inspector espacial</h4><p>Selecciona una cita para inspeccionar el documento, su página y las figuras relacionadas.</p></div>
      <div id="visual-content" className={'visual-content '+(!visual ? 'hidden':'')}>
        <div id="view-crop-wrap" className={'canvas-wrap '+(m.view!=='crop' ? 'hidden':'')}>
          <img id="visual-crop-img" src={crop} alt={visual?.caption || 'Figura o página de la fuente'}/>
          <button type="button" id="btn-zoom-image" className="zoom-btn" disabled={!crop} onClick={open}>Ampliar HD</button>
        </div>
        <div id="view-page-wrap" className={'canvas-wrap '+(m.view!=='page' ? 'hidden':'')}>
          <div className="page-overlay-container" id="page-overlay-container"><img id="visual-page-img" src={page} alt="Página del PDF con regiones de contexto"/>
            <svg className="bbox-svg" id="bbox-svg" viewBox="0 0 1000 1000" preserveAspectRatio="none">
              <Rectangle box={visual?.bbox} color="#10b981"/><Rectangle box={anchorBox} color="#06b6d4"/>
            </svg></div>
          <div className="bbox-legend"><span>Región fuente: <code id="legend-fig-bbox">{boxText(visual?.bbox)}</code></span><span>Texto: <code id="legend-txt-bbox">{boxText(anchorBox)}</code></span></div>
        </div>
        <div className="visual-meta-card"><div className="visual-meta-header">
          <span id="vis-source-pill" className="source-pill">{visual ? visual.document_name+', página '+visual.page : ''}</span>
          <span id="vis-distance-pill" className="distance-pill mono">d_bbox = {samePage ? numberText(visual?.distance) : 'N/A'} ({visual?.reason})</span></div>
          <p id="vis-caption" className="visual-caption">{visual?.caption} {sent ? '(enviada al modelo)':'(no enviada al modelo)'}</p>
          <p id="vis-coords" className="visual-coords mono">BBox: {boxText(visual?.bbox)} · Texto: {boxText(anchorBox)}</p>
        </div>
        <div id="related-strip" className="related-strip">{related.map(item=><button type="button" key={item.asset_id}
          className={'related-thumb '+(item.asset_id===visual?.asset_id ? 'active':'')} title={item.caption} onClick={()=>m.setVisual(item)}>
          <img src={safeImageUrl(item.image_url)} alt={item.caption || 'Figura relacionada'}/></button>)}</div>
      </div>
    </div>
    <div className="section-divider section-divider-row"><span>METADATOS Y TRAZABILIDAD</span><span id="citations-count-badge" className="badge-count">{citations.length} citas</span>
      <button id="btn-clear-citations" className={'btn-clear-citations '+(!citations.length ? 'hidden':'')} onClick={m.clearInspector}>Limpiar citas</button></div>
    <div className="citations-list" id="citations-list">{!citations.length && <p className="citations-placeholder">Aquí aparecerán las fuentes y páginas de la respuesta seleccionada.</p>}
      {citations.map(source=><button type="button" key={source.ref} data-ref={source.ref} className={'citation-card '+(m.activeRef===source.ref ? 'active':'')} onClick={()=>{if(m.activeId) m.inspect(m.activeId,source.ref);}}>
        <div className="citation-top"><strong className="citation-source">[{source.ref}] {source.document_name}, página {source.page}</strong>
          <span className="citation-score mono">Cos={numberText(source.dense_score)} · Léxico={numberText(source.sparse_score)} · RRF={numberText(source.score,5)}</span></div>
        <p className="citation-section">{source.section} ({source.kind})</p><p className="citation-snippet">{source.snippet}</p><span className="visual-coords mono">BBox: {boxText(source.bbox)}</span>
      </button>)}
    </div>
  </aside>;
}
