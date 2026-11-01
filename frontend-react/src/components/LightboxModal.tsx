import {useEffect, useState} from 'react';
import type {Workbench} from '../hooks/useWorkbench';
export function LightboxModal({m}:{m:Workbench}) {
  const [zoom,setZoom]=useState(1);
  useEffect(()=>setZoom(1),[m.lightbox]);
  return <div id="lightbox-modal" className={'modal-backdrop lightbox '+(!m.lightbox ? 'hidden':'')} onClick={event=>{if(event.target===event.currentTarget) m.setLightbox(null);}}>
    <section className="lightbox-content" role="dialog" aria-modal="true" aria-label="Vista ampliada">
      <button id="btn-close-lightbox" className="btn btn-icon lightbox-close" onClick={()=>m.setLightbox(null)}>✕ Cerrar</button>
      <div className="lightbox-tools"><button className="btn btn-xs btn-glass" onClick={()=>setZoom(value=>Math.max(.5,value-.25))}>−</button>
        <button className="btn btn-xs btn-glass" onClick={()=>setZoom(1)}>Restablecer</button><button className="btn btn-xs btn-glass" onClick={()=>setZoom(value=>Math.min(3,value+.25))}>+</button></div>
      <div className="lightbox-image-stage"><img id="lightbox-img" src={m.lightbox?.src} style={{transform:'scale('+zoom+')'}} alt={m.lightbox?.caption || 'Diagrama ampliado'}/></div>
      <p id="lightbox-caption" className="lightbox-caption">{m.lightbox?.caption}</p>
    </section>
  </div>;
}
