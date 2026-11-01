import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {ApiClient, errorMessage, isAborted} from '../services/apiClient';
import {useActivityPulse} from './useActivityPulse';
import type {ChatMessage, ChatResponse, ConversationTurn, DocumentRecord, Job, ProviderPayload, Session, Setup, SystemStatus, VisualAsset} from '../types/api';

export function useWorkbench() {
  const activity = useActivityPulse();
  const {begin, finish, fail, reset:resetActivity} = activity;
  const unauthorized = useRef<() => void>(() => {});
  const api = useMemo(() => new ApiClient(() => unauthorized.current()), []);
  const [user, setUser] = useState<string | null>(null);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [version, setVersion] = useState('Comprobando versión…');
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectedRef = useRef(new Set<string>());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [sending, setSending] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeRef, setActiveRef] = useState<number | null>(null);
  const [visual, setVisual] = useState<VisualAsset | null>(null);
  const [view, setView] = useState<'crop' | 'page'>('crop');
  const [lightbox, setLightbox] = useState<{src:string; caption:string} | null>(null);
  const [auth, setAuth] = useState({open:true, ready:false, register:false, local:false, token:'', message:'Comprobando el acceso…'});
  const turns = useRef<ConversationTurn[]>([]);
  const contextEpoch = useRef(0);
  const epoch = useRef(0), counter = useRef(0), pollEpoch = useRef(0), recoveryEpoch = useRef(0);
  const userRef = useRef<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout>>(), recoveryTimer = useRef<ReturnType<typeof setTimeout>>();
  const query = useRef<{id:string; question:string; controller:AbortController; token:number} | null>(null);
  const ingestion = useRef<{controller:AbortController; token:number} | null>(null);

  const clearInspector = useCallback(() => {setActiveId(null); setActiveRef(null); setVisual(null); setLightbox(null);}, []);
  const cancelQuery = useCallback(() => {
    const pending = query.current;
    if (!pending) return;
    query.current = null; pending.controller.abort(); finish(pending.token); setSending(false);
    setMessages(previous => previous.map(message => message.id === pending.id ? {...message, kind:'cancelled',
      text:'Se dejó de esperar en este navegador. La solicitud puede continuar en el servidor.'} : message));
  }, [finish]);
  const cancelOperations = useCallback(() => {
    cancelQuery(); pollEpoch.current++; recoveryEpoch.current++;
    clearTimeout(pollTimer.current); clearTimeout(recoveryTimer.current);
    if (ingestion.current) {ingestion.current.controller.abort(); finish(ingestion.current.token); ingestion.current = null;}
    setJob(previous => previous && !['completed','failed'].includes(previous.status) ? {...previous, status:'seguimiento detenido',
      message:'El trabajo puede continuar en el servidor.'} : previous);
    resetActivity();
  }, [cancelQuery, finish, resetActivity]);
  const clearSession = useCallback(() => {
    epoch.current++; userRef.current = null; api.clear(); cancelOperations();
    setUser(null); setDocuments([]); setStatus(null); setMessages([]); setSelected(new Set());
    selectedRef.current = new Set(); turns.current = []; setJob(null); setSettingsOpen(false); clearInspector();
    setAuth(previous => ({...previous, ready:true, open:true, message:'Inicia sesión para acceder al corpus documental.'}));
  }, [api, cancelOperations, clearInspector]);
  unauthorized.current = clearSession;
  const refreshStatus = useCallback(async () => {
    const current = epoch.current;
    const data = await api.request<SystemStatus>('/system/status');
    if (current !== epoch.current || !userRef.current) return null;
    setStatus(data); return data;
  }, [api]);
  const refreshDocuments = useCallback(async () => {
    const current = epoch.current;
    const data = await api.request<DocumentRecord[]>('/documents');
    if (current !== epoch.current || !userRef.current) return;
    setDocuments(data);
    const available = new Set(data.map(document => document.document_id));
    selectedRef.current = new Set([...selectedRef.current].filter(id => available.has(id)));
    setSelected(new Set(selectedRef.current));
  }, [api]);
  const acceptSession = useCallback(async (session: Session) => {
    epoch.current++; userRef.current = session.user; api.setSession(session.csrf_token || '');
    setUser(session.user); setAuth(previous => ({...previous, open:false, ready:true, register:false, token:'', message:''}));
    await Promise.all([refreshStatus(), refreshDocuments()]);
  }, [api, refreshStatus, refreshDocuments]);
  useEffect(() => {
    let mounted = true;
    try {localStorage.removeItem('aetherrag_provider_cfg');} catch { /* No credential persistence. */ }
    const fragment = new URLSearchParams(location.hash.slice(1));
    const token = fragment.get('setup') || '';
    if (fragment.has('setup')) window.history.replaceState(null, '', location.pathname+location.search);
    void (async () => {
      try {
        const setup = await api.request<Setup>('/auth/setup').catch(error => {if (isAborted(error)) throw error; return {} as Setup;});
        if (!mounted) return;
        setAuth(previous => ({...previous, ready:true, register:setup.registration_required === true,
          local:setup.local_registration_available === true, token,
          message:setup.registration_required ? 'Primera configuración: crea tu propietario.' : 'Inicia sesión.'}));
        const info = await api.request<{environment?:string; version?:string; build_id?:string}>('/system/version').catch(() => null);
        if (!mounted) return;
        if (info?.version) setVersion((info.environment || 'local')+' · v'+info.version+' · build '+info.build_id);
        if (!setup.registration_required) await acceptSession(await api.request<Session>('/auth/session'));
      } catch (error) {
        if (mounted && !isAborted(error)) setAuth(previous => ({...previous, ready:true, open:true, message:errorMessage(error)}));
      }
    })();
    return () => {mounted=false; api.clear(); pollEpoch.current++; recoveryEpoch.current++;
      clearTimeout(pollTimer.current); clearTimeout(recoveryTimer.current); query.current?.controller.abort(); ingestion.current?.controller.abort();};
  }, [api, acceptSession]);

  const authenticate = useCallback(async (username:string, password:string, register:boolean) => {
    resetActivity();
    if (register) {
      await api.post('/auth/register', {username, password, setup_token:auth.token});
      setAuth(previous => ({...previous, register:false, token:'', message:'Propietario creado. Inicia sesión.'}));
    }
    await acceptSession(await api.post<Session>('/auth/login', {username,password}));
  }, [api, auth.token, acceptSession, resetActivity]);
  const logout = useCallback(async () => {
    try {await api.post('/auth/logout'); clearSession();}
    catch (error) {if (!isAborted(error)) {const op=begin('No se pudo cerrar sesión'); fail(op,errorMessage(error));}}
  }, [api, clearSession, begin, fail]);
  const selectDocument = useCallback((id:string, checked:boolean) => {
    const next = new Set(selectedRef.current); if (checked) next.add(id); else next.delete(id);
    selectedRef.current=next; setSelected(next); turns.current=[]; contextEpoch.current++;
  }, []);
  const deleteDocument = useCallback(async (id:string) => {
    const op=begin('Eliminando documento');
    try {
      await api.request('/documents/'+encodeURIComponent(id), {method:'DELETE'});
      selectedRef.current.delete(id); setSelected(new Set(selectedRef.current)); turns.current=[]; contextEpoch.current++;
      await Promise.all([refreshDocuments(),refreshStatus()]); finish(op.id);
    } catch (error) {if (!isAborted(error)) fail(op,errorMessage(error));}
  }, [api, begin, fail, finish, refreshDocuments, refreshStatus]);
  const pollJob = useCallback((initial:Job, operation:ReturnType<typeof begin>) => {
    const current=++pollEpoch.current, sessionEpoch=epoch.current;
    const poll = async () => {
      try {
        const progress=await api.request<Job>('/jobs/'+encodeURIComponent(initial.job_id));
        if (current!==pollEpoch.current || sessionEpoch!==epoch.current) return;
        setJob({...initial,...progress});
        if (['completed','failed','cancelled'].includes(progress.status)) {
          ingestion.current=null;
          if (progress.status==='completed') {
            turns.current=[]; contextEpoch.current++;
            if (progress.document_id) {selectedRef.current=new Set([progress.document_id]); setSelected(new Set(selectedRef.current));}
            finish(operation.id);
          } else fail(operation,progress.error || progress.message || 'La ingesta no se completó.');
          await Promise.all([refreshDocuments(),refreshStatus()]); return;
        }
        pollTimer.current=setTimeout(poll,750);
      } catch (error) {
        if (current===pollEpoch.current && !isAborted(error)) {
          ingestion.current=null;
          setJob(previous=>previous && {...previous,message:'Seguimiento interrumpido; el trabajo puede continuar en el servidor.'});
          fail(operation,errorMessage(error));
        }
      }
    };
    clearTimeout(pollTimer.current); pollTimer.current=setTimeout(poll,0);
  }, [api, begin, finish, fail, refreshDocuments, refreshStatus]);
  const ingest = useCallback(async (file?:File) => {
    if (ingestion.current) return;
    const operation=begin(file ? 'Enviando PDF: '+file.name : 'Solicitando manual de demostración');
    const controller=new AbortController(); ingestion.current={controller,token:operation.id};
    setJob({job_id:'',file_name:file?.name || 'Manual demo',status:'pending',progress:0,message:'Enviando al servidor…'});
    try {
      let result:Job;
      if (file) {
        const body=new FormData(); body.append('file',file);
        result=await api.request<Job>('/documents/ingest',{method:'POST',body,signal:controller.signal});
      } else result=await api.post<Job>('/documents/seed-sample?force=true',undefined,controller.signal);
      if (controller.signal.aborted) return;
      setJob({...result,progress:0,status:'pending'}); pollJob(result,operation);
    } catch (error) {
      if (ingestion.current?.controller===controller) ingestion.current=null;
      if (!isAborted(error)) {setJob(previous=>previous && {...previous,status:'failed',error:errorMessage(error)}); fail(operation,errorMessage(error));}
    }
  }, [api, begin, fail, pollJob]);
  const inspect = useCallback((id:string, ref?:number) => {
    setActiveId(id); setActiveRef(ref ?? null); setVisual(null); setView(ref ? 'page' : 'crop');
  }, []);
  const submit = useCallback(async (question:string) => {
    const text=question.trim(); if (!text || query.current) return;
    const id='answer-'+(++counter.current), op=begin('Consultando el documento','Recuperación híbrida y generación con referencias…');
    const controller=new AbortController(); query.current={id,question:text,controller,token:op.id}; setSending(true);
    const scope=Array.from(selectedRef.current), currentHistory=turns.current.slice(-6), sessionEpoch=epoch.current, currentContext=contextEpoch.current;
    setMessages(previous=>[...previous,{id:'user-'+counter.current,kind:'user',text},{id,kind:'pending',text,question:text,started:op.start}]);
    try {
      const data=await api.post<ChatResponse>('/chat/query',{question:text,history:currentHistory,
        ...(scope.length ? {document_ids:scope} : {})},controller.signal);
      if (query.current?.id!==id || sessionEpoch!==epoch.current) return;
      // A document switch during a query must not restore history from the previous scope.
      if (currentContext===contextEpoch.current) turns.current=[...currentHistory,
        {role:'user',content:text.slice(0,4000)},{role:'assistant',content:data.answer.slice(0,4000)}];
      setMessages(previous=>previous.map(message=>message.id===id ? {...message,kind:'answer',response:data} : message));
      inspect(id); finish(op.id);
      void refreshStatus().catch(()=>{});
    } catch (error) {
      if (query.current?.id!==id || sessionEpoch!==epoch.current) return;
      setMessages(previous=>previous.map(message=>message.id===id ? {...message,kind:isAborted(error) ? 'cancelled':'error',
        text:isAborted(error) ? 'La solicitud puede continuar en el servidor.' : errorMessage(error)} : message));
      if (!isAborted(error)) fail(op,errorMessage(error));
    } finally {if (query.current?.id===id) {query.current=null; setSending(false);}}
  }, [api, begin, finish, fail, inspect, refreshStatus]);
  const saveProvider = useCallback(async (payload:ProviderPayload) => {
    const op=begin('Aplicando generador configurado');
    try {await api.post('/system/provider',payload); await refreshStatus(); finish(op.id); setSettingsOpen(false);}
    catch (error) {if (!isAborted(error)) fail(op,errorMessage(error)); throw error;}
  }, [api,begin,finish,fail,refreshStatus]);
  const testProvider = useCallback(async () => {
    const result=await api.post<{message:string}>('/system/test-provider'); return result.message;
  }, [api]);
  const recover = useCallback(async (semantic:boolean) => {
    const op=begin('Recuperando índice','Reconstrucción aislada; se conservan las fuentes.');
    const current=++recoveryEpoch.current, sessionEpoch=epoch.current;
    try {
      await api.post('/system/'+(semantic ? 'semantic-index':'recover-index'));
      const poll=async () => {
        try {
          const data=await refreshStatus();
          if (current!==recoveryEpoch.current || sessionEpoch!==epoch.current || !data) return;
          if (data.migration_status!=='running') {
            if (data.index_compatible!==false) {finish(op.id); await refreshDocuments();}
            else fail(op,'No se completó la reconstrucción. Se conservaron las fuentes y el índice anterior.');
          } else recoveryTimer.current=setTimeout(poll,1500);
        } catch (error) {if (!isAborted(error)) fail(op,errorMessage(error));}
      };
      recoveryTimer.current=setTimeout(poll,0);
    } catch (error) {if (!isAborted(error)) fail(op,errorMessage(error));}
  }, [api,begin,finish,fail,refreshStatus,refreshDocuments]);
  const activeResponse=messages.find(message=>message.id===activeId)?.response;
  return {activity,user,status,version,documents,selected,messages,job,sending,auth,setAuth,
    settingsOpen,setSettingsOpen,activeId,activeRef,activeResponse,visual,setVisual,view,setView,lightbox,setLightbox,
    authenticate,logout,selectDocument,deleteDocument,ingest,inspect,submit,cancelQuery,cancelOperations,
    clearInspector,saveProvider,testProvider,recover};
}
export type Workbench = ReturnType<typeof useWorkbench>;
