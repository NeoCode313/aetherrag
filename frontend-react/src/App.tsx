import {useEffect, useState} from 'react';
import {useWorkbench} from './hooks/useWorkbench';
import {Topbar} from './components/Topbar';
import {IngestionSidebar} from './components/IngestionSidebar';
import {ChatPanel} from './components/ChatPanel';
import {VisualInspector} from './components/VisualInspector';
import {ProviderModal} from './components/ProviderModal';
import {AuthModal} from './components/AuthModal';
import {LightboxModal} from './components/LightboxModal';
export function App() {
  const m=useWorkbench(), [theme,setTheme]=useState('dark');
  useEffect(()=>{document.documentElement.dataset.theme=theme;},[theme]);
  useEffect(()=>{
    const escape=(event:KeyboardEvent)=>{
      if(event.key!=='Escape') return;
      if(m.lightbox) m.setLightbox(null);
      else if(m.settingsOpen) m.setSettingsOpen(false);
      else if(m.auth.open) m.setAuth(previous=>({...previous,open:false}));
      else m.cancelOperations();
    };
    window.addEventListener('keydown',escape);return ()=>window.removeEventListener('keydown',escape);
  },[m.lightbox,m.settingsOpen,m.auth.open,m.setLightbox,m.setSettingsOpen,m.setAuth,m.cancelOperations]);
  return <><div className="bg-grid" aria-hidden="true"/><Topbar m={m} theme={theme} onTheme={()=>setTheme(theme==='dark' ? 'light':'dark')}/>
    <main className="workbench"><IngestionSidebar m={m}/><ChatPanel m={m}/><VisualInspector m={m}/></main>
    <ProviderModal m={m}/><AuthModal m={m}/><LightboxModal m={m}/></>;
}
