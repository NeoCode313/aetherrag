import {useEffect, useState} from 'react';
import type {FormEvent} from 'react';
import type {Workbench} from '../hooks/useWorkbench';
import type {Provider, ProviderPayload} from '../types/api';
import {errorMessage} from '../services/apiClient';
const names:Record<Provider,string>={local:'Extractivo local',openai:'OpenAI / Groq / Compatible',ollama:'Ollama',gemini:'Gemini',anthropic:'Claude / Anthropic'};
export function ProviderModal({m}:{m:Workbench}) {
  const [provider,setProvider]=useState<Provider>('local'), [models,setModels]=useState<Record<string,string>>({});
  const [tokens,setTokens]=useState<Record<string,string>>({}), [endpoint,setEndpoint]=useState('https://api.groq.com/openai/v1');
  const [ollama,setOllama]=useState('http://localhost:11434'), [vision,setVision]=useState(false), [format,setFormat]=useState('auto'), [approve,setApprove]=useState(false);
  const [message,setMessage]=useState(''), [pending,setPending]=useState(false);
  const status=m.status, editable=status?.credentials_editable===true;
  useEffect(()=>{
    if (!m.settingsOpen) {setTokens({}); return;}
    const choice=status?.llm_provider || 'local';
    setProvider(choice); setTokens({});setMessage('');setApprove(false);
    setModels({openai:'qwen/qwen3.8-27b',gemini:'',ollama:'',anthropic:'',
      [choice]:(status?.llm_model || '').replace(/^(groq|openai|gemini|ollama|anthropic)\//,'')});
    setEndpoint(status?.openai_base_url || 'https://api.groq.com/openai/v1');
    setOllama(status?.ollama_base_url || 'http://localhost:11434');
    setVision(status?.supports_images===true);setFormat(status?.response_format || 'auto');
  }, [m.settingsOpen,status?.llm_provider,status?.llm_model,status?.openai_base_url,status?.ollama_base_url,status?.supports_images,status?.response_format]);
  async function submit(event:FormEvent) {
    event.preventDefault();setPending(true);setMessage('');
    const payload:ProviderPayload={llm_provider:provider};
    if (provider!=='local') {
      payload[provider+'_model']=(models[provider] || '').trim();
      payload.llm_supports_images=vision;payload.openai_response_format=format;payload.approve_endpoint=approve;
      if (editable) {
        if (tokens[provider]?.trim()) payload[provider+'_api_key']=tokens[provider].trim();
        if (provider==='openai') payload.openai_base_url=endpoint.trim();
        if (provider==='ollama') payload.ollama_base_url=ollama.trim();
      }
    }
    setTokens({});
    try {await m.saveProvider(payload);} catch (error) {setMessage(errorMessage(error));} finally {setPending(false);}
  }
  const preset=(url:string,model:string)=>{setEndpoint(url);setModels(previous=>({...previous,openai:model}));setTokens({});};
  const close=()=>m.setSettingsOpen(false);
  return <div id="settings-modal" className={'modal-backdrop '+(!m.settingsOpen ? 'hidden':'')} onClick={event=>{if(event.target===event.currentTarget) close();}}>
    <section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <header className="modal-header"><h3 id="settings-title">Generador de respuestas</h3><button id="btn-close-settings" className="btn btn-icon" aria-label="Cerrar configuración" onClick={close}>✕</button></header>
      <form id="provider-form" className="modal-body" onSubmit={event=>void submit(event)}>
        <div className="config-note">
          <p id="embedding-model-note">Embeddings fijos: {status?.embedding_model || 'consultando'}. Cambiar el generador no modifica el índice.</p>
          <p id="capability-note">{provider==='local' ? 'El modo extractivo devuelve citas literales. Configura un generador para redactar explicaciones.' : vision ? 'Visión habilitada: utiliza solo un modelo que admita imágenes.' : 'El modelo utiliza texto, tablas y leyendas. Las preguntas sobre píxeles requieren visión.'}</p>
          <p id="provider-config-note">{editable ? 'Claves privadas de esta instalación. No se guardan en el navegador ni en Git.' : 'Credenciales y destinos administrados por el servidor. Aquí puedes elegir el modelo.'}</p>
        </div>
        <div className="provider-selector">{(Object.keys(names) as Provider[]).map(value=><label key={value} className="provider-option">
          <input type="radio" name="llm_provider" value={value} checked={provider===value} onChange={()=>{setProvider(value);setTokens({});}}/><span>{names[value]}</span></label>)}</div>
        {(['gemini','ollama','openai','anthropic'] as const).map(value=><div key={value} id={'fields-'+value} className={'provider-fields '+(provider!==value ? 'hidden':'')}>
          {value==='openai' && <div className="preset-actions">
            <button id="btn-preset-groq-qwen" type="button" className="btn btn-xs btn-ghost" disabled={!editable && status?.openai_backend!=='groq'} onClick={()=>preset('https://api.groq.com/openai/v1','qwen/qwen3.8-27b')}>Groq Qwen</button>
            <button id="btn-preset-openai" type="button" className="btn btn-xs btn-ghost" disabled={!editable && status?.openai_backend!=='openai'} onClick={()=>preset('https://api.openai.com/v1','gpt-4o-mini')}>OpenAI</button>
            <button id="btn-preset-xai" type="button" className="btn btn-xs btn-ghost" disabled={!editable} onClick={()=>preset('https://api.x.ai/v1','')}>Grok / xAI</button>
            <button id="btn-preset-lmstudio" type="button" className="btn btn-xs btn-ghost" disabled={!editable} onClick={()=>preset('http://localhost:1234/v1','')}>LM Studio / vLLM</button>
          </div>}
          {['openai','ollama'].includes(value) && <label className="field-label">Endpoint
            <input id={'input-'+value+'-endpoint'} className="text-input mono" disabled={!editable} value={value==='openai' ? endpoint : ollama} onChange={event=>value==='openai' ? setEndpoint(event.target.value) : setOllama(event.target.value)}/></label>}
          {value!=='ollama' && <label className="field-label">Credencial privada
            <input type="password" id={'input-'+value+'-token'} className="text-input" autoComplete="off" disabled={!editable} value={tokens[value] || ''} onChange={event=>setTokens(previous=>({...previous,[value]:event.target.value}))}/></label>}
          <label className="field-label">Modelo de tu cuenta
            <input id={'input-'+value+'-model'} className="text-input mono" value={models[value] || ''} onChange={event=>setModels(previous=>({...previous,[value]:event.target.value}))}/></label>
        </div>)}
        <label className="field-label"><input id="input-supports-images" type="checkbox" checked={vision} onChange={event=>setVision(event.target.checked)}/> El modelo admite imágenes</label>
        <label className="field-label">Formato de API compatible<select id="input-response-format" className="text-input" value={format} onChange={event=>setFormat(event.target.value)}>
          <option value="auto">Automático por perfil</option><option value="json_schema">JSON Schema</option><option value="json_object">JSON básico con validación</option></select></label>
        <label className="field-label"><input id="input-approve-endpoint" type="checkbox" checked={approve} onChange={event=>setApprove(event.target.checked)}/> Confío en este destino HTTPS personalizado y autorizo enviarle consultas y evidencia</label>
        <button id="btn-test-provider" type="button" className="btn btn-ghost" disabled={pending} onClick={()=>{setPending(true);void m.testProvider().then(result=>setMessage(result)).catch(error=>setMessage(errorMessage(error))).finally(()=>setPending(false));}}>Probar el proveedor guardado con un ejemplo sintético</button>
        <p id="provider-test-message" role="status">{message}</p>
        <footer className="modal-footer"><button id="btn-cancel-settings" type="button" className="btn btn-ghost" onClick={close}>Cancelar</button><button type="submit" className="btn btn-primary" disabled={pending}>Aplicar generador</button></footer>
      </form>
    </section>
  </div>;
}
