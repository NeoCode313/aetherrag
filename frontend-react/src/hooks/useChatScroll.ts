import {useCallback, useLayoutEffect, useRef, useState} from 'react';
export function useChatScroll(messages: readonly unknown[]) {
  const streamRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const previous = useRef({top:0, height:0, client:0});
  const [showJump, setShowJump] = useState(false);
  const onScroll = useCallback(() => {
    const stream = streamRef.current;
    if (!stream) return;
    const distance=stream.scrollHeight-stream.scrollTop-stream.clientHeight;
    const resized=stream.scrollHeight!==previous.current.height || stream.clientHeight!==previous.current.client;
    if (!resized && stream.scrollTop<previous.current.top-1) pinned.current=false;
    if (distance<=72) pinned.current=true;
    previous.current={top:stream.scrollTop,height:stream.scrollHeight,client:stream.clientHeight};
    setShowJump(!pinned.current && distance>72);
  }, []);
  const edge = useCallback((position: 'top' | 'bottom') => {
    const stream = streamRef.current;
    pinned.current=position==='bottom';
    if (stream) stream.scrollTo({top:position === 'top' ? 0 : stream.scrollHeight, behavior:'smooth'});
  }, []);
  useLayoutEffect(() => {
    const stream = streamRef.current;
    if (!stream) return;
    const observer = new ResizeObserver(() => {
      if (pinned.current) stream.scrollTop = stream.scrollHeight;
      onScroll();
    });
    Array.from(stream.children).forEach(child => observer.observe(child));
    if (pinned.current) stream.scrollTop = stream.scrollHeight;
    return () => observer.disconnect();
  }, [messages, onScroll]);
  return {streamRef, onScroll, showJump, edge};
}
