import {useCallback, useEffect, useRef, useState} from 'react';

interface Operation {id: number; title: string; detail: string; start: number}
interface Failure extends Operation {elapsed: number}
export function useActivityPulse() {
  const sequence = useRef(0);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [now, setNow] = useState(performance.now());
  const begin = useCallback((title: string, detail = 'Procesando la operación…') => {
    const operation = {id:++sequence.current, title, detail, start:performance.now()};
    setFailure(null); setNow(operation.start); setOperations(previous => [...previous, operation]); return operation;
  }, []);
  const finish = useCallback((id: number) => setOperations(previous => previous.filter(op => op.id !== id)), []);
  const fail = useCallback((operation: Operation, detail: string) => {
    setOperations(previous => previous.filter(op => op.id !== operation.id));
    setFailure({...operation, detail, elapsed:(performance.now() - operation.start)/1000});
  }, []);
  const dismiss = useCallback(() => setFailure(null), []);
  const reset = useCallback(() => {setOperations([]); setFailure(null);}, []);
  useEffect(() => {
    if (!operations.length && !failure) return;
    const timer = window.setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(timer);
  }, [operations.length, failure]);
  useEffect(() => {
    if (!failure) return;
    const timer = window.setTimeout(() => setFailure(null), 6000);
    return () => clearTimeout(timer);
  }, [failure]);
  const current = failure || operations[operations.length - 1];
  const seconds = failure ? failure.elapsed : current ? Math.max(0,(now-current.start)/1000) : 0;
  const mode = failure ? 'error' : !current ? 'idle' : seconds >= 4 ? 'warning' : 'working';
  return {begin, finish, fail, dismiss, reset, mode, seconds, elapsed:seconds.toFixed(1)+'s',
    title:current?.title || '', detail:current?.detail || '', now,
    label:mode === 'idle' ? 'LISTO' : mode === 'warning' ? 'LATENCIA ALTA' : mode === 'error' ? 'ATENCIÓN' : 'PROCESANDO'};
}
export type Activity = ReturnType<typeof useActivityPulse>;
