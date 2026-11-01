import {Component, StrictMode} from 'react';
import type {ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {App} from './App';
import './styles/workbench.css';

class ErrorBoundary extends Component<{children:ReactNode},{failed:boolean}> {
  state={failed:false};
  static getDerivedStateFromError() {return {failed:true};}
  render() {
    return this.state.failed ? <main className="fatal-error"><h1>No se pudo representar la interfaz</h1>
      <p>Recarga la página. Tus documentos permanecen en el servidor.</p><button onClick={()=>location.reload()}>Recargar</button></main> : this.props.children;
  }
}
const root=document.getElementById('root');
if(!root) throw new Error('Falta el contenedor de la aplicación.');
createRoot(root).render(<StrictMode><ErrorBoundary><App/></ErrorBoundary></StrictMode>);
