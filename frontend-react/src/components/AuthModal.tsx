import {useEffect, useState} from 'react';
import type {FormEvent} from 'react';
import type {Workbench} from '../hooks/useWorkbench';
import {errorMessage} from '../services/apiClient';
export function AuthModal({m}:{m:Workbench}) {
  const [username,setUsername]=useState(''), [owner,setOwner]=useState('admin');
  const [password,setPassword]=useState(''), [confirm,setConfirm]=useState(''), [ownerPassword,setOwnerPassword]=useState('');
  const [pending,setPending]=useState(false), [error,setError]=useState('');
  useEffect(()=>{if (!m.auth.open) {setPassword('');setOwnerPassword('');setConfirm('');setError('');}},[m.auth.open]);
  async function submit(event:FormEvent, register:boolean) {
    event.preventDefault(); setError('');
    if (register && ownerPassword!==confirm) {setError('Las contraseñas no coinciden.'); return;}
    const secret=register ? ownerPassword : password;
    setPassword('');setOwnerPassword('');setConfirm('');setPending(true);
    try {await m.authenticate(register ? owner.trim() : username.trim(),secret,register);}
    catch (error) {setError(errorMessage(error));} finally {setPending(false);}
  }
  const close=()=>m.setAuth(previous=>({...previous,open:false}));
  return <div id="login-modal" className={'modal-backdrop '+(!m.auth.open ? 'hidden':'')} onClick={event=>{if(event.target===event.currentTarget) close();}}>
    <section className="modal-card login-card" role="dialog" aria-modal="true" aria-labelledby="login-title">
      <header className="modal-header"><div><h3 id="login-title">{m.auth.register ? 'Crear propietario':'Iniciar sesión'}</h3>
        <p id="login-message" role="status">{error || m.auth.message}</p></div><button id="btn-close-login" className="btn btn-icon" aria-label="Cerrar acceso" onClick={close}>✕</button></header>
      <form id="register-form" className={'modal-body login-form '+(!m.auth.ready || !m.auth.register ? 'hidden':'')} onSubmit={event=>void submit(event,true)}>
        <p>Crea un único propietario para esta instalación con una contraseña propia.</p>
        <label className="field-label" htmlFor="register-username">Usuario</label><input id="register-username" className="text-input" autoComplete="username" required value={owner} onChange={event=>setOwner(event.target.value)}/>
        <label className="field-label" htmlFor="register-password">Contraseña (mínimo 15 caracteres)</label><input id="register-password" className="text-input" type="password" autoComplete="new-password" minLength={15} required value={ownerPassword} onChange={event=>setOwnerPassword(event.target.value)}/>
        <label className="field-label" htmlFor="register-confirm">Repetir contraseña</label><input id="register-confirm" className="text-input" type="password" autoComplete="new-password" minLength={15} required value={confirm} onChange={event=>setConfirm(event.target.value)}/>
        <p id="setup-auth-message">{m.auth.local ? 'Estás en tu equipo. Elige usuario y contraseña; no necesitas ningún código.' : m.auth.token.length>=20 ? 'Instalación autorizada desde este equipo.' : 'Abre localhost en el equipo que ejecuta la aplicación para crear el propietario.'}</p>
        <button id="btn-register" type="submit" className="btn btn-primary" disabled={pending || (!m.auth.local && m.auth.token.length<20)}>Crear propietario e iniciar sesión</button>
      </form>
      <form id="login-form" className={'modal-body login-form '+(!m.auth.ready || m.auth.register ? 'hidden':'')} onSubmit={event=>void submit(event,false)}>
        <label htmlFor="login-username" className="field-label">Usuario</label><input id="login-username" className="text-input" autoComplete="username" required value={username} onChange={event=>setUsername(event.target.value)}/>
        <label htmlFor="login-password" className="field-label">Contraseña</label><input id="login-password" type="password" className="text-input" autoComplete="current-password" required value={password} onChange={event=>setPassword(event.target.value)}/>
        <footer className="modal-footer"><button type="button" id="btn-cancel-login" className="btn btn-ghost" onClick={close}>Cerrar</button>
          <button id="btn-login" type="submit" className="btn btn-primary" disabled={pending}>Iniciar sesión</button></footer>
      </form>
    </section>
  </div>;
}
